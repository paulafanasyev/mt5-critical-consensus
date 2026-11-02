"""The churn gate has to count legs the broker saw, not only legs this run happened to sample.

Measured on the shared demo on 2026-11-02: the runner opened six legs, another process flattened
each of them inside the gap between two 20-second cycles, and the journal of seen tickets that the
gate reads still held two rows per symbol against four real round trips. The gate written to stand
that pattern down never fired, because the evidence it counted was a book sampled once a cycle -
and a leg that lives sixty seconds is not in the book when the next cycle reads it.

The broker's own deal history holds every leg, dead or alive, so this is the source that closes the
blind spot. It is read from the terminal adapter, treated as authoritative, and a history the
terminal will not give is a reason to stand down under its own code - not a clean record.
"""
from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from test_mt5_churn_guard import OURS, SYMBOL, churn_policy, leg
from test_mt5_agent import FakeBackend
from critical_consensus.mt5_agent import MT5Agent, codes
from critical_consensus.mt5_agent.backend import LiveMT5Backend

START = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
# MetaQuotes-Demo runs two hours ahead of UTC; deal stamps are server wall clock.
SERVER_OFFSET = 7200


# ------------------------------------------------------------------ the agent's side of it
UNSET = object()


class HistoryBackend(FakeBackend):
    """The fake terminal plus the optional opened-leg reader a live adapter offers."""

    def __init__(self, legs=(), *, error=None, shape=UNSET, **kwargs):
        super().__init__(**kwargs)
        self.legs = list(legs)
        self.error = error
        self.shape = shape
        self.history_calls: list[tuple[str, int | None, datetime]] = []

    def opened_leg_tickets(self, symbol, magic, since):
        self.history_calls.append((symbol, magic, since))
        if self.error is not None:
            raise self.error
        return self.legs if self.shape is UNSET else self.shape


def run(backend, *, policy=None, moment=START, journal=None):
    agent = MT5Agent(backend=backend, policy=policy or churn_policy(), journal=journal,
                     clock=lambda: moment)
    return agent.run_cycle()


def test_history_legs_stand_the_symbol_down_even_when_the_book_was_never_shown():
    # The exact failure the journal could not see: four legs opened and died between cycles, so no
    # cycle sampled a live position and no row was ever written.
    backend = HistoryBackend(legs=[11, 12, 13, 14], open_positions=[], clock=START)
    result = run(backend, policy=churn_policy(**{"risk__churn_max_legs_per_symbol": 3}))
    assert result.status == codes.STATUS_BLOCKED
    assert result.blockers == (codes.LEG_CHURN_DETECTED,)
    assert result.proposal is None
    # Stood down before a bar was read, so nothing downstream can price an entry into the fight.
    assert "bars" not in backend.calls


def test_the_history_asked_for_is_our_own_symbol_and_our_magic():
    backend = HistoryBackend(legs=[], open_positions=[], clock=START)
    run(backend, policy=churn_policy(**{"account__position_magic": OURS}))
    symbol, magic, window_start = backend.history_calls[0]
    assert (symbol, magic) == (SYMBOL, OURS)
    assert window_start == START - timedelta(hours=2)


def test_fewer_legs_than_the_limit_still_trades():
    backend = HistoryBackend(legs=[11, 12], open_positions=[], clock=START)
    result = run(backend, policy=churn_policy(**{"risk__churn_max_legs_per_symbol": 3}))
    assert codes.LEG_CHURN_DETECTED not in result.blockers
    assert codes.LEG_HISTORY_UNREADABLE not in result.blockers
    # The gate let the cycle through to the market read; where it lands after that is the
    # strategy's and the quote's business, not the churn guard's.
    assert "bars" in backend.calls


def test_a_history_the_terminal_will_not_give_is_not_read_as_a_clean_record():
    backend = HistoryBackend(error=RuntimeError("terminal did not answer"),
                             open_positions=[], clock=START)
    result = run(backend)
    assert result.status == codes.STATUS_BLOCKED
    assert result.blockers == (codes.LEG_HISTORY_UNREADABLE,)


def test_an_unshapely_answer_is_treated_the_same_way():
    # A reader that returns a string, a number or None has not said "no legs"; it has not answered.
    for shape in ("four", 4, None, {"a": "b"}):
        backend = HistoryBackend(shape=shape, open_positions=[], clock=START)
        assert run(backend).blockers == (codes.LEG_HISTORY_UNREADABLE,), shape


def test_a_policy_without_a_churn_limit_never_asks_the_history():
    backend = HistoryBackend(legs=[11, 12, 13, 14], open_positions=[], clock=START)
    result = run(backend, policy=churn_policy(
        **{"risk__churn_max_legs_per_symbol": None}))
    assert backend.history_calls == []
    assert codes.LEG_CHURN_DETECTED not in result.blockers


def test_a_backend_that_offers_no_history_keeps_the_journal_behaviour(tmp_path):
    # The paper book is closed by this very process, so its journal is complete and no adapter is
    # needed; the gate must not start blocking runs that have no history to read.
    from critical_consensus.mt5_agent.journal import DecisionJournal
    journal = DecisionJournal(tmp_path / "churn.db")
    backend = FakeBackend(open_positions=[leg(7)], clock=START)
    assert run(backend, journal=journal).blockers != (codes.LEG_HISTORY_UNREADABLE,)
    agent = MT5Agent(backend=FakeBackend(open_positions=[], clock=START + timedelta(minutes=1)),
                     policy=churn_policy(), journal=journal,
                     clock=lambda: START + timedelta(minutes=1))
    assert codes.LEG_CHURN_DETECTED not in agent.run_cycle().blockers


def test_history_and_journal_legs_are_counted_as_one_set(tmp_path):
    # Ticket 7 is in the journal and in the history; the same leg must not be counted twice.
    from critical_consensus.mt5_agent.journal import DecisionJournal
    from critical_consensus.mt5_agent.journal import OWN_LEG_EVENT_TYPE
    journal = DecisionJournal(tmp_path / "churn.db")
    journal.record(OWN_LEG_EVENT_TYPE, {"symbol": SYMBOL, "ticket": 7,
                                        "seen_at": START.isoformat(),
                                        "broker_order_sent": False})
    backend = HistoryBackend(legs=[7, 8], open_positions=[], clock=START)
    result = MT5Agent(backend=backend, policy=churn_policy(
        **{"risk__churn_max_legs_per_symbol": 2}), journal=journal,
        clock=lambda: START).run_cycle()
    assert codes.LEG_CHURN_DETECTED not in result.blockers


# ------------------------------------------------------------------- the adapter's side of it
class DealTerminal:
    """The handful of terminal calls the leg counter makes, with the stamps MetaQuotes gives."""

    def __init__(self, rows=()):
        self.rows = list(rows)
        self.windows: list[tuple[datetime, datetime]] = []

    def initialize(self, **kwargs):
        return True

    def shutdown(self):
        pass

    def last_error(self):
        return (0, "OK")

    def history_deals_get(self, start, end):
        self.windows.append((start, end))
        return None if self.rows is None else self.rows


def deal(*, symbol=SYMBOL, magic=OURS, entry=0, position_id=1, at=START):
    return SimpleNamespace(symbol=symbol, magic=magic, entry=entry, position_id=position_id,
                           time_msc=(at + timedelta(seconds=SERVER_OFFSET)).timestamp() * 1000)


def backend_for(rows):
    terminal = DealTerminal(rows)
    sys.modules["MetaTrader5"] = terminal
    try:
        return LiveMT5Backend(server_utc_offset_seconds=SERVER_OFFSET), terminal
    finally:
        del sys.modules["MetaTrader5"]


def test_the_counter_returns_the_positions_the_broker_saw_us_open():
    backend, _ = backend_for([deal(position_id=101), deal(position_id=102)])
    assert backend.opened_leg_tickets(SYMBOL, OURS, START - timedelta(hours=2)) == {101, 102}


def test_a_leg_is_one_position_not_one_deal():
    # Netting books let an order scale into an existing position; the leg is the position.
    backend, _ = backend_for([deal(position_id=101), deal(position_id=101), deal(position_id=101)])
    assert len(backend.opened_leg_tickets(SYMBOL, OURS, START - timedelta(hours=2))) == 1


def test_closing_deals_somebody_elses_legs_and_other_instruments_are_not_legs_we_opened():
    backend, _ = backend_for([
        deal(entry=1, position_id=201),
        deal(magic=8180017, position_id=202),
        deal(symbol="XAUUSD", position_id=203),
        deal(position_id=301),
    ])
    assert backend.opened_leg_tickets(SYMBOL, OURS, START - timedelta(hours=2)) == {301}


def test_a_leg_outside_the_window_is_not_this_churn():
    backend, _ = backend_for([deal(position_id=401, at=START - timedelta(hours=3)),
                              deal(position_id=402, at=START - timedelta(minutes=5))])
    assert backend.opened_leg_tickets(SYMBOL, OURS, START - timedelta(hours=2)) == {402}


def test_a_deal_that_cannot_be_placed_in_time_counts_anyway():
    # An undatable row may be the newest leg of all; dropping it is the mistake this gate is here
    # to stop making.
    broken = deal(position_id=501)
    broken.time_msc = 0
    backend, _ = backend_for([broken])
    assert backend.opened_leg_tickets(SYMBOL, OURS, START - timedelta(hours=2)) == {501}


def test_no_magic_in_the_policy_means_this_run_claims_the_whole_book():
    # Consistent with the open-position filter, which treats a policy without a magic as owning
    # every leg it can see.
    backend, _ = backend_for([deal(magic=8180017, position_id=601), deal(position_id=602)])
    assert backend.opened_leg_tickets(SYMBOL, None, START - timedelta(hours=2)) == {601, 602}


def test_the_history_window_is_oversized_on_the_side_the_server_clock_clips():
    # Measured defect: the terminal reads these naive stamps on its own clock, so a right edge
    # taken from UTC silently drops the newest deals - the exact rows this count exists to find.
    backend, terminal = backend_for([])
    backend.opened_leg_tickets(SYMBOL, OURS, START)
    start, end = terminal.windows[0]
    assert start.tzinfo is None and end.tzinfo is None
    assert start <= (START - timedelta(days=7)).replace(tzinfo=None)
    assert end > datetime.now(UTC).replace(tzinfo=None)


def test_a_silent_history_reader_raises_instead_of_reporting_an_empty_book():
    terminal = DealTerminal([])
    terminal.rows = None
    sys.modules["MetaTrader5"] = terminal
    try:
        backend = LiveMT5Backend(server_utc_offset_seconds=SERVER_OFFSET)
    finally:
        del sys.modules["MetaTrader5"]
    with pytest.raises(RuntimeError, match="history"):
        backend.opened_leg_tickets(SYMBOL, OURS, START - timedelta(hours=2))


def test_the_window_must_be_a_moment_this_run_can_place_on_the_clock():
    backend, _ = backend_for([])
    with pytest.raises(ValueError, match="timezone-aware"):
        backend.opened_leg_tickets(SYMBOL, OURS, datetime(2026, 11, 2, 12, 0))
