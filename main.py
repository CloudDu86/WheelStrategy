# region imports
from AlgorithmImports import *
import pandas as pd
# endregion

class IndexOptionsWheelSystem(QCAlgorithm):
    """
    指数+期权双轨交易轮动系统
    ─────────────────────────────────────────────────────
    标的  : QQQ（信号基准 + LEAPS）
            TQQQ（车轮策略：CSP → 被行权 → CC → 被Call走 → 重启）
            QQQM（底仓长期持有）
    信号  : QQQ > 200MA  +  RSI < 50  +  阴线  → 卖Put
    防守  : QQQ < 200MA  → 立刻平仓或卖深度ITM Call
    抄底  : VIX ≥ 30     → 动用10%备用金加仓QQQM
    仓位  : 50% QQQM | 10% 现金备用 | 32% 车轮 | 8% LEAPS
    ─────────────────────────────────────────────────────
    """

    # ── 车轮状态机 ─────────────────────────────────────────────────────────────
    SEEK_CSP   = "SEEK_CSP"    # 等待开仓信号
    SHORT_PUT  = "SHORT_PUT"   # 持有空头Put
    LONG_TQQQ  = "LONG_TQQQ"  # 被行权，持有TQQQ正股
    SHORT_CALL = "SHORT_CALL"  # 持有备兑Call

    def Initialize(self):
        # ── 回测区间 ─────────────────────────────────────────────────────────
        # QQQM上市日: 2020-10-13；包含2022熊市和2023-24牛市
        self.SetStartDate(2021, 1, 4)
        self.SetEndDate(2024, 12, 31)
        self.SetCash(100_000)
        self.SetBrokerageModel(
            BrokerageName.InteractiveBrokersBrokerage,
            AccountType.Margin
        )

        # ── 仓位配比 ──────────────────────────────────────────────────────────
        self.ALLOC_QQQM    = 0.50
        self.ALLOC_CASH    = 0.10
        self.ALLOC_WHEEL   = 0.32
        self.ALLOC_LEAPS   = 0.08

        # ── 策略参数 ──────────────────────────────────────────────────────────
        self.RSI_ENTRY     = 50       # RSI低于此值才开仓
        self.VIX_TRIGGER   = 30       # VIX高于此值抄底
        self.CSP_DTE_MIN   = 21       # CSP最小到期天数
        self.CSP_DTE_MAX   = 45       # CSP最大到期天数
        self.CSP_DELTA_TGT = 0.25     # CSP目标Delta（OTM）
        self.CC_DTE_MIN    = 14
        self.CC_DTE_MAX    = 30
        self.CC_DELTA_NORM = 0.30     # 牛市CC Delta
        self.CC_DELTA_BEAR = 0.65     # 熊市CC Delta（深度ITM保护）
        self.LEAPS_DTE_MIN = 500      # LEAPS最小天数（~1.4年）
        self.LEAPS_DTE_MAX = 800      # LEAPS最大天数（~2.2年）
        self.LEAPS_DELTA   = 0.80     # 深度实值

        # ── 证券订阅 ─────────────────────────────────────────────────────────
        for sym in ["QQQ", "TQQQ", "QQQM"]:
            eq = self.AddEquity(sym, Resolution.Daily)
            eq.SetDataNormalizationMode(DataNormalizationMode.Raw)

        tqqq_opt = self.AddOption("TQQQ", Resolution.Daily)
        tqqq_opt.SetFilter(lambda u: u
            .IncludeWeeklys()
            .Strikes(-20, 5)
            .Expiration(self.CSP_DTE_MIN - 7, self.CSP_DTE_MAX + 15))
        self._tqqq_root = tqqq_opt.Symbol

        qqq_opt = self.AddOption("QQQ", Resolution.Daily)
        qqq_opt.SetFilter(lambda u: u
            .Strikes(-15, 1)
            .Expiration(self.LEAPS_DTE_MIN, self.LEAPS_DTE_MAX))
        self._qqq_root = qqq_opt.Symbol

        # VIX指数
        try:
            self.AddIndex("VIX", Resolution.Daily)
            self._has_vix = True
        except Exception:
            self._has_vix = False

        # ── 技术指标 ─────────────────────────────────────────────────────────
        self.sma200 = self.SMA("QQQ", 200, Resolution.Daily)
        self.rsi14  = self.RSI("QQQ", 14,
                               MovingAverageType.Wilders,
                               Resolution.Daily)
        self.SetWarmUp(210, Resolution.Daily)

        # ── 状态变量 ─────────────────────────────────────────────────────────
        self.state            = self.SEEK_CSP
        self.csp_sym          = None    # 当前空头Put的Symbol
        self.cc_sym           = None    # 当前备兑Call的Symbol
        self.n_contracts      = 0       # 车轮合约数量
        self.tqqq_cost        = 0.0     # 被行权时的成本价
        self.leaps_sym        = None
        self.vix_cash_used    = False
        self.initialized      = False

        # P&L统计
        self.premium_total    = 0.0
        self.n_assigned       = 0
        self.n_called_away    = 0
        self.n_csp_expired    = 0
        self.n_cc_expired     = 0
        self.trade_log        = []      # 详细交易记录，存Object Store

        # 前日K线数据（判断阴线）
        self._qqq_prev_open  = None
        self._qqq_prev_close = None

        # ── 图表 ─────────────────────────────────────────────────────────────
        self._init_charts()

        # 每日开盘30分钟后执行主逻辑
        self.Schedule.On(
            self.DateRules.EveryDay("QQQ"),
            self.TimeRules.AfterMarketOpen("QQQ", 30),
            self._run
        )

    # ── 图表初始化 ────────────────────────────────────────────────────────────
    def _init_charts(self):
        c1 = Chart("Wheel")
        c1.AddSeries(Series("State",    SeriesType.Scatter, 0))
        c1.AddSeries(Series("Premium",  SeriesType.Bar,     1))
        self.AddChart(c1)

        c2 = Chart("Signals")
        c2.AddSeries(Series("RSI",      SeriesType.Line, 0))
        c2.AddSeries(Series("VIX",      SeriesType.Line, 1))
        c2.AddSeries(Series("BullMkt",  SeriesType.Scatter, 2))
        self.AddChart(c2)

    # ── 数据接收 ──────────────────────────────────────────────────────────────
    def OnData(self, data: Slice):
        if self.IsWarmingUp:
            return
        # 记录QQQ K线（判断阴线用）
        if data.Bars.ContainsKey("QQQ"):
            b = data.Bars["QQQ"]
            self._qqq_prev_open  = float(b.Open)
            self._qqq_prev_close = float(b.Close)
        self._data = data   # 供_run()使用

    # ── 日常主逻辑（每日调度）────────────────────────────────────────────────
    def _run(self):
        if self.IsWarmingUp or not hasattr(self, "_data"):
            return

        data = self._data

        # 首日建立底仓
        if not self.initialized:
            self._build_core_position()
            self.initialized = True
            return

        # 计算信号
        bull     = self._bull_market()
        entry_ok = self._entry_signal()
        vix      = self._vix_level()

        # VIX抄底
        if vix >= self.VIX_TRIGGER and not self.vix_cash_used:
            self._vix_buy_dip()

        # 车轮状态机
        if   self.state == self.SEEK_CSP:
            if entry_ok:
                self._open_csp(data)

        elif self.state == self.SHORT_PUT:
            self._monitor_csp(data, bull)

        elif self.state == self.LONG_TQQQ:
            self._open_cc(data, bull)

        elif self.state == self.SHORT_CALL:
            self._monitor_cc(data, bull)

        # LEAPS管理
        self._manage_leaps(data)

        # 绘图
        state_num = [self.SEEK_CSP,self.SHORT_PUT,
                     self.LONG_TQQQ,self.SHORT_CALL].index(self.state)
        self.Plot("Wheel",   "State",   state_num)
        self.Plot("Wheel",   "Premium", self.premium_total)
        if self.rsi14.IsReady:
            self.Plot("Signals", "RSI",    float(self.rsi14.Current.Value))
        if vix > 0:
            self.Plot("Signals", "VIX",    vix)
        self.Plot("Signals", "BullMkt", 1 if bull else 0)

    # ── 建立初始底仓 ──────────────────────────────────────────────────────────
    def _build_core_position(self):
        total = float(self.Portfolio.TotalPortfolioValue)
        price = float(self.Securities["QQQM"].Price)
        if price <= 0:
            return
        shares = int(total * self.ALLOC_QQQM / price)
        if shares > 0:
            self.MarketOrder("QQQM", shares)
        self.Log(f"INIT|Cash={total:,.0f}|QQQM×{shares}@{price:.2f}")

    # ── 信号判断 ──────────────────────────────────────────────────────────────
    def _bull_market(self) -> bool:
        if not self.sma200.IsReady:
            return False
        return float(self.Securities["QQQ"].Price) > float(self.sma200.Current.Value)

    def _entry_signal(self) -> bool:
        """三重条件：牛市 + RSI<50 + 阴线"""
        if not self._bull_market():
            return False
        if not self.rsi14.IsReady:
            return False
        if float(self.rsi14.Current.Value) >= self.RSI_ENTRY:
            return False
        if self._qqq_prev_open and self._qqq_prev_close:
            if self._qqq_prev_close >= self._qqq_prev_open:  # 非阴线
                return False
        return True

    def _vix_level(self) -> float:
        if self._has_vix and self.Securities.ContainsKey("VIX"):
            return float(self.Securities["VIX"].Price)
        return 0.0

    # ── VIX抄底 ───────────────────────────────────────────────────────────────
    def _vix_buy_dip(self):
        total  = float(self.Portfolio.TotalPortfolioValue)
        budget = total * self.ALLOC_CASH * 0.5   # 动用备用金的一半
        price  = float(self.Securities["QQQM"].Price)
        if price <= 0:
            return
        shares = int(budget / price)
        if shares > 0:
            self.MarketOrder("QQQM", shares)
            self.vix_cash_used = True
            vix = self._vix_level()
            self.Log(f"VIX≥{vix:.0f}|QQQM+{shares}@{price:.2f}")
            self._record("VIX_DIP", f"QQQM+{shares}", price, 0)

    # ── 开仓CSP ───────────────────────────────────────────────────────────────
    def _open_csp(self, data: Slice):
        if not data.OptionChains.ContainsKey(self._tqqq_root):
            return

        chain   = data.OptionChains[self._tqqq_root]
        today   = self.Time
        tqqq_px = float(self.Securities["TQQQ"].Price)

        # 筛选：Put + DTE范围
        puts = [c for c in chain
                if c.Right == OptionRight.Put
                and self.CSP_DTE_MIN <= (c.Expiry - today).days <= self.CSP_DTE_MAX
                and c.BidPrice > 0]

        if not puts:
            return

        # 选最接近目标Delta的合约
        def delta_dist(c):
            if c.Greeks:
                return abs(abs(float(c.Greeks.Delta)) - self.CSP_DELTA_TGT)
            # 无Greeks时按行权价估算（~92%现价 ≈ 0.25 delta OTM）
            return abs(float(c.Strike) - tqqq_px * 0.92)

        best = min(puts, key=delta_dist)

        # 合约数量：32%资金 ÷ (行权价 × 100)
        total      = float(self.Portfolio.TotalPortfolioValue)
        n_cont     = max(1, int(total * self.ALLOC_WHEEL / (float(best.Strike) * 100)))
        n_cont     = min(n_cont, 15)   # 上限

        ticket = self.Sell(best.Symbol, n_cont)
        if ticket.Status == OrderStatus.Invalid:
            return

        premium = float(best.BidPrice) * n_cont * 100
        self.premium_total += premium
        self.csp_sym        = best.Symbol
        self.n_contracts    = n_cont
        self.state          = self.SHORT_PUT

        delta_str = f"{float(best.Greeks.Delta):.2f}" if best.Greeks else "N/A"
        self.Log(
            f"CSP|TQQQ {float(best.Strike):.1f}P"
            f" {best.Expiry.strftime('%m/%d')}×{n_cont}"
            f"|Δ{delta_str}|${premium:.0f}"
        )
        self._record("CSP_OPEN", str(best.Symbol), float(best.Strike), premium)

    # ── 监控CSP ───────────────────────────────────────────────────────────────
    def _monitor_csp(self, data: Slice, bull: bool):
        if not self.csp_sym:
            self.state = self.SEEK_CSP
            return

        holding = self.Portfolio[self.csp_sym]

        if not holding.Invested:
            # 检查是否持有了TQQQ（被行权）
            tqqq_qty = float(self.Portfolio["TQQQ"].Quantity)
            if tqqq_qty > 0:
                self.tqqq_cost  = float(self.Portfolio["TQQQ"].AveragePrice)
                self.n_assigned += 1
                self.csp_sym    = None
                self.state      = self.LONG_TQQQ
                self.Log(f"ASSIGNED|TQQQ×{tqqq_qty:.0f}@{self.tqqq_cost:.2f}|total={self.n_assigned}")
                self._record("ASSIGNED", "TQQQ", self.tqqq_cost, 0)
            else:
                # Put到期归零
                self.n_csp_expired += 1
                self.csp_sym = None
                self.state   = self.SEEK_CSP
                self._record("CSP_EXP", "worthless", 0, 0)
            return

        # 熊市且深度价内 → 紧急平仓
        if not bull:
            tqqq_px = float(self.Securities["TQQQ"].Price)
            # 获取行权价
            try:
                strike = float(self.Securities[self.csp_sym].StrikePrice)
            except Exception:
                return
            if tqqq_px < strike * 0.88:
                self.Liquidate(self.csp_sym)
                self.csp_sym = None
                self.state   = self.SEEK_CSP
                self.Log(f"BEAR_CLOSE_CSP|TQQQ@{tqqq_px:.2f}")
                self._record("BEAR_CLOSE", "CSP", tqqq_px, 0)

    # ── 开仓CC ────────────────────────────────────────────────────────────────
    def _open_cc(self, data: Slice, bull: bool):
        if not data.OptionChains.ContainsKey(self._tqqq_root):
            return

        tqqq_qty = int(float(self.Portfolio["TQQQ"].Quantity))
        if tqqq_qty <= 0:
            self.state = self.SEEK_CSP
            return

        n_cont = tqqq_qty // 100
        if n_cont <= 0:
            return

        chain   = data.OptionChains[self._tqqq_root]
        today   = self.Time
        tqqq_px = float(self.Securities["TQQQ"].Price)
        tgt_d   = self.CC_DELTA_BEAR if not bull else self.CC_DELTA_NORM

        calls = [c for c in chain
                 if c.Right == OptionRight.Call
                 and self.CC_DTE_MIN <= (c.Expiry - today).days <= self.CC_DTE_MAX
                 and c.BidPrice > 0]

        if not calls:
            return

        def delta_dist(c):
            if c.Greeks:
                return abs(abs(float(c.Greeks.Delta)) - tgt_d)
            return abs(float(c.Strike) - tqqq_px * (1 + (tgt_d - 0.5) * 0.2))

        best    = min(calls, key=delta_dist)
        ticket  = self.Sell(best.Symbol, n_cont)
        if ticket.Status == OrderStatus.Invalid:
            return

        premium = float(best.BidPrice) * n_cont * 100
        self.premium_total += premium
        self.cc_sym  = best.Symbol
        self.state   = self.SHORT_CALL

        mode = "BEAR-ITM" if not bull else "NORM"
        delta_str = f"{float(best.Greeks.Delta):.2f}" if best.Greeks else "N/A"
        self.Log(
            f"CC|TQQQ {float(best.Strike):.1f}C"
            f" {best.Expiry.strftime('%m/%d')}×{n_cont}"
            f"|Δ{delta_str}|${premium:.0f}|{mode}"
        )
        self._record("CC_OPEN", str(best.Symbol), float(best.Strike), premium)

    # ── 监控CC ────────────────────────────────────────────────────────────────
    def _monitor_cc(self, data: Slice, bull: bool):
        if not self.cc_sym:
            tqqq_qty = float(self.Portfolio["TQQQ"].Quantity)
            self.state = self.LONG_TQQQ if tqqq_qty > 0 else self.SEEK_CSP
            return

        holding = self.Portfolio[self.cc_sym]

        if not holding.Invested:
            tqqq_qty = float(self.Portfolio["TQQQ"].Quantity)
            if tqqq_qty <= 0:
                # 股票被Call走
                self.n_called_away += 1
                self.cc_sym = None
                self.state  = self.SEEK_CSP
                self.Log(f"CALLED_AWAY|total={self.n_called_away}|restart wheel")
                self._record("CALLED_AWAY", "TQQQ", float(self.Securities["TQQQ"].Price), 0)
            else:
                # CC到期归零，继续卖新CC
                self.n_cc_expired += 1
                self.cc_sym = None
                self.state  = self.LONG_TQQQ
                self._record("CC_EXP", "worthless", 0, 0)

    # ── LEAPS管理 ─────────────────────────────────────────────────────────────
    def _manage_leaps(self, data: Slice):
        # 检查持仓是否有效
        if self.leaps_sym and not self.Portfolio[self.leaps_sym].Invested:
            self.Log(f"LEAPS expired → will re-enter")
            self.leaps_sym = None

        if self.leaps_sym:
            return  # 已持有，按兵不动

        if not data.OptionChains.ContainsKey(self._qqq_root):
            return

        chain   = data.OptionChains[self._qqq_root]
        today   = self.Time
        total   = float(self.Portfolio.TotalPortfolioValue)
        budget  = total * self.ALLOC_LEAPS

        # 筛选：Call + DTE范围（~2年）
        calls = [c for c in chain
                 if c.Right == OptionRight.Call
                 and self.LEAPS_DTE_MIN <= (c.Expiry - today).days <= self.LEAPS_DTE_MAX
                 and c.AskPrice > 0]

        if not calls:
            return

        # 选Delta最接近0.80的（深度实值）
        def delta_dist(c):
            if c.Greeks:
                return abs(abs(float(c.Greeks.Delta)) - self.LEAPS_DELTA)
            return abs(float(c.Strike) - float(self.Securities["QQQ"].Price) * 0.85)

        best   = min(calls, key=delta_dist)
        ask    = float(best.AskPrice)
        n_cont = max(1, int(budget / (ask * 100)))

        if ask * n_cont * 100 > float(self.Portfolio.Cash) * 0.85:
            n_cont = max(1, int(float(self.Portfolio.Cash) * 0.85 / (ask * 100)))

        if n_cont < 1 or ask <= 0:
            return

        self.Buy(best.Symbol, n_cont)
        self.leaps_sym = best.Symbol
        qqq_px = float(self.Securities["QQQ"].Price)
        delta_str = f"{float(best.Greeks.Delta):.2f}" if best.Greeks else "N/A"

        self.Log(
            f"LEAPS|QQQ {float(best.Strike):.0f}C"
            f" {best.Expiry.strftime('%Y-%m')}×{n_cont}"
            f"|@${ask:.2f}|Δ{delta_str}|QQQ@{qqq_px:.2f}"
        )
        self._record("LEAPS_BUY", str(best.Symbol),
                     float(best.Strike), ask * n_cont * 100)

    # ── 订单事件（捕获行权）──────────────────────────────────────────────────
    def OnOrderEvent(self, ev: OrderEvent):
        if ev.Status == OrderStatus.Filled and ev.IsAssignment:
            self.Log(f"OE|ASSIGNMENT|{ev.Symbol}|qty={ev.FillQuantity}|px={ev.FillPrice:.2f}")

    # ── 交易记录 ──────────────────────────────────────────────────────────────
    def _record(self, action: str, symbol: str, price: float, pnl: float):
        self.trade_log.append({
            "date"     : self.Time.strftime("%Y-%m-%d"),
            "action"   : action,
            "symbol"   : symbol,
            "price"    : round(price, 4),
            "pnl"      : round(pnl, 2),
            "portfolio": round(float(self.Portfolio.TotalPortfolioValue), 2),
            "state"    : self.state,
        })

    # ── 结束汇总 ──────────────────────────────────────────────────────────────
    def OnEndOfAlgorithm(self):
        total   = float(self.Portfolio.TotalPortfolioValue)
        ret_pct = (total / 100_000 - 1) * 100

        self.Log("=" * 55)
        self.Log(f"最终净值    : ${total:>12,.2f}")
        self.Log(f"总收益率    : {ret_pct:>+8.2f}%")
        self.Log(f"权利金总收入: ${self.premium_total:>10,.2f}")
        self.Log(f"被行权次数  : {self.n_assigned}")
        self.Log(f"被Call走次数: {self.n_called_away}")
        self.Log(f"CSP归零次数 : {self.n_csp_expired}")
        self.Log(f"CC归零次数  : {self.n_cc_expired}")
        self.Log(f"最终状态    : {self.state}")
        self.Log("=" * 55)

        # 交易记录存Object Store
        if self.trade_log:
            df  = pd.DataFrame(self.trade_log)
            key = "wheel_trade_log.csv"
            self.ObjectStore.Save(key, df.to_csv(index=False))
            self.Log(f"交易记录已存 Object Store: {key} ({len(df)}条)")
