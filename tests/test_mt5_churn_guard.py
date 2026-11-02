"""A symbol whose legs keep dying has to be stood down, not bought again every cycle.

Measured on the shared demo account on 2026-11-02: this run opened four 0.20-lot FX legs, the
broker confirmed each one, and 21 seconds later another process on the same account flattened all
four - EURUSD -1.00, GBPUSD -0.20, AUDUSD -0.40, NZDUSD -0.80, exactly the spread paid at entry.
Nothing in this run decided those closes. Without a guard the next cycle opens the same four
again, and the cycle after that, so the run spends its whole daily budget buying a stranger's
exit. The gate below counts the distinct legs this run has seen open on its own symbol recently
and refuses the entry once they pile up.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from test_mt5_agent import FakeBackend, agent_for, make_policy
from critical_consensus.mt5_agent import MT5Agent, codes
from critical_consensus.mt5_agent.agent import _CHURN_WINDOW
from critical_consensus.mt5_agent.backend import PositionSnapshot
from critical_consensus.mt5_agent.journal import DecisionJournal, OWN_LEG_EVENT_TYPE

OURS = 8172026
THEIRS = 8180017
SYMBOL = "EURUSD"
START = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)


def leg(ticket: int, *, symbol: str = SYMBOL, magic: int | None = OURS,
        opened: datetime = START) -> PositionSnapshot:
    return PositionSnapshot(ticket=ticket, symbol=symbol, direction="SELL", volume=0.2,
                            entry_price=1.12, stop_loss=1.1252, take_profit=1.11,
                            open_time=opened, magic=magic)


def churn_policy(**overrides):
    settings = {"execution_mode": "auto", "mode": "observe", "account__position_magic": OURS,
                "risk__max_open_positions": 7, "risk__risk_per_trade_fraction": 0.002}
    settings.update(overrides)
    return make_policy(symbol=SYMBOL, **settings)


def cycles(journal, moments, *, policy=None, legs=None):
    """One cycle per moment, each holding the matching leg (``None`` for an empty book)."""
    results = []
    for index, moment in enumerate(moments):
        position = None if legs is None else legs[index]
        backend = FakeBackend(clock=moment, open_positions=[] if position is None else [position])
        agent = MT5Agent(backend=backend, policy=policy or churn_policy(), journal=journal,
                         clock=lambda current=moment: current)
        results.append(agent.run_cycle())
    return tuple(results)


def dead_leg_moments(count: int):
    return [START + timedelta(seconds=20 * index) for index in range(count)]


def test_the_guard_needs_a_ledger_to_count_in():
    # No journal means no count that survives the cycle; the gate must not invent one and must
    # not block, because every other limit still applies.
    result = agent_for(FakeBackend(open_positions=[leg(1)]), churn_policy()).run_cycle()
    assert codes.LEG_CHURN_DETECTED not in result.blockers


def test_a_disabled_policy_never_blocks(tmp_path):
    journal = DecisionJournal(tmp_path / "churn.db")
    results = cycles(journal, dead_leg_moments(6), legs=[leg(t) for t in range(1, 7)],
                     policy=churn_policy(**{"risk__churn_max_legs_per_symbol": None}))
    assert all(codes.LEG_CHURN_DETECTED not in result.blockers for result in results)


def test_each_leg_this_run_opened_is_journaled(tmp_path):
    journal = DecisionJournal(tmp_path / "churn.db")
    cycles(journal, dead_leg_moments(3), legs=[leg(t) for t in range(1, 4)])
    rows = journal.events(limit=None, event_type=OWN_LEG_EVENT_TYPE)
    assert [row["payload"]["ticket"] for row in rows] == [1, 2, 3]
    assert all(row["payload"]["symbol"] == SYMBOL for row in rows)
    assert all(row["payload"]["broker_order_sent"] is False for row in rows)
    assert all(row["payload"]["open_time"] for row in rows)


def test_legs_that_keep_dying_stand_the_symbol_down(tmp_path):
    journal = DecisionJournal(tmp_path / "churn.db")
    # The pattern actually measured: a leg opens, the neighbour closes it, the book is empty when
    # the next cycle reads it, and the signal offers to open the symbol again.
    legs = [leg(1), None, leg(2), None, leg(3), None, leg(4), None]
    results = cycles(journal, dead_leg_moments(8), legs=legs)
    blocked = [index for index, result in enumerate(results)
               if codes.LEG_CHURN_DETECTED in result.blockers]
    assert blocked, "four legs in under three minutes is not trading"
    assert blocked[0] > 0, "the first legs are ordinary play, not evidence"
    assert results[blocked[0]].status == codes.STATUS_BLOCKED
    assert blocked == list(range(blocked[0], 8)), "once stood down, it stays stood down"


def test_a_leg_that_is_still_open_is_reported_as_open(tmp_path):
    # While we hold the symbol the honest answer is SYMBOL_ALREADY_OPEN; the churn verdict comes
    # when the book is empty and the run is about to put itself back into the same fight.
    journal = DecisionJournal(tmp_path / "churn.db")
    legs = [leg(1), None, leg(2), None]
    results = cycles(journal, dead_leg_moments(4), legs=legs,
                     policy=churn_policy(**{"risk__churn_max_legs_per_symbol": 1}))
    assert codes.SYMBOL_ALREADY_OPEN in results[0].blockers
    assert codes.LEG_CHURN_DETECTED not in results[1].blockers
    assert codes.SYMBOL_ALREADY_OPEN in results[2].blockers
    assert codes.LEG_CHURN_DETECTED in results[3].blockers


def test_the_count_survives_a_restart_of_the_run(tmp_path):
    journal = DecisionJournal(tmp_path / "churn.db")
    cycles(journal, dead_leg_moments(3), legs=[leg(t) for t in range(1, 4)])
    reopened = DecisionJournal(journal.path)
    # A fresh agent with an empty in-memory ledger and an empty book: only the rows the previous
    # process wrote can tell it that this symbol has already burned three legs.
    agent = MT5Agent(backend=FakeBackend(clock=START + timedelta(minutes=6)),
                     policy=churn_policy(**{"risk__churn_max_legs_per_symbol": 2}),
                     journal=reopened, clock=lambda: START + timedelta(minutes=6))
    assert codes.LEG_CHURN_DETECTED in agent.run_cycle().blockers


def test_old_legs_stop_counting(tmp_path):
    journal = DecisionJournal(tmp_path / "churn.db")
    cycles(journal, dead_leg_moments(3), legs=[leg(t) for t in range(1, 4)])
    later = START + _CHURN_WINDOW + timedelta(minutes=1)
    agent = MT5Agent(backend=FakeBackend(clock=later),
                     policy=churn_policy(**{"risk__churn_max_legs_per_symbol": 2}),
                     journal=DecisionJournal(journal.path), clock=lambda: later)
    assert codes.LEG_CHURN_DETECTED not in agent.run_cycle().blockers


def test_another_strategy_s_legs_are_not_our_churn(tmp_path):
    journal = DecisionJournal(tmp_path / "churn.db")
    results = cycles(journal, dead_leg_moments(6),
                     legs=[leg(t, magic=THEIRS) for t in range(1, 7)],
                     policy=churn_policy(**{"risk__churn_max_legs_per_symbol": 1}))
    assert all(codes.LEG_CHURN_DETECTED not in result.blockers for result in results)
    assert journal.events(limit=None, event_type=OWN_LEG_EVENT_TYPE) == []


def test_a_leg_on_another_symbol_is_not_our_evidence(tmp_path):
    journal = DecisionJournal(tmp_path / "churn.db")
    cycles(journal, dead_leg_moments(6), legs=[leg(t, symbol="GBPUSD") for t in range(1, 7)])
    assert journal.events(limit=None, event_type=OWN_LEG_EVENT_TYPE) == []


def test_an_unattributed_leg_counts_because_we_cannot_rule_it_out(tmp_path):
    # The count is conservative in the same direction as the position cap: a reader that cannot
    # say who opened a leg has not proved it was somebody else.
    journal = DecisionJournal(tmp_path / "churn.db")
    policy = churn_policy(**{"risk__churn_max_legs_per_symbol": 1})
    cycles(journal, dead_leg_moments(2), legs=[leg(t, magic=None) for t in (1, 2)], policy=policy)
    later = START + timedelta(seconds=40)
    agent = MT5Agent(backend=FakeBackend(clock=later), policy=policy,
                     journal=DecisionJournal(journal.path), clock=lambda: later)
    assert codes.LEG_CHURN_DETECTED in agent.run_cycle().blockers


def test_a_row_that_cannot_be_placed_in_time_counts(tmp_path):
    # A ledger this run can no longer read is not evidence that the legs stopped dying.
    journal = DecisionJournal(tmp_path / "churn.db")
    for ticket in range(1, 4):
        journal.record(OWN_LEG_EVENT_TYPE, {"symbol": SYMBOL, "ticket": ticket,
                                           "seen_at": "not a timestamp",
                                           "broker_order_sent": False})
    agent = MT5Agent(backend=FakeBackend(clock=START),
                     policy=churn_policy(**{"risk__churn_max_legs_per_symbol": 2}),
                     journal=journal, clock=lambda: START)
    assert codes.LEG_CHURN_DETECTED in agent.run_cycle().blockers


def test_an_unreadable_payload_is_skipped_not_fatal(tmp_path):
    journal = DecisionJournal(tmp_path / "churn.db")
    journal.record(OWN_LEG_EVENT_TYPE, {"symbol": SYMBOL, "ticket": "eleven",
                                        "seen_at": START.isoformat()})
    journal.record(OWN_LEG_EVENT_TYPE, {"symbol": 42, "ticket": 12, "seen_at": START.isoformat()})
    agent = MT5Agent(backend=FakeBackend(clock=START),
                     policy=churn_policy(**{"risk__churn_max_legs_per_symbol": 1}),
                     journal=journal, clock=lambda: START)
    assert codes.LEG_CHURN_DETECTED not in agent.run_cycle().blockers


def test_a_row_for_another_symbol_does_not_count(tmp_path):
    journal = DecisionJournal(tmp_path / "churn.db")
    for ticket in range(1, 6):
        journal.record(OWN_LEG_EVENT_TYPE, {"symbol": "GBPUSD", "ticket": ticket,
                                            "seen_at": START.isoformat(),
                                            "broker_order_sent": False})
    agent = MT5Agent(backend=FakeBackend(clock=START), policy=churn_policy(), journal=journal,
                     clock=lambda: START)
    assert codes.LEG_CHURN_DETECTED not in agent.run_cycle().blockers


def test_the_churn_limit_is_validated():
    with pytest.raises(ValueError, match="churn_max_legs_per_symbol"):
        make_policy(**{"risk__churn_max_legs_per_symbol": 0})
    with pytest.raises(ValueError, match="churn_max_legs_per_symbol"):
        make_policy(**{"risk__churn_max_legs_per_symbol": True})
    with pytest.raises(ValueError, match="churn_max_legs_per_symbol"):
        make_policy(**{"risk__churn_max_legs_per_symbol": 51})
