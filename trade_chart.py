"""交易复盘图：标的价格 + 每笔期权交易的开/平仓点与行权价区间，以及单笔合约价格走势。

纯函数模块，不依赖 Streamlit：
    underlying_series(df)                  期权链 → 每日标的价格（Put-Call Parity 反推）
    build_trade_table(open, closed, under) 仓位列表 → 交易表（含平仓方式分类）
    contract_series(df, pos)               单张合约在持有期内的 mid / delta
    chart_payload(...) / chart_html(...)   总览：TradingView Lightweight Charts 交互图
    contract_figure(...)                   单笔合约 plotly 图
"""
from __future__ import annotations

from datetime import date

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

# 颜色：dataviz 参考调色板（categorical 固定顺序）
COLOR_PUT = "#2a78d6"     # slot 1 blue
COLOR_CALL = "#eb6834"    # slot 2 orange
COLOR_PROFIT = "#1baf7a"  # slot 3 aqua
COLOR_LOSS = "#e34948"    # slot 8 red
COLOR_PRICE = "#52514e"   # neutral ink

# ── 数据 ───────────────────────────────────────────────────────────────────────

def underlying_series(df: pd.DataFrame) -> pd.Series:
    """每个快照日：取最近到期日上同时有 Call/Put 的行权价，选 |C-P| 最小的一档，
    S = C - P + K（短 DTE 忽略贴现）。当天没有成对合约时退回 ATM Call（Δ≈0.5）行权价。"""
    if df.empty:
        return pd.Series(dtype=float)

    piv = (df.pivot_table(index=["snapshot_date", "expiry", "strike"],
                          columns="type", values="mid", aggfunc="first")
             .reindex(columns=["Call", "Put"]).dropna().reset_index())
    result: dict[date, float] = {}
    if not piv.empty:
        nearest = piv.groupby("snapshot_date")["expiry"].transform("min")
        piv = piv[piv["expiry"] == nearest].copy()
        piv["gap"] = (piv["Call"] - piv["Put"]).abs()
        best = piv.loc[piv.groupby("snapshot_date")["gap"].idxmin()]
        result = dict(zip(best["snapshot_date"], best["Call"] - best["Put"] + best["strike"]))

    missing = set(df["snapshot_date"].unique()) - set(result)
    if missing:
        calls = df[df["snapshot_date"].isin(missing) & (df["type"] == "Call") & df["delta"].notna()]
        if not calls.empty:
            nearest = calls.groupby("snapshot_date")["expiry"].transform("min")
            calls = calls[calls["expiry"] == nearest]
            idx = (calls["delta"] - 0.5).abs().groupby(calls["snapshot_date"]).idxmin()
            atm = calls.loc[idx]
            result.update(zip(atm["snapshot_date"], atm["strike"].astype(float)))

    return pd.Series(result, dtype=float).sort_index()


def _spot_on_or_before(under: pd.Series, d: date) -> float | None:
    if under.empty:
        return None
    s = under[under.index <= d]
    return float(s.loc[max(s.index)]) if not s.empty else None


def _outcome_from_history(pos: dict, history: list) -> str | None:
    """账户流水是模拟器真实结算结果：被行权记录 price = 行权价，到期废弃记录 symbol = 合约代码。"""
    d = str(pos["close_date"])
    for h in history:
        if str(h.get("date")) != d:
            continue
        action = h.get("action", "")
        if "被行权" in action and pos["type"] in action \
                and abs(float(h.get("price") or 0) - float(pos["strike"])) < 1e-6:
            return "被行权"
        if "到期" in action and h.get("symbol") == pos["symbol"]:
            return "到期实值结算" if "ITM" in action else "到期作废"
    return None


def _outcome(pos: dict, under: pd.Series, history: list | None = None) -> str:
    if pos.get("close_date") is None:
        return "持仓中"
    if pos["close_date"] < pos["expiry"]:
        return "提前平仓"
    if history:
        found = _outcome_from_history(pos, history)
        if found:
            return found
    # 没有流水时：按到期日标的价格推断（临界行权价可能与实际结算不一致）
    spot = _spot_on_or_before(under, pos["expiry"])
    if spot is None:
        return "到期作废"
    itm = spot < pos["strike"] if pos["type"] == "Put" else spot > pos["strike"]
    if not itm:
        return "到期作废"
    return "到期实值结算" if pos.get("direction", -1) > 0 else "被行权"


def build_trade_table(open_positions: list, closed_positions: list,
                      under: pd.Series, history: list | None = None) -> pd.DataFrame:
    rows = []
    for status, plist in (("closed", closed_positions), ("open", open_positions)):
        for p in plist:
            rows.append({
                "symbol": p["symbol"], "ticker": p.get("ticker", ""),
                "type": p["type"], "direction": p["direction"], "dir_label": p["dir_label"],
                "strike": float(p["strike"]), "qty": p["qty"], "expiry": p["expiry"],
                "entry_date": p["entry_date"], "entry_mid": float(p["entry_mid"]),
                "close_date": p.get("close_date") if status == "closed" else None,
                "close_mid": p.get("close_mid") if status == "closed" else None,
                "realized": p.get("realized") if status == "closed" else None,
                "status": status,
                "outcome": _outcome(p if status == "closed" else {**p, "close_date": None},
                                    under, history),
            })
    if not rows:
        return pd.DataFrame()

    t = pd.DataFrame(rows).astype({"close_date": object, "close_mid": object, "realized": object})
    t = t.sort_values("entry_date", kind="stable").reset_index(drop=True)
    t = t.where(t.notna(), None)
    t["id"] = range(1, len(t) + 1)
    t["label"] = [
        f"#{r.id} {r.dir_label} {r.type} ${r.strike:g}  到期 {r.expiry}  ({r.outcome})"
        for r in t.itertuples()
    ]
    return t


def contract_series(df: pd.DataFrame, pos: dict) -> pd.DataFrame:
    end = pos.get("close_date") or df["snapshot_date"].max()
    m = ((df["expiry"] == pos["expiry"]) & (df["strike"] == pos["strike"])
         & (df["type"] == pos["type"])
         & (df["snapshot_date"] >= pos["entry_date"]) & (df["snapshot_date"] <= end))
    return (df.loc[m, ["snapshot_date", "mid", "delta"]]
              .drop_duplicates("snapshot_date").sort_values("snapshot_date")
              .reset_index(drop=True))


# ── 图 ─────────────────────────────────────────────────────────────────────────

def _money(v) -> str:
    return "—" if v is None else f"${v:+,.2f}"


def _hover(r) -> str:
    from html import escape
    close = "持仓中" if r.close_date is None else f"{r.close_date} @ ${r.close_mid:.2f}"
    return (f"<b>#{r.id} {escape(str(r.dir_label))} {escape(str(r.type))} ${r.strike:g}</b> ×{r.qty}<br>"
            f"到期 {r.expiry}<br>开仓 {r.entry_date} @ ${r.entry_mid:.2f}<br>"
            f"平仓 {close}<br>方式 {r.outcome}<br>盈亏 {_money(r.realized)}")


LWC_VERSION = "5.0.9"   # TradingView Lightweight Charts（Apache-2.0），固定版本
OUTCOME_SHAPE = {"到期作废": "circle", "提前平仓": "square", "被行权": "circle",
                 "到期实值结算": "circle"}
OUTCOME_TEXT = {"被行权": "行权", "到期实值结算": "ITM"}


def _iso(d) -> str:
    return d.isoformat() if hasattr(d, "isoformat") else str(d)


def chart_payload(under: pd.Series, trades: pd.DataFrame, as_of: date) -> dict:
    """把标的价格和交易整理成 Lightweight Charts 的数据（纯数据，便于测试）。
    每笔交易 = 一条行权价线段（开仓日 → 平仓日；未平仓画到 min(as_of, 到期日)），
    开/平仓标记画在线段两端的行权价上。"""
    under = under.sort_index()
    if not trades.empty and not under.empty:
        # 未复权数据里的拆股跳变（单日 >35%）：若全部交易都在跳变之后，丢掉跳变前的价格。
        # 拆股前价格与拆股后行权价不是同一口径，留着只会把 Y 轴拉长、挤扁交易区
        first = min(trades["entry_date"])
        jumps = [d for d, v in (under.pct_change().abs() > 0.35).items() if v and d <= first]
        if jumps:
            under = under[under.index >= max(jumps)]
    price = [{"time": _iso(d), "value": round(float(v), 4)} for d, v in under.items()]
    out, ends = [], []
    for r in ([] if trades.empty else trades.itertuples()):
        color = COLOR_PUT if r.type == "Put" else COLOR_CALL
        end = r.close_date if r.close_date is not None else min(as_of, r.expiry)
        end = max(end, r.entry_date)
        ends.append(end)
        pts = [{"time": _iso(r.entry_date), "value": r.strike}]
        if end != r.entry_date:
            pts.append({"time": _iso(end), "value": r.strike})
        markers = [{
            "time": _iso(r.entry_date), "position": "atPriceMiddle", "price": r.strike,
            "shape": "arrowDown" if r.dir_label == "Short" else "arrowUp",
            "color": color, "id": f"{r.id}", "size": 1.4,
        }]
        if r.close_date is not None:
            markers.append({
                "time": _iso(r.close_date), "position": "atPriceMiddle", "price": r.strike,
                "shape": OUTCOME_SHAPE.get(r.outcome, "circle"),
                "color": COLOR_PROFIT if (r.realized or 0) >= 0 else COLOR_LOSS,
                "id": f"{r.id}", "size": 1.2,
                **({"text": OUTCOME_TEXT[r.outcome]} if r.outcome in OUTCOME_TEXT else {}),
            })
        out.append({"id": int(r.id), "color": color, "points": pts, "markers": markers,
                    "tip": _hover(r), "entry": _iso(r.entry_date), "end": _iso(end)})

    rng = None
    if out:
        lo, hi = min(trades["entry_date"]), max(ends)
        pad = max(pd.Timedelta(days=10), (pd.Timestamp(hi) - pd.Timestamp(lo)) * 0.05)
        lo2 = (pd.Timestamp(lo) - pad).date()
        hi2 = (pd.Timestamp(hi) + pad).date()
        if not under.empty:
            lo2, hi2 = max(lo2, min(under.index)), min(hi2, max(under.index))
        rng = {"from": _iso(lo2), "to": _iso(hi2)}
    return {"price": price, "trades": out, "range": rng}


def chart_html(payload: dict, ticker: str, dark: bool = False, height: int = 520) -> str:
    """TradingView 风格交互图：滚轮缩放、拖动平移、Y 轴随可见区间自动适配、悬停看交易详情。"""
    import html as _html
    import json as _json

    theme = {
        "bg": "#1a1a19" if dark else "#fcfcfb",
        "text": "#c3c2b7" if dark else "#52514e",
        "strong": "#f0efec" if dark else "#0b0b0b",
        "grid": "#2c2c2a" if dark else "#efeeea",
        "price": "#c3c2b7" if dark else COLOR_PRICE,
        "tipbg": "rgba(26,26,25,.94)" if dark else "rgba(252,252,251,.96)",
        "border": "#3a3a37" if dark else "#dcdbd5",
    }
    data = _json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    name = _json.dumps(_html.escape(f"{ticker} 标的价格"), ensure_ascii=False)
    css = """
  html,body{margin:0;background:%(bg)s;color:%(text)s;
    font:12px/1.45 system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
  #bar{display:flex;gap:6px;align-items:center;flex-wrap:wrap;padding:4px 2px 6px}
  #bar button{font:inherit;color:inherit;background:transparent;border:1px solid %(border)s;
    border-radius:6px;padding:2px 9px;cursor:pointer}
  #bar button:hover{border-color:%(text)s}
  #hint{margin-left:auto;opacity:.75}
  #wrap{position:relative;height:%(h)dpx}
  #c{position:absolute;inset:0}
  #tip{position:absolute;left:8px;top:8px;z-index:5;pointer-events:none;background:%(tipbg)s;
    border:1px solid %(border)s;border-radius:8px;padding:6px 9px;max-width:320px;display:none}
  #tip b{color:%(strong)s}
  .row+.row{border-top:1px solid %(border)s;margin-top:5px;padding-top:5px}
""" % {**theme, "h": height}

    js = r"""
const L = LightweightCharts;
const el = document.getElementById('c');
const chart = L.createChart(el, {
  autoSize: true,
  layout: { background: { type: 'solid', color: T.bg }, textColor: T.text,
            fontFamily: 'system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif' },
  grid: { vertLines: { color: T.grid }, horzLines: { color: T.grid } },
  rightPriceScale: { borderColor: T.grid, scaleMargins: { top: 0.08, bottom: 0.08 } },
  timeScale: { borderColor: T.grid, rightOffset: 3, minBarSpacing: 0.3 },
  crosshair: { mode: L.CrosshairMode.Normal },
  localization: { locale: 'zh-CN', priceFormatter: p => '$' + p.toFixed(2) },
});
const price = chart.addSeries(L.LineSeries, { color: T.price, lineWidth: 2, title: NAME,
  priceLineVisible: false });
price.setData(P.price);

const byId = {};
for (const tr of P.trades) {
  byId[tr.id] = tr;
  const s = chart.addSeries(L.LineSeries, { color: tr.color, lineWidth: 2,
    lineStyle: L.LineStyle.Dotted, priceLineVisible: false, lastValueVisible: false,
    crosshairMarkerVisible: false });
  s.setData(tr.points);
  L.createSeriesMarkers(s, tr.markers);
}

const spotAt = new Map(P.price.map(p => [p.time, p.value]));
const tip = document.getElementById('tip');
function toISO(t) {
  if (typeof t === 'string') return t;
  if (t && t.year) return t.year + '-' + String(t.month).padStart(2, '0') + '-' + String(t.day).padStart(2, '0');
  return null;
}
chart.subscribeCrosshairMove(param => {
  const d = toISO(param.time);
  if (!d || !param.point) { tip.style.display = 'none'; return; }
  let rows;
  if (param.hoveredObjectId != null && byId[param.hoveredObjectId]) rows = [byId[param.hoveredObjectId]];
  else rows = P.trades.filter(tr => tr.entry === d || tr.end === d);
  const spot = spotAt.get(d);
  let h = '<div><b>' + d + '</b>' + (spot != null ? '　标的 $' + spot.toFixed(2) : '') + '</div>';
  if (rows.length) h += rows.slice(0, 4).map(r => '<div class="row">' + r.tip + '</div>').join('');
  else {
    const live = P.trades.filter(tr => tr.entry <= d && d <= tr.end);
    if (live.length) h += '<div class="row">持仓中 ' + live.length + ' 笔：' + live.map(r => '#' + r.id).join(' ') + '</div>';
  }
  tip.innerHTML = h;
  tip.style.display = 'block';
  if (param.point.x < el.clientWidth / 2) { tip.style.left = ''; tip.style.right = '70px'; }
  else { tip.style.right = ''; tip.style.left = '8px'; }
});

const ts = chart.timeScale();
function shiftISO(iso, days) {
  const t = new Date(iso + 'T00:00:00Z'); t.setUTCDate(t.getUTCDate() + days);
  return t.toISOString().slice(0, 10);
}
function setRange(r) {
  if (r === 'all' || !P.range) { ts.fitContent(); return; }
  if (r === 'fit') { ts.setVisibleRange(P.range); return; }
  const vr = ts.getVisibleRange();
  const mid = vr ? toISO(vr.to) : P.range.to;
  ts.setVisibleRange({ from: shiftISO(mid, -Number(r)), to: mid });
}
document.querySelectorAll('#bar button').forEach(b => b.onclick = () => setRange(b.dataset.r));
// autoSize 在首帧之后才拿到容器宽度：等布局完成再设默认区间，否则视图会被挪回右端
setRange('fit');
requestAnimationFrame(() => setRange('fit'));
"""
    return (
        '<!doctype html><html><head><meta charset="utf-8"><style>' + css + "</style></head><body>"
        '<div id="bar"><button data-r="fit">交易区间</button><button data-r="30">1M</button>'
        '<button data-r="91">3M</button><button data-r="182">6M</button><button data-r="365">1Y</button>'
        '<button data-r="all">全部</button>'
        '<span id="hint">滚轮缩放 · 拖动平移 · 拖 Y 轴拉伸价格 · 双击 Y 轴恢复自动</span></div>'
        '<div id="wrap"><div id="c"></div><div id="tip"></div></div>'
        f'<script src="https://cdn.jsdelivr.net/npm/lightweight-charts@{LWC_VERSION}'
        '/dist/lightweight-charts.standalone.production.js"></script>'
        f"<script>const P = {data};\nconst T = {_json.dumps(theme)};\nconst NAME = {name};\n"
        + js + "</script></body></html>"
    )


def contract_figure(series: pd.DataFrame, trade) -> go.Figure:
    """上：合约 mid；下：Delta（两个子图共用时间轴，不用双纵轴）。"""
    color = COLOR_PUT if trade.type == "Put" else COLOR_CALL
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.7, 0.3],
                        vertical_spacing=0.06)
    fig.add_trace(go.Scatter(
        x=list(series["snapshot_date"]), y=series["mid"], mode="lines+markers", name="合约 mid",
        line=dict(color=color, width=2), marker=dict(size=6),
        hovertemplate="%{x}<br>mid $%{y:.2f}<extra></extra>",
    ), row=1, col=1)
    fig.add_trace(go.Scatter(
        x=list(series["snapshot_date"]), y=series["delta"], mode="lines", name="Delta",
        line=dict(color=COLOR_PRICE, width=2),
        hovertemplate="%{x}<br>Δ %{y:.3f}<extra></extra>",
    ), row=2, col=1)

    # 注意：annotation 里出现两个 "$" 会被 plotly 当成 LaTeX，所以金额只保留一个 "$"
    fig.add_hline(y=trade.entry_mid, line=dict(color=color, dash="dash", width=1),
                  annotation_text=f"开仓 ${trade.entry_mid:.2f}", row=1, col=1)
    pnl_txt = "" if trade.realized is None else f"  盈亏 {trade.realized:+,.2f}"
    if trade.outcome == "被行权":
        # 模拟器把被行权记成 close_mid=0；实际是按行权价交割，不画 0 价线
        fig.add_annotation(text=f"被行权：按行权价 {trade.strike:g} 交割{pnl_txt}",
                           xref="paper", yref="paper", x=1, y=1, xanchor="right",
                           showarrow=False, font=dict(color=COLOR_PRICE))
    elif trade.close_mid is not None:
        pnl_color = COLOR_PROFIT if (trade.realized or 0) >= 0 else COLOR_LOSS
        fig.add_hline(y=trade.close_mid, line=dict(color=pnl_color, dash="dash", width=1),
                      annotation_text=f"{trade.outcome} {trade.close_mid:.2f}{pnl_txt}",
                      annotation_position="bottom right", row=1, col=1)
    fig.update_yaxes(title_text="合约价格 ($)", row=1, col=1)
    fig.update_yaxes(title_text="Delta", row=2, col=1)
    return _layout(fig, None, height=420)


def _layout(fig: go.Figure, ytitle: str | None, height: int = 480) -> go.Figure:
    fig.update_layout(
        height=height, margin=dict(l=10, r=10, t=30, b=10), hovermode="closest",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
    )
    if ytitle:
        fig.update_yaxes(title_text=ytitle)
    return fig
