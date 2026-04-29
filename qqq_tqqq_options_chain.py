"""
QQQ & TQQQ — Historical Options Chain Extractor (past 30 days)
Run this inside QuantConnect Research Notebook cell-by-cell.

QuantConnect Research environment provides:
  - qb  : QuantBook instance (already initialised in the notebook kernel)
  - All LEAN namespaces are pre-imported
"""

from datetime import datetime, timedelta
import pandas as pd
from pathlib import Path

# ─── 0. Configuration ─────────────────────────────────────────────────────────
TICKERS        = ["QQQ", "TQQQ"]
LOOKBACK_DAYS  = 30
OUTPUT_DIR     = Path(".")          # change to e.g. Path("/tmp") if needed
RESOLUTION     = Resolution.Daily   # Daily for chain snapshots; use Minute for intraday

end_date   = qb.Time                           # notebook's current time
start_date = end_date - timedelta(days=LOOKBACK_DAYS)

print(f"Date range: {start_date.date()} → {end_date.date()}")

# ─── 1. Add equities & subscribe to option chains ─────────────────────────────
option_symbols = {}

for ticker in TICKERS:
    equity = qb.AddEquity(ticker, Resolution.Daily)
    option = qb.AddOption(ticker, Resolution.Daily)

    # Broad filter — collect ALL strikes and ALL expirations
    option.SetFilter(lambda u: u.IncludeWeeklys().Strikes(-100, 100).Expiration(0, 365))

    option_symbols[ticker] = option.Symbol
    print(f"  Subscribed: {ticker}  →  option root: {option.Symbol}")

# ─── 2. Pull history ──────────────────────────────────────────────────────────
all_rows = []

for ticker in TICKERS:
    print(f"\nFetching option history for {ticker} …")

    history = qb.OptionHistory(
        option_symbols[ticker],
        start_date,
        end_date,
        Resolution.Daily,
    )

    if history.empty:
        print(f"  [WARN] No data returned for {ticker}.")
        continue

    # history is a MultiIndex DataFrame: (expiry, strike, type, symbol) × time
    # Flatten to a tidy frame
    df = history.reset_index()

    # Rename common columns for clarity
    rename_map = {
        "time"           : "date",
        "symbol"         : "option_symbol",
        "expiry"         : "expiration",
        "strike"         : "strike",
        "type"           : "option_type",         # 0 = Call, 1 = Put
        "open"           : "open",
        "high"           : "high",
        "low"            : "low",
        "close"          : "close",
        "volume"         : "volume",
        "openinterest"   : "open_interest",
        "impliedvolatility": "implied_volatility",
        "delta"          : "delta",
        "gamma"          : "gamma",
        "theta"          : "theta",
        "vega"           : "vega",
        "rho"            : "rho",
    }
    df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns}, inplace=True)

    # Decode option type integer → human-readable label
    if "option_type" in df.columns:
        df["option_type"] = df["option_type"].map({0: "Call", 1: "Put"}).fillna(df["option_type"])

    df.insert(0, "underlying", ticker)

    all_rows.append(df)
    print(f"  Rows fetched: {len(df):,}")

# ─── 3. Combine & export ──────────────────────────────────────────────────────
if not all_rows:
    print("\n[ERROR] No data collected. Check subscription and date range.")
else:
    combined = pd.concat(all_rows, ignore_index=True)

    # Sort for readability
    sort_cols = [c for c in ["underlying", "date", "expiration", "option_type", "strike"]
                 if c in combined.columns]
    combined.sort_values(sort_cols, inplace=True)
    combined.reset_index(drop=True, inplace=True)

    # ── per-ticker CSVs ──
    for ticker in TICKERS:
        subset = combined[combined["underlying"] == ticker]
        if subset.empty:
            continue
        out_path = OUTPUT_DIR / f"{ticker}_options_chain_{start_date.strftime('%Y%m%d')}_{end_date.strftime('%Y%m%d')}.csv"
        subset.to_csv(out_path, index=False)
        print(f"Saved: {out_path}  ({len(subset):,} rows)")

    # ── combined CSV ──
    combined_path = OUTPUT_DIR / f"QQQ_TQQQ_options_chain_combined.csv"
    combined.to_csv(combined_path, index=False)
    print(f"Saved: {combined_path}  ({len(combined):,} rows total)")

    # ── quick summary ──
    print("\n── Summary ──────────────────────────────────────────────")
    print(combined.groupby(["underlying", "option_type"])["strike"].count()
                  .rename("contract_snapshots")
                  .to_string())
    print("─────────────────────────────────────────────────────────")
