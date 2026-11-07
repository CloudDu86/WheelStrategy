# TQQQ options scraper for Call-buying study (DTE 1-60, Delta 0.05-0.95, DAILY)
# Purpose: validate "buy TQQQ Call" vs "buy QQQ Call" with FULL coverage.
#   - Delta up to 0.95 so winning ATM calls (which go ITM) stay in the dataset
#   - DTE down to 1 so near-expiry closes are trackable
#   (the old TQQQ CSVs capped Delta at 0.60 / DTE at 7 -> winners & expiry
#    closes dropped out; this fixes both)
#
# Coverage : 2021-2025, ONE CSV per year (avoids 50MB Object Store cap +
#            QC free-tier session termination on long runs)
# Sampling : every trade day (daily), 15:45 ET snapshot
# Notes    : Pure ASCII (QC Notebook API rejects emoji on Save)
#            Object Store save wrapped in try/except (50MB free-tier cap)
#            TQQQ had a 2:1 split on 2022-01-13; Raw mode shows actual
#            historical prices (2021 strikes look ~2x higher than post-2022).
#
# HOW TO RUN:
#   Set YEAR below to 2021, then Run All -> download CSV.
#   Restart notebook, set YEAR=2022, Run All -> download. Repeat to 2025.
#   Drop all 5 CSVs into  D:\Claude_Code\QuantConnect\data\
#   Dashboard auto-lists each as  [options] <year>0101 -> <year>1231

# -- Cell 1: init --------------------------------------------------------------
from AlgorithmImports import *
import pandas as pd
import gc
import os
import io
import base64
from datetime import datetime, timedelta
from IPython.display import HTML, display

qb     = QuantBook()
TICKER = "TQQQ"
YEAR   = 2021            # <-- CHANGE THIS each run: 2021 / 2022 / 2023 / 2024 / 2025

equity = qb.AddEquity(TICKER, Resolution.Hour)
equity.SetDataNormalizationMode(DataNormalizationMode.Raw)

option = qb.AddOption(TICKER, Resolution.Minute)
option.SetFilter(lambda u: u
    .IncludeWeeklys()
    .Strikes(-35, 20)         # deep ITM (delta->0.95) below, OTM (delta->0.05) above
    .Expiration(1, 60))       # DTE 1-60

STORE_KEY = f"{TICKER}_options_{YEAR}0101_{YEAR}1231_greeks.csv"
SAVE_PATH = os.path.join("Output", STORE_KEY)

print(f"Subscribed: {TICKER}  YEAR={YEAR}")
print(f"QB time: {qb.Time}")


# -- Cell 2: trade dates (single year) -----------------------------------------
start_time = datetime(YEAR, 1, 1)
end_time   = datetime(YEAR, 12, 31)

print(f"Range: {start_time.date()} -> {end_time.date()}")

daily_hist = qb.History(equity.Symbol, start_time, end_time, Resolution.Daily)
if daily_hist.empty:
    raise RuntimeError("No equity history")

trade_dates = (daily_hist.index
               .get_level_values("time")
               .normalize()
               .unique()
               .sort_values())

print(f"Trade days: {len(trade_dates)}")


# -- Cell 3: per-day OptionChain scrape ----------------------------------------
all_frames = []

for dt in trade_dates:
    try:
        # 15:45 ET - Greeks most stable near close
        qb.SetDateTime(datetime(dt.year, dt.month, dt.day, 15, 45))

        chain = qb.OptionChain(option.Symbol)

        rows = []
        for contract in chain:
            dte = (contract.Expiry - dt).days
            g   = contract.Greeks

            # Filter: DTE 1-60
            if not (1 <= dte <= 60):
                continue

            # Filter: Delta range 0.05-0.95 (covers far-OTM to deep-ITM calls;
            #         winning ATM calls that go ITM stay captured)
            if g:
                delta_abs = abs(float(g.Delta))
                if delta_abs < 0.05 or delta_abs > 0.95:
                    continue

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
            print(f"[{dt.strftime('%Y-%m-%d')}] empty")
            continue

        day_df    = pd.DataFrame(rows)
        has_delta = day_df["delta"].notna().any()
        all_frames.append(day_df)
        print(f"[{dt.strftime('%Y-%m-%d')}] OK  rows={len(rows)}  delta={'Y' if has_delta else 'N'}")

    except Exception as e:
        print(f"[{dt.strftime('%Y-%m-%d')}] FAIL  {e}")

    finally:
        gc.collect()


# -- Cell 4: merge + save ------------------------------------------------------
if not all_frames:
    print("No data collected")
else:
    combined = pd.concat(all_frames, ignore_index=True)
    combined["strike"] = pd.to_numeric(combined["strike"], errors="coerce")
    combined.sort_values(["snapshot_date", "type", "expiry", "strike"], inplace=True)
    combined.reset_index(drop=True, inplace=True)

    csv_string = combined.to_csv(index=False)

    # (1) Output dir (Explorer download)
    os.makedirs("Output", exist_ok=True)
    with open(SAVE_PATH, "w", encoding="utf-8") as f:
        f.write(csv_string)
    print(f"Saved: {os.path.abspath(SAVE_PATH)}  ({len(combined):,} rows)")

    # (2) Object Store backup - soft fail on quota exceed
    try:
        qb.ObjectStore.Save(STORE_KEY, csv_string)
        print("Object Store backup OK")
    except Exception as e:
        print(f"Object Store save failed (continuing): {e}")

    # Data quality report
    print(f"\nColumns     : {list(combined.columns)}")
    print(f"Trade days  : {combined['snapshot_date'].nunique()}")
    print(f"Strike range: {combined['strike'].min()} -> {combined['strike'].max()}")
    print(f"DTE range   : {combined['dte'].min()} -> {combined['dte'].max()}")

    if "delta" in combined.columns and combined["delta"].notna().any():
        valid = combined["delta"].notna().sum()
        print(f"delta valid : {valid:,} / {len(combined):,}")
        print(f"delta range : {combined['delta'].min():.3f} -> {combined['delta'].max():.3f}")

        # Call buyer reference: closest to delta=0.50 (ATM)
        calls = combined[(combined["type"] == "Call") & combined["delta"].notna()]
        if not calls.empty:
            near = calls.iloc[(calls["delta"] - 0.50).abs().argsort()[:5]]
            print(f"\nNear delta=0.50 Call (ATM buy target):")
            print(near[["snapshot_date","expiry","dte","strike","delta","iv","mid"]].to_string(index=False))
    else:
        print("delta col empty")

    print(f"\nHead 5:")
    print(combined.head(5).to_string())


# -- Cell 5: in-browser download button ----------------------------------------
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
           Download {STORE_KEY} (from ObjectStore)
        </a>
        '''))
    else:
        print("File not found; run Cell 3-4 first")
