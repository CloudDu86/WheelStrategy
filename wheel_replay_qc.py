# ─────────────────────────────────────────────────────────────────────────────
# 车轮策略 PnL 验证回测
# 来源流水：2026-04-23T03-11_export.csv（2022 年全年 TQQQ 车轮）
#
# 本地可确认部分（初始资金 $10,000）：
#   Short Put 权利金: +258.50 +273.00 +245.50 +270.50 = +$1,047.50
#   Short Call 权利金: +16 +54 +12 +50 +31.50 +22 +11 = +$196.50
#   权利金合计:       +$1,244.00
#   购股现金流:       -$4,500  (100 股 @ $45，2022-05-20 Put 被行权)
#   期末现金（可确认）: $6,744.00
#   期末持仓:         100 股 TQQQ（未平仓，市价由 QC 决定）
#
# 验证逻辑：比对 QC 现金是否 = $6,744，并打印股票持仓市值供手动核查。
# ─────────────────────────────────────────────────────────────────────────────

from AlgorithmImports import *
from datetime import date

# ── 硬编码开仓流水（仅"开仓 Short"行，到期结算由 QC 自动处理）────────────────
# 格式: (trade_date, opt_type, strike, expiry, qty, local_mid)
TRADE_SCHEDULE = [
    (date(2022,  1, 24), "P", 47.5, date(2022,  2, 18), 1, 2.585),
    (date(2022,  2, 18), "P", 45.0, date(2022,  3, 18), 1, 2.730),
    (date(2022,  3, 18), "P", 45.0, date(2022,  4, 14), 1, 2.455),
    (date(2022,  4, 14), "P", 45.0, date(2022,  5, 20), 1, 2.705),
    (date(2022,  5, 20), "C", 42.5, date(2022,  6, 17), 1, 0.160),
    (date(2022,  6, 17), "C", 39.0, date(2022,  7, 22), 1, 0.540),
    (date(2022,  7, 22), "C", 44.0, date(2022,  8, 19), 1, 0.120),
    (date(2022,  8, 19), "C", 45.0, date(2022,  9, 16), 1, 0.500),
    (date(2022,  9, 16), "C", 35.0, date(2022, 10, 21), 1, 0.315),
    (date(2022, 10, 21), "C", 28.0, date(2022, 11, 18), 1, 0.220),
    (date(2022, 11, 18), "C", 32.0, date(2022, 12, 16), 1, 0.110),
]

LOCAL_EXPECTED_CASH    = 6_744.00   # 期末现金（不含股票），可从流水直接计算
LOCAL_EXPECTED_PREMIUM = 1_244.00   # 权利金合计
LOCAL_STOCK_COST       =    45.00   # 股票成本价（Put 被行权价）


class WheelReplayVerification(QCAlgorithm):

    def Initialize(self):
        self.SetStartDate(2022,  1, 20)
        self.SetEndDate  (2022, 12, 20)
        self.SetCash(10_000)

        equity = self.AddEquity("TQQQ", Resolution.Daily)
        self._tqqq = equity.Symbol

        # 过滤范围：行权价 ±30 档（TQQQ 2022 年从 $65 跌到 $15，需要宽范围），
        # DTE 0–45 天（最长 36 天，留余量）
        option = self.AddOption("TQQQ", Resolution.Daily)
        option.SetFilter(lambda u: u.Strikes(-30, 30).Expiration(0, 45))
        self._opt_canonical = option.Symbol

        self._pending = [
            {"date": d, "type": t, "strike": k, "expiry": exp, "qty": q, "local_mid": m}
            for d, t, k, exp, q, m in TRADE_SCHEDULE
        ]
        self._order_lookup = {}

    # ── 每日数据 ──────────────────────────────────────────────────────────────
    def OnData(self, data):
        today = self.Time.date()

        for trade in list(self._pending):
            if trade["date"] != today:
                continue

            chain = data.OptionChains.get(self._opt_canonical)
            if not chain:
                self.Log(f"[{today}] ⚠️  期权链为空 → "
                         f"{'Put' if trade['type']=='P' else 'Call'} "
                         f"${trade['strike']} exp={trade['expiry']}")
                continue

            right = OptionRight.Put if trade["type"] == "P" else OptionRight.Call

            # 精确匹配行权价 + 到期日
            contract = next(
                (c for c in chain
                 if c.Right == right
                 and abs(c.Strike - trade["strike"]) < 0.01
                 and c.Expiry.date() == trade["expiry"]),
                None,
            )

            # 兜底：同到期日最近行权价
            if contract is None:
                same_exp = [c for c in chain
                            if c.Right == right and c.Expiry.date() == trade["expiry"]]
                if same_exp:
                    contract = min(same_exp, key=lambda c: abs(c.Strike - trade["strike"]))
                    self.Log(f"[{today}] ⚠️  精确合约未找到，最近匹配 strike={contract.Strike:.2f}"
                             f"（期望 {trade['strike']:.2f}）")
                else:
                    expiries = sorted({c.Expiry.date() for c in chain})
                    self.Log(f"[{today}] ❌  找不到 {right} exp={trade['expiry']}  "
                             f"链上到期日：{expiries}")
                    continue

            bid, ask = contract.BidPrice, contract.AskPrice
            mid_qc   = (bid + ask) / 2
            self.Log(
                f"[{today}] 卖出 {'Put' if trade['type']=='P' else 'Call'} "
                f"${contract.Strike:.2f}  exp={trade['expiry']}  "
                f"bid={bid:.3f} ask={ask:.3f} mid_QC={mid_qc:.3f}  "
                f"本地mid={trade['local_mid']:.3f}  差异={mid_qc - trade['local_mid']:+.3f}"
            )

            ticket = self.MarketOrder(contract.Symbol, -trade["qty"])
            self._order_lookup[ticket.OrderId] = trade
            self._pending.remove(trade)

    # ── 成交回报 ──────────────────────────────────────────────────────────────
    def OnOrderEvent(self, orderEvent):
        if orderEvent.Status != OrderStatus.Filled:
            return
        trade = self._order_lookup.get(orderEvent.OrderId)
        tag = (f"  [本地期望 ${trade['local_mid']:.3f}  差异 {orderEvent.FillPrice - trade['local_mid']:+.3f}]"
               if trade else "")
        self.Log(f"✅ 成交: {orderEvent.Symbol.Value}  "
                 f"qty={orderEvent.FillQuantity:+g}  price=${orderEvent.FillPrice:.3f}{tag}")

    # ── 回测结束汇总 ──────────────────────────────────────────────────────────
    def OnEndOfAlgorithm(self):
        pv      = self.Portfolio.TotalPortfolioValue
        cash    = self.Portfolio.Cash
        realized = self.Portfolio.TotalProfit
        unreal  = self.Portfolio.TotalUnrealizedProfit

        cash_diff = cash - LOCAL_EXPECTED_CASH

        self.Log("=" * 65)
        self.Log("  现金对比（可精确验证）")
        self.Log(f"  QC 现金:      ${cash:>10,.2f}  本地预期 ${LOCAL_EXPECTED_CASH:>10,.2f}  差异 {cash_diff:>+.2f}")
        self.Log("-" * 65)
        self.Log("  完整组合（含 TQQQ 持仓市值）")
        self.Log(f"  QC 组合价值:  ${pv:>10,.2f}")
        self.Log(f"  QC 已实现PnL: ${realized:>+10,.2f}")
        self.Log(f"  QC 未实现PnL: ${unreal:>+10,.2f}  (100 股 TQQQ 浮动，成本 ${LOCAL_STOCK_COST:.2f})")
        self.Log("=" * 65)

        if abs(cash_diff) < 1.0:
            self.Log("✅ 现金验证通过：QC 现金与本地误差 < $1")
        else:
            self.Log(f"❌ 现金差异 {cash_diff:+.2f}，请对照上方各笔成交价与本地 mid 的差异")

        # 打印所有剩余持仓
        self.Log("  期末持仓明细：")
        has_pos = False
        for sym, h in self.Portfolio.items():
            if h.Quantity != 0:
                has_pos = True
                self.Log(f"    {sym.Value:30s}  qty={h.Quantity:>6}  "
                         f"成本=${h.AveragePrice:.2f}  市值=${h.HoldingsValue:,.2f}  "
                         f"浮盈=${h.UnrealizedProfit:+,.2f}")
        if not has_pos:
            self.Log("    （无持仓）")
