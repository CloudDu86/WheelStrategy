"""
fetch_options_yfinance.py
用 yfinance 抓取 QQQ 和 TQQQ 的完整当前期权链，导出 CSV。

安装依赖：
    pip install yfinance pandas

说明：
  - yfinance 只提供当前可见的期权链（所有到期日），不含历史每日快照
  - 如需历史数据，请使用 LEAN CLI + Research Notebook 方案
"""

import yfinance as yf
import pandas as pd
from datetime import datetime
from pathlib import Path

TICKERS    = ["QQQ", "TQQQ"]
OUTPUT_DIR = Path(".")
TODAY      = datetime.today().strftime("%Y%m%d")

all_frames = []

for ticker_str in TICKERS:
    print(f"\n{'='*55}")
    print(f"  {ticker_str}")
    print(f"{'='*55}")

    t = yf.Ticker(ticker_str)

    # 当前股价
    info      = t.fast_info
    und_price = round(info.last_price, 4) if hasattr(info, "last_price") else None
    print(f"  Underlying price: {und_price}")

    # 所有可用到期日
    expirations = t.options
    print(f"  Available expirations: {len(expirations)}")
    for exp in expirations:
        print(f"    {exp}")

    rows = []
    for exp in expirations:
        try:
            chain = t.option_chain(exp)
        except Exception as e:
            print(f"  [WARN] Failed to fetch {exp}: {e}")
            continue

        for side, df_side in [("Call", chain.calls), ("Put", chain.puts)]:
            if df_side.empty:
                continue

            df_side = df_side.copy()
            df_side.insert(0, "fetch_date",      TODAY)
            df_side.insert(1, "underlying",      ticker_str)
            df_side.insert(2, "underlying_price", und_price)
            df_side.insert(3, "expiration",      exp)
            df_side.insert(4, "option_type",     side)

            rows.append(df_side)

        print(f"    {exp}: {len(chain.calls)} calls, {len(chain.puts)} puts")

    if not rows:
        print(f"  [WARN] No data for {ticker_str}")
        continue

    ticker_df = pd.concat(rows, ignore_index=True)

    # 整理列顺序
    front_cols = ["fetch_date","underlying","underlying_price","expiration","option_type",
                  "strike","lastPrice","bid","ask","volume","openInterest",
                  "impliedVolatility","inTheMoney","contractSymbol"]
    keep = [c for c in front_cols if c in ticker_df.columns]
    rest = [c for c in ticker_df.columns if c not in keep]
    ticker_df = ticker_df[keep + rest]

    # Rename for clarity
    ticker_df.rename(columns={
        "lastPrice"       : "last",
        "openInterest"    : "open_interest",
        "impliedVolatility": "iv",
        "inTheMoney"      : "itm",
        "contractSymbol"  : "option_symbol",
    }, inplace=True)

    all_frames.append(ticker_df)

    # Per-ticker CSV
    out = OUTPUT_DIR / f"{ticker_str}_options_{TODAY}.csv"
    ticker_df.to_csv(out, index=False)
    print(f"\n  Saved: {out}  ({len(ticker_df):,} rows)")

# Combined CSV
if all_frames:
    combined = pd.concat(all_frames, ignore_index=True)
    out = OUTPUT_DIR / f"QQQ_TQQQ_options_combined_{TODAY}.csv"
    combined.to_csv(out, index=False)
    print(f"\nCombined saved: {out}  ({len(combined):,} rows total)")

    # Quick summary
    print("\n── Summary ──────────────────────────────────────────────")
    summary = combined.groupby(["underlying","option_type"]).agg(
        expirations = ("expiration",   "nunique"),
        contracts   = ("option_symbol","count"),
        iv_mean     = ("iv",           "mean"),
    ).round(4)
    print(summary.to_string())
    print("─────────────────────────────────────────────────────────")
else:
    print("\n[ERROR] No data collected.")
