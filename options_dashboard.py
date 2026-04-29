import streamlit as st
import pandas as pd
import json
import io
import glob
import os
import re
from datetime import date, datetime

st.set_page_config(page_title="期权复盘工具", layout="wide")

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

def _scan_data_files() -> dict[str, list[tuple[str, str]]]:
    """扫描 data/ 目录，返回 {ticker: [(label, path), ...]} 按文件名排序。"""
    files = glob.glob(os.path.join(DATA_DIR, "*_options_*_greeks.csv"))
    result: dict[str, list[tuple[str, str]]] = {}
    for path in sorted(files):
        fname = os.path.basename(path)
        m = re.match(r"^([A-Z]+)_options_(.+)_greeks\.csv$", fname)
        if not m:
            continue
        ticker, period = m.group(1), m.group(2)
        label = period.replace("_", " → ")
        result.setdefault(ticker, []).append((label, path))
    return result

TICKER_DATA = _scan_data_files()  # {ticker: [(label, path), ...]}

CONTRACT_MULT = 100  # 标准期权乘数


# ── Data helpers ──────────────────────────────────────────────────────────────

@st.cache_data
def load_data(path: str, ticker: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["snapshot_date"] = pd.to_datetime(df["snapshot_date"]).dt.date
    df["expiry"] = pd.to_datetime(df["expiry"]).dt.date
    df["option_symbol"] = df.apply(
        lambda r: (
            f"{ticker} {str(r['expiry'])[2:].replace('-', '')}"
            f"{'C' if r['type'] == 'Call' else 'P'}"
            f"{int(r['strike'] * 1000):08d}"
        ),
        axis=1,
    )
    return df


def estimate_underlying(day_df: pd.DataFrame) -> float | None:
    # 用最短 DTE 的 Call 做 ATM 推算，避免长期期权 delta 偏移
    valid = day_df[(day_df["type"] == "Call") & day_df["delta"].notna()]
    if valid.empty:
        return None
    shortest_expiry = valid["expiry"].min()
    calls = valid[valid["expiry"] == shortest_expiry]
    idx = (calls["delta"] - 0.5).abs().idxmin()
    return float(calls.loc[idx, "strike"])


def get_mid(df: pd.DataFrame, symbol: str, d: date) -> float | None:
    row = df[(df["snapshot_date"] == d) & (df["option_symbol"] == symbol)]
    if row.empty:
        return None
    return float(row.iloc[0]["mid"])


def get_underlying_at(df: pd.DataFrame, d: date) -> float | None:
    return estimate_underlying(df[df["snapshot_date"] == d])


def intrinsic_value(opt_type: str, strike: float, spot: float | None) -> float:
    # ITM 按标的价 - 行权价结算，避开到期日盘口 mid 失真
    if spot is None:
        return 0.0
    if opt_type == "Put":
        return max(0.0, strike - spot)
    return max(0.0, spot - strike)


# ── Account logic ─────────────────────────────────────────────────────────────
# PnL 正负号统一公式（严格区分买卖方向）：
#   pnl = direction × (cur_mid - entry_mid) × 100 × qty
#   Short direction = -1：mid 下降 → PnL 为正
#   Long  direction = +1：mid 上升 → PnL 为正
# 现金流：
#   开仓现金流 = -direction × mid × 100 × qty   (Short 收入 / Long 支出)
#   平仓现金流 = +direction × mid × 100 × qty   (Short 买回支出 / Long 卖出收入)

def init_account(initial_capital: float) -> dict:
    return {
        "initial_capital": initial_capital,
        "cash": initial_capital,
        "collateral": 0.0,
        "shares": {},
        "realized_pnl": 0.0,
        "history": [],
        "equity_cost":       {},  # ticker → 累计买股现金支出（用于动态成本计算）
        "premium_collected": {},  # ticker → 累计 Short 期权权利金收入
    }


def record_trade(account: dict, entry: dict) -> None:
    account["history"].append(entry)


def open_position(account: dict, pos: dict, entry_date: date, entry_mid: float) -> None:
    cash_delta = -pos["direction"] * entry_mid * CONTRACT_MULT * pos["qty"]
    account["cash"] += cash_delta
    # Short 开仓：累计记录该标的的权利金收入
    if pos["direction"] == -1:
        ticker = pos.get("ticker", "")
        if ticker:
            pc = account.setdefault("premium_collected", {})
            pc[ticker] = pc.get(ticker, 0.0) + entry_mid * CONTRACT_MULT * pos["qty"]
    record_trade(account, {
        "date": entry_date,
        "action": f"开仓 {pos['dir_label']}",
        "symbol": pos["symbol"],
        "qty": pos["qty"],
        "price": entry_mid,
        "cash_delta": cash_delta,
        "realized": 0.0,
        "note": f"{pos['type']} ${pos['strike']:.0f} 到期 {pos['expiry']}",
    })


def close_position(account: dict, pos: dict, close_date: date, close_mid: float,
                   action_label: str) -> None:
    cash_delta = pos["direction"] * close_mid * CONTRACT_MULT * pos["qty"]
    realized = pos["direction"] * (close_mid - pos["entry_mid"]) * CONTRACT_MULT * pos["qty"]
    account["cash"] += cash_delta
    account["realized_pnl"] += realized
    pos["close_date"] = close_date
    pos["close_mid"] = close_mid
    pos["realized"] = realized
    record_trade(account, {
        "date": close_date,
        "action": f"{action_label} {pos['dir_label']}",
        "symbol": pos["symbol"],
        "qty": pos["qty"],
        "price": close_mid,
        "cash_delta": cash_delta,
        "realized": realized,
        "note": f"{pos['type']} ${pos['strike']:.0f}",
    })


def _assign_short_put(account: dict, pos: dict, assign_date: date) -> None:
    """Short Put ITM 到期：买入正股，实现期权权利金。"""
    shares_qty = CONTRACT_MULT * pos["qty"]
    cost = pos["strike"] * shares_qty
    account["cash"] -= cost
    # 追踪该标的的累计买股支出（用于动态成本计算）
    ec = account.setdefault("equity_cost", {})
    ec[pos["ticker"]] = ec.get(pos["ticker"], 0.0) + cost

    # 权利金全额实现（期权以 $0 消亡，行权价买入股票）
    realized = pos["entry_mid"] * CONTRACT_MULT * pos["qty"]
    account["realized_pnl"] += realized

    # 更新股票持仓（加权平均成本）
    ticker = pos["ticker"]
    if ticker not in account["shares"]:
        account["shares"][ticker] = {"qty": 0, "cost_basis": pos["strike"]}
    old_qty = account["shares"][ticker]["qty"]
    old_basis = account["shares"][ticker]["cost_basis"]
    new_qty = old_qty + shares_qty
    account["shares"][ticker]["cost_basis"] = (
        (old_qty * old_basis + shares_qty * pos["strike"]) / new_qty
    )
    account["shares"][ticker]["qty"] = new_qty

    pos["close_date"] = assign_date
    pos["close_mid"] = 0.0
    pos["realized"] = realized
    record_trade(account, {
        "date": assign_date,
        "action": "Short Put 被行权 - 买入股票",
        "symbol": ticker,
        "qty": shares_qty,
        "price": pos["strike"],
        "cash_delta": -cost,
        "realized": realized,
        "note": f"买入 {shares_qty} 股 @ ${pos['strike']:.2f} | 权利金 ${realized:.2f} 已实现",
    })


def _assign_short_call(account: dict, pos: dict, assign_date: date, spot: float | None) -> None:
    """Short Call ITM 到期：备兑→按行权价卖出正股；裸→按内在价值现金结算。"""
    ticker = pos["ticker"]
    shares_qty = CONTRACT_MULT * pos["qty"]
    premium = pos["entry_mid"] * CONTRACT_MULT * pos["qty"]

    # 优先走 covered 标记；兜底：未标记但确有足量正股也按备兑处理
    is_covered = bool(pos.get("covered"))
    held = account["shares"].get(ticker, {}).get("qty", 0)
    if not is_covered and held >= shares_qty:
        is_covered = True

    if is_covered:
        proceeds = pos["strike"] * shares_qty
        cost_basis = account["shares"][ticker]["cost_basis"]
        account["shares"][ticker]["qty"] -= shares_qty
        account["cash"] += proceeds
        stock_pnl = (pos["strike"] - cost_basis) * shares_qty
        realized = premium + stock_pnl
        cash_delta = proceeds
        action = "Short Call 被行权 - 卖出股票 (备兑)"
        note = (f"按 ${pos['strike']:.2f} 出售 {shares_qty} 股 | "
                f"权利金 ${premium:+,.2f} + 股票差价 ${stock_pnl:+,.2f}")
        # 持股归零时重置本轮成本追踪，避免历史数据污染下一轮动态成本
        if account["shares"][ticker]["qty"] <= 0:
            account.setdefault("equity_cost", {})[ticker] = 0.0
            account.setdefault("premium_collected", {})[ticker] = 0.0
    else:
        intrinsic = intrinsic_value("Call", pos["strike"], spot) * CONTRACT_MULT * pos["qty"]
        account["cash"] -= intrinsic
        realized = premium - intrinsic
        cash_delta = -intrinsic
        action = "Short Call 被行权 - 现金结算 (裸)"
        note = f"裸 Call 现金结算，内在价值 ${intrinsic:,.2f}"

    account["realized_pnl"] += realized
    pos["close_date"] = assign_date
    pos["close_mid"] = 0.0
    pos["realized"] = realized
    record_trade(account, {
        "date": assign_date,
        "action": action,
        "symbol": ticker if is_covered else pos["symbol"],
        "qty": shares_qty if is_covered else pos["qty"],
        "price": pos["strike"],
        "cash_delta": cash_delta,
        "realized": realized,
        "note": note,
    })


def _expire_worthless(account: dict, pos: dict, expire_date: date) -> None:
    """OTM 到期：期权归零，实现方向性损益。"""
    # Short: 保留全部权利金；Long: 损失全部权利金
    realized = pos["direction"] * (0 - pos["entry_mid"]) * CONTRACT_MULT * pos["qty"]
    account["realized_pnl"] += realized
    pos["close_date"] = expire_date
    pos["close_mid"] = 0.0
    pos["realized"] = realized
    record_trade(account, {
        "date": expire_date,
        "action": f"到期废弃 (OTM) {pos['dir_label']}",
        "symbol": pos["symbol"],
        "qty": pos["qty"],
        "price": 0.0,
        "cash_delta": 0.0,
        "realized": realized,
        "note": f"{pos['type']} ${pos['strike']:.0f} OTM 废弃",
    })


def buy_equity(account: dict, ticker: str, price: float, qty: int, buy_date: date) -> None:
    """直接以市价买入正股：扣款 + 更新持仓 + 追踪成本 + 记录流水。"""
    cost = price * qty
    account["cash"] -= cost
    ec = account.setdefault("equity_cost", {})
    ec[ticker] = ec.get(ticker, 0.0) + cost

    sh = account["shares"]
    if ticker not in sh:
        sh[ticker] = {"qty": 0, "cost_basis": price}
    old_qty   = sh[ticker]["qty"]
    old_basis = sh[ticker]["cost_basis"]
    new_qty   = old_qty + qty
    sh[ticker]["cost_basis"] = (old_qty * old_basis + qty * price) / new_qty
    sh[ticker]["qty"] = new_qty

    record_trade(account, {
        "date":       buy_date,
        "action":     "直接买入股票",
        "symbol":     ticker,
        "qty":        qty,
        "price":      price,
        "cash_delta": -cost,
        "realized":   0.0,
        "note":       f"以市价 ${price:.2f} 买入 {qty} 股 {ticker}",
    })


def compute_dynamic_cost(account: dict, ticker: str) -> float | None:
    """动态成本/股 = (累计买股支出 - 累计期权权利金) / 当前持股数。
    反映车轮策略将每股实际成本压缩了多少。"""
    qty = account["shares"].get(ticker, {}).get("qty", 0)
    if qty <= 0:
        return None
    equity_cost = account.get("equity_cost", {}).get(ticker, 0.0)
    premium     = account.get("premium_collected", {}).get(ticker, 0.0)
    return (equity_cost - premium) / qty


# ── Workspace save / load ──────────────────────────────────────────────────────

def _serialize_date(obj):
    if isinstance(obj, date):
        return obj.isoformat()
    raise TypeError(f"Not serializable: {type(obj)}")


def _pos_to_json(pos: dict) -> dict:
    out = dict(pos)
    for k in ("expiry", "entry_date", "close_date"):
        if k in out and isinstance(out[k], date):
            out[k] = out[k].isoformat()
    return out


def _pos_from_json(d: dict) -> dict:
    out = dict(d)
    for k in ("expiry", "entry_date", "close_date"):
        if k in out and isinstance(out[k], str):
            out[k] = date.fromisoformat(out[k])
    return out


def _history_to_json(h: dict) -> dict:
    out = dict(h)
    if isinstance(out.get("date"), date):
        out["date"] = out["date"].isoformat()
    return out


def _history_from_json(d: dict) -> dict:
    out = dict(d)
    if isinstance(out.get("date"), str):
        out["date"] = date.fromisoformat(out["date"])
    return out


def save_workspace(account: dict, positions: list, closed_positions: list,
                   current_date_idx: int, active_ticker: str) -> str:
    """将当前盘面状态序列化为 JSON 字符串。"""
    acc_copy = dict(account)
    acc_copy["history"] = [_history_to_json(h) for h in account["history"]]
    payload = {
        "version": 1,
        "active_ticker": active_ticker,
        "current_date_idx": current_date_idx,
        "initial_capital": account["initial_capital"],
        "account": acc_copy,
        "positions": [_pos_to_json(p) for p in positions],
        "closed_positions": [_pos_to_json(p) for p in closed_positions],
    }
    return json.dumps(payload, default=_serialize_date, ensure_ascii=False, indent=2)


def load_workspace(json_str: str) -> dict:
    """从 JSON 字符串还原盘面状态，返回 payload dict。"""
    payload = json.loads(json_str)
    acc = payload["account"]
    acc["history"] = [_history_from_json(h) for h in acc.get("history", [])]
    payload["account"] = acc
    payload["positions"] = [_pos_from_json(p) for p in payload.get("positions", [])]
    payload["closed_positions"] = [_pos_from_json(p) for p in payload.get("closed_positions", [])]
    return payload


def get_available_shares(account: dict, open_positions: list, ticker: str) -> int:
    """账户中未被现有 Short Call 备兑占用的可用股数。"""
    total = account["shares"].get(ticker, {}).get("qty", 0)
    reserved = sum(
        pos["qty"] * CONTRACT_MULT
        for pos in open_positions
        if (pos.get("ticker") == ticker
            and pos["type"] == "Call"
            and pos["direction"] == -1
            and pos.get("covered"))
    )
    return int(total - reserved)


def auto_expire(df: pd.DataFrame, account: dict, open_positions: list,
                closed_positions: list, cur_date: date) -> list:
    """到期自动结算。返回仍活跃的 positions。"""
    still_open = []
    for pos in open_positions:
        if pos["expiry"] > cur_date:
            still_open.append(pos)
            continue
        spot = get_underlying_at(df, pos["expiry"])
        is_itm = intrinsic_value(pos["type"], pos["strike"], spot) > 0
        if pos["direction"] == -1:  # Short
            if is_itm:
                if pos["type"] == "Put":
                    _assign_short_put(account, pos, pos["expiry"])
                else:
                    _assign_short_call(account, pos, pos["expiry"], spot)
            else:
                _expire_worthless(account, pos, pos["expiry"])
        else:  # Long
            iv = intrinsic_value(pos["type"], pos["strike"], spot)
            close_position(account, pos, pos["expiry"], iv,
                           action_label=f"到期结算 ({'ITM' if is_itm else 'OTM'})")
        closed_positions.append(pos)
    return still_open


def compute_unrealized(df: pd.DataFrame, open_positions: list, cur_date: date) -> float:
    total = 0.0
    for pos in open_positions:
        mid = get_mid(df, pos["symbol"], cur_date)
        if mid is not None:
            total += pos["direction"] * (mid - pos["entry_mid"]) * CONTRACT_MULT * pos["qty"]
    return total


def compute_equity_series(df: pd.DataFrame, account: dict, open_positions: list,
                          closed_positions: list, all_dates: list, cur_date: date) -> pd.Series:
    """Equity(d) = cash(d) + MTM(所有 d 日在持的 positions)"""
    # 预构建 mid 查询表
    needed_syms = {p["symbol"] for p in open_positions + closed_positions}
    mid_lookup: dict[str, dict[date, float]] = {}
    for sym in needed_syms:
        sub = df[df["option_symbol"] == sym][["snapshot_date", "mid"]]
        mid_lookup[sym] = dict(zip(sub["snapshot_date"], sub["mid"]))

    history_sorted = sorted(account["history"], key=lambda h: h["date"])
    end_idx = all_dates.index(cur_date)
    dates_up_to = all_dates[:end_idx + 1]

    equity_vals = []
    for d in dates_up_to:
        cash = account["initial_capital"]
        for h in history_sorted:
            if h["date"] <= d:
                cash += h["cash_delta"]
            else:
                break

        pos_value = 0.0
        # 活跃：entry_date <= d
        for pos in open_positions:
            if pos["entry_date"] <= d:
                mid = mid_lookup.get(pos["symbol"], {}).get(d)
                if mid is not None:
                    pos_value += pos["direction"] * float(mid) * CONTRACT_MULT * pos["qty"]
        # 已平仓：entry_date <= d < close_date
        for pos in closed_positions:
            if pos["entry_date"] <= d and d < pos["close_date"]:
                mid = mid_lookup.get(pos["symbol"], {}).get(d)
                if mid is not None:
                    pos_value += pos["direction"] * float(mid) * CONTRACT_MULT * pos["qty"]

        equity_vals.append(cash + pos_value)

    return pd.Series(equity_vals, index=dates_up_to, name="总权益")


# ── Session state ──────────────────────────────────────────────────────────────
if "positions" not in st.session_state:
    st.session_state.positions = []
if "closed_positions" not in st.session_state:
    st.session_state.closed_positions = []
if "active_ticker" not in st.session_state:
    st.session_state.active_ticker = "QQQ"
if "initial_capital" not in st.session_state:
    st.session_state.initial_capital = 10000.0
if "account" not in st.session_state:
    st.session_state.account = init_account(st.session_state.initial_capital)
if "current_date_idx" not in st.session_state:
    st.session_state.current_date_idx = None  # 加载数据后初始化
if "_pending_date_idx" not in st.session_state:
    st.session_state._pending_date_idx = None
if "active_data_file" not in st.session_state:
    st.session_state.active_data_file = None

# ── Sidebar ────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("过滤控制")

    selected_ticker = st.radio(
        "标的",
        list(TICKER_DATA.keys()),
        index=list(TICKER_DATA.keys()).index(st.session_state.active_ticker)
              if st.session_state.active_ticker in TICKER_DATA else 0,
        horizontal=True,
    )
    ticker_changed = selected_ticker != st.session_state.active_ticker
    if ticker_changed:
        st.session_state.active_ticker = selected_ticker
        st.session_state.positions = []
        st.session_state.closed_positions = []
        st.session_state.account = init_account(st.session_state.initial_capital)
        st.session_state.current_date_idx = None
        st.session_state.active_data_file = None  # 重置文件选择

    # ── 数据文件选择 ──────────────────────────────────────────────────────────
    file_options = TICKER_DATA.get(selected_ticker, [])
    if not file_options:
        st.error(f"data/ 目录下找不到 {selected_ticker} 的数据文件")
        st.stop()

    file_labels = [label for label, _ in file_options]
    file_paths  = [path  for _, path  in file_options]

    # 默认选最新文件（列表末尾）
    if "active_data_file" not in st.session_state or st.session_state.active_data_file not in file_paths:
        st.session_state.active_data_file = file_paths[-1]

    selected_file = st.selectbox(
        "数据文件",
        file_paths,
        index=file_paths.index(st.session_state.active_data_file),
        format_func=lambda p: file_labels[file_paths.index(p)],
    )
    file_changed = selected_file != st.session_state.active_data_file
    if file_changed:
        st.session_state.active_data_file = selected_file
        st.session_state.current_date_idx = None  # 重置日期，不清空账户

    data_path = selected_file
    try:
        df = load_data(data_path, selected_ticker)
    except FileNotFoundError:
        st.error(f"找不到数据文件：{data_path}")
        st.stop()

    all_dates = sorted(df["snapshot_date"].unique())

    # 工作区加载后，将暂存的日期索引校验并应用
    if st.session_state._pending_date_idx is not None:
        idx = st.session_state._pending_date_idx
        st.session_state.current_date_idx = max(0, min(idx, len(all_dates) - 1))
        st.session_state._pending_date_idx = None

    # 初始化日期索引
    if st.session_state.current_date_idx is None or st.session_state.current_date_idx >= len(all_dates):
        st.session_state.current_date_idx = len(all_dates) - 1

    st.divider()
    st.subheader("账户设置")
    new_capital = st.number_input(
        "初始资金 ($)",
        min_value=1000.0,
        max_value=10_000_000.0,
        value=st.session_state.initial_capital,
        step=1000.0,
    )
    if new_capital != st.session_state.initial_capital:
        st.session_state.initial_capital = new_capital
        st.session_state.account = init_account(new_capital)
        st.session_state.positions = []
        st.session_state.closed_positions = []

    if st.button("🔄 重置账户", use_container_width=True):
        st.session_state.account = init_account(st.session_state.initial_capital)
        st.session_state.positions = []
        st.session_state.closed_positions = []
        st.rerun()

    st.divider()
    st.subheader("工作区保存 / 加载")

    # ── 保存 ──────────────────────────────────────────────────────────────────
    ws_json = save_workspace(
        st.session_state.account,
        st.session_state.positions,
        st.session_state.closed_positions,
        st.session_state.current_date_idx if st.session_state.current_date_idx is not None else 0,
        st.session_state.active_ticker,
    )
    fname = f"workspace_{st.session_state.active_ticker}_{date.today().isoformat()}.json"
    st.download_button(
        "💾 保存进度",
        data=ws_json,
        file_name=fname,
        mime="application/json",
        use_container_width=True,
        help="将当前账户、持仓、历史记录全部保存为 JSON 文件",
    )

    # ── 加载 ──────────────────────────────────────────────────────────────────
    uploaded_ws = st.file_uploader(
        "📂 加载进度",
        type=["json"],
        key="workspace_uploader",
        help="上传之前保存的 .json 工作区文件，100% 还原盘面状态",
    )
    if uploaded_ws is not None:
        try:
            payload = load_workspace(uploaded_ws.read().decode("utf-8"))
            st.session_state.account           = payload["account"]
            st.session_state.positions         = payload["positions"]
            st.session_state.closed_positions  = payload["closed_positions"]
            st.session_state.initial_capital   = payload["initial_capital"]
            st.session_state.active_ticker     = payload["active_ticker"]
            # current_date_idx 需要在数据加载后校验范围，先暂存
            st.session_state._pending_date_idx = payload.get("current_date_idx", 0)
            st.success(f"工作区已加载：{len(payload['positions'])} 个持仓，{len(payload['account']['history'])} 条流水")
            st.rerun()
        except Exception as e:
            st.error(f"加载失败：{e}")

    st.divider()
    st.subheader("时间控制")
    selected_date = st.select_slider(
        "交易日（复盘日）",
        options=all_dates,
        value=all_dates[st.session_state.current_date_idx],
        format_func=lambda d: str(d),
    )
    st.session_state.current_date_idx = all_dates.index(selected_date)

    day_df = df[df["snapshot_date"] == selected_date]
    available_expiries = sorted(day_df["expiry"].unique())

    selected_expiry = st.selectbox(
        "到期日",
        available_expiries,
        format_func=lambda d: f"{d}  (DTE {(d - selected_date).days})",
    )

    # 跳到到期日：让复盘日 = 选中的到期日
    if selected_expiry in all_dates:
        if st.button("📅 跳到到期日", use_container_width=True,
                     help=f"将复盘日设为 {selected_expiry}"):
            st.session_state.current_date_idx = all_dates.index(selected_expiry)
            st.rerun()
    else:
        st.caption(f"⚠️ 到期日 {selected_expiry} 不在快照范围内，无法跳转")

    option_type = st.radio("期权类型", ["Call", "Put"])

# ── 到期自动结算（在所有计算之前执行）─────────────────────────────────────────
st.session_state.positions = auto_expire(
    df,
    st.session_state.account,
    st.session_state.positions,
    st.session_state.closed_positions,
    selected_date,
)

# acct 在此处提前定义，供后续所有 UI 区块共享
acct = st.session_state.account

# ── Filter chain ───────────────────────────────────────────────────────────────
filtered = (
    day_df[(day_df["expiry"] == selected_expiry) & (day_df["type"] == option_type)]
    .sort_values("strike")
    .reset_index(drop=True)
)

# ── Main area ──────────────────────────────────────────────────────────────────
st.title(f"{selected_ticker} 期权复盘工具")

# ── 顶部：股价/日期/DTE + 时间推进按钮 ───────────────────────────────────────
est_price = estimate_underlying(day_df)
top_cols = st.columns([1.2, 1.0, 1.2, 1, 0.8, 0.8, 0.8])
with top_cols[0]:
    st.metric(
        f"{selected_ticker} 估算股价",
        f"${est_price:.2f}" if est_price is not None else "N/A",
        help="由最短 DTE 的 delta≈0.5 ATM Call 行权价推算",
    )
with top_cols[1]:
    st.write("")
    st.write("")
    if est_price is not None:
        buy_cost = est_price * 100
        if acct["cash"] >= buy_cost:
            if st.button(
                f"🛒 买入 100 股\n-${buy_cost:,.0f}",
                use_container_width=True,
                key="buy_equity_btn",
                help=f"以当前估算市价 ${est_price:.2f} 直接买入 100 股 {selected_ticker}",
            ):
                buy_equity(acct, selected_ticker, est_price, 100, selected_date)
                st.rerun()
        else:
            st.button(
                f"🛒 买入 100 股",
                use_container_width=True,
                disabled=True,
                key="buy_equity_btn_disabled",
                help=f"现金不足（需 ${buy_cost:,.0f}，当前 ${acct['cash']:,.0f}）",
            )
    else:
        st.button("🛒 买入 100 股", use_container_width=True, disabled=True, key="buy_equity_btn_na")
with top_cols[2]:
    st.metric("复盘日", str(selected_date))
with top_cols[3]:
    dte = (selected_expiry - selected_date).days
    st.metric("距到期 (DTE)", f"{dte} 天")

with top_cols[4]:
    st.write("")
    st.write("")
    if st.button("⏪ -1日", use_container_width=True,
                 disabled=st.session_state.current_date_idx == 0):
        st.session_state.current_date_idx -= 1
        st.rerun()
with top_cols[5]:
    st.write("")
    st.write("")
    if st.button("⏭ +1日", use_container_width=True,
                 disabled=st.session_state.current_date_idx >= len(all_dates) - 1):
        st.session_state.current_date_idx += 1
        st.rerun()
with top_cols[6]:
    st.write("")
    st.write("")
    if st.button("⏩ +1周", use_container_width=True,
                 disabled=st.session_state.current_date_idx >= len(all_dates) - 1):
        st.session_state.current_date_idx = min(
            len(all_dates) - 1, st.session_state.current_date_idx + 5
        )
        st.rerun()

st.divider()

# ── 账户概览 ────────────────────────────────────────────────────────────────
# 期权未实现 PnL
option_unrealized = compute_unrealized(df, st.session_state.positions, selected_date)

# 股票市值（仅限当前显示标的；其他标的暂按成本计）
shares_market_value = 0.0
for tkr, sh in acct["shares"].items():
    if sh["qty"] <= 0:
        continue
    if tkr == selected_ticker and est_price is not None:
        price = est_price
    else:
        price = sh["cost_basis"]  # 其他标的暂用成本代替市价
    shares_market_value += sh["qty"] * price

shares_unrealized = sum(
    sh["qty"] * (est_price - sh["cost_basis"])
    for tkr, sh in acct["shares"].items()
    if sh["qty"] > 0 and tkr == selected_ticker and est_price is not None
)

total_equity = acct["cash"] + shares_market_value + sum(
    pos["direction"] * (get_mid(df, pos["symbol"], selected_date) or pos["entry_mid"])
    * CONTRACT_MULT * pos["qty"]
    for pos in st.session_state.positions
)

st.subheader("账户概览")
acc_cols = st.columns(6)
acc_cols[0].metric("初始资金", f"${acct['initial_capital']:,.2f}")
acc_cols[1].metric(
    "可用现金",
    f"${acct['cash']:,.2f}",
    delta=f"{acct['cash'] - acct['initial_capital']:+,.2f}",
)
acc_cols[2].metric("已实现 PnL", f"${acct['realized_pnl']:+,.2f}")
acc_cols[3].metric("未实现 PnL",
                   f"${option_unrealized + shares_unrealized:+,.2f}",
                   help="期权 MTM + 股票持仓浮动")
acc_cols[4].metric(
    "总权益",
    f"${total_equity:,.2f}",
    delta=f"{total_equity - acct['initial_capital']:+,.2f}",
)
# 动态成本：(买股支出 - 累计权利金) / 持股数
dyn_cost = compute_dynamic_cost(acct, selected_ticker)
if dyn_cost is not None:
    book_basis = acct["shares"][selected_ticker]["cost_basis"]
    acc_cols[5].metric(
        "动态成本/股",
        f"${dyn_cost:.2f}",
        delta=f"{dyn_cost - book_basis:+.2f} vs 账面",
        delta_color="inverse",
        help="(累计买股支出 - 历史期权权利金) ÷ 持股数。负 delta 表示车轮已压低成本",
    )
else:
    acc_cols[5].metric("动态成本/股", "—",
                       help="持有正股后自动计算：账面成本被期权权利金压低了多少")

# ── 股票持仓 ────────────────────────────────────────────────────────────────
if any(sh["qty"] > 0 for sh in acct["shares"].values()):
    st.subheader("股票持仓")
    share_rows = []
    for tkr, sh in acct["shares"].items():
        if sh["qty"] <= 0:
            continue
        mkt_price = est_price if (tkr == selected_ticker and est_price) else None
        mkt_val = sh["qty"] * mkt_price if mkt_price else None
        unreal = (mkt_price - sh["cost_basis"]) * sh["qty"] if mkt_price else None
        share_rows.append({
            "标的": tkr,
            "持有股数": sh["qty"],
            "成本价": sh["cost_basis"],
            "当前市价": mkt_price,
            "市值": mkt_val,
            "股票浮盈": unreal,
        })
    sh_df = pd.DataFrame(share_rows)

    def color_share_pnl(val):
        if pd.isna(val):
            return ""
        return "color: #4CAF50; font-weight:bold" if val > 0 else "color: #F44336; font-weight:bold"

    st.dataframe(
        sh_df.style
        .map(color_share_pnl, subset=["股票浮盈"])
        .format({
            "持有股数": "{:.0f}",
            "成本价":   lambda v: f"${v:.2f}" if pd.notna(v) else "—",
            "当前市价": lambda v: f"${v:.2f}" if pd.notna(v) else "N/A",
            "市值":     lambda v: f"${v:,.2f}" if pd.notna(v) else "—",
            "股票浮盈": lambda v: f"${v:+,.2f}" if pd.notna(v) else "—",
        }),
        use_container_width=True,
        hide_index=True,
    )

st.divider()

# ── Option chain ───────────────────────────────────────────────────────────────
st.subheader(f"期权链  {option_type} | 到期 {selected_expiry}")
st.caption("⚠️ Theta 为 QuantConnect 原始值，单位待确认。bid=ask 是数据源限制，建仓实际成本请心算滑点。")

chain_display = (
    filtered.dropna(subset=["delta"])
    [["strike", "mid", "delta", "theta", "gamma", "vega", "iv",
      "volume", "open_interest", "bid", "ask", "option_symbol"]]
    .copy()
)
chain_display.columns = ["Strike", "Mid", "Delta", "Theta(原始)", "Gamma", "Vega",
                         "IV", "Volume", "OI", "Bid", "Ask", "合约代码"]


def style_delta(val):
    if pd.isna(val):
        return ""
    return "color: #2196F3" if val > 0 else "color: #F44336"


styled = (
    chain_display.style
    .map(style_delta, subset=["Delta"])
    .format({
        "Strike": "{:.1f}", "Mid": "{:.2f}", "Bid": "{:.2f}", "Ask": "{:.2f}",
        "Delta": "{:.4f}", "Theta(原始)": "{:.2f}", "Gamma": "{:.5f}",
        "Vega": "{:.4f}", "IV": "{:.3f}", "Volume": "{:,.0f}", "OI": "{:,.0f}",
    })
)
st.dataframe(styled, use_container_width=True, hide_index=True)

st.divider()

# ── Covered Call (备兑卖出) ────────────────────────────────────────────────────
held_shares = acct["shares"].get(selected_ticker, {}).get("qty", 0)
if held_shares > 0:
    st.subheader("🛡️ 备兑卖出 Call (Covered Call)")
    available_shares = get_available_shares(acct, st.session_state.positions, selected_ticker)
    max_cc = available_shares // CONTRACT_MULT
    reserved = held_shares - available_shares

    cc_cols = st.columns(4)
    cc_cols[0].metric(f"{selected_ticker} 持股", f"{held_shares} 股")
    cc_cols[1].metric("已锁定 (备兑占用)", f"{reserved} 股")
    cc_cols[2].metric("可用正股", f"{available_shares} 股")
    cc_cols[3].metric("最多可卖 CC", f"{max_cc} 张")

    if max_cc == 0:
        st.info("所有正股已被现有备兑 Call 占用，等待到期或平仓后可再次卖出。")
    else:
        # 备兑区域独立选择到期日，不受侧边栏限制
        cc_all_expiries = sorted(
            day_df[day_df["type"] == "Call"]["expiry"].unique()
        )
        cc_expiry = st.selectbox(
            "CC 到期日",
            cc_all_expiries,
            format_func=lambda d: f"{d}  (DTE {(d - selected_date).days})",
            key="cc_expiry_select",
        )

        cc_chain = (
            day_df[(day_df["expiry"] == cc_expiry) & (day_df["type"] == "Call")]
            .dropna(subset=["delta"])
            .sort_values("strike")
        )
        # 典型 CC 用 OTM Call，过滤 strike >= 当前股价（允许 ATM）
        if est_price is not None:
            cc_chain = cc_chain[cc_chain["strike"] >= est_price]

        if cc_chain.empty:
            st.info(f"到期 {cc_expiry} 无可选 OTM Call。")
        else:
            cc_chain = cc_chain.copy()
            cc_chain["label"] = cc_chain.apply(
                lambda r: (f"${r['strike']:.0f} Call | Δ{r['delta']:+.2f} | "
                           f"mid ${r['mid']:.2f}"),
                axis=1,
            )
            with st.form("add_covered_call", clear_on_submit=True):
                ccf_cols = st.columns([3, 1, 1])
                cc_label = ccf_cols[0].selectbox(
                    "选择 OTM Call 合约",
                    cc_chain["label"].tolist(),
                    help="备兑策略通常卖出 OTM Call，赚取权利金的同时为正股设定止盈位",
                )
                cc_qty = ccf_cols[1].number_input(
                    "张数", min_value=1, max_value=int(max_cc), value=1,
                    help=f"最多 {max_cc} 张（由可用正股 {available_shares} 决定）",
                )
                ccf_cols[2].write("")
                ccf_cols[2].write("")
                cc_submit = ccf_cols[2].form_submit_button(
                    "🛡️ 卖出备兑 Call", use_container_width=True,
                )
                if cc_submit:
                    row = cc_chain[cc_chain["label"] == cc_label].iloc[0]
                    new_pos = {
                        "ticker": selected_ticker,
                        "symbol": row["option_symbol"],
                        "strike": float(row["strike"]),
                        "type": "Call",
                        "expiry": row["expiry"],
                        "direction": -1,
                        "dir_label": "Short",
                        "qty": int(cc_qty),
                        "entry_mid": float(row["mid"]),
                        "entry_date": selected_date,
                        "covered": True,
                    }
                    open_position(acct, new_pos, selected_date, float(row["mid"]))
                    st.session_state.positions.append(new_pos)
                    st.success(
                        f"已卖出备兑 Call：${row['strike']:.0f} x{cc_qty} "
                        f"(锁定 {int(cc_qty) * CONTRACT_MULT} 股)"
                    )
                    st.rerun()
    st.divider()

# ── Virtual position entry ─────────────────────────────────────────────────────
st.subheader("虚拟建仓")

if filtered.empty:
    st.info("当前过滤条件下无合约，请调整日期/到期日/类型。")
else:
    chain_for_entry = filtered.dropna(subset=["delta"]).copy()
    chain_for_entry["label"] = chain_for_entry.apply(
        lambda r: f"${r['strike']:.0f} {r['type']} | Δ{r['delta']:+.2f} | mid ${r['mid']:.2f}",
        axis=1,
    )

    with st.form("add_position", clear_on_submit=True):
        col_sym, col_dir, col_qty = st.columns([3, 1, 1])
        with col_sym:
            selected_label = st.selectbox("选择合约", chain_for_entry["label"].tolist())
        with col_dir:
            direction_label = st.radio("方向", ["卖出 (Short)", "买入 (Long)"])
        with col_qty:
            qty = st.number_input("张数", min_value=1, max_value=100, value=1)

        submitted = st.form_submit_button("加入持仓", use_container_width=True)
        if submitted:
            row = chain_for_entry[chain_for_entry["label"] == selected_label].iloc[0]
            direction = -1 if "Short" in direction_label else 1
            dir_label = "Short" if direction == -1 else "Long"
            new_pos = {
                "ticker": selected_ticker,
                "symbol": row["option_symbol"],
                "strike": float(row["strike"]),
                "type": row["type"],
                "expiry": row["expiry"],           # date 对象
                "direction": direction,
                "dir_label": dir_label,
                "qty": int(qty),
                "entry_mid": float(row["mid"]),
                "entry_date": selected_date,       # date 对象
            }
            # Short Call 且有足量未占用正股 → 自动备兑
            if direction == -1 and row["type"] == "Call":
                avail = get_available_shares(
                    st.session_state.account, st.session_state.positions, selected_ticker,
                )
                if avail >= int(qty) * CONTRACT_MULT:
                    new_pos["covered"] = True
            open_position(st.session_state.account, new_pos, selected_date, float(row["mid"]))
            st.session_state.positions.append(new_pos)
            covered_tag = " 🛡️" if new_pos.get("covered") else ""
            st.success(f"已加入：{selected_label}  {dir_label} x{qty}{covered_tag}")
            st.rerun()

st.divider()

# ── Positions & PnL ────────────────────────────────────────────────────────────
st.subheader("当前持仓")

if not st.session_state.positions:
    st.info("暂无活跃持仓。")
else:
    rows = []
    for pos in st.session_state.positions:
        cur_mid = get_mid(df, pos["symbol"], selected_date)
        if cur_mid is not None:
            pnl = pos["direction"] * (cur_mid - pos["entry_mid"]) * CONTRACT_MULT * pos["qty"]
        else:
            pnl = None

        dir_display = pos["dir_label"]
        if pos.get("covered"):
            dir_display += " 🛡️"
        rows.append({
            "标的": pos.get("ticker", selected_ticker),
            "合约": f"${pos['strike']:.0f} {pos['type']}",
            "方向": dir_display,
            "张数": pos["qty"],
            "入场价": pos["entry_mid"],
            "当前价": cur_mid,
            "PnL": pnl,
            "入场日": str(pos["entry_date"]),
            "到期日": str(pos["expiry"]),
        })

    pos_df = pd.DataFrame(rows)

    def color_pnl(val):
        if pd.isna(val):
            return ""
        if val > 0:
            return "color: #4CAF50; font-weight: bold"
        if val < 0:
            return "color: #F44336; font-weight: bold"
        return ""

    styled_pos = (
        pos_df.style
        .map(color_pnl, subset=["PnL"])
        .format({
            "入场价": lambda v: f"${v:.2f}" if pd.notna(v) else "—",
            "当前价": lambda v: f"${v:.2f}" if pd.notna(v) else "—",
            "PnL":    lambda v: f"${v:+.2f}" if pd.notna(v) else "—",
        })
    )
    st.dataframe(styled_pos, use_container_width=True, hide_index=True)

    st.divider()
    st.subheader("平仓操作")
    close_cols = st.columns(min(len(st.session_state.positions), 4))
    to_remove = None
    for i, pos in enumerate(st.session_state.positions):
        col = close_cols[i % len(close_cols)]
        label = f"平仓 #{i}  ${pos['strike']:.0f} {pos['type']} ({pos['dir_label']})"
        if col.button(label, key=f"close_{i}"):
            to_remove = i

    if to_remove is not None:
        pos = st.session_state.positions[to_remove]
        cur_mid = get_mid(df, pos["symbol"], selected_date)
        if cur_mid is None:
            st.warning(f"持仓 #{to_remove} 在 {selected_date} 无报价，无法平仓")
        else:
            close_position(st.session_state.account, pos, selected_date, cur_mid,
                           action_label="手动平仓")
            st.session_state.closed_positions.append(pos)
            st.session_state.positions.pop(to_remove)
            st.rerun()

st.divider()

# ── Equity curve ───────────────────────────────────────────────────────────────
st.subheader("总权益曲线")
if not acct["history"]:
    st.info("建仓后将显示权益曲线。")
else:
    equity = compute_equity_series(
        df, acct,
        st.session_state.positions,
        st.session_state.closed_positions,
        all_dates,
        selected_date,
    )
    chart_df = pd.DataFrame({
        "总权益": equity.values,
        "初始资金": [acct["initial_capital"]] * len(equity),
    }, index=equity.index)
    st.line_chart(chart_df, use_container_width=True)

# ── Trade history ──────────────────────────────────────────────────────────────
with st.expander(f"📜 交易流水（{len(acct['history'])} 条）", expanded=False):
    if not acct["history"]:
        st.info("暂无交易。")
    else:
        hist_df = pd.DataFrame(acct["history"])
        hist_df = hist_df[["date", "action", "symbol", "qty", "price",
                           "cash_delta", "realized", "note"]].copy()
        hist_df.columns = ["日期", "动作", "合约代码", "张数", "价格",
                           "现金变化", "已实现PnL", "备注"]
        hist_df["日期"] = hist_df["日期"].astype(str)
        st.dataframe(
            hist_df.style.format({
                "价格": "${:.2f}",
                "现金变化": "${:+,.2f}",
                "已实现PnL": "${:+,.2f}",
            }),
            use_container_width=True,
            hide_index=True,
        )

st.caption("数据来源：QuantConnect Research Notebook | 保证金/正股持仓为下一版本实现")
