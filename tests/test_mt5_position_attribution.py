"""On a shared demo account the position count cap must measure our positions, not the book.

The count cap and the one-position-per-symbol rule are concentration limits for one strategy.
The money limits - daily loss, drawdown, aggregate stop risk - are not, and keep reading every
lot on the account. Conflating the two is how a second agent's open positions froze this run:
fifteen positions that were never ours against a cap of seven meant every cycle journaled
POSITION_CAP_EXCEEDED and nothing traded.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from test_mt5_agent import FakeBackend, agent_for, make_policy
from critical_consensus.mt5_agent import codes
from critical_consensus.mt5_agent.backend import PositionSnapshot

OURS = 8172026
THEIRS = 8180017


def position(symbol="EURUSD", *, ticket=1, magic=OURS, volume=0.1,
             entry=1.2, stop=1.19, target=1.25) -> PositionSnapshot:
    return PositionSnapshot(ticket=ticket, symbol=symbol, direction="BUY", volume=volume,
                            entry_price=entry, stop_loss=stop, take_profit=target,
                            open_time=datetime(2026, 11, 2, tzinfo=UTC), magic=magic)


def blockers(policy_positions, *, magic=OURS, cap=7):
    # 0.002 x 7 still fits the day's 2% loss budget, which is the relation the policy validates.
    policy = make_policy(account__position_magic=magic, risk__max_open_positions=cap,
                         risk__risk_per_trade_fraction=0.002)
    agent = agent_for(FakeBackend(open_positions=policy_positions), policy)
    return set(agent._position_cap_blockers(policy_positions))  # noqa: SLF001


def test_a_shared_book_does_not_spend_our_concentration_allowance():
    foreign = [position(ticket=index + 1, magic=THEIRS, symbol=f"SYM{index}")
               for index in range(8)]
    assert blockers(foreign) == set()


def test_the_same_positions_count_against_us_when_we_cannot_attribute_them():
    unattributed = [position(ticket=index + 1, magic=None) for index in range(8)]
    assert codes.POSITION_CAP_EXCEEDED in blockers(unattributed)
    manual = [position(ticket=index + 1, magic=0) for index in range(8)]
    assert blockers(manual) == set()


def test_our_own_positions_still_hit_the_cap():
    ours = [position(ticket=index + 1, symbol=f"SYM{index}") for index in range(7)]
    assert codes.POSITION_CAP_REACHED in blockers(ours)
    assert codes.POSITION_CAP_EXCEEDED in blockers(ours + [position(ticket=99, symbol="US500")])


def test_one_position_per_symbol_is_our_rule_for_our_positions():
    assert codes.SYMBOL_ALREADY_OPEN in blockers([position(symbol="EURUSD")])
    assert codes.SYMBOL_ALREADY_OPEN not in blockers([position(symbol="EURUSD", magic=THEIRS)])


def test_a_policy_that_names_no_magic_still_owns_the_whole_book():
    foreign = [position(ticket=index + 1, magic=THEIRS) for index in range(2)]
    assert blockers(foreign, magic=None, cap=1) == {codes.POSITION_CAP_EXCEEDED,
                                                   codes.SYMBOL_ALREADY_OPEN}


def test_a_position_without_a_magic_still_reserves_the_daily_budget():
    # The count is scoped by attribution; the money is not. A foreign stop that spends the day's
    # allowance has to stop us opening another one, whoever owns it.
    backend = FakeBackend(open_positions=[position(ticket=5, magic=THEIRS, volume=8.0)])
    policy = make_policy(account__position_magic=OURS, risk__max_open_positions=7,
                         risk__risk_per_trade_fraction=0.002)
    result = agent_for(backend, policy).run_cycle()
    assert codes.LOSS_FLOOR_BREACHED in result.blockers


def test_the_magic_must_be_a_run_magic_not_the_manual_zero():
    with pytest.raises(ValueError, match="position_magic"):
        make_policy(account__position_magic=0)
    with pytest.raises(ValueError, match="position_magic"):
        make_policy(account__position_magic=-5)
