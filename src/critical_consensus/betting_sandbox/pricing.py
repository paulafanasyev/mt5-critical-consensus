"""Decimal-odds maths: margin removal, expected value and Kelly sizing."""
from __future__ import annotations

import math
from typing import Sequence


def _odds(values: Sequence[float]) -> list[float]:
    if len(values) not in (2, 3):
        raise ValueError("need two or three decimal odds")
    parsed: list[float] = []
    for value in values:
        if isinstance(value, bool):
            raise ValueError("decimal odds must be numeric, not booleans")
        try:
            odds = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("decimal odds must be numeric") from exc
        if not math.isfinite(odds) or odds <= 1.0:
            raise ValueError("decimal odds must be finite and greater than 1")
        parsed.append(odds)
    return parsed


def overround(odds: Sequence[float]) -> float:
    """Bookmaker margin: sum(1/o) - 1. Positive means the book has an edge."""
    return sum(1.0 / value for value in _odds(odds)) - 1.0


def implied_probabilities(odds: Sequence[float], *, method: str = "proportional") -> list[float]:
    """Remove the bookmaker margin.

    ``proportional`` divides each raw probability by the overround sum.
    ``power`` solves sum(raw_i ** k) = 1 with k > 1, which keeps longshots
    relatively lower than proportional scaling. Both are heuristics, not a
    calibrated market model.
    """
    values = _odds(odds)
    raw = [1.0 / value for value in values]
    total = sum(raw)
    if total <= 0 or not math.isfinite(total):
        raise ValueError("invalid odds")
    if method == "proportional":
        return [value / total for value in raw]
    if method != "power":
        raise ValueError("method must be 'proportional' or 'power'")
    if total <= 1.0:
        return [value / total for value in raw]
    low, high = 1.0, 100.0
    for _ in range(200):
        mid = (low + high) / 2.0
        if sum(value ** mid for value in raw) > 1.0:
            low = mid
        else:
            high = mid
    scale = (low + high) / 2.0
    scaled = [value ** scale for value in raw]
    norm = sum(scaled)
    return [value / norm for value in scaled]


def _decimal_price(odds: float) -> float:
    if isinstance(odds, bool):
        raise ValueError("decimal odds must be numeric, not booleans")
    try:
        price = float(odds)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("decimal odds must be numeric") from exc
    if not math.isfinite(price) or price <= 1.0:
        raise ValueError("decimal odds must be finite and greater than 1")
    return price


def _single_decimal_probability(model_probability: float) -> float:
    if isinstance(model_probability, bool):
        raise ValueError("probability must be numeric, not boolean")
    probability = float(model_probability)
    if not math.isfinite(probability) or not 0.0 <= probability <= 1.0:
        raise ValueError("probability must be finite and in [0, 1]")
    return probability


def expected_value(model_probability: float, odds: float) -> float:
    """EV per unit staked: p*(o-1) - (1-p). Positive means model edge."""
    probability = _single_decimal_probability(model_probability)
    price = _decimal_price(odds)
    return probability * (price - 1.0) - (1.0 - probability)


def kelly_fraction(model_probability: float, odds: float) -> float:
    """Full-Kelly stake fraction f* = (p*o - 1) / (o - 1), floored at zero."""
    probability = _single_decimal_probability(model_probability)
    price = _decimal_price(odds)
    return max(0.0, (probability * price - 1.0) / (price - 1.0))
