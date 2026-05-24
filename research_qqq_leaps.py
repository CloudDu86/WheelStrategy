# ==========================================================================
# research_qqq_leaps.py -- QQQ LEAPS (deep ITM long Call) data extraction
#
# Purpose: prepare QQQ LEAPS data for options_dashboard.py LEAPS mode
# Capture: QQQ Long Call, DTE 30-900, Delta 0.50-0.95 (deep ITM range)
# Period:  2018-01 -> 2025-12 (QQQ LEAPS liquidity active since 2018)
#
# Design goal: maximize sample period to cover:
#   2018 stable / 2019 bull / 2020 COVID / 2021 mania / 2022 bear
#   2023 rebound / 2024 AI bull / 2025 highs -- complete 8-year multi-cycle
#
# v2 (2026-05-21): DTE lower bound dropped from 365 to 30 to cover the
#   full LEAPS lifecycle. Previous version cut off the last ~12 months
#   before expiry, losing track of held contracts as DTE decayed below 365.
#   Data volume will increase ~2-3x (estimated 15-25 MB).
#
# Runtime: QuantConnect Cloud Research Notebook (run cells in order)
# Output : QQQ_LEAPS_20180101_20251231_greeks.csv (overwrites old file)
#
# NOTE: This file is intentionally ASCII-only. QC Notebook API rejects
#       emoji and certain Unicode characters when saving research.ipynb.
# ==========================================================================

# ---- Cell 1: initialization -----------------------------------------------
from AlgorithmImports import *
import pandas as pd
import gc
import os
import io
import base64
from datetime import datetime, timedelta
from IPython.display import HTML, display

qb     = QuantBook()
TICKER = "QQQ"

equity = qb.AddEquity(TICKER, Resolution.Hour)
equity.SetDataNormalizationMode(DataNormalizationMode.Raw)

option = qb.AddOption(TICKER, Resolution.Minute)
option.SetFilter(lambda u: u
    .IncludeWeeklys()
    .Strikes(-30, 5)           # deep ITM is below spot; widen downside to -30
    .Expiration(30, 900))      # v2: extend lower to 30 -> full lifecycle

STORE_KEY = f"{TICKER}_LEAPS_20180101_20251231_greeks.csv"
SAVE_PATH = os.path.join("Output", STORE_KEY)

print(f"Subscribed: {TICKER}")
print(f"Current time: {qb.Time}")
print(f"Filter: Strikes(-30, 5) x Expiration(30, 900)  [v2 full lifecycle]")


# ---- Cell 2: build sampling date list (weekly default) --------------------
# 8 years x 52 weeks ~= 416 sample points, 20-40 LEAPS each -> ~10-15K rows
# Switch to daily: set SAMPLE_MODE = "daily" (data volume ~5x)
start_time = datetime(2018, 1, 1)    # QQQ LEAPS backtrack to early 2018
end_time   = datetime(2025, 12, 31)

SAMPLE_MODE = "weekly"   # "weekly" / "daily" / "biweekly"

print(f"Range: {start_time.date()} -> {end_time.date()}  (sample: {SAMPLE_MODE})")
print(f"Span:  {(end_time - start_time).days} days  ~= {(end_time - start_time).days/365:.1f} years")

daily_hist = qb.History(equity.Symbol, start_time, end_time, Resolution.Daily)
if daily_hist.empty:
    raise RuntimeError("Failed to fetch equity history")

all_trade_dates = (daily_hist.index
                   .get_level_values("time")
                   .normalize()
                   .unique()
                   .sort_values())

if SAMPLE_MODE == "weekly":
    # Keep only last trading day per week (typically Friday)
    trade_dates = pd.Series(all_trade_dates).groupby(
        all_trade_dates.to_period("W")
    ).last().tolist()
elif SAMPLE_MODE == "biweekly":
    weekly = pd.Series(all_trade_dates).groupby(
        all_trade_dates.to_period("W")
    ).last().tolist()
    trade_dates = weekly[::2]
else:
    trade_dates = list(all_trade_dates)

print(f"Total trading days: {len(all_trade_dates)}  ->  sampled: {len(trade_dates)}")


# ---- Cell 3: SetDateTime + OptionChain per-day capture with Greeks --------
all_frames = []
skipped_count = 0

for dt in trade_dates:
    try:
        # 15:45 -- Greeks are most stable here
        qb.SetDateTime(datetime(dt.year, dt.month, dt.day, 15, 45))

        chain = qb.OptionChain(option.Symbol)

        rows = []
        for contract in chain:
            # LEAPS filter conditions
            # 1) Call only (LEAPS Put rarely used, saves 50%)
            if contract.Right != OptionRight.Call:
                continue

            dte = (contract.Expiry - dt).days
            # 2) DTE 30-900 (v2: full LEAPS lifecycle from new-issue to near-expiry)
            #    Original 365 lower bound dropped contracts in their final 12 months,
            #    losing visibility of holdings as theta accelerated.
            if not (30 <= dte <= 900):
                continue

            g = contract.Greeks
            if g:
                delta_abs = abs(float(g.Delta))
                # 3) Delta 0.50-0.95: deep ITM (sweet spot ~0.80)
                if delta_abs < 0.50 or delta_abs > 0.95:
                    continue

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
            skipped_count += 1
            # Early-year LEAPS are sparse; print every 10th skip only
            if skipped_count % 10 == 1:
                print(f"[{dt.strftime('%Y-%m-%d')}] no LEAPS (skipped total: {skipped_count})")
            continue

        day_df    = pd.DataFrame(rows)
        has_delta = day_df["delta"].notna().any()
        all_frames.append(day_df)
        print(f"[{dt.strftime('%Y-%m-%d')}] OK  {len(rows):>3} rows  "
              f"DTE {day_df['dte'].min()}-{day_df['dte'].max()}  "
              f"Strike ${day_df['strike'].min():.0f}-${day_df['strike'].max():.0f}  "
              f"delta={'Y' if has_delta else 'N'}")

    except Exception as e:
        print(f"[{dt.strftime('%Y-%m-%d')}] ERR  {e}")

    finally:
        gc.collect()

print(f"\nCapture done: {len(all_frames)} valid snapshots, {skipped_count} empty")


# ---- Cell 4: merge, clean, save ------------------------------------------
if not all_frames:
    print("WARN  No LEAPS data collected. Possible reasons:")
    print("    1. Start date too early; QQQ LEAPS sparse pre-2018")
    print("    2. SetFilter too narrow; try Strikes(-40, 5)")
    print("    3. QC data permission or network issue")
else:
    combined = pd.concat(all_frames, ignore_index=True)
    combined["strike"] = pd.to_numeric(combined["strike"], errors="coerce")
    combined.sort_values(["snapshot_date", "expiry", "strike"], inplace=True)
    combined.reset_index(drop=True, inplace=True)

    csv_string = combined.to_csv(index=False)

    # (1) Output directory (browser-downloadable)
    os.makedirs("Output", exist_ok=True)
    with open(SAVE_PATH, "w", encoding="utf-8") as f:
        f.write(csv_string)
    print(f"\nOK  Saved: {os.path.abspath(SAVE_PATH)}  ({len(combined):,} rows)")

    # (2) Object Store backup (soft-fail: QC Free has 50MB cap, may overflow)
    try:
        qb.ObjectStore.Save(STORE_KEY, csv_string)
        print(f"OK  Object Store backup done ({len(csv_string)/1024/1024:.2f} MB)")
    except Exception as e:
        print(f"WARN  Object Store backup failed: {type(e).__name__}")
        print(f"      Output/ CSV is unaffected -- Cell 5 download button still works.")
        print(f"      To free Object Store space, list keys with qb.ObjectStore.Keys")
        print(f"      and delete unused ones via qb.ObjectStore.Delete('key_name').")

    # Data quality report
    print(f"\n=== Data quality report ===")
    print(f"Columns       : {list(combined.columns)}")
    print(f"Snapshot days : {combined['snapshot_date'].nunique()}")
    print(f"Expiry count  : {combined['expiry'].nunique()}")
    print(f"Strike range  : ${combined['strike'].min():.0f} -> ${combined['strike'].max():.0f}")
    print(f"DTE range     : {combined['dte'].min()} -> {combined['dte'].max()} days")

    # Yearly breakdown
    combined["year"] = combined["snapshot_date"].str[:4]
    print(f"\nDistribution by year:")
    yr_stats = combined.groupby("year").agg(
        snapshots=("snapshot_date", "nunique"),
        rows=("strike", "count"),
    )
    print(yr_stats.to_string())
    combined.drop(columns="year", inplace=True)

    if combined["delta"].notna().any():
        valid = combined["delta"].notna().sum()
        print(f"\ndelta valid rows: {valid:,} / {len(combined):,}")
        print(f"delta range     : {combined['delta'].min():.3f} -> {combined['delta'].max():.3f}")

        near_sweet = combined.iloc[(combined["delta"] - 0.80).abs().argsort()[:5]]
        print(f"\nNearest to delta=0.80 (LEAPS sweet spot reference):")
        print(near_sweet[["snapshot_date","expiry","dte","strike","delta","iv","mid"]].to_string(index=False))

        liquid = combined[(combined["volume"] + combined["open_interest"]) >= 5]
        print(f"\nLiquidity >= 5 (volume+OI): {len(liquid):,} / {len(combined):,} = "
              f"{100*len(liquid)/len(combined):.1f}%")
    else:
        print("WARN  delta column empty")

    print(f"\nFirst 5 rows:")
    print(combined.head(5).to_string())
    print(f"\nLast 5 rows:")
    print(combined.tail(5).to_string())


# ---- Cell 5: in-browser download button -----------------------------------
if os.path.exists(SAVE_PATH):
    with open(SAVE_PATH, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    display(HTML(f'''
    <a download="{STORE_KEY}"
       href="data:text/csv;base64,{b64}"
       style="display:inline-block;padding:10px 24px;background:#0066cc;
              color:white;border-radius:6px;text-decoration:none;font-weight:bold">
       Download {STORE_KEY}
    </a>
    '''))
else:
    # Recover from Object Store and download
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
           Download {STORE_KEY} (from Object Store)
        </a>
        '''))
    else:
        print("File not found, please run Cell 3-4 first")
