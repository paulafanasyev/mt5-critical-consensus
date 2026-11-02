"""A foreign leg in a currency this run may not trade is still money on this account.

Measured on MetaQuotes-Demo account 114929502 on 2026-11-02: a second agent's 0.02-lot USDJPY
sell made every cycle of this run journal MARKET_DATA_READ_FAILED. ``SymbolSpec.from_terminal``
refuses pairs whose profit currency is not the account's — correctly, because a lot sized in
account currency would be wrong there — but that refusal was being applied to the *measurement*
of somebody else's exposure too, so one untradeable position froze seven tradeable instruments.
The fix prices the reserve with the terminal's own money math and keeps the refusal where it
belongs: on opening a trade.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest
from test_mt5_agent import FakeBackend, agent_for, make_policy

from critical_consensus.mt5_agent import codes
from critical_consensus.mt5_agent.agent import _position_stop_risk, _stop_distance
from critical_consensus.mt5_agent.backend import (LiveMT5Backend, PositionSnapshot, SymbolSpec)

OURS = 8172026
THEIRS = 8180021
# The pair this run cannot price and could never open: profit pays in JPY on a USD account.
REFUSED = "USDJPY"
JPY_LEG = dict(symbol=REFUSED, ticket=2, magic=THEIRS, direction="SELL", volume=0.02,
               entry=157.701, stop=165.588)


def leg(symbol="EURUSD", *, ticket=1, magic=OURS, direction="BUY", volume=0.1,
        entry=1.2, stop=1.19, target=0.0) -> PositionSnapshot:
    return PositionSnapshot(ticket=ticket, symbol=symbol, direction=direction, volume=volume,
                            entry_price=entry, stop_loss=stop, take_profit=target,
                            open_time=datetime(2026, 11, 2, tzinfo=UTC), magic=magic)


def jpy_leg(**overrides: Any) -> PositionSnapshot:
    return leg(**{**JPY_LEG, **overrides})


@dataclass
class RefusingBackend(FakeBackend):
    """A terminal that will not hand back a spec for a pair that pays profit elsewhere."""

    refused_symbols: tuple[str, ...] = (REFUSED,)

    def symbol_spec(self, symbol: str) -> Any:
        if symbol.strip().upper() in self.refused_symbols:
            raise RuntimeError(f"MT5 symbol {symbol} pays profit in JPY, not in the account"
                               " currency USD; sizing in account currency would be wrong")
        return FakeBackend.symbol_spec(self, symbol)


@dataclass
class BrokerPricedBackend(RefusingBackend):
    """The same terminal plus the optional reader: it prices what the spec refuses."""

    broker_risks: dict[int, Any] = field(default_factory=dict)
    broker_questions: list[int] = field(default_factory=list)

    def position_stop_risk(self, position: PositionSnapshot) -> Any:
        self.broker_questions.append(position.ticket)
        answer = self.broker_risks[position.ticket]
        if isinstance(answer, Exception):
            raise answer
        return answer


def agent_for_book(*positions: PositionSnapshot, risks: dict[int, Any] | None = None,
                   backend: Any | None = None) -> Any:
    backend = backend or BrokerPricedBackend(open_positions=list(positions),
                                             broker_risks=risks or {})
    policy = make_policy(account__position_magic=OURS, risk__risk_per_trade_fraction=0.002)
    return agent_for(backend, policy)


# --------------------------------------------------------------------- the measurement
def test_a_refused_leg_is_priced_by_the_terminal_instead_of_freezing_the_book():
    # Our own 0.1-lot EURUSD stop is 100 $; the broker says the foreign JPY leg would cost 95.26 $.
    own, foreign = leg(ticket=1), jpy_leg()
    agent = agent_for_book(own, foreign, risks={2: 95.26})
    risks, blocker = agent._stop_risks([own, foreign])  # noqa: SLF001
    assert blocker is None
    assert risks == pytest.approx((100.0, 95.26))
    assert agent._aggregate_stop_risk([own, foreign]) == pytest.approx(195.26)  # noqa: SLF001


def test_an_unpriceable_leg_without_a_broker_reader_still_blocks():
    """Paper mode and any adapter that cannot answer keep the old fail-closed behaviour."""
    agent = agent_for_book(jpy_leg(), backend=RefusingBackend(open_positions=[jpy_leg()]))
    assert agent._stop_risks([jpy_leg()]) == ((), codes.MARKET_DATA_READ_FAILED)  # noqa: SLF001


def test_pricing_a_reserve_does_not_make_the_pair_tradable():
    """The sizing path must keep refusing an instrument whose spec it cannot obtain."""
    foreign = jpy_leg()
    agent = agent_for_book(foreign, risks={2: 95.26})
    assert agent._stop_risks([foreign])[0] == pytest.approx((95.26,))  # noqa: SLF001
    assert agent._spec_for(REFUSED) is None  # noqa: SLF001


def test_a_policy_that_asks_to_trade_the_refused_pair_is_still_blocked():
    """Measuring a leg somebody else owns is not this run trading there."""
    backend = RefusingBackend(open_positions=[])
    policy = make_policy(symbol=REFUSED, symbol_spec__symbol=REFUSED,
                         account__position_magic=OURS, risk__risk_per_trade_fraction=0.002)
    result = agent_for(backend, policy).run_cycle()
    assert result.status == codes.STATUS_BLOCKED
    assert codes.MARKET_DATA_READ_FAILED in result.blockers
    assert result.ticket is None


def test_a_stopless_foreign_leg_is_named_as_a_missing_stop_not_a_missing_quote():
    agent = agent_for_book(jpy_leg(stop=0.0), risks={2: 1.0})
    assert agent._stop_risks([jpy_leg(stop=0.0)])[1] == codes.POSITION_STOP_UNSET  # noqa: SLF001


def test_a_stop_on_the_profitable_side_is_not_a_loss_however_the_pair_is_quoted():
    # BUY at 157.701 with the stop above entry: reaching it would pay, so there is nothing to
    # reserve, and the question is settled before the terminal is ever asked.
    foreign = jpy_leg(direction="BUY")
    backend = BrokerPricedBackend(open_positions=[foreign], broker_risks={2: 95.26})
    agent = agent_for_book(foreign, backend=backend)
    assert agent._stop_risks([foreign])[1] == codes.POSITION_STOP_UNSET  # noqa: SLF001
    assert backend.broker_questions == []


@pytest.mark.parametrize("answer", [None, -3.0, "95.26", float("nan"), float("inf"), True,
                                   RuntimeError("terminal disconnected")])
def test_a_broker_answer_that_is_not_a_risk_is_refused(answer: Any):
    foreign = jpy_leg()
    agent = agent_for_book(foreign, risks={2: answer})
    assert agent._stop_risks([foreign])[1] == codes.MARKET_DATA_READ_FAILED  # noqa: SLF001


# --------------------------------------------------------------------- the money caps
def test_a_foreign_leg_can_still_spend_the_whole_days_budget():
    """The reserve is account-wide: pricing the leg must not make it cheaper than it is."""
    foreign = jpy_leg()
    backend = BrokerPricedBackend(open_positions=[foreign], broker_risks={2: 5_000.0})
    agent = agent_for_book(foreign, backend=backend)
    # The day's allowance on this 10 000 $ account is 200 $, which the JPY leg alone blows.
    blockers = agent._open_book_blockers([foreign], equity=10_000.0,  # noqa: SLF001
                                         daily_budget=200.0, realized_loss=0.0)
    assert blockers == (codes.LOSS_FLOOR_BREACHED,)
    assert backend.broker_questions == [2]


def test_a_modest_foreign_leg_leaves_the_cycle_alone():
    """Observe mode opens nothing, so a proposal here means every money gate was passed."""
    backend = BrokerPricedBackend(open_positions=[jpy_leg()], broker_risks={2: 95.26})
    policy = make_policy(mode="observe", account__position_magic=OURS,
                         risk__risk_per_trade_fraction=0.002)
    result = agent_for(backend, policy).run_cycle()
    assert codes.MARKET_DATA_READ_FAILED not in result.blockers
    assert result.status == codes.STATUS_OBSERVED


def test_a_foreign_leg_that_eats_the_budget_stops_the_run_end_to_end():
    agent = agent_for_book(jpy_leg(), risks={2: 250.0})
    assert codes.LOSS_FLOOR_BREACHED in agent.run_cycle().blockers


# --------------------------------------------------------------------- the adapter's math
@dataclass
class FakeTerminal:
    """The ``order_calc_profit`` answers the live terminal gave for these exact legs."""

    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    profits: dict[str, Any] = field(default_factory=dict)
    calls: list[tuple] = field(default_factory=list)

    def order_calc_profit(self, action, symbol, volume, price_open, price_close):
        self.calls.append((action, symbol, volume, price_open, price_close))
        return self.profits.get(symbol)


def adapter(profits: dict[str, Any], names: dict[str, str] | None = None
            ) -> tuple[LiveMT5Backend, FakeTerminal]:
    """A bare adapter: this method needs no connection, only the terminal's money math."""
    terminal = LiveMT5Backend.__new__(LiveMT5Backend)
    fake = FakeTerminal(profits=profits)
    terminal._mt5 = fake  # noqa: SLF001
    if names:
        terminal.use_broker_names(names)
    return terminal, fake


# (position, the signed profit the terminal reported at its stop, the loss in account currency).
# Every row is a leg that was actually open on account 114929502 on 2026-11-02.
MEASURED = [
    pytest.param(leg(symbol="EURUSD", ticket=3, magic=THEIRS, direction="SELL", volume=0.02,
                     entry=1.11985, stop=1.17585), -112.0, 112.0, id="eurusd"),
    pytest.param(leg(symbol="GBPUSD", ticket=4, magic=THEIRS, direction="BUY", volume=0.02,
                     entry=1.34149, stop=1.27442), -134.14, 134.14, id="gbpusd"),
    pytest.param(leg(symbol="XAUUSD", ticket=6, magic=THEIRS, direction="SELL", volume=0.02,
                     entry=3897.71, stop=3950.37), -105.32, 105.32, id="xauusd"),
    pytest.param(leg(symbol="XAGUSD", ticket=8, magic=THEIRS, direction="SELL", volume=0.02,
                     entry=50.304, stop=51.056), -75.2, 75.2, id="xagusd"),
]

# The geometry this server publishes for those same four instruments.
SPECS = {
    "EURUSD": dict(point=0.00001, digits=5, tick_size=0.00001, tick_value=1.0,
                   contract_size=100_000.0),
    "GBPUSD": dict(point=0.00001, digits=5, tick_size=0.00001, tick_value=1.0,
                   contract_size=100_000.0),
    "XAUUSD": dict(point=0.01, digits=2, tick_size=0.01, tick_value=1.0, contract_size=100.0),
    "XAGUSD": dict(point=0.001, digits=3, tick_size=0.001, tick_value=5.0,
                   contract_size=5_000.0),
}


@pytest.mark.parametrize(("position", "broker_profit", "expected"), MEASURED)
def test_the_adapters_stop_risk_is_the_brokers_own_number_made_positive(
        position: PositionSnapshot, broker_profit: float, expected: float):
    terminal, fake = adapter({position.symbol: broker_profit})
    assert terminal.position_stop_risk(position) == pytest.approx(expected)
    action = FakeTerminal.ORDER_TYPE_BUY if position.direction == "BUY" \
        else FakeTerminal.ORDER_TYPE_SELL
    assert fake.calls == [(action, position.symbol, position.volume, position.entry_price,
                           position.stop_loss)]


@pytest.mark.parametrize(("position", "broker_profit", "expected"), MEASURED)
def test_where_both_methods_can_price_a_leg_they_agree(position: PositionSnapshot,
                                                       broker_profit: float, expected: float):
    """No second money model: the broker's answer is the number the contract path already gives.

    The fifth leg that day, the USDJPY sell, is the one only the broker can answer.
    """
    spec = SymbolSpec(symbol=position.symbol, volume_min=0.01, volume_max=100.0, volume_step=0.01,
                      spread_points=10, stops_level_points=0, **SPECS[position.symbol])
    assert _position_stop_risk(spec, position) == pytest.approx(expected)
    assert broker_profit < 0


def test_the_adapter_prices_the_jpy_leg_the_spec_refuses():
    terminal, fake = adapter({REFUSED: -95.26})
    assert terminal.position_stop_risk(jpy_leg()) == pytest.approx(95.26)
    assert fake.calls[0][1] == REFUSED


def test_the_servers_own_spelling_is_the_one_the_terminal_is_asked_about():
    terminal, fake = adapter({"USDJPYm": -95.26}, names={"USDJPY": "USDJPYm"})
    assert terminal.position_stop_risk(jpy_leg()) == pytest.approx(95.26)
    assert fake.calls[0][1] == "USDJPYm"


def test_a_profit_or_a_missing_stop_costs_nothing_to_reserve():
    terminal, fake = adapter({"EURUSD": 12.0})
    assert terminal.position_stop_risk(leg(stop=1.21)) is None
    assert terminal.position_stop_risk(leg(stop=0.0)) is None
    assert terminal.position_stop_risk(leg(volume=0.02, entry=1.11985, stop=1.17585)) is None
    assert fake.calls == []


def test_a_terminal_that_answers_zero_or_nothing_is_not_read_as_no_risk():
    position = leg(volume=0.02, entry=1.11985, stop=1.17585)
    for answer in (None, 0.0, "n/a", float("nan")):
        terminal, _ = adapter({"EURUSD": answer})
        assert terminal.position_stop_risk(position) is None


def test_the_geometry_used_for_the_reserve_needs_no_symbol_data():
    """Which side of entry a stop sits on is the same question for every instrument."""
    assert _stop_distance(leg(entry=1.2, stop=1.19)) == pytest.approx(0.01)
    assert _stop_distance(leg(direction="SELL", entry=1.2, stop=1.21)) == pytest.approx(0.01)
    assert _stop_distance(leg(direction="SELL", entry=1.2, stop=1.19)) is None
    assert _stop_distance(leg(entry=1.2, stop=1.2)) is None
    assert _stop_distance(leg(entry=1.2, stop=0.0)) is None
    assert _stop_distance(jpy_leg()) == pytest.approx(7.887)
