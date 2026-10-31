"""Volume sizing from the broker's own tick specification, never from a declared percent."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .backend import SymbolSpec
from .codes import MARGIN_MINIMUM_LOT_UNFUNDED, MINIMUM_LOT_EXCEEDS_RISK_BUDGET


@dataclass(frozen=True)
class SizedOrder:
    volume: float
    risk_value: float
    risk_fraction: float
    risk_per_lot: float
    margin_required: float

    def as_dict(self) -> dict[str, Any]:
        return {"volume": self.volume, "risk_value": self.risk_value,
                "risk_fraction": self.risk_fraction, "risk_per_lot": self.risk_per_lot,
                "margin_required": self.margin_required}


@dataclass(frozen=True)
class SizingResult:
    """An order, or the reason the trade was refused. Never a silently smaller risk."""

    order: SizedOrder | None
    denial: str = ""

    @property
    def accepted(self) -> bool:
        return self.order is not None

    def as_dict(self) -> dict[str, Any] | None:
        return self.order.as_dict() if self.order is not None else None


def size_position(*, spec: SymbolSpec, equity: float, risk_fraction_limit: float,
                  stop_distance: float, leverage: int, margin_available: float,
                  price: float) -> SizingResult:
    """Largest stepped volume whose stop-out loss and margin both stay inside the account.

    A stop distance is only half the story: the same risk on a very tight stop implies a lot
    the broker will refuse (or that consumes the whole account as margin), so the volume is
    also capped by the free margin the terminal reports. ``price`` is the entry being sized
    for: the broker's reserved margin scales with the instrument's notional, so sizing a
    EURUSD trade and a gold trade at the same constant would be wrong for both. A refusal is
    a denial, never a silent oversize.
    """
    if not math.isfinite(equity) or equity <= 0:
        raise ValueError("equity must be positive and finite")
    if not math.isfinite(risk_fraction_limit) or not 0 < risk_fraction_limit <= 1:
        raise ValueError("risk_fraction_limit must be in (0, 1]")
    if not math.isfinite(stop_distance) or stop_distance <= 0:
        raise ValueError("stop_distance must be positive and finite")
    if not math.isfinite(margin_available):
        raise ValueError("margin_available must be finite")
    if not math.isfinite(price) or price <= 0:
        raise ValueError("price must be positive and finite")

    risk_per_lot = stop_distance / spec.tick_size * spec.tick_value
    if not math.isfinite(risk_per_lot) or risk_per_lot <= 0:
        raise ValueError("computed risk per lot is not positive and finite")
    budget = equity * risk_fraction_limit
    minimum_margin = spec.margin_requirement(spec.volume_min, leverage, price)
    if minimum_margin > margin_available:
        return SizingResult(None, MARGIN_MINIMUM_LOT_UNFUNDED)
    if risk_per_lot * spec.volume_min > budget:
        return SizingResult(None, MINIMUM_LOT_EXCEEDS_RISK_BUDGET)

    steps = math.floor(budget / (risk_per_lot * spec.volume_step) + 1e-12)
    volume = spec.price_to_volume(steps * spec.volume_step)
    if volume <= 0:
        return SizingResult(None, MINIMUM_LOT_EXCEEDS_RISK_BUDGET)
    denied_by_margin = False
    while volume > 0:
        over_risk = volume * risk_per_lot > budget
        over_margin = spec.margin_requirement(volume, leverage, price) > margin_available
        if not over_risk and not over_margin:
            break
        denied_by_margin = over_margin
        volume = spec.price_to_volume(volume - spec.volume_step)
    if volume < spec.volume_min:
        # Report the constraint that eliminated the last candidate lot; a margin refusal
        # labelled as a risk-budget problem sends the operator to the wrong setting.
        return SizingResult(None, MARGIN_MINIMUM_LOT_UNFUNDED if denied_by_margin
                            else MINIMUM_LOT_EXCEEDS_RISK_BUDGET)
    risk_value = volume * risk_per_lot
    return SizingResult(SizedOrder(volume=volume, risk_value=risk_value,
                                   risk_fraction=risk_value / equity,
                                   risk_per_lot=risk_per_lot,
                                   margin_required=spec.margin_requirement(
                                       volume, leverage, price)))
