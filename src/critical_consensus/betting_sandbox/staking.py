"""Staking rules for the paper wallet: flat or fractional Kelly, hard-capped."""
from __future__ import annotations

import math
from dataclasses import dataclass

from .pricing import expected_value, kelly_fraction


@dataclass(frozen=True)
class StakingPlan:
    mode: str = "flat"
    flat_fraction: float = 0.01
    kelly_multiplier: float = 0.25
    max_fraction: float = 0.02
    min_edge: float = 0.02
    min_odds: float = 1.5
    max_odds: float = 10.0

    def __post_init__(self) -> None:
        if self.mode not in ("flat", "kelly"):
            raise ValueError("mode must be 'flat' or 'kelly'")
        for name, value in (("flat_fraction", self.flat_fraction),
                            ("kelly_multiplier", self.kelly_multiplier),
                            ("max_fraction", self.max_fraction),
                            ("min_edge", self.min_edge)):
            if isinstance(value, bool) or not math.isfinite(float(value)) or not 0.0 <= float(value) <= 1.0:
                raise ValueError(f"{name} must be finite and in [0, 1]")
        if self.flat_fraction <= 0 or self.kelly_multiplier <= 0 or self.max_fraction <= 0:
            raise ValueError("staking fractions must be positive")
        if self.flat_fraction > self.max_fraction:
            raise ValueError("flat_fraction cannot exceed max_fraction")
        if not (1.0 < self.min_odds < self.max_odds):
            raise ValueError("odds bounds must satisfy 1 < min_odds < max_odds")


@dataclass(frozen=True)
class StakeDecision:
    stake_units: int
    edge: float
    kelly_full: float
    reason: str


def decide_stake(plan: StakingPlan, model_probability: float, odds: float,
                 bankroll_units: int) -> StakeDecision:
    if isinstance(bankroll_units, bool) or not isinstance(bankroll_units, int) or bankroll_units < 0:
        raise ValueError("bankroll_units must be a non-negative integer")
    if not math.isfinite(float(odds)) or odds <= 1.0:
        raise ValueError("odds must be finite and greater than 1")
    edge = expected_value(model_probability, odds)
    kelly_full = kelly_fraction(model_probability, odds)
    if odds < plan.min_odds:
        return StakeDecision(0, edge, kelly_full, "ODDS_BELOW_MINIMUM")
    if odds > plan.max_odds:
        return StakeDecision(0, edge, kelly_full, "ODDS_ABOVE_MAXIMUM")
    if edge + 1e-12 < plan.min_edge:
        return StakeDecision(0, edge, kelly_full, "EDGE_BELOW_MINIMUM")
    if bankroll_units <= 0:
        return StakeDecision(0, edge, kelly_full, "EMPTY_BANKROLL")
    fraction = plan.flat_fraction if plan.mode == "flat" else min(
        plan.max_fraction, plan.kelly_multiplier * kelly_full)
    stake = int(math.floor(bankroll_units * fraction))
    if stake < 1:
        return StakeDecision(0, edge, kelly_full, "STAKE_BELOW_ONE_UNIT")
    if stake > bankroll_units:
        stake = bankroll_units
    return StakeDecision(stake, edge, kelly_full, "OK")
