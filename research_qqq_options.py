# ── Cell 1：初始化 ─────────────────────────────────────────────────────────────
from AlgorithmImports import *
import pandas as pd
import gc
import os
import io
import base64
from datetime import datetime, timedelta
from IPython.display import HTML, display

qb     = QuantBook()
TICKER = "VOO"

equity = qb.AddEquity(TICKER, Resolution.Hour)
equity.SetDataNormalizationMode(DataNormalizationMode.Raw)

option = qb.AddOption(TICKER, Resolution.Minute)
option.SetFilter(lambda u: u
    .IncludeWeeklys()
    .Strikes(-15, 15)     # VOO 价格高、strike 间距大，需要更宽
    .Expiration(1, 60))   # CC 操作覆盖 30-45 DTE，上限扩到 60

STORE_KEY = f"{TICKER}_options_20220101_20251231_greeks.csv"
SAVE_PATH = os.path.join("Output", STORE_KEY)

print(f"订阅完成: {TICKER}")
print(f"当前时间: {qb.Time}")


# ── Cell 2：获取交易日列表 ──────────────────────────────────────────────────────
start_time = datetime(2022, 1, 1)
end_time   = datetime(2025, 12, 31)

print(f"数据区间: {start_time.date()} → {end_time.date()}")

daily_hist = qb.History(equity.Symbol, start_time, end_time, Resolution.Daily)
if daily_hist.empty:
    raise RuntimeError("未能获取正股历史")

trade_dates = (daily_hist.index
               .get_level_values("time")
               .normalize()
               .unique()
               .sort_values())

print(f"找到 {len(trade_dates)} 个交易日")


# ── Cell 3：SetDateTime + OptionChain 逐日抓取（含 Greeks）────────────────────
all_frames = []

for dt in trade_dates:
    try:
        # 定位到当天 15:45，Greeks 最稳定
        qb.SetDateTime(datetime(dt.year, dt.month, dt.day, 15, 45))

        chain = qb.OptionChain(option.Symbol)

        rows = []
        for contract in chain:
            dte = (contract.Expiry - dt).days
            g   = contract.Greeks

            # ── 车轮策略过滤条件 ───────────────────────────────────────────
            # 保留 DTE 1-60 的合约：DTE<7 用于 Roll Up & Out 取最后一周报价
            if not (1 <= dte <= 60):
                continue
            # 只保留 delta 在有效范围内的合约
            if g:
                delta_abs = abs(float(g.Delta))
                # Put：delta -0.05 ~ -0.50（CSP目标区间 -0.25）
                # Call：delta  0.05 ~  0.60（CC目标区间  0.30）
                if delta_abs < 0.05 or delta_abs > 0.60:
                    continue
            # ─────────────────────────────────────────────────────────────

            rows.append({
                "snapshot_date" : dt.strftime("%Y-%m-%d"),
                "expiry"        : contract.Expiry.strftime("%Y-%m-%d"),
                "dte"           : dte,
                "strike"        : float(contract.Strike),
                "type"          : "Call" if contract.Right == OptionRight.Call else "Put",
                "bid"           : float(contract.BidPrice),
                "ask"           : float(contract.AskPrice),
                "mid"           : round((float(contract.BidPrice) + float(contract.AskPrice)) / 2, 4),
                "last"          : float(contract.LastPrice),
                "volume"        : float(contract.Volume),
                "open_interest" : int(contract.OpenInterest),
                "iv"            : round(float(contract.ImpliedVolatility), 5) if contract.ImpliedVolatility else None,
                "delta"         : round(float(g.Delta), 5) if g else None,
                "gamma"         : round(float(g.Gamma), 5) if g else None,
                "theta"         : round(float(g.Theta), 5) if g else None,
                "vega"          : round(float(g.Vega),  5) if g else None,
                "rho"           : round(float(g.Rho),   5) if g else None,
            })

        if not rows:
            print(f"[{dt.strftime('%Y-%m-%d')}] 无数据")
            continue

        day_df    = pd.DataFrame(rows)
        has_delta = day_df["delta"].notna().any()
        all_frames.append(day_df)
        print(f"[{dt.strftime('%Y-%m-%d')}] ✓  {len(rows)} 条  delta={'有' if has_delta else '无'}")

    except Exception as e:
        print(f"[{dt.strftime('%Y-%m-%d')}] ✗  {e}")

    finally:
        gc.collect()


# ── Cell 4：合并、清洗、保存 ───────────────────────────────────────────────────
if not all_frames:
    print("⚠️  没有收集到任何数据")
else:
    combined = pd.concat(all_frames, ignore_index=True)
    combined["strike"] = pd.to_numeric(combined["strike"], errors="coerce")
    combined.sort_values(["snapshot_date", "type", "expiry", "strike"], inplace=True)
    combined.reset_index(drop=True, inplace=True)

    csv_string = combined.to_csv(index=False)

    # ① Output 目录（Explorer 可下载）
    os.makedirs("Output", exist_ok=True)
    with open(SAVE_PATH, "w", encoding="utf-8") as f:
        f.write(csv_string)
    print(f"✅ 已保存: {os.path.abspath(SAVE_PATH)}  ({len(combined):,} 行)")

    # ② Object Store 备份
    qb.ObjectStore.Save(STORE_KEY, csv_string)
    print(f"✅ Object Store 备份完成")

    # ── 数据质量报告 ─────────────────────────────────────────────────────────
    print(f"\n列名        : {list(combined.columns)}")
    print(f"交易日数    : {combined['snapshot_date'].nunique()}")
    print(f"行权价范围  : {combined['strike'].min()} → {combined['strike'].max()}")

    if "delta" in combined.columns and combined["delta"].notna().any():
        valid = combined["delta"].notna().sum()
        print(f"delta 有效行: {valid:,} / {len(combined):,}")
        print(f"delta 范围  : {combined['delta'].min():.3f} → {combined['delta'].max():.3f}")

        # 车轮策略参考：接近 delta=-0.25 的 Put
        puts = combined[(combined["type"] == "Put") & combined["delta"].notna()]
        if not puts.empty:
            near = puts.iloc[(puts["delta"].abs() - 0.25).abs().argsort()[:5]]
            print(f"\n接近 delta=-0.25 的 Put（CSP 目标行权价参考）:")
            print(near[["snapshot_date","expiry","dte","strike","delta","iv","mid"]].to_string(index=False))
    else:
        print("⚠️  delta 列为空")

    print(f"\n前5行:")
    print(combined.head(5).to_string())


# ── Cell 5：生成浏览器内下载按钮 ───────────────────────────────────────────────
if os.path.exists(SAVE_PATH):
    with open(SAVE_PATH, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    display(HTML(f'''
    <a download="{STORE_KEY}"
       href="data:text/csv;base64,{b64}"
       style="display:inline-block;padding:10px 24px;background:#0066cc;
              color:white;border-radius:6px;text-decoration:none;font-weight:bold">
       ⬇ 下载 {STORE_KEY}
    </a>
    '''))
else:
    # 从 Object Store 恢复再下载
    if qb.ObjectStore.ContainsKey(STORE_KEY):
        content = qb.ObjectStore.Read(STORE_KEY)
        os.makedirs("Output", exist_ok=True)
        with open(SAVE_PATH, "w", encoding="utf-8") as f:
            f.write(content)
        with open(SAVE_PATH, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        display(HTML(f'''
        <a download="{STORE_KEY}"
           href="data:text/csv;base64,{b64}"
           style="display:inline-block;padding:10px 24px;background:#0066cc;
                  color:white;border-radius:6px;text-decoration:none;font-weight:bold">
           ⬇ 下载 {STORE_KEY}（从 Object Store 恢复）
        </a>
        '''))
    else:
        print("未找到文件，请先运行 Cell 3-4")
