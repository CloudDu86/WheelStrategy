# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ research_voo_leaps.py — VOO LEAPS (深实值长期 Call) 数据抓取             ║
# ║                                                                          ║
# ║ 用途：为 options_dashboard.py 的 🚀 LEAPS 模式准备数据                    ║
# ║ 抓取：VOO Long Call，DTE 365-800，Delta 0.50-0.95（深 ITM 区间）          ║
# ║ 周期：2023-01 → 2025-12（LEAPS 流动性更早年份较差，从 2023 起）          ║
# ║                                                                          ║
# ║ 运行环境：QuantConnect Cloud Research Notebook（按 Cell 顺序逐块运行）   ║
# ║ 输出：VOO_LEAPS_20230101_20251231_greeks.csv                             ║
# ║                                                                          ║
# ║ 与 research_qqq_options.py 的区别：                                       ║
# ║   1. Strikes(-30, 5)    — 深 ITM 在标的价下方，向下放宽                   ║
# ║   2. Expiration(365,800)— 长期合约（典型 LEAPS 1-2 年到期）              ║
# ║   3. 只抓 Call          — LEAPS Put 几乎无人用                           ║
# ║   4. Delta 0.50-0.95    — 深 ITM 替代正股区间，Δ≈0.80 为甜区             ║
# ║   5. 按周采样          — LEAPS 价格日内变化小，周采样节省 5x 数据量    ║
# ╚══════════════════════════════════════════════════════════════════════════╝

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
    .Strikes(-30, 5)           # 深 ITM 在标的下方，向下放宽到 -30；上方留 5 档备用
    .Expiration(365, 800))     # LEAPS 范围：~1 年到 ~2.2 年

STORE_KEY = f"{TICKER}_LEAPS_20230101_20251231_greeks.csv"
SAVE_PATH = os.path.join("Output", STORE_KEY)

print(f"订阅完成: {TICKER}")
print(f"当前时间: {qb.Time}")
print(f"过滤器: Strikes(-30, 5) × Expiration(365, 800)")


# ── Cell 2：获取交易日列表（按周采样，每周五一次）──────────────────────────
# LEAPS 价格日内变化很小，周采样足以支持回测，且数据量减少约 5 倍
start_time = datetime(2023, 1, 1)
end_time   = datetime(2025, 12, 31)

# 切到日度模式可改为：SAMPLE_MODE = "daily"
SAMPLE_MODE = "weekly"   # "weekly" 或 "daily"

print(f"数据区间: {start_time.date()} → {end_time.date()}  (采样: {SAMPLE_MODE})")

daily_hist = qb.History(equity.Symbol, start_time, end_time, Resolution.Daily)
if daily_hist.empty:
    raise RuntimeError("未能获取正股历史")

all_trade_dates = (daily_hist.index
                   .get_level_values("time")
                   .normalize()
                   .unique()
                   .sort_values())

if SAMPLE_MODE == "weekly":
    # 每周仅保留最后一个交易日（一般是周五，遇假期顺延为周四）
    trade_dates = pd.Series(all_trade_dates).groupby(
        all_trade_dates.to_period("W")
    ).last().tolist()
else:
    trade_dates = list(all_trade_dates)

print(f"全部交易日: {len(all_trade_dates)}  →  采样后: {len(trade_dates)}")


# ── Cell 3：SetDateTime + OptionChain 逐日抓取（含 Greeks）──────────────────
all_frames = []

for dt in trade_dates:
    try:
        # 定位到当天 15:45，Greeks 最稳定
        qb.SetDateTime(datetime(dt.year, dt.month, dt.day, 15, 45))

        chain = qb.OptionChain(option.Symbol)

        rows = []
        for contract in chain:
            # ── LEAPS 过滤条件 ─────────────────────────────────────────────
            # 1) 只要 Call（LEAPS Put 几乎无人用，省 50% 数据量）
            if contract.Right != OptionRight.Call:
                continue

            dte = (contract.Expiry - dt).days
            # 2) DTE 365-800（一般 LEAPS 上市约 2.5 年到期）
            if not (365 <= dte <= 800):
                continue

            g = contract.Greeks
            if g:
                delta_abs = abs(float(g.Delta))
                # 3) Delta 0.50-0.95：深 ITM 替代正股区间
                #    Δ≈0.80 是经典 LEAPS 甜区（杠杆 2-3x，但 theta 损耗有限）
                if delta_abs < 0.50 or delta_abs > 0.95:
                    continue
            # ─────────────────────────────────────────────────────────────

            rows.append({
                "snapshot_date" : dt.strftime("%Y-%m-%d"),
                "expiry"        : contract.Expiry.strftime("%Y-%m-%d"),
                "dte"           : dte,
                "strike"        : float(contract.Strike),
                "type"          : "Call",
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
            print(f"[{dt.strftime('%Y-%m-%d')}] 无 LEAPS 数据")
            continue

        day_df    = pd.DataFrame(rows)
        has_delta = day_df["delta"].notna().any()
        all_frames.append(day_df)
        print(f"[{dt.strftime('%Y-%m-%d')}] ✓  {len(rows)} 条  "
              f"DTE {day_df['dte'].min()}-{day_df['dte'].max()}  "
              f"Strike {day_df['strike'].min():.0f}-{day_df['strike'].max():.0f}  "
              f"delta={'有' if has_delta else '无'}")

    except Exception as e:
        print(f"[{dt.strftime('%Y-%m-%d')}] ✗  {e}")

    finally:
        gc.collect()


# ── Cell 4：合并、清洗、保存 ───────────────────────────────────────────────────
if not all_frames:
    print("⚠️  没有收集到任何 LEAPS 数据。可能原因：")
    print("    1. 抓取时间太早，VOO LEAPS 当时未上市（早期建议从 2023 起）")
    print("    2. SetFilter 范围太窄，建议放宽 Strikes")
else:
    combined = pd.concat(all_frames, ignore_index=True)
    combined["strike"] = pd.to_numeric(combined["strike"], errors="coerce")
    combined.sort_values(["snapshot_date", "expiry", "strike"], inplace=True)
    combined.reset_index(drop=True, inplace=True)

    csv_string = combined.to_csv(index=False)

    # ① Output 目录（Explorer 可下载）
    os.makedirs("Output", exist_ok=True)
    with open(SAVE_PATH, "w", encoding="utf-8") as f:
        f.write(csv_string)
    print(f"\n✅ 已保存: {os.path.abspath(SAVE_PATH)}  ({len(combined):,} 行)")

    # ② Object Store 备份
    qb.ObjectStore.Save(STORE_KEY, csv_string)
    print(f"✅ Object Store 备份完成")

    # ── 数据质量报告 ─────────────────────────────────────────────────────────
    print(f"\n=== 数据质量报告 ===")
    print(f"列名        : {list(combined.columns)}")
    print(f"快照日数    : {combined['snapshot_date'].nunique()}")
    print(f"到期日数    : {combined['expiry'].nunique()}")
    print(f"行权价范围  : ${combined['strike'].min():.0f} → ${combined['strike'].max():.0f}")
    print(f"DTE 范围    : {combined['dte'].min()} → {combined['dte'].max()} 天")

    if combined["delta"].notna().any():
        valid = combined["delta"].notna().sum()
        print(f"delta 有效行: {valid:,} / {len(combined):,}")
        print(f"delta 范围  : {combined['delta'].min():.3f} → {combined['delta'].max():.3f}")

        # LEAPS 甜区参考：接近 delta=0.80 的合约
        near_sweet = combined.iloc[(combined["delta"] - 0.80).abs().argsort()[:5]]
        print(f"\n接近 delta=0.80 的 LEAPS（深 ITM 甜区参考）:")
        print(near_sweet[["snapshot_date","expiry","dte","strike","delta","iv","mid"]].to_string(index=False))

        # 流动性检查
        liquid = combined[(combined["volume"] + combined["open_interest"]) >= 5]
        print(f"\n流动性 ≥ 5 (volume+OI): {len(liquid):,} / {len(combined):,} = "
              f"{100*len(liquid)/len(combined):.1f}%")
    else:
        print("⚠️  delta 列为空")

    print(f"\n前 5 行:")
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
