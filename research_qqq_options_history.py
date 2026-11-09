# QuantConnect Research Notebook — 按年抓取 QQQ 短期期权链（含 Greeks）+ QQQ 日线
# 用于 auto_wheel.py 的长期随机回测（2007–2026）。
#
# 使用方法：
#   1. 在 QC Research 新建 Notebook，把下面各 Cell 依次粘贴运行
#   2. 修改 Cell 1 的 YEARS（免费账户单次运行时间有限，建议一次 1–2 年）
#   3. 每年生成一个 QQQ_options_<Y>0101_<Y>1231_greeks.csv；Cell 5 的按钮下载
#      → 放到本地 data/ 目录（文件名不要改，auto_wheel.py 按此命名自动读取）
#   4. 运行一次 Cell 6 下载 QQQ_daily_20050101_20261231.csv（未复权收盘价），同样放到 data/
#
# 注意：
#   - QC 的美股期权数据有起始年份限制（大约 2010–2012 年以后，以实际抓取结果为准）；
#     更早的年份 OptionChain 会返回空，Cell 3 会打印「无数据」。
#   - 2013 年以前 QQQ 周度期权很少，大部分时候只有月度到期日；
#     auto_wheel.py 会自动退而选择 45 天内最近的到期日。
#   - 过滤条件与现有 2021–2025 文件一致：DTE 1–60、|Delta| 0.05–0.70（已核对：与 2021 年本地文件逐行一致）。
#   - QuantBook 的时钟被 SetDateTime 调到过去后，无法再请求"未来"数据：抓当年数据前先 qb.SetDateTime(datetime.now())。
#   - QC 免费账户的 QQQ 期权数据从 2012 年开始，2011 年及以前为空。

# ── Cell 1：初始化 ─────────────────────────────────────────────────────────────
from AlgorithmImports import *
import pandas as pd
import gc
import os
import base64
from datetime import datetime
from IPython.display import HTML, display

qb     = QuantBook()
TICKER = "QQQ"
YEARS  = [2012]          # ← 每次改这里，例如 [2013, 2014]

equity = qb.AddEquity(TICKER, Resolution.Daily)
equity.SetDataNormalizationMode(DataNormalizationMode.Raw)   # 期权行权价对应未复权价格

option = qb.AddOption(TICKER, Resolution.Minute)
option.SetFilter(lambda u: u
    .IncludeWeeklys()
    .Strikes(-25, 25)
    .Expiration(1, 60))

os.makedirs("Output", exist_ok=True)
print(f"订阅完成: {TICKER}，年份 {YEARS}")


# ── Cell 2：抓取函数 ───────────────────────────────────────────────────────────
def fetch_year(year: int) -> pd.DataFrame | None:
    start, end = datetime(year, 1, 1), datetime(year, 12, 31)
    hist = qb.History(equity.Symbol, start, end, Resolution.Daily)
    if hist.empty:
        print(f"{year}: 无正股历史")
        return None
    days = hist.index.get_level_values("time").normalize().unique().sort_values()
    frames = []
    for dt in days:
        try:
            qb.SetDateTime(datetime(dt.year, dt.month, dt.day, 15, 45))
            chain = qb.OptionChain(option.Symbol)
            rows = []
            for c in chain:
                dte = (c.Expiry - dt).days
                if not (1 <= dte <= 60):
                    continue
                g = c.Greeks
                if g:
                    da = abs(float(g.Delta))
                    if da < 0.05 or da > 0.70:
                        continue
                rows.append({
                    "snapshot_date": dt.strftime("%Y-%m-%d"),
                    "expiry": c.Expiry.strftime("%Y-%m-%d"),
                    "dte": dte,
                    "strike": float(c.Strike),
                    "type": "Call" if c.Right == OptionRight.Call else "Put",
                    "bid": float(c.BidPrice),
                    "ask": float(c.AskPrice),
                    "mid": round((float(c.BidPrice) + float(c.AskPrice)) / 2, 4),
                    "last": float(c.LastPrice),
                    "volume": float(c.Volume),
                    "open_interest": int(c.OpenInterest),
                    "iv": round(float(c.ImpliedVolatility), 5) if c.ImpliedVolatility else None,
                    "delta": round(float(g.Delta), 5) if g else None,
                    "gamma": round(float(g.Gamma), 5) if g else None,
                    "theta": round(float(g.Theta), 5) if g else None,
                    "vega": round(float(g.Vega), 5) if g else None,
                    "rho": round(float(g.Rho), 5) if g else None,
                })
            if rows:
                frames.append(pd.DataFrame(rows))
            else:
                print(f"[{dt:%Y-%m-%d}] 无数据")
        except Exception as e:
            print(f"[{dt:%Y-%m-%d}] ✗ {e}")
        finally:
            gc.collect()
    if not frames:
        return None
    df = pd.concat(frames, ignore_index=True)
    df.sort_values(["snapshot_date", "type", "expiry", "strike"], inplace=True)
    return df.reset_index(drop=True)


# ── Cell 3：逐年抓取并保存 ─────────────────────────────────────────────────────
saved = []
for y in YEARS:
    df = fetch_year(y)
    if df is None:
        print(f"⚠ {y}: 没有期权数据（可能早于 QC 数据起始年份）")
        continue
    key = f"{TICKER}_options_{y}0101_{y}1231_greeks.csv"
    csv = df.to_csv(index=False)
    with open(os.path.join("Output", key), "w", encoding="utf-8") as f:
        f.write(csv)
    qb.ObjectStore.Save(key, csv)
    saved.append(key)
    print(f"✅ {key}: {len(df):,} 行，{df['snapshot_date'].nunique()} 个交易日，"
          f"到期日数/日 中位 {df.groupby('snapshot_date')['expiry'].nunique().median():.0f}")


# ── Cell 4：质量检查（每日是否有 7–21 天到期的合约）────────────────────────────
for key in saved:
    d = pd.read_csv(os.path.join("Output", key))
    ok = d[(d.dte >= 7) & (d.dte <= 21)].groupby("snapshot_date").size()
    print(f"{key}: {len(ok)}/{d.snapshot_date.nunique()} 天有 7–21 DTE 合约"
          f"（其余天 auto_wheel 会退而用 ≤45 天的月度到期）")


# ── Cell 5：下载按钮 ───────────────────────────────────────────────────────────
def download_button(path: str):
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    name = os.path.basename(path)
    display(HTML(f'<a download="{name}" href="data:text/csv;base64,{b64}" '
                 f'style="display:inline-block;margin:4px;padding:8px 18px;background:#0066cc;'
                 f'color:white;border-radius:6px;text-decoration:none">⬇ {name}</a>'))

for key in saved:
    download_button(os.path.join("Output", key))


# ── Cell 6：QQQ 日线（未复权，2005 起，含预热期）只需运行一次 ───────────────────
daily = qb.History(equity.Symbol, datetime(2005, 1, 1), datetime(2026, 12, 31), Resolution.Daily)
daily = daily.reset_index()[["time", "open", "high", "low", "close", "volume"]]
daily["date"] = pd.to_datetime(daily["time"]).dt.strftime("%Y-%m-%d")
daily = daily[["date", "open", "high", "low", "close", "volume"]]
dkey = f"{TICKER}_daily_20050101_20261231.csv"
daily.to_csv(os.path.join("Output", dkey), index=False)
print(f"✅ {dkey}: {len(daily)} 行，{daily['date'].iloc[0]} → {daily['date'].iloc[-1]}")
download_button(os.path.join("Output", dkey))
