"""Explicit execution permission checks; live execution is disabled by default."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OrderIntent:
    symbol: str
    direction: str
    volume: float
    stop_loss: float | None
    take_profit: float | None
    user_approved: bool = False
    signed: bool = False


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
    reasons: list[str] = []
    if intent.symbol not in allowed_symbols:
        reasons.append("symbol is not allow-listed")
    if intent.direction not in {"BUY", "SELL"}:
        reasons.append("direction must be BUY or SELL")
    if not 0 < intent.volume <= max_volume:
        reasons.append("volume outside permitted range")
    if intent.stop_loss is None:
        reasons.append("stop loss is required")
    if intent.take_profit is None:
        reasons.append("take profit is required")
    if not intent.user_approved:
        reasons.append("explicit user approval is required")
    if live_mode and not live_enabled:
        reasons.append("live execution is disabled by policy")
    if live_mode and not intent.signed:
        reasons.append("live order requires a valid signature")
    if not live_mode and not demo_enabled:
        reasons.append("demo execution is disabled by policy")
    return PermissionResult(not reasons, tuple(reasons))
