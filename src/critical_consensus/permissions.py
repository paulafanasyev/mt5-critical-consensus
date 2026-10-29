"""Explicit execution permission checks; live execution is disabled by default.

This module evaluates policy metadata only; it never sends orders. A passing result
must not be treated as proof that an execution adapter is safe to use.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


@dataclass(frozen=True)
class OrderIntent:
    symbol: str
    direction: str
    volume: float
    stop_loss: float | None
    take_profit: float | None
    user_approved: bool = False
    signed: bool = False
    expected_price: float | None = None


@dataclass(frozen=True)
class PermissionResult:
    allowed: bool
    reasons: tuple[str, ...]


def authorize_order(
    intent: OrderIntent,
    *,
    allowed_symbols: tuple[str, ...] = ("EURUSD", "XAUUSD"),
    max_volume: float = 0.20,
    demo_enabled: bool = True,
    live_enabled: bool = False,
    live_mode: bool = False,
) -> PermissionResult:
    """Check a proposed intent; never dispatch or create an order.

    Price and volume fields must be finite real numbers. Stop loss and take profit
    must bracket an explicit expected entry price in the correct direction.
    """
    reasons: list[str] = []
    if not isinstance(intent.symbol, str) or intent.symbol not in allowed_symbols:
        reasons.append("symbol is not allow-listed")
    if intent.direction not in {"BUY", "SELL"}:
        reasons.append("direction must be BUY or SELL")

    volume = _finite_number(intent.volume)
    volume_limit = _finite_number(max_volume)
    if volume is None or volume_limit is None or volume <= 0 or volume_limit <= 0 or volume > volume_limit:
        reasons.append("volume outside permitted range")

    stop = _finite_number(intent.stop_loss)
    take = _finite_number(intent.take_profit)
    entry = _finite_number(intent.expected_price)
    if stop is None:
        reasons.append("stop loss is required and must be finite")
    if take is None:
        reasons.append("take profit is required and must be finite")
    if entry is None or entry <= 0:
        reasons.append("expected entry price is required and must be finite and positive")
    if stop is not None and take is not None and entry is not None and entry > 0:
        if intent.direction == "BUY":
            geometry_valid = 0 < stop < entry < take
        elif intent.direction == "SELL":
            geometry_valid = 0 < take < entry < stop
        else:
            geometry_valid = False
        if not geometry_valid:
            reasons.append("stop loss and take profit geometry is invalid")

    if intent.user_approved is not True:
        reasons.append("explicit user approval is required")
    if live_mode and live_enabled is not True:
        reasons.append("live execution is disabled by policy")
    if live_mode and intent.signed is not True:
        reasons.append("live order requires a valid signature")
    if not live_mode and demo_enabled is not True:
        reasons.append("demo execution is disabled by policy")
    return PermissionResult(not reasons, tuple(reasons))
