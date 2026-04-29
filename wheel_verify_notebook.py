# ============================================================
# 车轮策略 PnL 验证 — QuantConnect Research Notebook
# Cell 1：初始化（如果 qb 已存在可跳过）
# ============================================================
from AlgorithmImports import *
qb = QuantBook()

# ============================================================
# Cell 2：验证主逻辑（粘贴到下一个 Cell）
# ============================================================
import pandas as pd
from io import StringIO
from datetime import date

# ── 1. 从 Object Store 加载 TQQQ 期权数据 ────────────────────────────────────
csv_key = "TQQQ_options_20260201_20260417_greeks.csv"
raw = qb.ObjectStore.Read(csv_key)
df = pd.read_csv(StringIO(raw))
df["snapshot_date"] = pd.to_datetime(df["snapshot_date"]).dt.date
df["expiry"]        = pd.to_datetime(df["expiry"]).dt.date
df["mid"]           = (df["bid"] + df["ask"]) / 2

def get_mid(trade_date: date, opt_type: str, strike: float, expiry: date) -> float | None:
    """从 DataFrame 查询指定合约的 mid 价格。"""
    mask = (
        (df["snapshot_date"] == trade_date) &
        (df["type"]          == opt_type)   &
        (df["strike"]        == strike)     &
        (df["expiry"]        == expiry)
    )
    rows = df[mask]
    if rows.empty:
        return None
    return float(rows.iloc[0]["mid"])

# ── 2. 硬编码交易流水 ─────────────────────────────────────────────────────────
# 字段: trade_date, opt_type, strike, expiry, qty, action
#   action: "sell_put" | "sell_call"
# 到期结算由循环自动处理（不需要单独列出行权事件）
TRADES = [
    # 2026-02-02: 卖 Put $50，到期 2026-02-20
    dict(trade_date=date(2026,2,2),  opt_type="Put",  strike=50.0, expiry=date(2026,2,20), qty=1, action="sell_put"),
    # 2026-02-23: 卖备兑 Call $50.5，到期 2026-03-13
    dict(trade_date=date(2026,2,23), opt_type="Call", strike=50.5, expiry=date(2026,3,13), qty=1, action="sell_call"),
    # 2026-03-13: 卖备兑 Call $48，到期 2026-03-20
    dict(trade_date=date(2026,3,13), opt_type="Call", strike=48.0, expiry=date(2026,3,20), qty=1, action="sell_call"),
    # 2026-03-20: 卖备兑 Call $46，到期 2026-03-27
    dict(trade_date=date(2026,3,20), opt_type="Call", strike=46.0, expiry=date(2026,3,27), qty=1, action="sell_call"),
    # 2026-03-27: 卖备兑 Call $42，到期 2026-04-10
    dict(trade_date=date(2026,3,27), opt_type="Call", strike=42.0, expiry=date(2026,4,10), qty=1, action="sell_call"),
]

COMMISSION   = 0.65   # 每张期权手续费 $
MULTIPLIER   = 100    # 标准期权乘数
INITIAL_CASH = 10_000.0

# ── 3. 模拟资金流 ─────────────────────────────────────────────────────────────
cash         = INITIAL_CASH
shares       = 0          # 持有 TQQQ 股数
share_cost   = 0.0        # 买入成本价（用于计算股票差价）
premium_total = 0.0       # 累计权利金收入（扣手续费后）
stock_pnl    = 0.0        # 股票买卖差价损益
commission_total = 0.0    # 累计手续费

log = []  # 对账单明细

def record(date_, action, detail, cash_delta, cash_after):
    log.append(dict(日期=str(date_), 动作=action, 说明=detail,
                    现金变化=cash_delta, 现金余额=cash_after))

for t in TRADES:
    mid = get_mid(t["trade_date"], t["opt_type"], t["strike"], t["expiry"])

    # ── 查不到 mid：用本地导出值兜底 ──────────────────────────────────────
    LOCAL_MIDS = {
        (date(2026,2,2),  "Put",  50.0, date(2026,2,20)):  1.635,
        (date(2026,2,23), "Call", 50.5, date(2026,3,13)):  2.660,
        (date(2026,3,13), "Call", 48.0, date(2026,3,20)):  1.825,
        (date(2026,3,20), "Call", 46.0, date(2026,3,27)):  1.825,
        (date(2026,3,27), "Call", 42.0, date(2026,4,10)):  2.435,
    }
    key = (t["trade_date"], t["opt_type"], t["strike"], t["expiry"])
    source = "CSV"
    if mid is None:
        mid = LOCAL_MIDS.get(key)
        source = "本地兜底"
        if mid is None:
            print(f"❌ 找不到 mid：{key}，跳过")
            continue

    # ── 卖出期权：收取权利金，扣手续费 ───────────────────────────────────
    premium   = mid * MULTIPLIER * t["qty"]
    comm      = COMMISSION * t["qty"]
    net_prem  = premium - comm
    cash     += net_prem
    premium_total    += net_prem
    commission_total += comm
    record(t["trade_date"],
           f"卖出 {t['opt_type']} ${t['strike']}",
           f"mid={mid:.3f} [{source}]  权利金={premium:.2f}  手续费={comm:.2f}",
           net_prem, cash)

    # ── 到期结算 ──────────────────────────────────────────────────────────
    expiry = t["expiry"]

    # 查到期日标的估算价（用最短 DTE ATM Call delta≈0.5 推算）
    exp_df = df[(df["snapshot_date"] == expiry) & (df["type"] == "Call")]
    spot = None
    if not exp_df.empty:
        shortest = exp_df["expiry"].min()
        near = exp_df[exp_df["expiry"] == shortest]
        idx  = (near["delta"] - 0.5).abs().idxmin()
        spot = float(near.loc[idx, "strike"])

    if t["opt_type"] == "Put":
        # Short Put 到期：ITM → 买入正股
        itm = (spot is not None and spot < t["strike"]) or (spot is None)
        if itm:
            cost = t["strike"] * MULTIPLIER * t["qty"]
            cash       -= cost
            shares     += MULTIPLIER * t["qty"]
            share_cost  = t["strike"]
            spot_str = f"${spot:.2f}" if spot else "N/A"
            record(expiry,
                   f"Put 被行权 → 买入 {MULTIPLIER*t['qty']} 股",
                   f"到期日股价≈{spot_str}  行权价=${t['strike']}  买股成本={cost:.2f}",
                   -cost, cash)
        else:
            record(expiry, "Put OTM 废弃", f"到期日股价≈${spot:.2f}  行权价=${t['strike']}", 0, cash)

    else:  # Call
        # Short Call 到期：ITM → 卖出正股（备兑）
        itm = (spot is not None and spot > t["strike"])
        if itm and shares >= MULTIPLIER * t["qty"]:
            proceeds   = t["strike"] * MULTIPLIER * t["qty"]
            sold_shares = MULTIPLIER * t["qty"]
            diff        = (t["strike"] - share_cost) * sold_shares
            cash       += proceeds
            shares     -= sold_shares
            stock_pnl  += diff
            spot_str = f"${spot:.2f}" if spot else "N/A"
            record(expiry,
                   f"Call 被行权 → 卖出 {sold_shares} 股",
                   f"到期日股价≈{spot_str}  行权价=${t['strike']}  收回={proceeds:.2f}  股票差价={diff:+.2f}",
                   proceeds, cash)
        elif itm:
            # 裸 Call 现金结算
            intrinsic = max(0, spot - t["strike"]) * MULTIPLIER * t["qty"]
            cash -= intrinsic
            record(expiry, "裸 Call 现金结算",
                   f"内在价值={intrinsic:.2f}", -intrinsic, cash)
        else:
            spot_str = f"${spot:.2f}" if spot else "N/A"
            record(expiry, "Call OTM 废弃", f"到期日股价≈{spot_str}  行权价=${t['strike']}", 0, cash)

# ── 4. 打印对账单 ─────────────────────────────────────────────────────────────
print("\n" + "="*70)
print("  车轮策略 PnL 验证对账单")
print("="*70)
log_df = pd.DataFrame(log)
log_df["现金变化"] = log_df["现金变化"].map(lambda v: f"${v:+,.2f}")
log_df["现金余额"] = log_df["现金余额"].map(lambda v: f"${v:,.2f}")
print(log_df.to_string(index=False))

# 股票剩余市值（理论上应为 0，因为最后一笔 Call 被行权卖出了）
share_value = shares * (spot if spot else share_cost)

total_value = cash + share_value
net_pnl     = total_value - INITIAL_CASH

LOCAL_EXPECTED_VALUE = 10_237.50
LOCAL_EXPECTED_PNL   =    237.50
# 本地未扣手续费，加回手续费后对比（本地模拟器未计手续费）
adj_pnl = net_pnl + commission_total  # 还原手续费后的"无佣金"净利

print("\n" + "="*70)
print(f"  初始资金:          ${INITIAL_CASH:>10,.2f}")
print(f"  期末现金:          ${cash:>10,.2f}")
print(f"  剩余股票市值:      ${share_value:>10,.2f}  ({shares} 股)")
print(f"  期末总资产:        ${total_value:>10,.2f}")
print(f"  净盈亏 (含手续费): ${net_pnl:>+10,.2f}")
print(f"  累计手续费:        ${commission_total:>10,.2f}")
print(f"  净盈亏 (不含手续费): ${adj_pnl:>+10,.2f}")
print("-"*70)
print(f"  本地预期 (无手续费): ${LOCAL_EXPECTED_PNL:>+10,.2f}  总资产 ${LOCAL_EXPECTED_VALUE:,.2f}")
diff = adj_pnl - LOCAL_EXPECTED_PNL
print(f"  差异:              ${diff:>+10,.2f}")
print("="*70)
if abs(diff) < 1.0:
    print("  ✅ 验证通过：与本地结果误差 < $1")
else:
    print(f"  ❌ 存在差异 {diff:+.2f}，请检查上方明细中 mid 来源标注为[本地兜底]的行")
print("="*70)
