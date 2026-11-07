from datetime import date

import pandas as pd
import pytest

import trade_chart as tc


def _chain(rows):
    """rows: (snapshot_date, expiry, strike, type, mid, delta)"""
    df = pd.DataFrame(rows, columns=["snapshot_date", "expiry", "strike", "type", "mid", "delta"])
    df["option_symbol"] = [
        f"X {e:%y%m%d}{'C' if t == 'Call' else 'P'}{int(k * 1000):08d}"
        for e, k, t in zip(df["expiry"], df["strike"], df["type"])
    ]
    return df


D1, D2 = date(2022, 1, 3), date(2022, 1, 4)
E1, E2 = date(2022, 1, 7), date(2022, 1, 14)


# ── underlying_series ─────────────────────────────────────────────────────────

def test_underlying_uses_put_call_parity_on_nearest_expiry():
    df = _chain([
        # nearest expiry E1: S = C - P + K ; strike 100 has smallest |C-P|
        (D1, E1, 100.0, "Call", 2.30, 0.55), (D1, E1, 100.0, "Put", 1.80, -0.45),
        (D1, E1, 105.0, "Call", 0.40, 0.15), (D1, E1, 105.0, "Put", 4.95, -0.85),
        # farther expiry is ignored
        (D1, E2, 100.0, "Call", 9.00, 0.55), (D1, E2, 100.0, "Put", 1.00, -0.45),
    ])
    s = tc.underlying_series(df)
    assert s.loc[D1] == pytest.approx(100.50)


def test_underlying_falls_back_to_atm_call_strike_without_pairs():
    df = _chain([
        (D2, E1, 98.0, "Call", 3.0, 0.70),
        (D2, E1, 101.0, "Call", 1.0, 0.48),
    ])
    s = tc.underlying_series(df)
    assert s.loc[D2] == pytest.approx(101.0)


def test_underlying_is_sorted_by_date():
    df = _chain([
        (D2, E1, 100.0, "Call", 1.0, 0.5), (D2, E1, 100.0, "Put", 1.0, -0.5),
        (D1, E1, 100.0, "Call", 2.0, 0.5), (D1, E1, 100.0, "Put", 1.0, -0.5),
    ])
    s = tc.underlying_series(df)
    assert list(s.index) == [D1, D2]
    assert list(s.values) == pytest.approx([101.0, 100.0])


# ── build_trade_table ─────────────────────────────────────────────────────────

def _pos(**kw):
    base = dict(ticker="X", symbol="X 220107P00095000", strike=95.0, type="Put",
                expiry=E1, direction=-1, dir_label="Short", qty=1,
                entry_mid=1.20, entry_date=D1)
    base.update(kw)
    return base


def test_trade_table_classifies_outcomes():
    under = pd.Series({D1: 100.0, E1: 101.0, date(2022, 1, 5): 90.0})
    closed = [
        _pos(close_date=E1, close_mid=0.0, realized=120.0),                       # OTM expiry
        _pos(close_date=D2, close_mid=0.50, realized=70.0),                       # bought back early
        _pos(strike=105.0, close_date=E1, close_mid=0.0, realized=120.0),         # put ITM -> assigned
    ]
    opened = [_pos(entry_date=D2)]
    t = tc.build_trade_table(opened, closed, under)

    assert list(t["outcome"]) == ["到期作废", "提前平仓", "被行权", "持仓中"]
    assert list(t["status"]) == ["closed", "closed", "closed", "open"]
    assert t.loc[3, "close_date"] is None
    assert t["id"].tolist() == [1, 2, 3, 4]


def test_trade_table_prefers_account_history_over_spot_guess():
    # spot at expiry says ITM (90 < 95), but the simulator actually expired it
    under = pd.Series({D1: 100.0, E1: 90.0})
    expired = _pos(close_date=E1, close_mid=0.0, realized=120.0)
    assigned = _pos(strike=80.0, symbol="X 220107P00080000",
                    close_date=E1, close_mid=0.0, realized=50.0)
    history = [
        {"date": E1, "action": "到期废弃 (OTM) Short", "symbol": expired["symbol"], "price": 0.0},
        {"date": E1, "action": "Short Put 被行权 - 买入股票", "symbol": "X", "price": 80.0},
    ]
    t = tc.build_trade_table([], [expired, assigned], under, history)
    assert list(t["outcome"]) == ["到期作废", "被行权"]


def test_trade_table_long_itm_expiry_is_settlement_not_expired():
    under = pd.Series({D1: 100.0, E1: 110.0})
    long_call = _pos(symbol="X 220107C00105000", type="Call", strike=105.0,
                     direction=1, dir_label="Long", close_date=E1, close_mid=5.0, realized=380.0)
    history = [{"date": E1, "action": "到期结算 (ITM) Long", "symbol": long_call["symbol"], "price": 5.0}]
    assert list(tc.build_trade_table([], [long_call], under, history)["outcome"]) == ["到期实值结算"]
    # no history: spot says ITM -> Long is settled, not "assigned"
    assert list(tc.build_trade_table([], [long_call], under)["outcome"]) == ["到期实值结算"]


def test_trade_table_sorted_by_entry_date_and_has_label():
    closed = [
        _pos(entry_date=D2, close_date=E1, close_mid=0.0, realized=10.0),
        _pos(entry_date=D1, close_date=E1, close_mid=0.0, realized=20.0),
    ]
    t = tc.build_trade_table([], closed, pd.Series(dtype=float))
    assert list(t["entry_date"]) == [D1, D2]
    assert "Short Put $95" in t.loc[0, "label"]


def test_trade_table_empty():
    t = tc.build_trade_table([], [], pd.Series(dtype=float))
    assert t.empty


# ── contract_series ───────────────────────────────────────────────────────────

def test_contract_series_filters_by_expiry_strike_type_and_window():
    df = _chain([
        (D1, E1, 95.0, "Put", 1.20, -0.30),
        (D2, E1, 95.0, "Put", 0.90, -0.25),
        (E1, E1, 95.0, "Put", 0.00, -0.01),
        (D2, E1, 95.0, "Call", 6.0, 0.80),      # wrong type
        (D2, E1, 96.0, "Put", 1.5, -0.35),       # wrong strike
    ])
    pos = _pos(close_date=D2, close_mid=0.9, realized=30.0)
    s = tc.contract_series(df, pos)
    assert list(s["snapshot_date"]) == [D1, D2]
    assert list(s["mid"]) == pytest.approx([1.20, 0.90])
    assert list(s["delta"]) == pytest.approx([-0.30, -0.25])


# ── figures ───────────────────────────────────────────────────────────────────

def _closed_and_open():
    under = pd.Series({D1: 100.0, D2: 99.0, E1: 101.0, E2: 102.0})
    closed = [_pos(close_date=E1, close_mid=0.0, realized=120.0)]
    opened = [_pos(symbol="X 220114C00110000", type="Call", strike=110.0,
                   expiry=E2, entry_date=D2)]
    return under, tc.build_trade_table(opened, closed, under)


def test_chart_payload_price_and_trades():
    under, t = _closed_and_open()
    p = tc.chart_payload(under, t, as_of=D2 + (E1 - D2) // 2)
    assert p["price"][0] == {"time": "2022-01-03", "value": 100.0}
    assert [tr["id"] for tr in p["trades"]] == [1, 2]
    closed = p["trades"][0]
    assert closed["points"] == [{"time": "2022-01-03", "value": 95.0},
                                {"time": "2022-01-07", "value": 95.0}]
    shapes = [m["shape"] for m in closed["markers"]]
    assert shapes == ["arrowDown", "circle"]           # short entry, expired exit
    assert all(m["position"] == "atPriceMiddle" and m["price"] == 95.0 for m in closed["markers"])
    assert closed["markers"][1]["color"] == tc.COLOR_PROFIT


def test_chart_payload_open_position_ends_at_as_of_not_data_end():
    under, t = _closed_and_open()
    p = tc.chart_payload(under, t, as_of=E1)
    opened = p["trades"][1]
    assert opened["points"][-1]["time"] == "2022-01-07"   # as_of, not E2 / last data day
    assert len(opened["markers"]) == 1                    # entry only


def test_chart_payload_open_position_capped_at_expiry():
    under, t = _closed_and_open()
    p = tc.chart_payload(under, t, as_of=date(2022, 3, 1))
    assert p["trades"][1]["points"][-1]["time"] == "2022-01-14"


def test_chart_payload_default_range_hugs_trades():
    under = pd.Series({date(2021, 1, 4): 1.0, D1: 2.0, E1: 3.0, date(2025, 12, 31): 4.0})
    t = tc.build_trade_table([], [_pos(close_date=E1, close_mid=0.0, realized=1.0)], under)
    p = tc.chart_payload(under, t, as_of=E1)
    assert "2021-06-01" < p["range"]["from"] <= "2022-01-03"
    assert "2022-01-07" <= p["range"]["to"] < "2022-06-01"


def test_chart_payload_default_range_does_not_cross_split_jump():
    # unadjusted 2:1 split right before first trade: pre-split ~200, post ~100
    days = pd.bdate_range("2022-01-03", "2022-02-28").date
    under = pd.Series({d: (200.0 if d < date(2022, 1, 14) else 100.0) for d in days})
    t = tc.build_trade_table([], [_pos(entry_date=date(2022, 1, 18), expiry=date(2022, 2, 4),
                                       close_date=date(2022, 2, 4), close_mid=0.0, realized=1.0)],
                             under)
    p = tc.chart_payload(under, t, as_of=date(2022, 2, 28))
    assert p["price"][0]["time"] == "2022-01-14"      # pre-split prices dropped
    assert p["range"]["from"] == "2022-01-14"


def test_chart_payload_keeps_pre_split_prices_when_trades_before_split():
    days = pd.bdate_range("2022-01-03", "2022-02-28").date
    under = pd.Series({d: (200.0 if d < date(2022, 1, 14) else 100.0) for d in days})
    t = tc.build_trade_table([], [_pos(entry_date=date(2022, 1, 5), expiry=date(2022, 2, 4),
                                       close_date=date(2022, 2, 4), close_mid=0.0, realized=1.0)],
                             under)
    p = tc.chart_payload(under, t, as_of=date(2022, 2, 28))
    assert p["price"][0]["time"] == "2022-01-03"


def test_chart_html_embeds_payload_and_pinned_lib():
    under, t = _closed_and_open()
    html = tc.chart_html(tc.chart_payload(under, t, as_of=E1), "X", dark=False)
    assert "lightweight-charts@5.0.9" in html
    assert '"strike": 95.0' in html or '"value": 95.0' in html


def test_contract_figure_has_mid_and_delta():
    df = _chain([(D1, E1, 95.0, "Put", 1.20, -0.30), (D2, E1, 95.0, "Put", 0.90, -0.25)])
    pos = _pos(close_date=D2, close_mid=0.9, realized=30.0)
    t = tc.build_trade_table([], [pos], pd.Series(dtype=float))
    fig = tc.contract_figure(tc.contract_series(df, pos), t.iloc[0])
    names = [tr.name for tr in fig.data]
    assert "合约 mid" in names and "Delta" in names
