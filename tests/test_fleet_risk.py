"""Behavioural tests for the money logic: sizing, exits, day stops and portfolio caps.

MT5 itself is replaced with tiny stand-ins, so these tests pin the decisions that would cost real
money if they regressed — without needing a terminal or a funded account.
"""
from __future__ import annotations

import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fleet import risk, strategy  # noqa: E402
from fleet.broker import BrokerSymbol  # noqa: E402
from fleet.config import Account, PortfolioLimits  # noqa: E402


def make_account(**risk_overrides) -> Account:
    account = Account(name="test", login=1, server="Srv")
    for key, value in risk_overrides.items():
        setattr(account.risk, key, value)
    return account


def make_spec(**overrides) -> BrokerSymbol:
    data = dict(name="EURUSD", digits=5, point=0.00001, tick_size=0.00001, tick_value=1.0,
                volume_min=0.01, volume_max=100.0, volume_step=0.01, stops_level=0,
                freeze_level=0, spread=5, filling=1)
    data.update(overrides)
    return BrokerSymbol(**data)


# --- sizing ---------------------------------------------------------------------------------

def test_sizing_never_exceeds_the_risk_allowance() -> None:
    account = make_account(risk_per_trade_pct=0.25)
    spec = make_spec()
    volume = risk.size_for(account, spec, stop_distance=0.0020, balance=10_000)
    # 25 USD of risk, 200 ticks * 1 USD/lot -> 0.125 lots, capped by step to 0.12 or 0.13
    loss = (0.0020 / spec.tick_size) * spec.tick_value * volume
    assert 0 < volume <= 0.13
    assert loss <= 25 * 1.5


def test_sizing_skips_when_min_lot_risks_too_much() -> None:
    account = make_account(risk_per_trade_pct=0.1)
    spec = make_spec(tick_value=1.0)
    # 100 USD balance, 0.1% = 0.10 USD; one 0.01 lot with a 2000-point stop risks 20 USD
    assert risk.size_for(account, spec, stop_distance=0.02, balance=100) == 0.0


def test_sizing_respects_max_lots() -> None:
    account = make_account(risk_per_trade_pct=5.0, max_lots=0.05)
    spec = make_spec()
    assert risk.size_for(account, spec, stop_distance=0.0001, balance=100_000) == 0.05


def test_sizing_zero_on_bad_inputs() -> None:
    account = make_account()
    assert risk.size_for(account, make_spec(), stop_distance=0.0, balance=1000) == 0.0
    assert risk.size_for(account, make_spec(tick_value=0.0), 0.001, 1000) == 0.0
    assert risk.size_for(account, make_spec(), 0.001, 0.0) == 0.0


# --- exits ----------------------------------------------------------------------------------

class FakePosition:
    def __init__(self, side="buy", price_open=1.10000, time=0.0):
        self.type = 0 if side == "buy" else 1
        self.price_open = price_open
        self.time = time


def _patch_quotes(monkeypatch_target, atr_value: float, price: float):
    strategy.broker.atr = lambda *args, **kwargs: atr_value
    strategy.mt5 = types.SimpleNamespace(
        TIMEFRAME_H1=1,
        POSITION_TYPE_BUY=0,
        symbol_info_tick=lambda name: types.SimpleNamespace(bid=price, ask=price),
    )


def test_take_profit_fires_at_take_atr() -> None:
    account = make_account(stop_atr=1.0, take_atr=2.0)
    _patch_quotes(None, atr_value=0.0010, price=1.1021)
    reason = strategy.exit_reason(account, FakePosition(price_open=1.1000), make_spec(), peak=0.0)
    assert reason == "тейк по ATR"


def test_hard_stop_fires_beyond_stop_atr() -> None:
    account = make_account(stop_atr=1.0, take_atr=2.0)
    _patch_quotes(None, atr_value=0.0010, price=1.0988)
    reason = strategy.exit_reason(account, FakePosition(price_open=1.1000), make_spec(), peak=0.0)
    assert reason == "жёсткий стоп по ATR"


def test_giveback_protects_a_winner() -> None:
    account = make_account(stop_atr=1.0, take_atr=5.0, trail_arm_atr=0.5,
                           giveback_fraction=0.3)
    # peak gain was 0.0018, now 0.0012 -> giveback 0.0006 > 0.3 * 0.0018
    _patch_quotes(None, atr_value=0.0010, price=1.1012)
    reason = strategy.exit_reason(account, FakePosition(price_open=1.1000), make_spec(),
                                  peak=0.0018)
    assert reason == "фиксация после отката от пика"


def test_time_exit_when_holding_too_long() -> None:
    import time as _time

    account = make_account(max_hold_minutes=30)
    _patch_quotes(None, atr_value=0.0010, price=1.1001)
    position = FakePosition(price_open=1.1000, time=_time.time() - 3600)
    assert "время вышло" in strategy.exit_reason(account, position, make_spec(), peak=0.0)


def test_nothing_fires_inside_the_channel() -> None:
    account = make_account(stop_atr=1.0, take_atr=5.0, trail_arm_atr=2.0)
    _patch_quotes(None, atr_value=0.0010, price=1.1003)
    assert strategy.exit_reason(account, FakePosition(price_open=1.1000), make_spec(),
                                peak=0.0003) == ""


# --- day state and portfolio -----------------------------------------------------------------

def test_day_state_rollover_and_stop() -> None:
    state = risk.DayState(day=__import__("datetime").date(2020, 1, 1), start_balance=1000.0,
                          realized=5.0)
    state.rollover(1200.0)
    assert state.start_balance == 1200.0
    assert state.realized == 0.0
    assert state.stopped is False
    assert round(state.daily_pct(1188.0), 2) == -1.0


def test_portfolio_stops_on_daily_loss() -> None:
    limits = PortfolioLimits(daily_stop_pct=2.0)
    allowed, reason = risk.can_open_portfolio(limits, [], [], day_pct=-2.5)
    assert not allowed and "портфеля" in reason


def test_portfolio_caps_gold_and_position_count() -> None:
    limits = PortfolioLimits(max_gold_lots=0.20, max_open_positions=2)
    gold = [types.SimpleNamespace(symbol="XAUUSD", volume=0.25)]
    allowed, reason = risk.can_open_portfolio(limits, [], gold, day_pct=0.0)
    assert not allowed and "золот" in reason.lower()
    many = [types.SimpleNamespace(symbol="EURUSD", volume=0.01) for _ in range(3)]
    allowed, reason = risk.can_open_portfolio(limits, [], many, day_pct=0.0)
    assert not allowed and "позиций" in reason


def test_portfolio_allows_a_healthy_book() -> None:
    limits = PortfolioLimits()
    book = [types.SimpleNamespace(symbol="EURUSD", volume=0.10)]
    allowed, reason = risk.can_open_portfolio(limits, [], book, day_pct=-0.2)
    assert allowed and reason == ""
