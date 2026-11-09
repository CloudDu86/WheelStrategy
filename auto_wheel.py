"""QQQ 自动车轮模拟器 —— 按你的期权交易逻辑在历史期权链上自动交易（随机期间回测）。

交易规则（与 workspace 复盘总结一致）：
  1. 先低价买入：Sell Put，DTE ≈ 14，Delta ≈ put_delta（现金担保）
  2. 持股后在高价卖出：Covered Call，Delta ≈ call_delta
     - 行权价永远 ≥ 动态成本（= (累计买股支出 - 累计权利金) / 持股数）
     - 若 Delta 目标的行权价 < 动态成本 → 改卖「≥ 动态成本的最低行权价」
     - 若该 Call 权利金 < min_premium → 视为深套，等待反弹（本期不卖）
     - 卖不出（OTM 到期）就继续收租
  3. 直接买入：价格很低（W 底低点距 52 周高点 ≥ dip_drawdown）
               + 日线 W 底突破颈线 + 低点落在周线支撑区（前期周线摆动低/高点 ±support_tol）

数据：data/<TICKER>_options_*_greeks.csv（QuantConnect 导出，可按年分文件，见 research_qqq_options_history.py）。
标的价格：有 data/<TICKER>_daily*.csv（未复权收盘价）时用它；否则用最近到期 ATM 的
Put-Call Parity 估算：S ≈ K + C - P。期权数据有缺口（某年没下载）时，随机期间不会跨越缺口。

输出：
  data/Simulator/auto_wheel/summary_<ts>.csv          每次回测的汇总
  data/Simulator/auto_wheel/auto_<T>_<start>_<end>.json  可在 Dashboard 里「加载工作区」复盘

用法：
  python auto_wheel.py                      # 随机 20 段，每段 6–24 个月
  python auto_wheel.py --runs 50 --seed 7 --min-months 12 --max-months 36
  python auto_wheel.py --years 3 --runs 30   # 每段固定 3 年，另输出第 1/2/3 年末的回撤与持仓
  python auto_wheel.py --start 2022-01-03 --end 2023-12-29   # 指定期间
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import pickle
import random
from dataclasses import dataclass, asdict, fields
from datetime import date, datetime

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data")
CACHE_DIR = os.path.join(DATA_DIR, ".cache")
OUT_DIR = os.path.join(DATA_DIR, "Simulator", "auto_wheel")
MULT = 100


# ── 参数 ──────────────────────────────────────────────────────────────────────

@dataclass
class Params:
    ticker: str = "QQQ"
    capital: float = 100_000.0
    # 期限 / 行权价选择
    dte_target: int = 14
    dte_min: int = 7
    dte_max: int = 21
    dte_fallback_max: int = 45         # 7–21 天内没有到期日（早年只有月度期权）时，退而选 ≤ 此天数的最近到期日
    put_delta: float = 0.20
    call_delta: float = 0.30
    min_premium: float = 0.15          # 每股权利金下限（Call 低于此视为深套等待；Put 低于此不开）
    # 仓位
    put_alloc: float = 0.8             # 按权益定张数：新开 Put 后 (持股成本 + Put 担保金) ≤ put_alloc × 权益
    max_put_contracts: int = 0         # 每次新开 Put 的张数上限（0 = 不限，只按 put_alloc 定）
    put_while_holding: bool = True     # 持股期间是否继续 Sell Put（会加仓）
    max_exposure: float = 1.0          # (持股成本 + Put 担保金) / 权益 的上限
    # 直接买入（W 底 + 周线支撑）
    direct_buy: bool = True
    dip_drawdown: float = 0.15         # W 底低点距 52 周高点的最小跌幅
    w_lookback: int = 60               # 识别 W 底的回看交易日
    w_swing_order: int = 5             # 摆动低点左右各需几根 K 线确认
    w_low_tol: float = 0.03            # 两个低点的最大差距
    w_min_gap: int = 10                # 两个低点最少间隔交易日
    w_neck_rise: float = 0.04          # 颈线需高出低点的幅度
    support_weeks: int = 104           # 周线支撑回看周数
    support_order: int = 2             # 周线摆动点左右确认周数
    support_tol: float = 0.03          # W 低点与周线支撑的容差
    max_direct_lots: int = 2           # 直接买入批次同时持有上限
    direct_alloc: float = 0.2          # 每次直接买入约占权益的比例（向下取整到 100 股，至少 100 股）
    direct_cooldown: int = 20          # 同一 W 形态只买一次；两次直接买入最少间隔交易日
    # 交易成本（MooMoo Japan，与 Dashboard 默认一致）
    slippage: float = 0.015
    order_fee: float = 2.07
    per_contract: float = 0.24
    commission: float = 0.0
    min_liquidity: int = 5             # volume + open_interest 下限

    def fee(self, qty: int) -> float:
        if self.order_fee == 0 and self.per_contract == 0 and self.commission == 0:
            return 0.0
        return self.order_fee + (self.per_contract + self.commission) * qty


# ── 数据 ──────────────────────────────────────────────────────────────────────

def _data_files(ticker: str, pattern: str) -> list[str]:
    """在 data/ 和 data/<TICKER>/ 两处查找数据文件（同名时子目录优先）。"""
    found = {}
    for d in (DATA_DIR, os.path.join(DATA_DIR, ticker)):
        for f in glob.glob(os.path.join(d, pattern)):
            found[os.path.basename(f)] = f
    return [found[k] for k in sorted(found)]


_COLS = ["snapshot_date", "expiry", "dte", "strike", "type", "mid",
         "volume", "open_interest", "delta"]


def load_chain(ticker: str, max_dte: int = 45) -> pd.DataFrame:
    """读取该标的所有短期期权 CSV（DTE ≤ max_dte），带 pickle 缓存。"""
    files = _data_files(ticker, f"{ticker}_options_*_greeks.csv")
    if not files:
        raise FileNotFoundError(f"找不到 data/{ticker}_options_*_greeks.csv 或 data/{ticker}/ 下的同名文件")
    key = "|".join(f"{os.path.basename(f)}:{os.path.getmtime(f):.0f}" for f in files) + f"|{max_dte}"
    cache = os.path.join(CACHE_DIR, f"{ticker}_chain_dte{max_dte}.pkl")
    if os.path.exists(cache):
        with open(cache, "rb") as fh:
            cached_key, df = pickle.load(fh)
        if cached_key == key:
            return df
    frames = []
    for f in files:
        part = pd.read_csv(f, usecols=_COLS)
        frames.append(part[part["dte"] <= max_dte])
    df = pd.concat(frames, ignore_index=True)
    df["snapshot_date"] = pd.to_datetime(df["snapshot_date"]).dt.date
    df["expiry"] = pd.to_datetime(df["expiry"]).dt.date
    df = df.drop_duplicates(["snapshot_date", "expiry", "strike", "type"]).reset_index(drop=True)
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(cache, "wb") as fh:
        pickle.dump((key, df), fh)
    return df


def estimate_spot(day: pd.DataFrame) -> float | None:
    """Put-Call Parity：最近到期日、|C-P| 最小的行权价，S ≈ K + C - P。"""
    d = day[(day["dte"] >= 1) & (day["mid"] > 0)]
    if d.empty:
        return None
    exp = d["expiry"].min()
    e = d[d["expiry"] == exp]
    c = e[e["type"] == "Call"].set_index("strike")["mid"]
    p = e[e["type"] == "Put"].set_index("strike")["mid"]
    both = c.index.intersection(p.index)
    if len(both) == 0:
        return None
    diff = (c[both] - p[both])
    k = diff.abs().idxmin()
    return float(k + diff[k])


def build_spot_series(df: pd.DataFrame) -> pd.Series:
    vals = {d: estimate_spot(g) for d, g in df.groupby("snapshot_date")}
    s = pd.Series(vals).sort_index().dropna()
    # 去掉估算异常的跳点（单日偏离前后中位数 > 8%）
    med = s.rolling(5, center=True, min_periods=1).median()
    return s[(s / med - 1).abs() < 0.08]


def load_daily(ticker: str) -> pd.Series | None:
    """可选：data/<TICKER>_daily*.csv（列 date/time + close，未复权 Raw 价格）。
    有它时用真实收盘价做信号与到期结算，并提供期权数据开始前的预热历史。"""
    files = _data_files(ticker, f"{ticker}_daily*.csv")
    if not files:
        return None
    frames = []
    for f in files:
        d = pd.read_csv(f)
        d.columns = [c.lower() for c in d.columns]
        dcol = "date" if "date" in d.columns else "time"
        frames.append(pd.DataFrame({"date": pd.to_datetime(d[dcol]).dt.date, "close": d["close"].astype(float)}))
    s = pd.concat(frames).drop_duplicates("date", keep="last").set_index("date")["close"].sort_index()
    return s


def build_spot(df: pd.DataFrame, ticker: str) -> tuple[pd.Series, str]:
    est = build_spot_series(df)
    daily = load_daily(ticker)
    if daily is None:
        return est, "期权 Put-Call Parity 估算"
    # QC 日线的 time 可能是 bar 结束时间（次日 00:00）→ 自动对齐：与 Parity 估算比对，误差小的为准
    shifted = pd.Series(daily.values, index=[d - pd.Timedelta(days=1) for d in daily.index])
    def _err(s):
        common = s.index.intersection(est.index)
        return (s[common] / est[common] - 1).abs().median() if len(common) > 20 else np.inf
    note = ""
    if _err(shifted) < _err(daily):
        daily, note = shifted, "（已把 QC bar 结束时间回调 1 天）"
    merged = daily.combine_first(est).sort_index()
    return merged, f"日线收盘价{note}（缺失日用 Parity 估算）"


def option_segments(opt_dates: list[date], max_gap_days: int = 15) -> list[tuple[int, int]]:
    """把期权交易日按缺口（> max_gap_days 天）切成连续区段，返回下标区间 [i, j]。"""
    segs, s = [], 0
    for i in range(1, len(opt_dates)):
        if (opt_dates[i] - opt_dates[i - 1]).days > max_gap_days:
            segs.append((s, i - 1))
            s = i
    segs.append((s, len(opt_dates) - 1))
    return segs


# ── 信号：日线 W 底 + 周线支撑 ─────────────────────────────────────────────────

def _swing_lows(x: np.ndarray, lo: int, hi: int, k: int) -> list[int]:
    """[lo, hi] 内已确认的摆动低点下标（左右各 k 根内最低）。"""
    out = []
    for i in range(max(lo, k), min(hi, len(x) - 1 - k) + 1):
        if x[i] == x[i - k:i + k + 1].min():
            out.append(i)
    return out


def _swing_highs(x: np.ndarray, lo: int, hi: int, k: int) -> list[int]:
    out = []
    for i in range(max(lo, k), min(hi, len(x) - 1 - k) + 1):
        if x[i] == x[i - k:i + k + 1].max():
            out.append(i)
    return out


def find_w_bottom(closes: np.ndarray, t: int, p: Params) -> dict | None:
    """在第 t 根 K 线（只用 ≤ t 的数据）检测 W 底突破。
    返回 {"low1", "low2", "i1", "i2", "neck"}，否则 None。"""
    k = p.w_swing_order
    if t < 2 * k + p.w_min_gap + 1:
        return None
    lows = _swing_lows(closes, t - p.w_lookback, t - k, k)
    # 从最近的低点对开始找
    for b in range(len(lows) - 1, 0, -1):
        i2 = lows[b]
        for a in range(b - 1, -1, -1):
            i1 = lows[a]
            if i2 - i1 < p.w_min_gap:
                continue
            c1, c2 = closes[i1], closes[i2]
            base = min(c1, c2)
            if abs(c2 - c1) / c1 > p.w_low_tol:
                continue
            neck = closes[i1:i2 + 1].max()
            if neck < base * (1 + p.w_neck_rise):
                continue
            if closes[i2:t + 1].min() < base * (1 - p.w_low_tol):
                continue  # 第二个低点之后又破位
            if closes[t] > neck and closes[t - 1] <= neck:
                return {"i1": i1, "i2": i2, "low1": float(c1), "low2": float(c2), "neck": float(neck)}
        # 只看最近的第二低点，避免旧形态
        break
    return None


def weekly_support_levels(spot: pd.Series, upto: date, p: Params) -> list[float]:
    """截至 upto 已确认的周线摆动低点/高点（支撑区 & 阻力转支撑）。"""
    s = spot[spot.index <= upto]
    if s.empty:
        return []
    idx = pd.to_datetime(pd.Index(s.index))
    wk = pd.Series(s.values, index=idx)
    w_low = wk.resample("W-FRI").min().dropna().values
    w_high = wk.resample("W-FRI").max().dropna().values
    n = len(w_low)
    lo = max(0, n - p.support_weeks)
    k = p.support_order
    levels = [float(w_low[i]) for i in _swing_lows(w_low, lo, n - 1 - k, k)]
    levels += [float(w_high[i]) for i in _swing_highs(w_high, lo, n - 1 - k, k)]
    return levels


def direct_buy_signal(spot: pd.Series, dates: list[date], t: int, p: Params) -> dict | None:
    closes = spot.loc[dates[:t + 1]].values
    w = find_w_bottom(closes, t, p)
    if w is None:
        return None
    base = min(w["low1"], w["low2"])
    high52 = closes[max(0, t - 252):t + 1].max()
    if base > high52 * (1 - p.dip_drawdown):
        return None
    # 支撑区只用 W 第一个低点之前已确认的周线点
    levels = weekly_support_levels(spot, dates[w["i1"]], p)
    near = [lv for lv in levels if abs(base / lv - 1) <= p.support_tol]
    if not near:
        return None
    w["support"] = min(near, key=lambda lv: abs(base / lv - 1))
    w["high52"] = float(high52)
    return w


# ── 合约选择 ──────────────────────────────────────────────────────────────────

def _liquid(chain: pd.DataFrame, p: Params) -> pd.DataFrame:
    liq = chain["volume"].fillna(0) + chain["open_interest"].fillna(0)
    return chain[(liq >= p.min_liquidity) & (chain["mid"] > 0) & chain["delta"].notna()]


def pick_expiry(day: pd.DataFrame, p: Params) -> date | None:
    all_e = day[["expiry", "dte"]].drop_duplicates()
    e = all_e[(all_e["dte"] >= p.dte_min) & (all_e["dte"] <= p.dte_max)]
    if e.empty:
        # 早年 QQQ 只有月度期权：取 dte_min 以上最近的一个（不超过 dte_fallback_max）
        e = all_e[(all_e["dte"] >= p.dte_min) & (all_e["dte"] <= p.dte_fallback_max)]
        if e.empty:
            return None
        return e.sort_values("dte").iloc[0]["expiry"]
    e = e.assign(gap=(e["dte"] - p.dte_target).abs()).sort_values(["gap", "dte"], ascending=[True, False])
    return e.iloc[0]["expiry"]


def pick_put(day: pd.DataFrame, spot: float, p: Params) -> pd.Series | None:
    exp = pick_expiry(day, p)
    if exp is None:
        return None
    c = _liquid(day[(day["expiry"] == exp) & (day["type"] == "Put") & (day["strike"] < spot)], p)
    if c.empty:
        return None
    row = c.loc[(c["delta"].abs() - p.put_delta).abs().idxmin()]
    if row["mid"] < p.min_premium:
        return None
    return row


def pick_call(day: pd.DataFrame, spot: float, floor: float | None, p: Params) -> tuple[pd.Series | None, str]:
    """返回 (合约, 原因)。原因 ∈ {"高价卖出", "动态成本卖出", "深套等待", "无合约"}。"""
    exp = pick_expiry(day, p)
    if exp is None:
        return None, "无合约"
    c = _liquid(day[(day["expiry"] == exp) & (day["type"] == "Call") & (day["strike"] > spot)], p)
    if c.empty:
        return None, "无合约"
    target = c.loc[(c["delta"] - p.call_delta).abs().idxmin()]
    if floor is None or target["strike"] >= floor:
        if target["mid"] < p.min_premium:
            return None, "深套等待" if floor is not None and floor > spot else "无合约"
        return target, "高价卖出"
    above = c[c["strike"] >= floor].sort_values("strike")
    if above.empty or above.iloc[0]["mid"] < p.min_premium:
        return None, "深套等待"
    return above.iloc[0], "动态成本卖出"


def occ_symbol(ticker: str, expiry: date, typ: str, strike: float) -> str:
    return f"{ticker} {expiry:%y%m%d}{'C' if typ == 'Call' else 'P'}{int(round(strike * 1000)):08d}"


# ── 账户（与 Dashboard workspace 格式兼容） ────────────────────────────────────

class Account:
    def __init__(self, p: Params):
        self.p = p
        self.t = p.ticker
        self.cash = p.capital
        self.realized = 0.0
        self.fees = 0.0
        self.gross_premium = 0.0            # 累计卖出权利金（不因换轮清零，用于统计）
        self.lots: list[dict] = []          # {"qty","price","date","src"}
        self.equity_cost = 0.0
        self.premium = 0.0
        self.history: list[dict] = []
        self.open: list[dict] = []
        self.closed: list[dict] = []

    # ── 持股
    @property
    def shares(self) -> int:
        return sum(l["qty"] for l in self.lots)

    @property
    def cost_basis(self) -> float:
        q = self.shares
        return sum(l["qty"] * l["price"] for l in self.lots) / q if q else 0.0

    def dynamic_cost(self) -> float | None:
        q = self.shares
        return (self.equity_cost - self.premium) / q if q else None

    def put_collateral(self) -> float:
        return sum(x["strike"] * MULT * x["qty"] for x in self.open if x["type"] == "Put")

    def covered_shares(self) -> int:
        return sum(x["qty"] * MULT for x in self.open if x["type"] == "Call")

    def free_cash(self) -> float:
        return self.cash - self.put_collateral()

    def exposure(self) -> float:
        return self.cost_basis * self.shares + self.put_collateral()

    def _log(self, **kw):
        kw["date"] = kw["date"].isoformat()
        self.history.append(kw)

    def _buy_shares(self, d: date, qty: int, price: float, src: str):
        self.cash -= qty * price
        self.equity_cost += qty * price
        self.lots.append({"qty": qty, "price": price, "date": d.isoformat(), "src": src})

    def _sell_shares_fifo(self, qty: int) -> float:
        remaining, cost, new = qty, 0.0, []
        for lot in self.lots:
            if remaining <= 0:
                new.append(lot)
            elif lot["qty"] <= remaining:
                cost += lot["qty"] * lot["price"]
                remaining -= lot["qty"]
            else:
                cost += remaining * lot["price"]
                new.append({**lot, "qty": lot["qty"] - remaining})
                remaining = 0
        self.lots = new
        return cost

    # ── 交易
    def sell_option(self, d: date, row: pd.Series, qty: int, reason: str):
        p = self.p
        fill = max(float(row["mid"]) - p.slippage, 0.0)
        fee = p.fee(qty)
        cash_delta = fill * MULT * qty - fee
        self.cash += cash_delta
        self.fees += fee
        self.premium += fill * MULT * qty
        self.gross_premium += fill * MULT * qty
        typ = row["type"]
        pos = {
            "ticker": self.t,
            "symbol": occ_symbol(self.t, row["expiry"], typ, float(row["strike"])),
            "strike": float(row["strike"]), "type": typ,
            "expiry": row["expiry"].isoformat(),
            "direction": -1, "dir_label": "Short", "qty": qty,
            "entry_mid": fill, "entry_date": d.isoformat(),
            "entry_commission": fee, "entry_delta": float(row["delta"]),
            "reason": reason,
        }
        if typ == "Call":
            pos["covered"] = True
        self.open.append(pos)
        self._log(date=d, action="开仓 Short", symbol=pos["symbol"], qty=qty, price=fill,
                  cash_delta=cash_delta, realized=0.0,
                  note=f"{typ} ${pos['strike']:g} 到期 {pos['expiry']} Δ{row['delta']:+.2f} [{reason}] 手续费${fee:.2f}")

    def buy_direct(self, d: date, price: float, qty: int, note: str):
        fill = price + self.p.slippage
        self._buy_shares(d, qty, fill, "direct")
        self._log(date=d, action="直接买入股票", symbol=self.t, qty=qty, price=fill,
                  cash_delta=-qty * fill, realized=0.0, note=note)

    def settle(self, d: date, spot_at_expiry: float):
        still = []
        for pos in self.open:
            if date.fromisoformat(pos["expiry"]) > d:
                still.append(pos)
                continue
            exp = date.fromisoformat(pos["expiry"])
            k, q = pos["strike"], pos["qty"]
            prem = pos["entry_mid"] * MULT * q - pos["entry_commission"]
            itm = (spot_at_expiry < k) if pos["type"] == "Put" else (spot_at_expiry > k)
            if not itm:
                self.realized += prem
                pos.update(close_date=exp.isoformat(), close_mid=0.0, realized=prem)
                self._log(date=exp, action="到期废弃 (OTM) Short", symbol=pos["symbol"], qty=q,
                          price=0.0, cash_delta=0.0, realized=prem,
                          note=f"{pos['type']} ${k:g} OTM 废弃 (到期价 ${spot_at_expiry:.2f})")
            elif pos["type"] == "Put":
                self._buy_shares(exp, q * MULT, k, "put")
                self.realized += prem
                pos.update(close_date=exp.isoformat(), close_mid=0.0, realized=prem)
                self._log(date=exp, action="Short Put 被行权 - 买入股票", symbol=self.t, qty=q * MULT,
                          price=k, cash_delta=-k * q * MULT, realized=prem,
                          note=f"买入 {q * MULT} 股 @ ${k:.2f}（到期价 ${spot_at_expiry:.2f}）")
            else:
                n = q * MULT
                fifo = self._sell_shares_fifo(n)
                self.cash += k * n
                stock_pnl = k * n - fifo
                r = prem + stock_pnl
                self.realized += r
                if self.shares <= 0:
                    self.equity_cost, self.premium = 0.0, 0.0
                else:
                    self.equity_cost = max(0.0, self.equity_cost - fifo)
                pos.update(close_date=exp.isoformat(), close_mid=0.0, realized=r)
                self._log(date=exp, action="Short Call 被行权 - 卖出股票 (备兑)", symbol=self.t, qty=n,
                          price=k, cash_delta=k * n, realized=r,
                          note=f"按 ${k:.2f} 出售 {n} 股（FIFO均价 ${fifo / n:.2f}）| 权利金 ${prem:+,.2f} + 股票差价 ${stock_pnl:+,.2f}")
            self.closed.append(pos)
        self.open = still

    def option_liability(self, day: pd.DataFrame | None, spot: float) -> float:
        tot = 0.0
        for pos in self.open:
            exp = date.fromisoformat(pos["expiry"])
            mid = None
            if day is not None and not day.empty:
                m = day[(day["expiry"] == exp) & (day["type"] == pos["type"]) & (day["strike"] == pos["strike"])]
                if not m.empty:
                    mid = float(m.iloc[0]["mid"])
            if mid is None:
                mid = max(0.0, pos["strike"] - spot) if pos["type"] == "Put" else max(0.0, spot - pos["strike"])
            tot += mid * MULT * pos["qty"]
        return tot

    def to_workspace(self, current_date_idx: int) -> dict:
        lots = [{"qty": l["qty"], "price": l["price"], "date": l["date"]} for l in self.lots]
        return {
            "version": 1,
            "active_ticker": self.t,
            "current_date_idx": current_date_idx,
            "initial_capital": self.p.capital,
            "account": {
                "initial_capital": self.p.capital,
                "cash": self.cash,
                "collateral": 0.0,
                "shares": {self.t: {"qty": self.shares, "cost_basis": self.cost_basis, "lots": lots}},
                "realized_pnl": self.realized,
                "history": self.history,
                "equity_cost": {self.t: self.equity_cost},
                "premium_collected": {self.t: self.premium},
            },
            "positions": self.open,
            "closed_positions": self.closed,
        }


# ── 回测引擎 ──────────────────────────────────────────────────────────────────

def _spot_on_or_before(spot: pd.Series, d: date) -> float:
    s = spot[spot.index <= d]
    return float(s.iloc[-1]) if not s.empty else float(spot.iloc[0])


def run_backtest(df: pd.DataFrame, spot: pd.Series, by_day: dict, start: date, end: date,
                 p: Params) -> tuple[dict, Account, pd.DataFrame]:
    all_dates = list(spot.index)
    dates = [d for d in all_dates if start <= d <= end]
    acct = Account(p)
    rows = []
    stuck_days = 0
    max_shares = 0
    direct_signals = 0
    last_direct_t = -10**9
    used_w_lows: set[int] = set()

    for d in dates:
        s = float(spot[d])
        day = by_day.get(d)
        t = all_dates.index(d)

        # 1) 到期结算（用到期日价格）
        due = {x["expiry"] for x in acct.open if date.fromisoformat(x["expiry"]) <= d}
        for e in sorted(due):
            acct.settle(date.fromisoformat(e), _spot_on_or_before(spot, date.fromisoformat(e)))

        if day is None:
            continue

        # 2) 直接买入（W 底 + 周线支撑）
        if p.direct_buy:
            sig = direct_buy_signal(spot, all_dates, t, p)
            if sig and (sig["i2"] in used_w_lows or t - last_direct_t < p.direct_cooldown):
                sig = None
            if sig:
                direct_signals += 1
                used_w_lows.add(sig["i2"])
                n_direct = sum(1 for l in acct.lots if l["src"] == "direct")
                equity = acct.cash + acct.shares * s - acct.option_liability(day, s)
                lots = max(1, int(p.direct_alloc * equity // (s * MULT)))
                lots = min(lots, int(acct.free_cash() // (s * MULT)),
                           int((p.max_exposure * equity - acct.exposure()) // (s * MULT)))
                if n_direct < p.max_direct_lots and lots >= 1:
                    acct.buy_direct(d, s, lots * MULT,
                                    f"W底突破颈线 ${sig['neck']:.2f}｜低点 ${min(sig['low1'], sig['low2']):.2f}"
                                    f" 距52周高 {1 - min(sig['low1'], sig['low2']) / sig['high52']:.0%}"
                                    f"｜周线支撑 ${sig['support']:.2f}")
                    last_direct_t = t

        # 3) Covered Call（未被备兑的整百股）
        avail = acct.shares - acct.covered_shares()
        stuck_today = False
        if avail >= MULT:
            row, why = pick_call(day, s, acct.dynamic_cost(), p)
            if row is not None:
                acct.sell_option(d, row, avail // MULT, why)
            elif why == "深套等待":
                stuck_days += 1
                stuck_today = True

        # 4) Sell Put（现金担保 + 敞口上限）
        has_put = any(x["type"] == "Put" for x in acct.open)
        if not has_put and (acct.shares == 0 or p.put_while_holding):
            row = pick_put(day, s, p)
            if row is not None:
                coll = float(row["strike"]) * MULT
                equity = acct.cash + acct.shares * s - acct.option_liability(day, s)
                room = min(p.put_alloc, p.max_exposure) * equity - acct.exposure()
                n = int(min(acct.free_cash() // coll, room // coll))
                if p.max_put_contracts > 0:
                    n = min(n, p.max_put_contracts)
                if n >= 1:
                    acct.sell_option(d, row, n, "低价买入")

        max_shares = max(max_shares, acct.shares)
        equity = acct.cash + acct.shares * s - acct.option_liability(day, s)
        rows.append({"date": d, "spot": s, "equity": equity, "shares": acct.shares, "cash": acct.cash,
                     "cost_basis": acct.cost_basis, "dyn_cost": acct.dynamic_cost(),
                     "collateral": acct.put_collateral(), "stuck": stuck_today,
                     "premium_cum": acct.gross_premium, "fees_cum": acct.fees})

    curve = pd.DataFrame(rows).set_index("date")
    eq = curve["equity"]
    bh = p.capital / curve["spot"].iloc[0] * curve["spot"]
    years = max((dates[-1] - dates[0]).days / 365.25, 1e-9)
    opens = [h for h in acct.history if h["action"] == "开仓 Short"]
    summary = {
        "start": dates[0].isoformat(),
        "end": dates[-1].isoformat(),
        "months": round(years * 12, 1),
        "spot_start": round(curve["spot"].iloc[0], 2),
        "spot_end": round(curve["spot"].iloc[-1], 2),
        "return": eq.iloc[-1] / p.capital - 1,
        "bh_return": bh.iloc[-1] / p.capital - 1,
        "cagr": (eq.iloc[-1] / p.capital) ** (1 / years) - 1,
        "bh_cagr": (bh.iloc[-1] / p.capital) ** (1 / years) - 1,
        "max_dd": (eq / eq.cummax() - 1).min(),
        "bh_max_dd": (bh / bh.cummax() - 1).min(),
        "premium_net": sum(h["cash_delta"] for h in opens),
        "fees": acct.fees,
        "n_puts": sum(1 for h in opens if "Put" in h["note"]),
        "n_calls": sum(1 for h in opens if "Call" in h["note"]),
        "put_assigned": sum(1 for h in acct.history if "Put 被行权" in h["action"]),
        "call_assigned": sum(1 for h in acct.history if "Call 被行权" in h["action"]),
        "direct_buys": sum(1 for h in acct.history if h["action"] == "直接买入股票"),
        "direct_signals": direct_signals,
        "stuck_days": stuck_days,
        "max_shares": max_shares,
        "end_shares": acct.shares,
        "end_cost_basis": round(acct.cost_basis, 2),
        "avg_invested": float((curve["shares"] * curve["spot"]).mean() / p.capital),
    }
    summary.update(year_snapshots(curve, p))
    return summary, acct, curve


YEAR_FIELDS = ["date", "ret", "bh_ret", "dd", "bh_dd", "max_dd", "spot", "shares", "cost_basis",
               "dyn_cost", "stock_pnl_pct", "collateral", "exposure", "cash", "stuck_days",
               "premium", "fees"]


def year_snapshots(curve: pd.DataFrame, p: Params, max_years: int = 10) -> dict:
    """按「开始日 + k 年」切分，给出每年末的状态：
      y{k}_ret / bh_ret   至该年末的累计收益（策略 / 买入持有）
      y{k}_dd / bh_dd     该年内的最大回撤（相对开始以来的最高点）
      y{k}_max_dd         开始至该年末的最大回撤
      y{k}_shares, cost_basis, dyn_cost, spot, stock_pnl_pct   年末持股情况
      y{k}_collateral     年末 Put 担保金；y{k}_exposure = (持股市值 + 担保金) / 权益
      y{k}_stuck_days     该年内深套等待天数；y{k}_premium / fees 该年权利金 / 手续费
    """
    if curve.empty:
        return {}
    idx = pd.to_datetime(pd.Index(curve.index))
    eq = curve["equity"].values
    bh = (p.capital / curve["spot"].iloc[0] * curve["spot"]).values
    dd = eq / np.maximum.accumulate(eq) - 1
    bh_dd = bh / np.maximum.accumulate(bh) - 1
    t0 = idx[0]
    out = {}
    prev_end = -1
    for k in range(1, max_years + 1):
        cut = t0 + pd.DateOffset(years=k)
        sel = np.where(idx <= cut)[0]
        end = int(sel[-1])
        if end <= prev_end:
            break
        rng_ = slice(prev_end + 1, end + 1)
        r = curve.iloc[end]
        start_prem = curve["premium_cum"].iloc[prev_end] if prev_end >= 0 else 0.0
        start_fee = curve["fees_cum"].iloc[prev_end] if prev_end >= 0 else 0.0
        mv = r["shares"] * r["spot"]
        out.update({
            f"y{k}_date": curve.index[end].isoformat(),
            f"y{k}_ret": eq[end] / p.capital - 1,
            f"y{k}_bh_ret": bh[end] / p.capital - 1,
            f"y{k}_dd": float(dd[rng_].min()),
            f"y{k}_bh_dd": float(bh_dd[rng_].min()),
            f"y{k}_max_dd": float(dd[:end + 1].min()),
            f"y{k}_spot": round(float(r["spot"]), 2),
            f"y{k}_shares": int(r["shares"]),
            f"y{k}_cost_basis": round(float(r["cost_basis"]), 2) if r["shares"] else None,
            f"y{k}_dyn_cost": round(float(r["dyn_cost"]), 2) if r["shares"] else None,
            f"y{k}_stock_pnl_pct": (r["spot"] / r["cost_basis"] - 1) if r["shares"] else None,
            f"y{k}_collateral": float(r["collateral"]),
            f"y{k}_exposure": (mv + r["collateral"]) / eq[end],
            f"y{k}_cash": float(r["cash"]),
            f"y{k}_stuck_days": int(curve["stuck"].iloc[rng_].sum()),
            f"y{k}_premium": float(r["premium_cum"] - start_prem),
            f"y{k}_fees": float(r["fees_cum"] - start_fee),
        })
        prev_end = end
        if end == len(curve) - 1 and cut >= idx[-1]:
            break
    return out


def random_periods(dates: list[date], n: int, min_m: int, max_m: int, rng: random.Random,
                   warmup: int = 126, years: int | None = None,
                   segments: list[tuple[int, int]] | None = None) -> list[tuple[date, date]]:
    """随机抽样期间。years 给定时每段固定 years 年；否则长度在 [min_m, max_m] 月之间。
    segments：期权数据的连续区段（下标），期间不会跨越数据缺口。"""
    segments = segments or [(0, len(dates) - 1)]
    # 候选起点：区段内、预热后、且能装下最短长度
    cands = []
    for a, b in segments:
        for i in range(max(a, warmup if a == 0 else a), b + 1):
            d0 = dates[i]
            need_end = (pd.Timestamp(d0) + (pd.DateOffset(years=years) if years
                                            else pd.DateOffset(months=min_m))).date()
            if need_end <= dates[b]:
                cands.append((i, b))
    if not cands:
        raise ValueError("数据太短（或缺口太多），无法按该长度抽样")
    out = []
    for _ in range(n):
        i, b = rng.choice(cands)
        d0 = dates[i]
        if years:
            target = (pd.Timestamp(d0) + pd.DateOffset(years=years)).date()
        else:
            m = rng.randint(min_m, max_m)
            target = min((pd.Timestamp(d0) + pd.DateOffset(months=m)).date(), dates[b])
        j = max(k for k in range(i, b + 1) if dates[k] <= target)
        out.append((d0, dates[j]))
    return out


# ── CLI ───────────────────────────────────────────────────────────────────────

def _pct(v):
    return "—" if v is None or (isinstance(v, float) and np.isnan(v)) else f"{v * 100:+.1f}%"


def _fmt_table(res: pd.DataFrame) -> str:
    show = res[["start", "end", "months", "spot_start", "spot_end", "return", "bh_return",
                "max_dd", "bh_max_dd", "n_puts", "n_calls", "put_assigned", "call_assigned",
                "direct_buys", "stuck_days", "end_shares"]].copy()
    for c in ["return", "bh_return", "max_dd", "bh_max_dd"]:
        show[c] = show[c].map(_pct)
    show.columns = ["开始", "结束", "月", "起价", "终价", "策略收益", "持有收益", "策略回撤", "持有回撤",
                    "Put", "Call", "Put行权", "Call行权", "直买", "深套天", "期末股数"]
    return show.to_string(index=False)


def _fmt_year_table(res: pd.DataFrame, k: int) -> str | None:
    col = f"y{k}_ret"
    if col not in res.columns:
        return None
    r = res[res[col].notna()]
    if r.empty:
        return None
    g = lambda name: r[f"y{k}_{name}"]
    show = pd.DataFrame({
        "开始": r["start"],
        "年末日": g("date"),
        "累计收益": g("ret").map(_pct),
        "持有累计": g("bh_ret").map(_pct),
        "当年回撤": g("dd").map(_pct),
        "持有当年回撤": g("bh_dd").map(_pct),
        "至今最大回撤": g("max_dd").map(_pct),
        "现价": g("spot"),
        "股数": g("shares"),
        "持股成本": g("cost_basis").map(lambda v: "—" if v is None or pd.isna(v) else f"{v:.2f}"),
        "动态成本": g("dyn_cost").map(lambda v: "—" if v is None or pd.isna(v) else f"{v:.2f}"),
        "股票浮盈": g("stock_pnl_pct").map(_pct),
        "Put担保": g("collateral").map(lambda v: f"{v:,.0f}"),
        "敞口": g("exposure").map(lambda v: f"{v:.0%}"),
        "深套天": g("stuck_days"),
        "当年权利金": g("premium").map(lambda v: f"{v:,.0f}"),
    })
    return show.to_string(index=False)


def _year_aggregate(res: pd.DataFrame, max_k: int) -> str:
    lines = []
    for k in range(1, max_k + 1):
        col = f"y{k}_ret"
        if col not in res.columns:
            break
        r = res[res[col].notna()]
        if r.empty:
            break
        holding = (r[f"y{k}_shares"] > 0).mean()
        underwater = (r[f"y{k}_stock_pnl_pct"].fillna(0) < 0).mean()
        lines.append(
            f"第{k}年末（{len(r)}段）｜累计收益 中位 {_pct(r[col].median())}（持有 {_pct(r[f'y{k}_bh_ret'].median())}）"
            f"｜当年回撤 中位 {_pct(r[f'y{k}_dd'].median())} 最差 {_pct(r[f'y{k}_dd'].min())}"
            f"（持有 最差 {_pct(r[f'y{k}_bh_dd'].min())}）"
            f"｜持股中 {holding:.0%}｜股票浮亏 {underwater:.0%}"
            f"｜敞口 中位 {r[f'y{k}_exposure'].median():.0%}｜深套天 中位 {r[f'y{k}_stuck_days'].median():.0f}")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="QQQ 自动车轮模拟（随机期间）")
    ap.add_argument("--runs", type=int, default=20)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--years", type=int, default=None, help="每段固定 N 年（如 3）；不给则按 --min/--max-months 随机长度")
    ap.add_argument("--min-months", type=int, default=6)
    ap.add_argument("--max-months", type=int, default=24)
    ap.add_argument("--start", type=str, default=None)
    ap.add_argument("--end", type=str, default=None)
    ap.add_argument("--no-save", action="store_true", help="不保存 workspace JSON")
    for f in fields(Params):
        flag = "--" + f.name.replace("_", "-")
        if f.type in ("bool", bool):
            ap.add_argument(flag, type=lambda v: v.lower() in ("1", "true", "yes", "y"), default=None)
        else:
            caster = {"int": int, "float": float, "str": str}.get(str(f.type), str)
            ap.add_argument(flag, type=caster, default=None)
    a = ap.parse_args(argv)

    p = Params(**{f.name: getattr(a, f.name) for f in fields(Params) if getattr(a, f.name) is not None})
    print(f"加载 {p.ticker} 期权链 …", flush=True)
    df = load_chain(p.ticker, max_dte=max(50, p.dte_fallback_max + 5))
    by_day = dict(tuple(df.groupby("snapshot_date")))
    spot, spot_src = build_spot(df, p.ticker)
    opt_dates = sorted(by_day)
    segs = option_segments(opt_dates)
    print(f"期权数据 {opt_dates[0]} → {opt_dates[-1]}，{len(opt_dates)} 个交易日；标的价格：{spot_src}", flush=True)
    if len(segs) > 1:
        gaps = "、".join(f"{opt_dates[b]}→{opt_dates[c]}" for (_, b), (c, _) in zip(segs, segs[1:]))
        print(f"⚠ 期权数据有缺口：{gaps}（随机期间不会跨越缺口）")

    if a.start or a.end:
        start = date.fromisoformat(a.start) if a.start else opt_dates[0]
        end = date.fromisoformat(a.end) if a.end else opt_dates[-1]
        periods = [(start, end)]
    else:
        seed = a.seed if a.seed is not None else random.randrange(10**6)
        print(f"随机种子 {seed}（用 --seed {seed} 复现）")
        pre = int((spot.index < opt_dates[0]).sum())          # 期权数据之前已有的价格历史
        warmup = max(0, 126 - pre)
        periods = random_periods(opt_dates, a.runs, a.min_months, a.max_months, random.Random(seed),
                                 warmup=warmup, years=a.years, segments=segs)

    os.makedirs(OUT_DIR, exist_ok=True)
    results = []
    for start, end in sorted(periods):
        summ, acct, _curve = run_backtest(df, spot, by_day, start, end, p)
        if not a.no_save:
            path = os.path.join(OUT_DIR, f"auto_{p.ticker}_{summ['start']}_{summ['end']}.json")
            end_d = date.fromisoformat(summ["end"])
            idx = max(i for i, d in enumerate(opt_dates) if d <= end_d)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(acct.to_workspace(idx), fh, ensure_ascii=False, indent=2)
            summ["workspace"] = os.path.relpath(path, ROOT)
        results.append(summ)

    res = pd.DataFrame(results)
    print()
    print(_fmt_table(res))
    max_k = a.years or 0
    if max_k:
        for k in range(1, max_k + 1):
            t = _fmt_year_table(res, k)
            if t:
                print(f"\n── 第 {k} 年末 ──")
                print(t)
        print()
        print(_year_aggregate(res, max_k))
    beat = (res["return"] > res["bh_return"]).mean()
    print(f"\n共 {len(res)} 段｜策略收益 中位 {res['return'].median():+.1%}（持有 {res['bh_return'].median():+.1%}）"
          f"｜跑赢持有 {beat:.0%}｜最差回撤 {res['max_dd'].min():+.1%}（持有 {res['bh_max_dd'].min():+.1%}）"
          f"｜平均股票仓位 {res['avg_invested'].mean():.0%}")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = os.path.join(OUT_DIR, f"summary_{ts}.csv")
    res.assign(**{k: v for k, v in asdict(p).items()}).to_csv(out, index=False, encoding="utf-8-sig")
    print(f"汇总已保存：{os.path.relpath(out, ROOT)}")
    if not a.no_save:
        print(f"每段的 workspace JSON 在 {os.path.relpath(OUT_DIR, ROOT)}/，可在 Dashboard「加载工作区」复盘")
    return res


if __name__ == "__main__":
    main()
