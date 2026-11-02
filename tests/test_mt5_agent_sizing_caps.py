"""Volumes must obey the broker ceiling and the account's notional ceiling, not just risk."""
from __future__ import annotations

from critical_consensus.mt5_agent import SymbolSpec
from critical_consensus.mt5_agent.sizing import size_position

# Silver as MetaQuotes-Demo quotes it: 500 ounces per lot, so one lot is worth ~$36k at $71.
SPEC = SymbolSpec(symbol="XAGUSD", point=0.001, digits=3, tick_size=0.001, tick_value=0.5,
                  volume_min=0.01, volume_max=100.0, volume_step=0.01, spread_points=22,
                  stops_level_points=0)


def size(**overrides):
    kwargs = dict(spec=SPEC, equity=10_000.0, risk_fraction_limit=0.005, stop_distance=0.066,
                  leverage=100, margin_available=10_000.0, price=71.5)
    kwargs.update(overrides)
    return size_position(**kwargs)


def test_a_tight_stop_does_not_size_past_the_broker_maximum():
    result = size(stop_distance=0.004, risk_fraction_limit=0.005)
    assert result.accepted
    assert result.order.volume <= SPEC.volume_max + 1e-12


def test_the_notional_cap_binds_before_the_risk_budget():
    result = size(max_notional=30_000.0)
    assert result.accepted
    notional = result.order.volume * SPEC.value_per_price_unit(1.0) * 71.5
    assert notional <= 30_000.0 + 1e-6


def test_a_notional_cap_below_one_lot_is_a_denial():
    # One minimum lot of silver is ~$357 of notional here, so a $100 cap cannot be honored.
    result = size(max_notional=100.0)
    assert not result.accepted
    assert result.denial


def test_the_notional_cap_uses_the_contract_size_not_the_raw_price():
    """One lot of silver is 500 ounces: a raw-price cap would allow ~500x the position."""
    capped = size(max_notional=30_000.0, stop_distance=0.066)
    uncapped = size(stop_distance=0.066)
    assert capped.order.volume < uncapped.order.volume
