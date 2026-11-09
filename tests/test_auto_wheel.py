from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

import auto_wheel as aw

D0 = date(2023, 1, 3)
EXP = D0 + timedelta(days=14)


def _day(rows):
    """rows: (type, strike, mid, delta) for one expiry 14 DTE on D0."""
    df = pd.DataFrame(rows, columns=["type", "strike", "mid", "delta"])
    df["snapshot_date"] = D0
    df["expiry"] = EXP
    df["dte"] = 14
    df["volume"] = 100
    df["open_interest"] = 100
    return df


# ── W 底 ──────────────────────────────────────────────────────────────────────

def _w_series():
    # 下跌 → 低点1 (100) → 反弹到 108 → 低点2 (101) → 突破 108
    seg = [130 - i for i in range(30)]                 # 130 → 101
    seg += [100, 100.5, 101, 102]                       # 低点1 = 100
    seg += list(np.linspace(103, 108, 6))               # 颈线 108
    seg += list(np.linspace(107, 102, 6)) + [101, 101.5, 102.5]   # 低点2 = 101
    seg += list(np.linspace(103, 107.5, 6))
    seg += [109]                                        # 突破
    return np.array(seg, dtype=float)


def test_w_bottom_detected_on_breakout_day():
    x = _w_series()
    w = aw.find_w_bottom(x, len(x) - 1, aw.Params())
    assert w is not None
    assert w["low1"] == pytest.approx(100)
    assert w["low2"] == pytest.approx(101)
    assert w["neck"] == pytest.approx(108)


def test_w_bottom_not_triggered_before_breakout():
    x = _w_series()
    assert aw.find_w_bottom(x, len(x) - 2, aw.Params()) is None


def test_no_w_bottom_in_steady_decline():
    x = np.linspace(200, 100, 120)
    assert all(aw.find_w_bottom(x, t, aw.Params()) is None for t in range(20, 120))


# ── Covered Call 选择 ─────────────────────────────────────────────────────────

CALLS = _day([
    ("Call", 105, 2.00, 0.40),
    ("Call", 108, 1.20, 0.30),
    ("Call", 112, 0.50, 0.18),
    ("Call", 120, 0.10, 0.05),
])


def test_call_uses_delta_target_when_above_dynamic_cost():
    row, why = aw.pick_call(CALLS, 100.0, floor=95.0, p=aw.Params())
    assert row["strike"] == 108 and why == "高价卖出"


def test_call_moves_up_to_dynamic_cost():
    row, why = aw.pick_call(CALLS, 100.0, floor=110.0, p=aw.Params())
    assert row["strike"] == 112 and why == "动态成本卖出"


def test_call_waits_when_deep_underwater():
    row, why = aw.pick_call(CALLS, 100.0, floor=115.0, p=aw.Params())
    assert row is None and why == "深套等待"


def test_put_picks_target_delta():
    day = _day([("Put", 95, 1.00, -0.30), ("Put", 92, 0.60, -0.21), ("Put", 88, 0.20, -0.08)])
    row = aw.pick_put(day, 100.0, aw.Params())
    assert row["strike"] == 92


# ── 账户 ──────────────────────────────────────────────────────────────────────

def _row(typ, strike, mid, delta=0.2):
    return pd.Series({"type": typ, "strike": strike, "mid": mid, "delta": delta, "expiry": EXP})


def test_put_assignment_then_call_away_bookkeeping():
    p = aw.Params(capital=50_000, slippage=0.0, order_fee=0.0, per_contract=0.0)
    a = aw.Account(p)
    a.sell_option(D0, _row("Put", 300, 2.0), 1, "低价买入")
    a.settle(EXP, 290.0)                       # ITM → 买入 100 股 @300
    assert a.shares == 100 and a.cash == pytest.approx(50_000 + 200 - 30_000)
    assert a.dynamic_cost() == pytest.approx(298.0)

    exp2 = EXP + timedelta(days=14)
    a.sell_option(EXP, pd.Series({"type": "Call", "strike": 305, "mid": 3.0, "delta": 0.3, "expiry": exp2}), 1, "高价卖出")
    a.settle(exp2, 310.0)                      # ITM → 按 305 卖出
    assert a.shares == 0
    assert a.cash == pytest.approx(50_000 + 200 + 300 + 500)
    assert a.realized == pytest.approx(200 + 300 + 500)
    assert a.equity_cost == 0 and a.premium == 0
    actions = [h["action"] for h in a.history]
    assert "Short Put 被行权 - 买入股票" in actions
    assert "Short Call 被行权 - 卖出股票 (备兑)" in actions


def test_otm_expiry_keeps_premium_net_of_fee():
    p = aw.Params(capital=50_000, slippage=0.01)
    a = aw.Account(p)
    a.sell_option(D0, _row("Put", 300, 2.0), 1, "低价买入")
    a.settle(EXP, 320.0)
    fee = p.fee(1)
    assert a.realized == pytest.approx((2.0 - 0.01) * 100 - fee)
    assert a.cash == pytest.approx(50_000 + (2.0 - 0.01) * 100 - fee)
    assert a.open == [] and len(a.closed) == 1


# ── 早年月度期权 / 期间抽样 / 年度快照 ─────────────────────────────────────────

def test_expiry_falls_back_to_monthly_when_no_weekly():
    day = _day([("Put", 95, 1.0, -0.2)])
    day["dte"] = 33
    day["expiry"] = D0 + timedelta(days=33)
    assert aw.pick_expiry(day, aw.Params()) == D0 + timedelta(days=33)
    day["dte"] = 60
    assert aw.pick_expiry(day, aw.Params()) is None


def test_option_segments_split_on_gap():
    ds = [date(2015, 1, 2) + timedelta(days=i) for i in range(10)]
    ds += [date(2017, 1, 2) + timedelta(days=i) for i in range(10)]
    assert aw.option_segments(ds) == [(0, 9), (10, 19)]


def test_random_periods_fixed_years_stay_inside_segment():
    import random
    ds = list(pd.bdate_range("2012-01-02", "2016-12-30").date) + list(pd.bdate_range("2019-01-02", "2021-12-31").date)
    segs = aw.option_segments(ds)
    per = aw.random_periods(ds, 50, 6, 24, random.Random(1), warmup=0, years=3, segments=segs)
    for s, e in per:
        assert (e - s).days >= 3 * 365 - 5
        assert e <= date(2016, 12, 30)          # 第二段只有 3 年，装不下完整 3 年（从 1/2 开始到 12/31 不足）或刚好装下
        assert not (s < date(2019, 1, 1) <= e)  # 不跨缺口


def test_year_snapshots_three_years():
    idx = list(pd.bdate_range("2015-03-02", "2018-03-02").date)
    n = len(idx)
    spot = np.linspace(100, 130, n)
    eq = np.full(n, 100_000.0)
    eq[100] = 90_000.0                          # 第 1 年内 -10% 回撤
    curve = pd.DataFrame({
        "spot": spot, "equity": eq, "shares": 100, "cash": 60_000.0,
        "cost_basis": 110.0, "dyn_cost": 105.0, "collateral": 0.0, "stuck": False,
        "premium_cum": np.arange(n, dtype=float), "fees_cum": 0.0,
    }, index=idx)
    out = aw.year_snapshots(curve, aw.Params(capital=100_000))
    assert out["y1_dd"] == pytest.approx(-0.10)
    assert out["y2_dd"] == pytest.approx(0.0)
    assert out["y1_max_dd"] == pytest.approx(-0.10) and out["y3_max_dd"] == pytest.approx(-0.10)
    assert out["y3_date"] == "2018-03-02"
    assert "y4_date" not in out
    assert out["y2_shares"] == 100 and out["y2_dyn_cost"] == 105.0
