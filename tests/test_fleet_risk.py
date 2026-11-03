"""Money tests: sizing floors, the broker-day clock, book limits in USD, and exit geometry.

Every case here is a defect a critic proved by running the old code: rounding up, the local-clock
day boundary, deposits masking a loss, volume caps ignoring risk, and exits measured from the live
ATR instead of the trade's own levels.
"""
from __future__ import annotations

import datetime as dt
import pathlib
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fleet import risk, strategy  # noqa: E402
from fleet.broker import BrokerSymbol  # noqa: E402
from fleet.config import Account, PortfolioLimits  # noqa: E402


def make_account(mode: str = "demo", **overrides) -> Account:
    account = Account(name="test", login=1, server="Srv", mode=mode)
    for key, value in overrides.items():
        setattr(account.risk, key, value)
    return account


def make_spec(**overrides) -> BrokerSymbol:
    data = dict(name="EURUSD", digits=5, point=0.00001, tick_size=0.00001, tick_value=1.0,
                volume_min=0.01, volume_max=100.0, volume_step=0.01, stops_level=0,
                freeze_level=0, spread=5, filling=3)
    data.update(overrides)
    return BrokerSymbol(**data)


# --- sizing: never round up ------------------------------------------------------------------

def test_sizing_rounds_down_not_up() -> None:
    account = make_account(risk_per_trade_pct=0.25, max_lots=10.0)
    spec = make_spec()
    # 25 USD / (200 ticks * 1 USD) = 0.125 lots; floor to 0.12, never 0.13
    volume = risk.size_for(account, spec, stop_distance=0.0020, balance=10_000)
    assert volume == pytest.approx(0.12)
    assert spec.distance_money(0.0020, volume) <= 25.0


def test_sizing_skips_when_minimum_lot_risks_more_than_allowed() -> None:
    account = make_account(risk_per_trade_pct=0.20)
    spec = make_spec(tick_value=1.0, volume_min=0.01)
    # 1000 USD, 0.2% = 2 USD; 0.01 lot with a 29 pip stop risks 2.90 USD > 2.00
    assert risk.size_for(account, spec, stop_distance=0.0029, balance=1000) == 0.0


def test_sizing_respects_max_lots_and_min_lots() -> None:
    account = make_account(risk_per_trade_pct=5.0, max_lots=0.05)
    assert risk.size_for(account, make_spec(), 0.0001, 100_000) == 0.05
    account.risk.min_lots = 0.10
    assert risk.size_for(account, make_spec(), 0.0001, 100_000) == 0.0


def test_risk_profile_validation_catches_impossible_arithmetic() -> None:
    account = make_account(mode="real", risk_per_trade_pct=0.30, daily_stop_pct=0.50,
                           max_open_positions=2)
    problems = risk.validate_risk_profile(account)
    assert problems and "не помещаются" in problems[0]
    account.risk.max_open_positions = 1
    assert risk.validate_risk_profile(account) == []


# --- the day: broker clock, deposits, zero balance -------------------------------------------

def test_day_state_rolls_over_on_the_broker_day() -> None:
    state = risk.DayState(day="2026-11-03", start_balance=1000.0)
    state.rollover(1200.0, "2026-11-03")
    assert state.start_balance == 1000.0
    state.rollover(1200.0, "2026-11-04")
    assert state.start_balance == 1200.0 and state.stopped is False


def test_deposit_does_not_mask_a_loss() -> None:
    state = risk.DayState(day="2026-11-03", start_balance=1000.0)
    state.net_flow = 100.0  # operator deposited 100 during the day
    # equity 1096 = 1000 + 100 deposit - 4 loss -> -0.4%, not +9.6%
    assert state.daily_pct(1096.0) == pytest.approx(-0.4)


def test_zero_start_balance_stops_instead_of_disabling_the_stop() -> None:
    state = risk.DayState(day="2026-11-03", start_balance=0.0)
    assert state.daily_pct(50.0) == float("-inf")


def test_withdrawal_does_not_fake_a_stop() -> None:
    state = risk.DayState(day="2026-11-03", start_balance=1000.0)
    state.net_flow = -500.0
    assert state.daily_pct(510.0) == pytest.approx(1.0)


# --- book limits in money --------------------------------------------------------------------

def test_book_stops_on_daily_loss() -> None:
    allowed, reason = risk.can_open_portfolio(PortfolioLimits(daily_stop_pct=2.0), 10_000,
                                              0.0, 10.0, [], {}, day_pct=-2.5)
    assert not allowed and "портфеля" in reason


def test_book_limits_total_risk_in_money() -> None:
    limits = PortfolioLimits(max_total_risk_pct=2.0)
    allowed, reason = risk.can_open_portfolio(limits, 10_000, 190.0, 20.0, [], {}, day_pct=0.0)
    assert not allowed and "риска" in reason
    allowed, _ = risk.can_open_portfolio(limits, 10_000, 100.0, 20.0, [], {}, day_pct=0.0)
    assert allowed


def test_book_limits_gold_exposure_by_signed_volume() -> None:
    limits = PortfolioLimits(max_gold_lots=0.30)
    long_gold = [types.SimpleNamespace(symbol="XAUUSD", volume=0.35, type=0)]
    allowed, reason = risk.can_open_portfolio(limits, 10_000, 0.0, 0.0, long_gold, {},
                                             day_pct=0.0)
    assert not allowed and "золот" in reason.lower()
    long_and_short = long_gold + [types.SimpleNamespace(symbol="XAUUSD", volume=0.20, type=1)]
    allowed, _ = risk.can_open_portfolio(limits, 10_000, 0.0, 0.0, long_and_short, {},
                                         day_pct=0.0)
    assert allowed  # net exposure is flat


# --- exits measured from the trade's own levels ----------------------------------------------

class FakePosition:
    def __init__(self, side: str = "buy", price_open: float = 1.10000, sl: float = 0.0,
                 tp: float = 0.0, when: float = 0.0):
        self.type = 0 if side == "buy" else 1
        self.price_open = price_open
        self.sl = sl
        self.tp = tp
        self.time = when
        self.ticket = 1


def patch_quotes(atr_value: float, price: float) -> None:
    strategy.broker.atr = lambda *a, **k: atr_value
    strategy.mt5 = types.SimpleNamespace(
        TIMEFRAME_H1=1, POSITION_TYPE_BUY=0,
        symbol_info_tick=lambda name: types.SimpleNamespace(bid=price, ask=price))


def test_take_profit_uses_the_trade_target_not_the_live_atr() -> None:
    account = make_account(stop_atr=1.0, take_atr=2.0)
    # opened with 1.0 ATR stop / 2.0 ATR target; now the live ATR halved — the target must not move
    position = FakePosition(price_open=1.10000, sl=1.09900, tp=1.10200)
    patch_quotes(atr_value=0.0005, price=1.10210)
    assert strategy.exit_reason(account, position, make_spec(), peak=0.0, atr_entry=0.0010) == \
        "тейк 2.00 ATR"


def test_stop_uses_the_trade_stop() -> None:
    account = make_account(stop_atr=1.0, take_atr=5.0)
    position = FakePosition(price_open=1.10000, sl=1.09900, tp=1.10500)
    patch_quotes(atr_value=0.0010, price=1.09890)
    assert "стоп" in strategy.exit_reason(account, position, make_spec(), peak=0.0,
                                          atr_entry=0.0010)


def test_giveback_protects_a_small_winner() -> None:
    account = make_account(stop_atr=1.0, take_atr=5.0, giveback_fraction=0.30)
    # peak was +0.9 ATR (0.0009), price is back at the entry: old code returned "" here
    position = FakePosition(price_open=1.10000, sl=1.09900, tp=1.10500)
    patch_quotes(atr_value=0.0010, price=1.10002)
    reason = strategy.exit_reason(account, position, make_spec(), peak=0.0009,
                                  atr_entry=0.0010)
    assert reason == "фиксация после отката от пика"


def test_real_account_takes_profit_earlier_than_demo() -> None:
    demo = make_account(mode="demo", stop_atr=1.0, take_atr=2.6)
    real = make_account(mode="real", stop_atr=1.0, take_atr=2.6, take_atr_real=1.8)
    position = FakePosition(price_open=1.10000, sl=1.09900, tp=1.10260)
    patch_quotes(atr_value=0.0010, price=1.10190)  # +1.9 ATR
    assert strategy.exit_reason(demo, position, make_spec(), 0.0, 0.0010) == ""
    assert "тейк" in strategy.exit_reason(real, position, make_spec(), 0.0, 0.0010)


def test_break_even_moves_stop_to_entry_after_partial_profit() -> None:
    account = make_account(break_even_atr=0.4)
    position = FakePosition(price_open=1.10000, sl=1.09950)
    patch_quotes(atr_value=0.0010, price=1.10060)
    level = strategy.break_even_level(account, position, make_spec(), atr_entry=0.0010)
    assert level is not None and level >= 1.10000


def test_time_exit() -> None:
    import time as _time

    account = make_account(max_hold_minutes=30)
    position = FakePosition(price_open=1.10000, sl=1.09900, tp=1.10500,
                            when=_time.time() - 3600)
    patch_quotes(atr_value=0.0010, price=1.10010)
    assert "время вышло" in strategy.exit_reason(account, position, make_spec(), 0.0, 0.0010)
