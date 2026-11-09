import streamlit as st
import pandas as pd
import json
import io
import glob
import os
import re
from datetime import date, datetime

import trade_chart

st.set_page_config(page_title="期权复盘工具", layout="wide")

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
SIM_DIR = os.path.join(DATA_DIR, "Simulator")

def _scan_data_files() -> dict[str, list[tuple[str, str]]]:
    """扫描 data/ 目录，返回 {ticker: [(label, path), ...]} 按文件名排序。
    接受两种命名：
      TICKER_options_<period>_greeks.csv   (短期 CC/CSP 数据)
      TICKER_LEAPS_<period>_greeks.csv     (深 ITM 长期 LEAPS 数据)
    """
    files = glob.glob(os.path.join(DATA_DIR, "*_greeks.csv"))
    result: dict[str, list[tuple[str, str]]] = {}
    for path in sorted(files):
        fname = os.path.basename(path)
        m = re.match(r"^([A-Z]+)_(options|LEAPS)_(.+)_greeks\.csv$", fname)
        if not m:
            continue
        ticker, category, period = m.group(1), m.group(2), m.group(3)
        tag = "LEAPS" if category == "LEAPS" else "options"
        label = f"[{tag}] " + period.replace("_", " → ")
        result.setdefault(ticker, []).append((label, path))
    return result

TICKER_DATA = _scan_data_files()  # {ticker: [(label, path), ...]}

CONTRACT_MULT = 100  # 标准期权乘数

# ── 交易成本（MooMoo 证券 日本 · 实盘拟真）默认值 ─────────────────────────────
# 依据 2026-06-02/03 实际成交报告 + 官网费率表 (moomoo.com/jp/support/topic7_374)
# ★ round-turn 方式：手续费仅在【新规开仓】一次性收取，【返済平仓】免手续费！
# 新规成本 = 每单固定费 + 每张变动费（非纯线性）：
#   每单固定 ≈ システム利用料 1.88 + 消费税 0.18 = $2.07（活动期 ≤7 张/单，按单收）
#   每张变动 ≈ 決済 0.18 + 清算 0.025 + 规制 0.013 + CAT + 卖出 SEC/TAF ≈ $0.24
#   佣金 = $0（活动期 ≤7 张）；活动结束后定额 0.30 税込 0.33/张
# 实测：新规 1 张卖出 = 2.07 + 0.24 = $2.31 ≈ 报告 $2.30 ✓；返済 1 张 = $0 ✓
# → 1 张完整往返手续费 ≈ $2.30（不是 $4.60！平仓免费）；滑点开平各算
DEFAULT_SLIPPAGE     = 0.015   # 每股滑点（美元）。买入往上、卖出往下，模拟跨越买卖价差
DEFAULT_ORDER_FEE    = 2.07    # 每单（每笔开/平）固定费：系统利用料含税
DEFAULT_PER_CONTRACT = 0.24    # 每张变动费：美国本地结算/清算/规制等
DEFAULT_COMMISSION   = 0.00    # 每张佣金：活动期免费（活动后改 0.33）

def _txn_costs() -> tuple[float, float, float, float]:
    """读取滑点 / 每单固定费 / 每张变动费 / 每张佣金（来自侧边栏）。
    开关关闭时返回 (0, 0, 0, 0)。"""
    if not st.session_state.get("_txn_enabled", True):
        return 0.0, 0.0, 0.0, 0.0
    slip      = st.session_state.get("_txn_slippage", DEFAULT_SLIPPAGE)
    order_fee = st.session_state.get("_txn_order_fee", DEFAULT_ORDER_FEE)
    per_ct    = st.session_state.get("_txn_per_contract", DEFAULT_PER_CONTRACT)
    comm      = st.session_state.get("_txn_commission", DEFAULT_COMMISSION)
    return float(slip), float(order_fee), float(per_ct), float(comm)

def _fee_for(qty: int) -> tuple[float, float]:
    """返回 (slippage_per_share, total_fee) ；total_fee = 每单固定 + (每张变动+佣金)×张数。"""
    slip, order_fee, per_ct, comm = _txn_costs()
    if order_fee == 0 and per_ct == 0 and comm == 0:
        return slip, 0.0
    return slip, order_fee + (per_ct + comm) * qty


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


def get_mid_smart(df: pd.DataFrame, pos: dict, d: date, spot: float | None) -> tuple[float | None, bool]:
    """优先取真实 mid；若数据缺失（如 LEAPS 跌出 DTE 365 过滤范围），
    用 intrinsic value 估算当前价（只对 Long 仓位安全）。
    返回 (value, is_estimated)。"""
    real_mid = get_mid(df, pos["symbol"], d)
    if real_mid is not None:
        return real_mid, False
    # 已到期：直接给 0（auto_expire 通常会先处理掉，但兜底）
    if pos["expiry"] <= d:
        return 0.0, True
    # Long: intrinsic = 内在价值（安全的下界，深 ITM 误差小）
    # Short: intrinsic 略低估买回成本（漏算时间价值），仅在确实无报价时兜底，
    #        用 ⋯ 标记提示用户估算不精确。深 OTM Short (TQQQ 拆股遗留 strike 等)
    #        intrinsic = 0，让你能至少看到 PnL 和平仓。
    if spot is not None:
        iv = intrinsic_value(pos["type"], pos["strike"], spot)
        return iv, True
    return None, False


def get_underlying_at(df: pd.DataFrame, d: date) -> float | None:
    return estimate_underlying(df[df["snapshot_date"] == d])


LIQUIDITY_MIN = 5  # volume + open_interest 的最小阈值

def filter_liquid(chain: pd.DataFrame) -> pd.DataFrame:
    """过滤无流动性合约（volume + open_interest < LIQUIDITY_MIN），避免 ghost quotes 污染选择。
    无成交、OI 极低的远 OTM 合约在 QC 数据里 bid=ask=理论值，IV/Delta 严重失真。
    阈值 5 = 至少有几手持仓或成交才算真实可交易合约。"""
    if chain.empty:
        return chain
    liq = chain["volume"].fillna(0) + chain["open_interest"].fillna(0)
    return chain[liq >= LIQUIDITY_MIN]


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
    slip, commission = _fee_for(pos["qty"])
    # 滑点：Long 买入往上成交（付得多），Short 卖出往下成交（收得少）
    fill = entry_mid + pos["direction"] * slip
    fill = max(fill, 0.0)
    pos["entry_mid"] = fill                       # 以真实成交价为入场基准（下游 PnL 一致）
    pos["entry_commission"] = commission          # 记录开仓费用，平仓时计入 realized
    cash_delta = -pos["direction"] * fill * CONTRACT_MULT * pos["qty"] - commission
    account["cash"] += cash_delta
    # Short 开仓：累计记录该标的的权利金收入（按真实成交价）
    if pos["direction"] == -1:
        ticker = pos.get("ticker", "")
        if ticker:
            pc = account.setdefault("premium_collected", {})
            pc[ticker] = pc.get(ticker, 0.0) + fill * CONTRACT_MULT * pos["qty"]
    record_trade(account, {
        "date": entry_date,
        "action": f"开仓 {pos['dir_label']}",
        "symbol": pos["symbol"],
        "qty": pos["qty"],
        "price": fill,
        "cash_delta": cash_delta,
        "realized": 0.0,
        "note": f"{pos['type']} ${pos['strike']:.0f} 到期 {pos['expiry']} "
                f"[新规·滑点${slip:.3f} 手续费${commission:.2f}]",
    })


def close_position(account: dict, pos: dict, close_date: date, close_mid: float,
                   action_label: str, charge_costs: bool = True) -> None:
    """平仓（返済）。MooMoo 实测：返済不收手续费（手续费在新规开仓时一次性收取），
    但滑点是市场成本仍然适用。charge_costs=False 用于到期结算（连滑点也不收）。"""
    if charge_costs:
        # 返済手续费 = 0（已在 entry_commission 一次性计入），仅扣滑点
        slip, _of, _pc, _cm = _txn_costs()
        # 滑点：平 Long 是卖出（往下），平 Short 是买回（往上）
        fill = close_mid - pos["direction"] * slip
        fill = max(fill, 0.0)
    else:
        slip = 0.0
        fill = close_mid
    commission = 0.0  # 返済无手续费
    entry_comm = pos.get("entry_commission", 0.0)
    cash_delta = pos["direction"] * fill * CONTRACT_MULT * pos["qty"]
    # realized 计入开仓时一次性收取的手续费（返済不再收），反映全周期净现金影响
    realized = (pos["direction"] * (fill - pos["entry_mid"]) * CONTRACT_MULT * pos["qty"]
                - entry_comm)
    account["cash"] += cash_delta
    account["realized_pnl"] += realized
    pos["close_date"] = close_date
    pos["close_mid"] = fill
    pos["realized"] = realized
    record_trade(account, {
        "date": close_date,
        "action": f"{action_label} {pos['dir_label']}",
        "symbol": pos["symbol"],
        "qty": pos["qty"],
        "price": fill,
        "cash_delta": cash_delta,
        "realized": realized,
        "note": f"{pos['type']} ${pos['strike']:.0f}"
                + (f" [返済·免手续费 滑点${slip:.3f}]" if charge_costs else " [到期结算·免费用]"),
    })


def partial_close_position(account: dict, positions: list, idx: int, close_date: date,
                           close_mid: float, qty_to_close: int, action_label: str) -> dict:
    """部分平仓：把第 idx 个持仓拆成「已平 qty_to_close 张」+「剩余张数」。
    开仓手续费(entry_commission)按张数比例分摊。返回已平的那一块（调用方负责
    append 到 closed_positions）。qty_to_close ≥ 总张数时退化为整仓平。"""
    pos = positions[idx]
    total_qty = pos["qty"]
    qty_to_close = max(1, min(int(qty_to_close), total_qty))
    full_entry_comm = pos.get("entry_commission", 0.0)

    if qty_to_close == total_qty:
        close_position(account, pos, close_date, close_mid, action_label)
        positions.pop(idx)
        return pos

    # 拆出被平的那块（按比例分摊开仓手续费）
    chunk = dict(pos)
    chunk["qty"] = qty_to_close
    chunk["entry_commission"] = full_entry_comm * qty_to_close / total_qty
    close_position(account, chunk, close_date, close_mid, f"{action_label}(部分)")

    # 更新剩余仓位
    pos["qty"] = total_qty - qty_to_close
    pos["entry_commission"] = full_entry_comm * (total_qty - qty_to_close) / total_qty
    return chunk


def _add_lot(account: dict, ticker: str, qty: int, price: float, lot_date: date) -> None:
    """追加一个买入批次到持股记录。"""
    sh = account["shares"].setdefault(ticker, {"qty": 0, "cost_basis": price, "lots": []})
    sh.setdefault("lots", []).append({"qty": qty, "price": price, "date": str(lot_date)})


def _sell_lots_fifo(account: dict, ticker: str, shares_qty: int) -> float:
    """FIFO 卖出：消耗最早批次，返回实际成本总额，并更新 lots 列表和 cost_basis。"""
    sh = account["shares"][ticker]
    lots = sh.get("lots", [])
    if not lots:
        return sh["cost_basis"] * shares_qty

    remaining = shares_qty
    fifo_cost = 0.0
    new_lots = []
    for lot in lots:
        if remaining <= 0:
            new_lots.append(lot)
        elif lot["qty"] <= remaining:
            fifo_cost += lot["qty"] * lot["price"]
            remaining -= lot["qty"]
        else:
            fifo_cost += remaining * lot["price"]
            new_lots.append({"qty": lot["qty"] - remaining, "price": lot["price"], "date": lot["date"]})
            remaining = 0

    sh["lots"] = new_lots
    total_qty = sum(l["qty"] for l in new_lots)
    if total_qty > 0:
        sh["cost_basis"] = sum(l["qty"] * l["price"] for l in new_lots) / total_qty
    return fifo_cost


def rewind_to_date(initial_capital: float, full_history: list, all_positions: list,
                    all_closed_positions: list, target_date: date) -> tuple:
    """⏰ Time Machine：从 initial_capital 起，重放 history 至 target_date，
    返回 (account, open_positions, closed_positions) 三元组。
    - close_date > target_date 的 closed_position 会"复活"为 target 时点的 open 仓
    - shares / lots / equity_cost / premium_collected 全部按动作精确重建"""

    def _to_d(x):
        if isinstance(x, date):
            return x
        return date.fromisoformat(str(x))

    acc = {
        "initial_capital": initial_capital,
        "cash": initial_capital,
        "collateral": 0.0,
        "shares": {},
        "realized_pnl": 0.0,
        "history": [],
        "equity_cost": {},
        "premium_collected": {},
    }

    # ── 按时间顺序重放 history ──
    for h in sorted(full_history, key=lambda x: _to_d(x.get("date"))):
        h_d = _to_d(h["date"])
        if h_d > target_date:
            break
        acc["cash"] += h.get("cash_delta", 0.0) or 0.0
        acc["realized_pnl"] += h.get("realized", 0.0) or 0.0
        acc["history"].append(h)

        action = h.get("action", "") or ""
        sym    = h.get("symbol", "") or ""
        qty    = h.get("qty", 0) or 0
        price  = h.get("price", 0.0) or 0.0
        cash_d = h.get("cash_delta", 0.0) or 0.0

        if action == "直接买入股票":
            sh = acc["shares"].setdefault(sym, {"qty": 0, "cost_basis": price, "lots": []})
            new_q = sh["qty"] + qty
            sh["cost_basis"] = (sh["qty"] * sh["cost_basis"] + qty * price) / new_q if new_q else price
            sh["qty"] = new_q
            sh["lots"].append({"qty": qty, "price": price, "date": str(h_d)})
            acc["equity_cost"][sym] = acc["equity_cost"].get(sym, 0.0) + price * qty

        elif "被行权 - 买入股票" in action:
            sh = acc["shares"].setdefault(sym, {"qty": 0, "cost_basis": price, "lots": []})
            new_q = sh["qty"] + qty
            sh["cost_basis"] = (sh["qty"] * sh["cost_basis"] + qty * price) / new_q if new_q else price
            sh["qty"] = new_q
            sh["lots"].append({"qty": qty, "price": price, "date": str(h_d)})
            acc["equity_cost"][sym] = acc["equity_cost"].get(sym, 0.0) + price * qty

        elif "被行权 - 卖出股票" in action:
            sh = acc["shares"].get(sym)
            if sh and sh["qty"] >= qty:
                fifo_cost = _sell_lots_fifo(acc, sym, qty)
                sh["qty"] -= qty
                if sh["qty"] <= 0:
                    acc["equity_cost"][sym] = 0.0
                    acc["premium_collected"][sym] = 0.0
                else:
                    acc["equity_cost"][sym] = max(0.0, acc["equity_cost"].get(sym, 0.0) - fifo_cost)

        elif ("开仓" in action and "Short" in action) or ("虚拟建仓" in action and "Short" in action):
            # Short 开仓 → 累计权利金（按底层股票归集）
            if cash_d > 0:
                underlying = sym.split(" ")[0] if " " in sym else ""
                if underlying:
                    acc["premium_collected"][underlying] = \
                        acc["premium_collected"].get(underlying, 0.0) + cash_d

    # ── 重建仓位 ──
    open_pos, closed_pos = [], []

    for p in all_positions:
        ed = _to_d(p.get("entry_date"))
        if ed <= target_date:
            open_pos.append(p)

    for p in all_closed_positions:
        ed = _to_d(p.get("entry_date"))
        if ed > target_date:
            continue  # 还没建仓
        cd_raw = p.get("close_date")
        cd = _to_d(cd_raw) if cd_raw else None
        if cd is None or cd > target_date:
            # target 时此仓未平 → 复活为 open
            revived = {k: v for k, v in p.items() if k not in ("close_date", "close_mid", "realized")}
            open_pos.append(revived)
        else:
            closed_pos.append(p)

    return acc, open_pos, closed_pos


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
        account["shares"][ticker] = {"qty": 0, "cost_basis": pos["strike"], "lots": []}
    old_qty = account["shares"][ticker]["qty"]
    old_basis = account["shares"][ticker]["cost_basis"]
    new_qty = old_qty + shares_qty
    account["shares"][ticker]["cost_basis"] = (
        (old_qty * old_basis + shares_qty * pos["strike"]) / new_qty
    )
    account["shares"][ticker]["qty"] = new_qty
    _add_lot(account, ticker, shares_qty, pos["strike"], assign_date)

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
        # FIFO：消耗最早批次，返回该批实际成本，同时更新 lots 和 cost_basis
        fifo_cost = _sell_lots_fifo(account, ticker, shares_qty)
        account["shares"][ticker]["qty"] -= shares_qty
        account["cash"] += proceeds
        stock_pnl = proceeds - fifo_cost
        realized = premium + stock_pnl
        cash_delta = proceeds
        action = "Short Call 被行权 - 卖出股票 (备兑)"
        avg_fifo = fifo_cost / shares_qty
        note = (f"按 ${pos['strike']:.2f} 出售 {shares_qty} 股（FIFO均价 ${avg_fifo:.2f}）| "
                f"权利金 ${premium:+,.2f} + 股票差价 ${stock_pnl:+,.2f}")
        # equity_cost 按 FIFO 实际成本缩减
        ec = account.setdefault("equity_cost", {})
        if account["shares"][ticker]["qty"] <= 0:
            # 持股归零：重置本轮成本追踪
            ec[ticker] = 0.0
            account.setdefault("premium_collected", {})[ticker] = 0.0
        else:
            ec[ticker] = max(0.0, ec.get(ticker, 0.0) - fifo_cost)
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
        sh[ticker] = {"qty": 0, "cost_basis": price, "lots": []}
    old_qty   = sh[ticker]["qty"]
    old_basis = sh[ticker]["cost_basis"]
    new_qty   = old_qty + qty
    sh[ticker]["cost_basis"] = (old_qty * old_basis + qty * price) / new_qty
    sh[ticker]["qty"] = new_qty
    _add_lot(account, ticker, qty, price, buy_date)

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
    # 向后兼容：旧 workspace 的 shares 无 lots 字段，合成单一批次
    for sh in acc.get("shares", {}).values():
        if "lots" not in sh and sh.get("qty", 0) > 0:
            sh["lots"] = [{"qty": sh["qty"], "price": sh["cost_basis"], "date": "imported"}]
        elif "lots" not in sh:
            sh["lots"] = []
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
                           action_label=f"到期结算 ({'ITM' if is_itm else 'OTM'})",
                           charge_costs=False)
        closed_positions.append(pos)
    return still_open


def compute_unrealized(df: pd.DataFrame, open_positions: list, cur_date: date) -> float:
    total = 0.0
    # 估算当前 spot，供 Long 仓位 intrinsic fallback 用
    day = df[df["snapshot_date"] == cur_date]
    spot = estimate_underlying(day) if not day.empty else None
    for pos in open_positions:
        mid, _ = get_mid_smart(df, pos, cur_date, spot)
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
if "active_data_files" not in st.session_state:
    st.session_state.active_data_files = []

# ── Sidebar ────────────────────────────────────────────────────────────────────
PAGE_SIM, PAGE_REPLAY = "🎮 模拟实盘", "📊 复盘交易图"

with st.sidebar:
    page = st.segmented_control(
        "页面", [PAGE_SIM, PAGE_REPLAY], default=PAGE_SIM, key="_page",
        label_visibility="collapsed",
    ) or PAGE_SIM   # 再次点击已选中的选项会变成 None → 回到默认页
    is_replay = page == PAGE_REPLAY

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
        st.session_state.active_data_files = []  # 重置文件选择

    # ── 数据文件选择（多选）────────────────────────────────────────────────────
    file_options = TICKER_DATA.get(selected_ticker, [])
    if not file_options:
        st.error(f"data/ 目录下找不到 {selected_ticker} 的数据文件")
        st.stop()

    file_labels = [label for label, _ in file_options]
    file_paths  = [path  for _, path  in file_options]
    label_of    = dict(zip(file_paths, file_labels))

    # 默认选全部文件
    if not st.session_state.active_data_files or \
       not any(p in file_paths for p in st.session_state.active_data_files):
        st.session_state.active_data_files = list(file_paths)

    selected_files = st.multiselect(
        "数据文件（可多选跨年）",
        file_paths,
        default=[p for p in st.session_state.active_data_files if p in file_paths],
        format_func=lambda p: label_of.get(p, p),
    )
    if not selected_files:
        st.warning("请至少选择一个数据文件")
        st.stop()

    files_changed = set(selected_files) != set(st.session_state.active_data_files)
    if files_changed:
        st.session_state.active_data_files = selected_files
        st.session_state.current_date_idx = None  # 重置日期，不清空账户

    # 合并多个文件，去重排序
    try:
        frames = [load_data(p, selected_ticker) for p in sorted(selected_files)]
        df = pd.concat(frames, ignore_index=True).drop_duplicates(
            subset=["snapshot_date", "option_symbol"]
        ).sort_values(["snapshot_date", "type", "expiry", "strike"]).reset_index(drop=True)
    except FileNotFoundError as e:
        st.error(f"找不到数据文件：{e}")
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

    # ── 交易成本（MooMoo 日本 · 实盘拟真）──────────────────────────────────────
    with st.expander("💱 交易成本（MooMoo 实盘拟真）", expanded=False):
        st.caption(
            "依实际成交报告校准。★手续费仅在【新规开仓】一次性收取，【返済平仓】免手续费。\n"
            "下方费用 = 每单固定 + 每张变动，仅用于新规。滑点开/平均扣（市场成本）。"
        )
        st.checkbox(
            "启用交易成本",
            value=st.session_state.get("_txn_enabled", True),
            key="_txn_enabled",
            help="关闭则按裸 mid 价成交（理论值）",
        )
        if st.session_state.get("_txn_enabled", True):
            st.number_input(
                "滑点 / 每股 ($)",
                min_value=0.0, max_value=0.50, step=0.005, format="%.3f",
                value=st.session_state.get("_txn_slippage", DEFAULT_SLIPPAGE),
                key="_txn_slippage",
                help="买入往上、卖出往下各偏移此值（≈跨越买卖价差）。报告无法反推，需自行估，建议 0.01~0.02。",
            )
            st.number_input(
                "每单固定费 ($/单)",
                min_value=0.0, max_value=20.0, step=0.1, format="%.2f",
                value=st.session_state.get("_txn_order_fee", DEFAULT_ORDER_FEE),
                key="_txn_order_fee",
                help="システム利用料(含税)。活动期 ≤7 张/单 = $2.07，按单收（不按张）。",
            )
            st.number_input(
                "每张变动费 ($/张)",
                min_value=0.0, max_value=5.0, step=0.01, format="%.2f",
                value=st.session_state.get("_txn_per_contract", DEFAULT_PER_CONTRACT),
                key="_txn_per_contract",
                help="决済0.18+清算0.025+规制0.013+CAT+卖出SEC/TAF ≈ $0.24/张（美国本地费）。",
            )
            st.number_input(
                "佣金 ($/张)",
                min_value=0.0, max_value=5.0, step=0.01, format="%.2f",
                value=st.session_state.get("_txn_commission", DEFAULT_COMMISSION),
                key="_txn_commission",
                help="取引手数料。活动期 ≤7 张免费 = $0；活动后定额 $0.33(税込)/张。",
            )
        slip_now, order_now, perct_now, comm_now = _txn_costs()
        fee_1 = (order_now + (perct_now + comm_now) * 1) if (order_now or perct_now or comm_now) else 0.0
        st.caption(
            f"新规 1 张手续费 ≈ ${fee_1:.2f}（实测 $2.30）· 返済免手续费 · "
            f"滑点 ${slip_now:.3f}/股（开平各扣）· "
            f"1 张往返手续费 ≈ ${fee_1:.2f}"
        )

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
    default_fname = f"workspace_{st.session_state.active_ticker}_{date.today().isoformat()}.json"

    # ── 浏览器下载（原有方式，下载到浏览器默认目录）──────────────────────────
    st.download_button(
        "💾 下载进度（浏览器）",
        data=ws_json,
        file_name=default_fname,
        mime="application/json",
        use_container_width=True,
        help="将当前账户、持仓、历史记录下载为 JSON 文件（存到浏览器下载目录）",
    )

    # ── 直接写盘到 data/Simulator/ ────────────────────────────────────────────
    src_name = st.session_state.get("_loaded_ws_name")
    src_path = os.path.join(SIM_DIR, src_name) if src_name else None
    src_exists = bool(src_path) and os.path.exists(src_path)

    save_mode = st.radio(
        "写盘方式",
        options=["保存为新文件", "覆盖源文件"],
        horizontal=True,
        help="直接写入 data/Simulator/ 目录。覆盖源文件仅在加载过本地工作区后可用。",
    )

    if save_mode == "保存为新文件":
        new_name = st.text_input(
            "文件名",
            value=default_fname,
            help="保存到 data/Simulator/ 目录",
        )
        if st.button("📁 保存到 data/Simulator/", use_container_width=True):
            if not new_name.strip():
                st.error("请输入文件名")
            else:
                fn = new_name.strip()
                if not fn.endswith(".json"):
                    fn += ".json"
                dest = os.path.join(SIM_DIR, fn)
                try:
                    os.makedirs(SIM_DIR, exist_ok=True)
                    with open(dest, "w", encoding="utf-8") as f:
                        f.write(ws_json)
                    st.session_state._loaded_ws_name = fn  # 之后可直接覆盖此文件
                    st.success(f"已保存：data/Simulator/{fn}")
                except Exception as e:
                    st.error(f"保存失败：{e}")
    else:  # 覆盖源文件
        if not src_exists:
            st.caption(
                "⚠️ 无可覆盖的源文件。请先用「📂 加载进度」上传一个位于 "
                "`data/Simulator/` 的工作区，或改用「保存为新文件」。"
            )
            st.button("♻️ 覆盖源文件", use_container_width=True, disabled=True,
                      key="overwrite_na")
        else:
            st.caption(f"将覆盖：`data/Simulator/{src_name}`")
            if st.button("♻️ 覆盖源文件", use_container_width=True, type="primary"):
                try:
                    with open(src_path, "w", encoding="utf-8") as f:
                        f.write(ws_json)
                    st.success(f"已覆盖：data/Simulator/{src_name}")
                except Exception as e:
                    st.error(f"覆盖失败：{e}")

    # ── 加载 ──────────────────────────────────────────────────────────────────
    uploaded_ws = st.file_uploader(
        "📂 加载进度",
        type=["json"],
        key="workspace_uploader",
        help="上传之前保存的 .json 工作区文件，100% 还原盘面状态",
    )
    if uploaded_ws is not None:
        # 用 (文件名, 文件大小) 作为唯一标识，防止 rerun 后重复加载死循环
        file_id = (uploaded_ws.name, uploaded_ws.size)
        if st.session_state.get("_loaded_ws_id") != file_id:
            try:
                payload = load_workspace(uploaded_ws.read().decode("utf-8"))
                st.session_state.account           = payload["account"]
                st.session_state.positions         = payload["positions"]
                st.session_state.closed_positions  = payload["closed_positions"]
                st.session_state.initial_capital   = payload["initial_capital"]
                st.session_state.active_ticker     = payload["active_ticker"]
                # current_date_idx 需要在数据加载后校验范围，先暂存
                st.session_state._pending_date_idx = payload.get("current_date_idx", 0)
                st.session_state._loaded_ws_id     = file_id   # 标记已加载，下次 rerun 不重复
                st.session_state._loaded_ws_name   = uploaded_ws.name  # 记录源文件名（用于覆盖保存）
                st.success(f"工作区已加载：{len(payload['positions'])} 个持仓，{len(payload['account']['history'])} 条流水")
                st.rerun()
            except Exception as e:
                st.error(f"加载失败：{e}")
        else:
            # 文件已加载过（同一会话），显示静态提示
            st.caption(f"✅ 已加载 `{uploaded_ws.name}`（取消文件并重新上传可强制重载）")
    else:
        # 文件被用户取消 → 清除标记，下次再上传同一文件时可以再次加载
        st.session_state.pop("_loaded_ws_id", None)

    st.divider()
    st.subheader("⏰ 时间机器")
    st.caption("回到某一历史日期的仓位状态。当前盘面会被快照保存，可一键返回。")

    # 用 history 中的全部日期范围作为参考（fallback：全数据快照日期）
    hist_dates = [date.fromisoformat(str(h["date"]))
                  for h in st.session_state.account.get("history", [])]
    _tm_candidates = hist_dates + ([all_dates[0]] if all_dates else [])
    tm_min = min(_tm_candidates) if _tm_candidates else date.today()
    tm_max = all_dates[-1] if all_dates else date.today()
    if tm_min > tm_max:
        tm_min = tm_max

    _tm_default = st.session_state.get("_tm_last_target")
    if _tm_default is None and all_dates:
        _idx = min(max(st.session_state.current_date_idx, 0), len(all_dates) - 1)
        _tm_default = all_dates[_idx]
    _tm_default = min(max(_tm_default or tm_max, tm_min), tm_max)  # 夹紧到合法区间

    tm_target = st.date_input(
        "目标日期",
        value=_tm_default,
        min_value=tm_min,
        max_value=tm_max,
        help="将账户回放到该日期当时的状态（基于交易历史精确重建）",
    )

    tm_cols = st.columns(2)
    if tm_cols[0].button("⏰ 回到该日", use_container_width=True, key="tm_rewind"):
        # 备份当前完整状态（用于"返回现在"）
        if "_tm_snapshot" not in st.session_state:
            st.session_state._tm_snapshot = {
                "account": st.session_state.account,
                "positions": st.session_state.positions,
                "closed_positions": st.session_state.closed_positions,
                "current_date_idx": st.session_state.current_date_idx,
            }
        # 重放历史到 target
        acc_new, open_new, closed_new = rewind_to_date(
            st.session_state.initial_capital,
            st.session_state.account["history"],
            st.session_state.positions,
            st.session_state.closed_positions,
            tm_target,
        )
        st.session_state.account = acc_new
        st.session_state.positions = open_new
        st.session_state.closed_positions = closed_new
        st.session_state._tm_last_target = tm_target
        # 把复盘日定位到 target（取 ≤ target 的最近快照）
        valid_dates = [d for d in all_dates if d <= tm_target]
        if valid_dates:
            st.session_state.current_date_idx = all_dates.index(valid_dates[-1])
        st.success(f"已回到 {tm_target}：{len(open_new)} 个持仓 / {len(closed_new)} 个已平仓")
        st.rerun()

    if "_tm_snapshot" in st.session_state:
        if tm_cols[1].button("🔙 返回现在", use_container_width=True, key="tm_restore"):
            snap = st.session_state._tm_snapshot
            st.session_state.account = snap["account"]
            st.session_state.positions = snap["positions"]
            st.session_state.closed_positions = snap["closed_positions"]
            st.session_state.current_date_idx = snap["current_date_idx"]
            del st.session_state._tm_snapshot
            st.session_state.pop("_tm_last_target", None)
            st.success("已恢复到最新状态")
            st.rerun()
        st.caption("🕓 当前处于回溯视图")
    else:
        tm_cols[1].button("🔙 返回现在", use_container_width=True, disabled=True,
                          key="tm_restore_na", help="未进入回溯，无需返回")

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

    if not is_replay:
        available_expiries = sorted(day_df["expiry"].unique())

        # ── 🚀 LEAPS 模式（联动过滤到期日和期权类型）──
        leaps_mode = st.checkbox(
            "🚀 LEAPS 模式（一键过滤长期深实值 Call）",
            value=st.session_state.get("_leaps_mode_saved", False),
            key="_leaps_mode_cb",
            help="开启后：仅显示 Call、DTE>365 的到期日、Delta 0.6-0.9 的合约。\n"
                 "便于挑选 LEAPS 长期持仓（典型 Delta≈0.80，DTE 接近 730）",
        )

        st.session_state["_leaps_mode_saved"] = leaps_mode

        leaps_expiries = [d for d in available_expiries if (d - selected_date).days > 365]
        if leaps_mode:
            if not leaps_expiries:
                st.warning("⚠️ 当前数据中无 DTE>365 的到期日，无 LEAPS 可选。请关闭 LEAPS 模式或换日期。")
                expiries_to_show = available_expiries  # 兜底
            else:
                expiries_to_show = leaps_expiries
        else:
            expiries_to_show = available_expiries

        # 跨日期保留上次选中的到期日（若仍在新列表里）
        prev_expiry = st.session_state.get("_selected_expiry")
        if prev_expiry in expiries_to_show:
            default_idx = expiries_to_show.index(prev_expiry)
        else:
            default_idx = 0
        selected_expiry = st.selectbox(
            "到期日" + ("（仅 LEAPS）" if leaps_mode else ""),
            expiries_to_show,
            index=default_idx,
            format_func=lambda d: f"{d}  (DTE {(d - selected_date).days})",
            key="_expiry_selectbox",
        )
        st.session_state["_selected_expiry"] = selected_expiry

        # 跳到到期日：让复盘日 = 选中的到期日（若该日无快照，取最近的 ≤ 到期日的快照）
        from datetime import timedelta as _td
        exp_candidates = [d for d in all_dates if d <= selected_expiry]
        if exp_candidates:
            exp_jump = exp_candidates[-1]
            label = "📅 跳到到期日" if exp_jump == selected_expiry else f"📅 跳到到期日 (最近: {exp_jump})"
            if st.button(label, use_container_width=True,
                         help=f"将复盘日设为 {exp_jump}"):
                st.session_state.current_date_idx = all_dates.index(exp_jump)
                st.rerun()
        else:
            st.caption(f"⚠️ {selected_expiry} 及之前无可用快照")

        # 跳到到期日的 1 周之前：用于 Roll Up & Out 收租时机检视
        target_date = selected_expiry - _td(days=7)
        candidates = [d for d in all_dates if d <= target_date]
        if candidates:
            jump_date = candidates[-1]
            if st.button("⏪ 跳到期日的 1 周之前", use_container_width=True,
                         help=f"将复盘日设为 {jump_date}（{selected_expiry} 前 7 天最近快照）"):
                st.session_state.current_date_idx = all_dates.index(jump_date)
                st.rerun()
        else:
            st.caption(f"⚠️ {selected_expiry} 前 7 天无可用快照")

        if leaps_mode:
            option_type = "Call"
            st.caption("⚙️ LEAPS 模式：固定 Call，过滤 Delta 0.60-0.90")
        else:
            option_type = st.radio("期权类型", ["Call", "Put"],
                                   index=["Call", "Put"].index(st.session_state.get("_option_type_saved", "Call")))
            st.session_state["_option_type_saved"] = option_type

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

# ── 复盘交易图页面 ─────────────────────────────────────────────────────────────
@st.cache_data(show_spinner="反推标的价格…")
def _underlying_for(files: tuple, ticker: str) -> pd.Series:
    frames = [load_data(p, ticker) for p in files]
    return trade_chart.underlying_series(pd.concat(frames, ignore_index=True))


def render_trade_replay(df: pd.DataFrame, selected_ticker: str, selected_files: list,
                        selected_date: date) -> None:
    """复盘交易图页：筛选 + TradingView 交互图 + 交易明细 + 单笔合约走势。"""
    _all_pos = [p for p in st.session_state.positions + st.session_state.closed_positions
                if p.get("ticker", selected_ticker) == selected_ticker]
    if not _all_pos:
        st.info("加载 workspace 或建仓后，这里会把每笔交易的开仓/平仓点画在标的价格上。")
    else:
        _under = _underlying_for(tuple(sorted(selected_files)), selected_ticker)
        _trades = trade_chart.build_trade_table(
            [p for p in st.session_state.positions if p.get("ticker", selected_ticker) == selected_ticker],
            [p for p in st.session_state.closed_positions if p.get("ticker", selected_ticker) == selected_ticker],
            _under,
            st.session_state.account.get("history", []),
        )
        _d0, _d1 = min(_under.index), max(_under.index)

        f1, f2, f3, f4 = st.columns([2, 2, 2, 1])
        with f1:
            _rng = st.date_input("日期范围", value=(_d0, _d1), min_value=_d0, max_value=_d1,
                                 key="tc_range")
        _kinds = sorted((_trades["dir_label"] + " " + _trades["type"]).unique())
        with f2:
            _sel_kinds = st.multiselect("交易类型", _kinds, default=_kinds, key="tc_kinds")
        _outs = list(dict.fromkeys(_trades["outcome"]))
        with f3:
            _sel_outs = st.multiselect("平仓方式", _outs, default=_outs, key="tc_outs")
        with f4:
            _pnl = st.radio("盈亏", ["全部", "盈利", "亏损"], key="tc_pnl")

        _r0, _r1 = (_rng if isinstance(_rng, tuple) and len(_rng) == 2 else (_d0, _d1))
        _m = ((_trades["dir_label"] + " " + _trades["type"]).isin(_sel_kinds)
              & _trades["outcome"].isin(_sel_outs)
              & (_trades["entry_date"] >= _r0) & (_trades["entry_date"] <= _r1))
        _rz = _trades["realized"].fillna(0)
        if _pnl == "盈利":
            _m &= _trades["realized"].notna() & (_rz >= 0)
        elif _pnl == "亏损":
            _m &= _rz < 0
        _shown = _trades[_m]
        try:
            _dark = st.context.theme.type == "dark"
        except Exception:
            _dark = False
        st.iframe(
            trade_chart.chart_html(trade_chart.chart_payload(_under, _shown, as_of=selected_date),
                                   selected_ticker, dark=_dark, height=640),
            height=680,
        )
        _outside = int(((_trades["entry_date"] < _d0) | (_trades["entry_date"] > _d1)).sum())
        st.caption(
            f"显示 {len(_shown)} / {len(_trades)} 笔。标的价格由期权链 Put-Call Parity 反推（每日 15:45 快照，未复权，"
            f"与行权价同一口径）。▼ Short / ▲ Long，蓝 Put / 橙 Call；"
            f"平仓 ● 到期作废 · ■ 提前平仓 · ●行权 = 被行权 · ●ITM = Long 到期实值结算，绿盈红亏。每笔交易 = 一段行权价虚线（开仓日 → 平仓日，"
            f"未平仓画到当前模拟日），虚线与价格线的距离就是安全垫。默认显示交易区间，滚轮缩放、拖动平移，Y 轴自动适配。"
            + (f" ⚠️ 有 {_outside} 笔开仓日不在已选数据文件范围内，请在侧边栏多选对应年份。" if _outside else "")
        )

        if not _shown.empty:
            _tbl = _shown[["id", "dir_label", "type", "strike", "qty", "expiry", "entry_date",
                           "entry_mid", "close_date", "close_mid", "outcome", "realized"]].copy()
            _tbl.columns = ["#", "方向", "类型", "行权价", "张数", "到期", "开仓日", "开仓价",
                            "平仓日", "平仓价", "方式", "盈亏"]
            for c in ("到期", "开仓日", "平仓日"):
                _tbl[c] = _tbl[c].astype(str).replace("None", "—")
            st.dataframe(_tbl, hide_index=True, width="stretch", column_config={
                "行权价": st.column_config.NumberColumn(format="$%.2f"),
                "开仓价": st.column_config.NumberColumn(format="$%.2f"),
                "平仓价": st.column_config.NumberColumn(format="$%.2f"),
                "盈亏": st.column_config.NumberColumn(format="$%+.2f"),
            })

            _pick = st.selectbox("查看单笔合约价格走势", list(_shown["id"]),
                                 format_func=lambda i: _trades.loc[_trades["id"] == i, "label"].iloc[0],
                                 key="tc_pick")
            _row = _trades[_trades["id"] == _pick].iloc[0]
            _pos = {"expiry": _row["expiry"], "strike": _row["strike"], "type": _row["type"],
                    "entry_date": _row["entry_date"], "close_date": _row["close_date"]}
            _series = trade_chart.contract_series(df, _pos)
            if _series.empty:
                st.info("所选合约在已加载的数据文件中没有报价（可能 Δ/DTE 超出抓取范围）。")
            else:
                st.plotly_chart(trade_chart.contract_figure(_series, _row),
                                width="stretch", key="tc_contract")


def render_trade_history(acct: dict) -> None:
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


if is_replay:
    st.title(f"{selected_ticker} 交易复盘")
    render_trade_replay(df, selected_ticker, selected_files, selected_date)
    render_trade_history(acct)
    st.stop()


# ── Filter chain ───────────────────────────────────────────────────────────────
filtered = (
    day_df[(day_df["expiry"] == selected_expiry) & (day_df["type"] == option_type)]
    .sort_values("strike")
    .reset_index(drop=True)
)

# LEAPS 模式：进一步收窄到 Delta 0.60-0.90 的深实值区
if leaps_mode and not filtered.empty:
    filtered = filtered[filtered["delta"].abs().between(0.60, 0.90)].reset_index(drop=True)

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

# 自动判断数据采样节奏（日采样 vs 周采样）
if len(all_dates) >= 2:
    gap_days = (all_dates[1] - all_dates[0]).days
else:
    gap_days = 1
is_weekly = gap_days >= 5
step_label = "1周" if is_weekly else "1日"
big_step_label = "5周" if is_weekly else "1周"
big_step = 5  # 5 个快照（日采样=1周，周采样=5周）

with top_cols[4]:
    st.write("")
    st.write("")
    if st.button(f"⏪ -{step_label}", use_container_width=True,
                 disabled=st.session_state.current_date_idx == 0):
        st.session_state.current_date_idx -= 1
        st.rerun()
with top_cols[5]:
    st.write("")
    st.write("")
    if st.button(f"⏭ +{step_label}", use_container_width=True,
                 disabled=st.session_state.current_date_idx >= len(all_dates) - 1):
        st.session_state.current_date_idx += 1
        st.rerun()
with top_cols[6]:
    st.write("")
    st.write("")
    if st.button(f"⏩ +{big_step_label}", use_container_width=True,
                 disabled=st.session_state.current_date_idx >= len(all_dates) - 1):
        st.session_state.current_date_idx = min(
            len(all_dates) - 1, st.session_state.current_date_idx + big_step
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
    lot["qty"] * (est_price - lot["price"])
    for tkr, sh in acct["shares"].items()
    if sh["qty"] > 0 and tkr == selected_ticker and est_price is not None
    for lot in sh.get("lots", [{"qty": sh["qty"], "price": sh["cost_basis"]}])
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
    lot_rows = []
    for tkr, sh in acct["shares"].items():
        if sh["qty"] <= 0:
            continue
        mkt_price = est_price if (tkr == selected_ticker and est_price) else None
        lots = sh.get("lots", [])
        if lots:
            for i, lot in enumerate(lots, 1):
                lot_val = lot["qty"] * mkt_price if mkt_price else None
                lot_pnl = (mkt_price - lot["price"]) * lot["qty"] if mkt_price else None
                lot_rows.append({
                    "标的": tkr,
                    "批次": f"#{i}",
                    "买入日": lot["date"],
                    "该批股数": lot["qty"],
                    "买入价/股": lot["price"],
                    "当前市价": mkt_price,
                    "该批市值": lot_val,
                    "该批浮盈": lot_pnl,
                })
        else:
            # 无 lots 记录时退化为单行（向后兼容）
            mkt_val = sh["qty"] * mkt_price if mkt_price else None
            unreal  = (mkt_price - sh["cost_basis"]) * sh["qty"] if mkt_price else None
            lot_rows.append({
                "标的": tkr,
                "批次": "#1",
                "买入日": "—",
                "该批股数": sh["qty"],
                "买入价/股": sh["cost_basis"],
                "当前市价": mkt_price,
                "该批市值": mkt_val,
                "该批浮盈": unreal,
            })

    lot_df = pd.DataFrame(lot_rows)

    def color_lot_pnl(val):
        if pd.isna(val):
            return ""
        return "color: #4CAF50; font-weight:bold" if val > 0 else "color: #F44336; font-weight:bold"

    st.dataframe(
        lot_df.style
        .map(color_lot_pnl, subset=["该批浮盈"])
        .format({
            "该批股数":  "{:.0f}",
            "买入价/股": lambda v: f"${v:.2f}" if pd.notna(v) else "—",
            "当前市价":  lambda v: f"${v:.2f}" if pd.notna(v) else "N/A",
            "该批市值":  lambda v: f"${v:,.2f}" if pd.notna(v) else "—",
            "该批浮盈":  lambda v: f"${v:+,.2f}" if pd.notna(v) else "—",
        }),
        use_container_width=True,
        hide_index=True,
    )

st.divider()

# ── Positions & PnL ────────────────────────────────────────────────────────────
st.subheader("当前持仓")

if not st.session_state.positions:
    st.info("暂无活跃持仓。")
else:
    rows = []
    profit_take_candidates = []  # 锁定利润候选：≥50% 已实现的 Short 期权
    any_estimated = False
    for i, pos in enumerate(st.session_state.positions):
        cur_mid, is_est = get_mid_smart(df, pos, selected_date, est_price)
        if cur_mid is not None:
            if is_est:
                any_estimated = True
            pnl = pos["direction"] * (cur_mid - pos["entry_mid"]) * CONTRACT_MULT * pos["qty"]
            if pos["entry_mid"] > 0:
                lock_pct = (pos["direction"] * (cur_mid - pos["entry_mid"])
                            / pos["entry_mid"] * 100)
            else:
                lock_pct = None
            # Short 期权 ≥ 50% 视为可锁定利润候选（estimated 价格的 Short 不参与，避免误触发）
            if pos["direction"] == -1 and not is_est and lock_pct is not None and lock_pct >= 50:
                profit_take_candidates.append((i, pos, cur_mid, lock_pct))
        else:
            pnl = None
            lock_pct = None

        dir_display = pos["dir_label"]
        if pos.get("covered"):
            dir_display += " 🛡️"
        # 价格标记是否为估算
        cur_mid_display = cur_mid
        if cur_mid is not None and is_est:
            dir_display += " ⋯"  # 估算标记
        rows.append({
            "标的": pos.get("ticker", selected_ticker),
            "合约": f"${pos['strike']:.0f} {pos['type']}",
            "方向": dir_display,
            "张数": pos["qty"],
            "入场价": pos["entry_mid"],
            "当前价": cur_mid,
            "PnL": pnl,
            "锁定%": lock_pct,
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

    def color_lock_pct(val):
        """≥80% 深绿粗体 / ≥50% 浅绿 / <0 红色"""
        if pd.isna(val):
            return ""
        if val >= 80:
            return "background-color: #C8E6C9; color: #1B5E20; font-weight: bold"
        if val >= 50:
            return "background-color: #E8F5E9; color: #2E7D32; font-weight: bold"
        if val < 0:
            return "color: #F44336"
        return ""

    styled_pos = (
        pos_df.style
        .map(color_pnl, subset=["PnL"])
        .map(color_lock_pct, subset=["锁定%"])
        .format({
            "入场价": lambda v: f"${v:.2f}" if pd.notna(v) else "—",
            "当前价": lambda v: f"${v:.2f}" if pd.notna(v) else "—",
            "PnL":    lambda v: f"${v:+.2f}" if pd.notna(v) else "—",
            "锁定%":  lambda v: f"{v:+.1f}%" if pd.notna(v) else "—",
        })
    )
    st.dataframe(styled_pos, use_container_width=True, hide_index=True)
    if any_estimated:
        st.caption(
            "⋯ 标记：该合约当前 DTE 已跌出 LEAPS 数据过滤范围（>365 天），"
            "当前价用 **intrinsic value** (max(spot−strike, 0)) 估算。"
            "对深 ITM Long Call 误差通常 < 5%；如需精确价格请重抓数据放宽 DTE 下限。"
        )

    # ── 利润锁定建议（≥50% Short 期权一键回购）───────────────────────────
    profit_take_close_idx = None
    if profit_take_candidates:
        st.success(
            f"💰 **利润锁定建议**：{len(profit_take_candidates)} 个 Short 期权"
            f"已锁定 ≥50% 利润，建议提前回购（避免到期前的尾部风险）"
        )
        pt_cols = st.columns(min(len(profit_take_candidates), 4))
        for j, (orig_idx, pos, cur_mid, lock_pct) in enumerate(profit_take_candidates):
            col = pt_cols[j % len(pt_cols)]
            saved = (pos["entry_mid"] - cur_mid) * CONTRACT_MULT * pos["qty"]
            btn_label = (f"💰 锁定 #{orig_idx}  ${pos['strike']:.0f} {pos['type']}\n"
                         f"{lock_pct:.0f}%  +${saved:.0f}")
            if col.button(btn_label, key=f"profit_take_{orig_idx}",
                          use_container_width=True):
                profit_take_close_idx = orig_idx

    if profit_take_close_idx is not None:
        pos = st.session_state.positions[profit_take_close_idx]
        cur_mid = get_mid(df, pos["symbol"], selected_date)
        close_position(st.session_state.account, pos, selected_date, cur_mid,
                       action_label="利润锁定")
        st.session_state.closed_positions.append(pos)
        st.session_state.positions.pop(profit_take_close_idx)
        st.rerun()

    st.divider()
    st.subheader("平仓操作")
    st.caption("张数可调，支持部分平仓；剩余张数继续持有。")
    close_request = None  # (idx, qty_to_close)
    for i, pos in enumerate(st.session_state.positions):
        c_info, c_qty, c_btn = st.columns([3, 1, 1.4])
        c_info.markdown(
            f"**#{i}　${pos['strike']:.0f} {pos['type']}** ({pos['dir_label']})　"
            f"· 持有 {pos['qty']} 张 · 到期 {pos['expiry']}"
        )
        if pos["qty"] > 1:
            qty_to_close = c_qty.number_input(
                "平仓张数", min_value=1, max_value=int(pos["qty"]),
                value=int(pos["qty"]), step=1, key=f"close_qty_{i}",
                label_visibility="collapsed",
            )
        else:
            qty_to_close = 1
            c_qty.markdown("1 张")
        btn_label = (f"平仓 {qty_to_close}/{pos['qty']} 张"
                     if pos["qty"] > 1 else "平仓")
        if c_btn.button(btn_label, key=f"close_{i}", use_container_width=True):
            close_request = (i, int(qty_to_close))

    if close_request is not None:
        idx, qty_to_close = close_request
        pos = st.session_state.positions[idx]
        cur_mid, is_est = get_mid_smart(df, pos, selected_date, est_price)
        if cur_mid is None:
            st.warning(f"持仓 #{idx} 在 {selected_date} 无报价、且不能用 intrinsic 估算，无法平仓")
        else:
            if is_est:
                st.info(f"⋯ 持仓 #{idx} 当天无盘口报价，按 intrinsic value ${cur_mid:.2f} 平仓（深 ITM Long 估算）")
            chunk = partial_close_position(
                st.session_state.account, st.session_state.positions,
                idx, selected_date, cur_mid, qty_to_close, action_label="手动平仓",
            )
            st.session_state.closed_positions.append(chunk)
            st.rerun()

    # ── Roll Up & Out ──────────────────────────────────────────────────────────
    short_positions = [(i, p) for i, p in enumerate(st.session_state.positions)
                       if p["direction"] == -1]
    if short_positions:
        st.divider()
        with st.expander("🔄 Roll Up & Out（一键展期）", expanded=False):
            st.caption("一次完成：平仓回购老 Short + 新到期/新 Strike 开仓。"
                       "Call → Roll Up（新 Strike ≥ 原 Strike）；Put → Roll Down（新 Strike ≤ 原 Strike）")

            pos_labels = [
                f"#{i}  ${p['strike']:.0f} {p['type']} {p.get('dir_label','Short')}"
                f"{' 🛡️' if p.get('covered') else ''}  到期 {p['expiry']}"
                for i, p in short_positions
            ]
            sel_idx_in_short = st.selectbox(
                "选要 Roll 的 Short 持仓",
                range(len(short_positions)),
                format_func=lambda i: pos_labels[i],
                key="roll_pos_select",
            )
            _, sel_pos = short_positions[sel_idx_in_short]

            old_mid = get_mid(df, sel_pos["symbol"], selected_date)
            if old_mid is None:
                st.warning(f"原持仓在 {selected_date} 无报价，无法 Roll。")
            else:
                future_expiries = [e for e in sorted(day_df["expiry"].unique())
                                   if e > sel_pos["expiry"]]
                if not future_expiries:
                    st.info(f"当前数据中没有晚于 {sel_pos['expiry']} 的到期日。")
                else:
                    new_expiry = st.selectbox(
                        "新到期日（必须 > 原到期）",
                        future_expiries,
                        format_func=lambda d: f"{d}  (DTE {(d - selected_date).days})",
                        key="roll_new_expiry",
                    )

                    roll_chain = (
                        day_df[(day_df["expiry"] == new_expiry) &
                               (day_df["type"] == sel_pos["type"])]
                        .dropna(subset=["delta"])
                        .sort_values("strike")
                    )
                    roll_chain = filter_liquid(roll_chain)  # 过滤 ghost quotes
                    if sel_pos["type"] == "Call":
                        roll_chain = roll_chain[roll_chain["strike"] >= sel_pos["strike"]]
                    else:
                        roll_chain = roll_chain[roll_chain["strike"] <= sel_pos["strike"]]

                    if roll_chain.empty:
                        direction_word = "更高" if sel_pos["type"] == "Call" else "更低"
                        st.info(f"到期 {new_expiry} 无可用 {direction_word} Strike 合约。")
                    else:
                        roll_chain = roll_chain.copy()
                        roll_chain["label"] = roll_chain.apply(
                            lambda r: (f"${r['strike']:.0f} {r['type']} | "
                                       f"Δ{r['delta']:+.2f} | mid ${r['mid']:.2f}"),
                            axis=1,
                        )

                        # 注意：不能用 st.form —— form 内 widget 变化不重渲染，
                        # 会导致切换"新合约"下拉框时预览金额不更新（用旧 mid 算出）。
                        col1, col2 = st.columns([3, 1])
                        new_label = col1.selectbox(
                            "新合约",
                            roll_chain["label"].tolist(),
                            key="roll_new_strike",
                        )
                        new_qty = col2.number_input(
                            "张数",
                            min_value=1, max_value=int(sel_pos["qty"]),
                            value=int(sel_pos["qty"]),
                            key="roll_new_qty",
                        )

                        new_row = roll_chain[roll_chain["label"] == new_label].iloc[0]
                        buyback_cost = old_mid * CONTRACT_MULT * sel_pos["qty"]
                        new_premium = float(new_row["mid"]) * CONTRACT_MULT * int(new_qty)
                        net_credit = new_premium - buyback_cost

                        mark = "🟢" if net_credit >= 0 else "🔴"
                        verdict = "credit" if net_credit >= 0 else "debit"
                        st.markdown(
                            f"**预览**: 回购 -${buyback_cost:,.2f}  +  "
                            f"新开仓 +${new_premium:,.2f}  =  "
                            f"{mark} 净 {verdict} **${net_credit:+,.2f}**"
                        )

                        roll_submit = st.button(
                            "🔄 执行 Roll", use_container_width=True,
                            key="roll_submit_btn",
                        )
                        if roll_submit:
                                # 1. 平掉老仓
                                close_position(
                                    st.session_state.account, sel_pos,
                                    selected_date, old_mid,
                                    action_label="Roll 平仓",
                                )
                                st.session_state.closed_positions.append(sel_pos)
                                for i, p in enumerate(st.session_state.positions):
                                    if p is sel_pos:
                                        st.session_state.positions.pop(i)
                                        break

                                # 2. 开新仓
                                new_pos = {
                                    "ticker":     sel_pos["ticker"],
                                    "symbol":     new_row["option_symbol"],
                                    "strike":     float(new_row["strike"]),
                                    "type":       sel_pos["type"],
                                    "expiry":     new_row["expiry"],
                                    "direction":  -1,
                                    "dir_label":  "Short",
                                    "qty":        int(new_qty),
                                    "entry_mid":  float(new_row["mid"]),
                                    "entry_date": selected_date,
                                }
                                # 继承 covered 标记（若仍有足够正股）
                                if sel_pos.get("covered"):
                                    avail = get_available_shares(
                                        st.session_state.account,
                                        st.session_state.positions,
                                        sel_pos["ticker"],
                                    )
                                    if avail >= int(new_qty) * CONTRACT_MULT:
                                        new_pos["covered"] = True

                                open_position(
                                    st.session_state.account, new_pos,
                                    selected_date, float(new_row["mid"]),
                                )
                                st.session_state.positions.append(new_pos)

                                # 3. history 最后两条加 [Roll] 标记
                                hist = st.session_state.account["history"]
                                if len(hist) >= 2:
                                    for h in hist[-2:]:
                                        h["note"] = f"[Roll] {h['note']}"

                                st.success(
                                    f"已 Roll: ${sel_pos['strike']:.0f}@{sel_pos['expiry']} "
                                    f"→ ${new_row['strike']:.0f}@{new_expiry}  "
                                    f"净 {verdict} ${net_credit:+,.2f}"
                                )
                                st.rerun()

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


def highlight_chain_row(row):
    """优先级：流动性灰显 > Delta<0.10 橙色警告 > Delta 0.75-0.85 绿色甜区（LEAPS）"""
    liq = (row["Volume"] if pd.notna(row["Volume"]) else 0) + \
          (row["OI"]     if pd.notna(row["OI"])     else 0)
    if liq < LIQUIDITY_MIN:
        return ["background-color: #F5F5F5; color: #9E9E9E"] * len(row)
    delta_abs = abs(row["Delta"]) if pd.notna(row["Delta"]) else 1.0
    if delta_abs < 0.10:
        return ["background-color: #FFF3E0; color: #E65100"] * len(row)
    if 0.75 <= delta_abs <= 0.85:
        return ["background-color: #E8F5E9; color: #1B5E20; font-weight: 600"] * len(row)
    return [""] * len(row)


styled = (
    chain_display.style
    .apply(highlight_chain_row, axis=1)
    .map(style_delta, subset=["Delta"])
    .format({
        "Strike": "{:.1f}", "Mid": "{:.2f}", "Bid": "{:.2f}", "Ask": "{:.2f}",
        "Delta": "{:.4f}", "Theta(原始)": "{:.2f}", "Gamma": "{:.5f}",
        "Vega": "{:.4f}", "IV": "{:.3f}", "Volume": "{:,.0f}", "OI": "{:,.0f}",
    })
)
st.dataframe(styled, use_container_width=True, hide_index=True)
st.caption(
    f"ℹ️ 灰色行 = Volume+OI < {LIQUIDITY_MIN} 的 ghost quote，已从选择器排除。"
    "  橙色行 = Delta < 0.10，远 OTM 区域价格可能失真（多个行权价 mid 相同），建议回避。"
    "  绿色行 = Delta 0.75-0.85 LEAPS 甜区（深实值 Long Call 推荐区间）。"
)

# ── 🚀 一键买入最优 LEAPS（仅 LEAPS 模式可见）─────────────────────────────────
if leaps_mode and option_type == "Call":
    leaps_pool = (
        filter_liquid(filtered.dropna(subset=["delta"]))
        .copy()
    )
    if not leaps_pool.empty:
        # 选 Delta 最接近 0.80 的合约（深实值首选）
        leaps_pool["_dist"] = (leaps_pool["delta"].abs() - 0.80).abs()
        best = leaps_pool.sort_values("_dist").iloc[0]
        dte_days = (selected_expiry - selected_date).days
        cost_per = float(best["mid"]) * CONTRACT_MULT
        leaps_cols = st.columns([2, 1, 1])
        with leaps_cols[0]:
            st.success(
                f"🎯 **最优 LEAPS 候选**: ${best['strike']:.0f} Call  "
                f"Δ{best['delta']:+.3f}  mid ${best['mid']:.2f}  "
                f"DTE {dte_days}  Vol {int(best['volume'] or 0)} / OI {int(best['open_interest'] or 0)}"
            )
        with leaps_cols[1]:
            leaps_qty = st.number_input(
                "张数", min_value=1, max_value=20, value=1, key="leaps_qty",
                help=f"每张成本 ≈ ${cost_per:,.2f}"
            )
        with leaps_cols[2]:
            total_cost = cost_per * int(leaps_qty)
            can_afford = acct["cash"] >= total_cost
            if st.button(
                f"🚀 买入 {int(leaps_qty)} 张\n-${total_cost:,.0f}",
                use_container_width=True,
                disabled=not can_afford,
                key="leaps_buy_btn",
                help="按当前 mid 直接 Long 开仓 LEAPS（深实值 Call，长期持有替代正股）",
            ):
                new_pos = {
                    "ticker":     selected_ticker,
                    "symbol":     best["option_symbol"],
                    "strike":     float(best["strike"]),
                    "type":       "Call",
                    "expiry":     best["expiry"],
                    "direction":  1,
                    "dir_label":  "Long",
                    "qty":        int(leaps_qty),
                    "entry_mid":  float(best["mid"]),
                    "entry_date": selected_date,
                }
                open_position(acct, new_pos, selected_date, float(best["mid"]))
                st.session_state.positions.append(new_pos)
                st.success(
                    f"✅ 已买入 {int(leaps_qty)} 张 ${best['strike']:.0f} Call @ ${best['mid']:.2f}，"
                    f"现金支出 ${total_cost:,.2f}"
                )
                st.rerun()
            if not can_afford:
                st.caption(f"⚠️ 现金不足（需 ${total_cost:,.0f}，余 ${acct['cash']:,.0f}）")
    else:
        st.info("当前过滤条件下无可交易 LEAPS 合约（已剔除流动性不足）。")

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
        cc_chain = filter_liquid(cc_chain)  # 过滤 ghost quotes
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
    chain_for_entry = filter_liquid(filtered.dropna(subset=["delta"])).copy()
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

render_trade_history(acct)

st.caption("数据来源：QuantConnect Research Notebook | 保证金/正股持仓为下一版本实现")
