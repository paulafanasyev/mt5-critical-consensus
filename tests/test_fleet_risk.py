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

from fleet import broker, risk, strategy, worker  # noqa: E402
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


# --- the broker's two money statements, settled by its own calculator -------------------------
# Measured 2026-11-05 on MetaQuotes-Demo (login 114929502), one lot over a price distance, per price
# unit: XAUUSD tick says 10, contract says 100, `order_calc_profit` answers 100. XAGUSD 500 / 5000 /
# 5000. EURUSD 100000 / 100000 / 100000. USDJPY 633.24 / 100000 (в иенах) / 634.53. The calculator is
# the only one of the three stated in the account's own money, so it is the one that decides.

def make_gold_spec(**overrides) -> BrokerSymbol:
    data = dict(name="XAUUSD", digits=2, point=0.01, tick_size=0.01, tick_value=0.1,
                volume_min=0.01, volume_max=100.0, volume_step=0.01, stops_level=0,
                freeze_level=0, spread=13, filling=3,
                contract_size=100.0, profit_currency="USD")
    data.update(overrides)
    return BrokerSymbol(**data)


def calculating(per_unit: float):
    """A terminal whose own calculator answers `per_unit` for one lot over one price unit."""
    return lambda spec: (per_unit, "")


def refusing(why: str = "расчёт не отвечен"):
    return lambda spec: (0.0, why)


def test_the_calculator_is_what_chooses_the_ruler() -> None:
    spec = broker.reconcile_tick_value(make_gold_spec(), "USD", probe=calculating(100.0))
    assert spec.tick_value_source == "contract"
    assert spec.tick_value == pytest.approx(1.0)
    assert spec.tick_value_raw == pytest.approx(0.1)  # the field the broker published, kept
    assert spec.cash_divergence == pytest.approx(0.9)
    assert spec.distance_money(1.0, 1.0) == pytest.approx(100.0)


def test_a_calculator_that_repeats_the_tick_keeps_the_tick() -> None:
    # The same diverging fields, but the broker's money math says 10: then the contract is the wrong
    # field, and following it would have multiplied this account's risk by ten.
    raw = make_gold_spec()
    spec = broker.reconcile_tick_value(raw, "USD", probe=calculating(10.0))
    assert spec.tick_value_source == "tick-proven"
    assert spec.tick_value == pytest.approx(0.1)
    assert spec.cash_divergence == pytest.approx(0.9)


def test_a_cent_account_is_not_enlarged_by_a_dollar_contract() -> None:
    """BLOCKER: on `currency_scale: 0.01` the tick field is cents and the contract is still dollars.

    A 100-times difference between them is units, not a broken broker. The calculator answers in
    cents like the balance, so it repeats the tick and nothing is rewritten.
    """
    cents = make_gold_spec(tick_value=10.0)  # 1000 cents per price unit per lot, the true answer
    spec = broker.reconcile_tick_value(cents, "USD", probe=calculating(1000.0))
    assert spec.tick_value_source == "tick-proven"
    assert spec.tick_value == pytest.approx(10.0)
    account = make_account(risk_per_trade_pct=0.25, max_lots=100.0, min_lots=0.01)
    assert risk.size_for(account, spec, 48.16, 9921261.0) == pytest.approx(
        risk.size_for(account, cents, 48.16, 9921261.0))


def test_a_corrupt_contract_does_not_enlarge_a_lot() -> None:
    """BLOCKER: the correction may only ever follow a proof, and a proof is the calculator."""
    lying = make_gold_spec(tick_value=1.0, contract_size=10.0)  # tick right, contract a tenth
    spec = broker.reconcile_tick_value(lying, "USD", probe=calculating(100.0))
    assert spec.tick_value_source == "tick-proven"
    assert spec.tick_value == pytest.approx(1.0)
    account = make_account(risk_per_trade_pct=0.25, max_lots=100.0, min_lots=0.01)
    assert risk.size_for(account, spec, 48.16, 99212.61) == pytest.approx(
        risk.size_for(account, lying, 48.16, 99212.61))


def test_an_instrument_neither_field_can_price_is_kept_out_of_the_entries() -> None:
    for blind in (refusing(),):
        spec = broker.reconcile_tick_value(make_gold_spec(), "USD", probe=blind)
        assert spec.tick_value_source == "unproven"
        assert spec.tick_value == pytest.approx(0.1)  # nothing was rewritten
        assert spec.cash_probe_error
    # A calculator that answers a number matching neither field is the same refusal, not a coin toss.
    odd = broker.reconcile_tick_value(make_gold_spec(), "USD", probe=calculating(55.0))
    assert odd.tick_value_source == "unproven"
    assert "ни на тик" in odd.cash_probe_error


def test_silence_from_the_calculator_costs_a_sane_broker_nothing() -> None:
    agree = make_gold_spec(tick_value=1.0)
    spec = broker.reconcile_tick_value(agree, "USD", probe=refusing())
    assert spec.tick_value == pytest.approx(1.0)  # the ruler is the tick, exactly as before
    # ... and it is called what it is: a ruler nobody was able to check, not one the broker proved.
    assert spec.tick_value_source == "tick-unchecked"
    # Half a percent of rounding is not a broken field either.
    almost = make_gold_spec(tick_value=0.995)
    assert broker.reconcile_tick_value(almost, "USD",
                                       probe=refusing()).tick_value_source == "tick-unchecked"


def test_a_pair_paying_in_another_currency_is_confirmed_not_guessed() -> None:
    # USDJPY: the contract is 100 000 yen and the tick field is 633 dollars. Comparing them as if they
    # were the same money would call a correct broker broken and cut the lot by 158 times; the
    # calculator answers in dollars and repeats the tick.
    jpy = make_gold_spec(name="USDJPY", digits=3, point=0.001, tick_size=0.001,
                         tick_value=0.633, contract_size=100000.0, profit_currency="JPY")
    spec = broker.reconcile_tick_value(jpy, "USD", probe=calculating(634.53))
    assert spec.tick_value_source == "tick"
    assert spec.tick_value == pytest.approx(0.633)
    assert spec.cash_divergence == 0.0  # not in question, so not claimed as a defect


def test_the_calculator_is_asked_once_per_spec_and_never_of_a_missing_contract() -> None:
    asked: list[str] = []

    def spy(spec):
        asked.append(spec.name)
        return (100.0, "")

    blind = make_gold_spec(contract_size=0.0)
    unchecked = broker.reconcile_tick_value(blind, "USD", probe=spy)
    assert unchecked.tick_value_source == "tick-unchecked"
    assert "не опубликовал контракт" in unchecked.cash_probe_error
    assert asked == []  # an unpublished contract is not an opinion about the tick
    spec = broker.reconcile_tick_value(make_gold_spec(), "USD", probe=spy)
    assert spec.tick_value == pytest.approx(1.0)
    assert asked == ["XAUUSD"]
    # An account that never named its currency still gets the calculator's verdict, not a guess.
    quiet = broker.reconcile_tick_value(make_gold_spec(), "", probe=calculating(10.0))
    assert quiet.tick_value_source == "tick"
    assert quiet.cash_divergence == 0.0


def test_symbol_builds_the_spec_the_calculator_needs_to_arbitrate() -> None:
    """The wiring: `symbol()` reads the contract and the profit currency and lets the calculator pick.

    Without this the whole rule can be deleted from `symbol()` and every unit test above still passes.
    """
    info = types.SimpleNamespace(
        digits=2, point=0.01, trade_tick_size=0.01, trade_tick_value=0.1, volume_min=0.01,
        volume_max=100.0, volume_step=0.01, trade_stops_level=0, trade_freeze_level=0, spread=13,
        filling_mode=3, visible=True, name="XAUUSD", trade_contract_size=100.0,
        currency_profit="USD")
    real = broker.mt5
    seen: list[tuple] = []

    class FakeMT5:
        ORDER_TYPE_BUY = 0

        @staticmethod
        def symbol_info(name):
            return info

        @staticmethod
        def symbol_info_tick(name):
            return types.SimpleNamespace(bid=4000.00, ask=4000.13)

        @staticmethod
        def account_info():
            return types.SimpleNamespace(currency="USD")

        @staticmethod
        def order_calc_profit(action, symbol, volume, price_open, price_close):
            seen.append((symbol, volume, price_open, price_close))
            return (price_close - price_open) * 100.0 * volume

    broker.mt5 = FakeMT5
    try:
        spec = broker.symbol("XAUUSD")
    finally:
        broker.mt5 = real
    assert spec is not None
    assert seen, "the terminal's calculator was never asked"
    assert spec.tick_value_source == "contract"
    assert spec.tick_value == pytest.approx(1.0)
    assert spec.calc_per_unit == pytest.approx(100.0)


def test_a_gold_lot_is_sized_to_the_loss_the_broker_charges() -> None:
    account = make_account(risk_per_trade_pct=0.25, max_lots=0.10, min_lots=0.01)
    raw = make_gold_spec()
    corrected = broker.reconcile_tick_value(raw, "USD", probe=calculating(100.0))
    stop, balance = 48.16, 99212.61
    # The defect in one line: the tick field sizes this lot ten times too big and the cap hides it.
    assert risk.size_for(account, raw, stop, balance) == pytest.approx(0.1)
    volume = risk.size_for(account, corrected, stop, balance)
    assert volume == pytest.approx(0.05)
    loss = corrected.distance_money(stop, volume)
    # Step rounding gives a little of the budget back; what must never happen is the lot costing more
    # than the 0.25 % promised, which is the tick field's answer: 10 x 48.16 = 481.6 $ = 0.49 %.
    assert balance * 0.25 / 100 - 48.16 < loss <= balance * 0.25 / 100
    # And the ruler the lot was measured with is the one the broker's calculator charges.
    assert loss == pytest.approx(corrected.calc_per_unit * stop * volume, rel=1e-9)


def test_the_journal_names_the_ruler_and_its_direction() -> None:
    """A divergence is quoted with its sign: which field is the bigger money, and by how much."""
    contracted = broker.reconcile_tick_value(make_gold_spec(), "USD", probe=calculating(100.0))
    finger, detail = worker._cash_ruler(contracted)
    assert finger.startswith("contract:")
    assert "лот по контракту в 10.00 раз меньше лота по тик-оценке" in detail
    assert "расчёт брокера 100" in detail
    # The opposite direction, which the old wording called "меньше" as well: here the contract is the
    # smaller money, so following it makes the lot bigger and the note has to say "больше".
    bigger = broker.reconcile_tick_value(make_gold_spec(tick_value=10.0, contract_size=100.0),
                                         "USD", probe=calculating(100.0))
    assert bigger.tick_value == pytest.approx(1.0)
    _finger, upward = worker._cash_ruler(bigger)
    assert "в 10.00 раз больше" in upward
    # A broker that repeats the tick says nothing at all when nothing contradicted it.
    silent = make_gold_spec(tick_value=1.0)
    assert worker._cash_ruler(broker.reconcile_tick_value(silent, "USD",
                                                          probe=calculating(100.0))) is None
    # The other proof — the calculator repeated the tick while the published contract disagreed — is
    # said out loud, and says which field it refused.
    proven = broker.reconcile_tick_value(make_gold_spec(tick_value=1.0, contract_size=10.0),
                                         "USD", probe=calculating(100.0))
    assert proven.tick_value_source == "tick-proven"
    finger, quiet_word = worker._cash_ruler(proven)
    assert finger.startswith("tick-proven:")
    assert "подтвердил тик-оценку" in quiet_word


def test_a_ruler_nobody_checked_is_never_printed_as_a_checked_one() -> None:
    """BLOCKER round 2: `risk_basis: "tick"` meant both "the calculator repeated the tick" and "the
    calculator could not be asked at all". Only the first is a proof, and a lot is cut from it.

    The ruler itself does not change in either case; what changes is whether the operator is allowed to
    read the journal as though the broker had vouched for the number.
    """
    unchecked = broker.reconcile_tick_value(make_gold_spec(tick_value=1.0), "USD", probe=refusing())
    assert unchecked.tick_value_source == "tick-unchecked"
    finger, word = worker._cash_ruler(unchecked)
    assert "не подтвердил" in word and finger.startswith("tick-unchecked:")
    # The proof of the same tick by the same broker's calculator stays silent in the log.
    proved = broker.reconcile_tick_value(make_gold_spec(tick_value=1.0), "USD",
                                         probe=calculating(100.0))
    assert proved.tick_value_source == "tick" and worker._cash_ruler(proved) is None


def test_a_cent_account_named_in_its_own_currency_is_neither_frozen_nor_flattered() -> None:
    """A cent account says `USCent` while the instrument pays in `USD`: different words, same money.

    `comparable` is false for it, so an unanswered calculator must not freeze the symbol (that would
    be a factor-of-100000 no-trade) — and must not be written up as a proved ruler either.
    """
    cents = make_gold_spec(tick_value=10.0)
    spec = broker.reconcile_tick_value(cents, "USCent", probe=refusing())
    assert spec.tick_value_source == "tick-unchecked"
    assert spec.tick_value == pytest.approx(10.0)
    assert spec.cash_divergence == 0.0
    account = make_account(risk_per_trade_pct=0.25, max_lots=100.0, min_lots=0.01)
    assert risk.size_for(account, spec, 48.16, 9921261.0) == pytest.approx(
        risk.size_for(account, cents, 48.16, 9921261.0))


def test_the_calculator_is_probed_at_the_lots_this_broker_accepts() -> None:
    """A contract whose minimum lot is above one lot used to be unprobeable — and then blocked for good.

    Clipping the probe at 1.0 answered 0.0 for a symbol with `volume_min: 5`, so the calculator was
    never asked; on a symbol whose two fields disagree that silence is the very thing `unproven`
    refuses to trade on, and the instrument would stay shut whatever the broker published.
    """
    big = make_gold_spec(volume_min=5.0, volume_step=1.0, volume_max=50.0)
    assert broker._probe_volume(big) == pytest.approx(5.0)
    assert broker._probe_volume(make_gold_spec()) == pytest.approx(1.0)
    # A ceiling under one lot is still probed at the ceiling, and an inconsistent pair of limits
    # (min above max) answers 0.0 — the calculator is not asked a question the broker bans.
    assert broker._probe_volume(make_gold_spec(volume_min=0.01, volume_max=0.5)) == pytest.approx(0.5)
    assert broker._probe_volume(make_gold_spec(volume_min=10.0, volume_max=5.0)) == 0.0


class _Calc:
    """A terminal whose calculator answers from a script, one entry per call.

    `None` is the broker's own way of saying nothing, so a list of replies is the whole difference
    between a flake and a silence — and the cost of the second call is what makes retrying right:
    measured 2026-11-05 on login 114929502, 200 probes of this shape cost 0.009–0.121 ms apiece and
    none of them was dropped.
    """

    ORDER_TYPE_BUY = 0

    def __init__(self, replies: list) -> None:
        self.replies = replies
        self.asked: list[float] = []

    @staticmethod
    def symbol_info_tick(name):
        return types.SimpleNamespace(bid=4000.00, ask=4000.13)

    def order_calc_profit(self, action, symbol, volume, price_open, price_close):
        self.asked.append(volume)
        reply = self.replies[min(len(self.asked), len(self.replies)) - 1]
        if reply is None:
            return None
        return (price_close - price_open) * reply * volume


def _probed(spec: BrokerSymbol, replies: list) -> tuple[float, str, list]:
    real = broker.mt5
    terminal = _Calc(replies)
    broker.mt5 = terminal
    try:
        money, why = broker.calc_money_per_unit(spec)
    finally:
        broker.mt5 = real
    return money, why, terminal.asked


def test_a_calculator_that_drops_one_answer_is_asked_a_second_time() -> None:
    """One lost reply is not a verdict, and writing it as one would flip the ruler in the journal.

    The flip is what the retry is bought against: `tick-proven` one cycle and `never checked` the next
    is two rows for one instrument, and the second tells the operator the calculator was never asked.
    Measured 2026-11-05 on login 114929502 this terminal answered 200 of 200 probes, 0.009–0.121 ms
    each, so asking twice is free where the answer came and buys the verdict where it did not.
    """
    money, why, asked = _probed(make_gold_spec(), [None, 100.0])
    assert why == "" and money == pytest.approx(100.0)
    assert len(asked) == 2, "проба не повторена"


def test_a_calculator_that_stays_silent_is_called_twice_and_reported_in_its_own_words() -> None:
    """The retry does not invent an answer: two silences are the silence the caller has to say."""
    money, why, asked = _probed(make_gold_spec(), [None, None])
    assert money == 0.0 and len(asked) == 2
    assert why == "order_calc_profit вернул None"
    # The probe volume comes from the broker's own limits, and a symbol that offers none is refused
    # before the calculator is asked at all — one call, not two.
    money, why, asked = _probed(make_gold_spec(volume_min=0.0), [100.0])
    assert money == 0.0 and asked == [] and "нет лота" in why




# --- the stop level the broker measures at the moment the order arrives ----------------------

def test_a_stop_planned_at_the_published_minimum_is_a_stop_the_next_tick_crosses() -> None:
    """GTC live, XAUUSD, 2026-11-05 evening: `order_check` of a stop 1.60 below the bid answers
    «check passed», `order_send` of the same geometry answers `invalid stops (10016)`.

    The level is measured from the bid *when the order arrives*, and gold makes 0.10–0.30 of price a
    second, so a plan parked on the published minimum is a plan the round trip can cross. The floor
    pays the spread (the fill pays ask, the server judges bid) and 20 points of movement.
    """
    gold = make_gold_spec(stops_level=150, spread=48)
    assert broker.min_level_distance(gold, spread=0.48) == pytest.approx(1.50 + 0.20 + 0.48)
    # A server naming no minimum still gets the round trip, and nothing is invented for it.
    assert broker.min_level_distance(make_gold_spec(stops_level=0), 0.13) == pytest.approx(0.20 + 0.13)
    assert broker.min_level_distance(make_gold_spec(stops_level=-5), 0.0) == pytest.approx(0.20)


def test_a_signal_never_plans_a_level_the_guard_would_refuse(monkeypatch) -> None:
    """Where the plan is made is where the broker's floor belongs: an ATR level on its own can sit
    inside the distance the server publishes, and then the lot is billed for a geometry already refused.
    """
    account = make_account(stop_atr=1.6, take_atr=2.6)
    account.strategy.min_atr_pct = 0.0
    wide = make_gold_spec(stops_level=1500, spread=10)  # 15.0 цены минимума при спреде 0.10
    bid, ask = 4119.95, 4120.05
    tick = lambda name: types.SimpleNamespace(bid=bid, ask=ask)
    monkeypatch.setattr(strategy.broker, "symbol", lambda name: wide)
    monkeypatch.setattr(strategy.broker, "atr", lambda *a, **k: 5.0)
    monkeypatch.setattr(strategy.broker, "ema",
                        lambda name, tf, period: 4110.0 if period == 21 else 4100.0)
    monkeypatch.setattr(strategy.broker.mt5, "symbol_info_tick", tick)
    monkeypatch.setattr(strategy, "mt5", types.SimpleNamespace(
        TIMEFRAME_H1=1, TIMEFRAME_H4=2, symbol_info_tick=tick,
        copy_rates_from_pos=lambda *a, **k: [{"close": 4120.0}] * 3))

    signal = strategy.trend_signal(account, "XAUUSD")

    assert signal is not None
    # 1.6 ATR alone is 8.0 and 2.6 ATR alone is 13.0 — both inside the 15.0 the server wants.
    assert "стоп" in broker.stops_refusal(wide, True, bid, ask - 8.0, ask + 20.0)
    assert "тейк" in broker.stops_refusal(wide, True, bid, ask - 20.0, ask + 13.0)
    floor = broker.min_level_distance(wide, ask - bid)
    assert signal.stop_distance == pytest.approx(floor)
    assert signal.target - ask == pytest.approx(floor)
    assert broker.stops_refusal(wide, True, bid, signal.stop, signal.target) == ""
