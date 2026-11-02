"""A lot ceiling has to be taken while sizing, not only checked afterwards.

One ceiling does not fall on every instrument the same way: at the same risk budget a metal
sizes to its broker minimum while an FX pair sizes to several times the ceiling. Checked only
after sizing, that deletes the cheap contracts from the book and leaves the expensive ones
untouched; clipped here, they trade at the largest lot the ceiling allows and report the risk
they really carry.
"""
from __future__ import annotations

import math

import pytest
from critical_consensus.mt5_agent import codes
from critical_consensus.mt5_agent.backend import SymbolSpec
from critical_consensus.mt5_agent.policy import AgentPolicy, RiskLimits
from critical_consensus.mt5_agent.sizing import size_position

# EURUSD as MetaQuotes-Demo publishes it: 100 000 base units per lot, so one price unit per lot
# is worth 100 000 $ and a 161-point stop puts 161 $ on one lot.
EURUSD = SymbolSpec(symbol="EURUSD", point=0.00001, digits=5, tick_size=0.00001, tick_value=1.0,
                    volume_min=0.01, volume_max=100.0, volume_step=0.01, spread_points=1,
                    stops_level_points=0, contract_size=100_000.0)
# 100 troy ounces per lot, like the broker's gold.
XAUUSD = SymbolSpec(symbol="XAUUSD", point=0.01, digits=2, tick_size=0.01, tick_value=0.1,
                    volume_min=0.01, volume_max=100.0, volume_step=0.01, spread_points=20,
                    stops_level_points=0, contract_size=100.0)


def size(spec=EURUSD, **overrides):
    kwargs = dict(spec=spec, equity=100_000.0, risk_fraction_limit=0.0005,
                  stop_distance=0.00161, leverage=100, margin_available=100_000.0,
                  price=1.11617)
    kwargs.update(overrides)
    return size_position(**kwargs)


def test_the_budget_alone_asks_for_more_than_the_ceiling_allows():
    result = size()
    assert result.accepted
    assert math.isclose(result.order.volume, 0.31, abs_tol=1e-9)


def test_the_ceiling_clips_the_lot_instead_of_refusing_the_trade():
    result = size(max_volume=0.20)
    assert result.accepted
    assert math.isclose(result.order.volume, 0.20, abs_tol=1e-9)
    # The risk is the clipped lot's, not the budget's: under budget is reported as under budget.
    assert math.isclose(result.order.risk_value, 0.20 * 161.0, abs_tol=1e-6)
    assert result.order.risk_value < 100_000.0 * 0.0005
    assert math.isclose(result.order.risk_fraction, 32.2 / 100_000.0, abs_tol=1e-9)


def test_the_clipped_lot_stays_on_the_broker_step():
    assert math.isclose(size(max_volume=0.235).order.volume, 0.23, abs_tol=1e-9)


def test_a_ceiling_that_does_not_bind_changes_nothing():
    assert size(max_volume=0.50).order.volume == size().order.volume


def test_a_metal_at_its_broker_minimum_is_left_alone():
    # 2x-ATR of 44.8 $ puts 4 480 $ on one gold lot, so the same budget sizes 0.01 - the
    # broker minimum - and a 0.20 ceiling has nothing to clip.
    unclipped = size(spec=XAUUSD, stop_distance=44.8, price=3895.76)
    clipped = size(spec=XAUUSD, stop_distance=44.8, price=3895.76, max_volume=0.20)
    assert clipped.accepted
    assert clipped.order.volume == unclipped.order.volume == pytest.approx(0.01)


def test_a_ceiling_below_the_brokers_smallest_lot_refuses_the_contract():
    result = size(max_volume=0.005)
    assert not result.accepted
    assert result.denial == codes.VOLUME_CEILING_BELOW_BROKER_MIN


@pytest.mark.parametrize("bad", [0.0, -0.2, math.nan, math.inf])
def test_a_ceiling_that_is_not_a_positive_finite_number_is_a_bug_not_a_trade(bad):
    with pytest.raises(ValueError):
        size(max_volume=bad)


def test_the_policy_carries_the_ceiling_and_rejects_an_unusable_one():
    assert RiskLimits(max_volume_lots=0.20).max_volume_lots == 0.20
    assert RiskLimits().max_volume_lots is None
    with pytest.raises(ValueError):
        RiskLimits(max_volume_lots=0.005)
    with pytest.raises(ValueError):
        RiskLimits(max_volume_lots=0.0)


def test_the_all_universe_policy_declares_the_ceiling_the_executor_enforces():
    from pathlib import Path

    import yaml

    path = (Path(__file__).resolve().parents[1] / "config" / "forward"
            / "06_ema_all_universe_h1.yaml")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    policy = AgentPolicy.from_mapping(raw)
    assert policy.risk.max_volume_lots == 0.20
