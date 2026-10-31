"""Deterministic, transparent signal generation from MT5 bars.

The score reported as ``confidence`` is a heuristic ranking of the rule inputs. It is
not a calibrated probability and must never be read as one by a downstream gate.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Sequence

from ..market_data import MarketBar, validate_bar_series
from . import codes
from .backend import TIMEFRAME_DURATION, SymbolSpec, Tick
from .policy import StrategyConfig

# A closed bar is stamped with its open time, so it can legitimately sit a few seconds ahead of
# the tick that arrived with it. Anything larger means the two clocks disagree.
_BAR_TICK_TOLERANCE = timedelta(seconds=5)


@dataclass(frozen=True)
class Proposal:
    symbol: str
    timeframe: str
    direction: str
    evaluated_at: datetime
    entry_price: float
    stop_loss: float
    take_profit: float
    risk_distance: float
    reward_distance: float
    risk_reward: float
    atr: float
    confidence: float
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {"symbol": self.symbol, "timeframe": self.timeframe, "direction": self.direction,
                "evaluated_at": self.evaluated_at.isoformat(), "entry_price": self.entry_price,
                "stop_loss": self.stop_loss, "take_profit": self.take_profit,
                "risk_distance": self.risk_distance, "reward_distance": self.reward_distance,
                "risk_reward": self.risk_reward, "atr": self.atr, "confidence": self.confidence,
                "reasons": list(self.reasons)}


@dataclass(frozen=True)
class SignalEvaluation:
    proposal: Proposal | None
    blockers: tuple[str, ...]
    features: dict[str, Any]

    @property
    def has_proposal(self) -> bool:
        return self.proposal is not None

    @property
    def data_integrity_failure(self) -> bool:
        """True when the inputs are unusable, as distinct from the market offering no signal."""
        return any(blocker in codes.DATA_INTEGRITY_CODES for blocker in self.blockers)


def _sma(values: Sequence[float], period: int) -> float:
    window = values[-period:]
    return sum(window) / len(window)


def _true_ranges(bars: Sequence[MarketBar]) -> list[float]:
    ranges = []
    for index in range(1, len(bars)):
        previous_close = bars[index - 1].close
        ranges.append(max(bars[index].high - bars[index].low,
                          abs(bars[index].high - previous_close),
                          abs(bars[index].low - previous_close)))
    return ranges


def _blocked(blockers: tuple[str, ...], features: dict[str, Any]) -> SignalEvaluation:
    return SignalEvaluation(None, blockers, features)


def evaluate_signal(bars: Sequence[MarketBar], spec: SymbolSpec, tick: Tick,
                    config: StrategyConfig, *, max_spread_points: int,
                    expected_timeframe: str) -> SignalEvaluation:
    """Return a proposal only when every input and geometry check passes."""
    rows_input = list(bars)
    try:
        # Validate the series as supplied, before any symbol is dropped: filtering first would
        # let a feed that mixes symbols look like a short history instead of a broken source.
        validate_bar_series(rows_input)
    except ValueError:
        return _blocked((codes.BAR_SERIES_INVALID,), {})

    rows = [bar for bar in rows_input if bar.symbol == spec.symbol]
    # The series must be the cadence the policy asked for: indicators, the stale-bar horizon
    # and the journal's timeframe all derive from the label, so a backend that silently serves
    # another timeframe would size and time the signal against the wrong market.
    if any(bar.timeframe != expected_timeframe for bar in rows):
        return _blocked((codes.BAR_TIMEFRAME_MISMATCH,), {"bar_count": len(rows)})
    if len(rows) < config.min_bars:
        return _blocked((codes.INSUFFICIENT_BARS,), {"bar_count": len(rows)})
    if tick.symbol != spec.symbol:
        return _blocked((codes.BAR_SYMBOL_MISMATCH,), {"bar_count": len(rows)})
    # The backend returns closed bars, so the newest one closed at most one period before now.
    # A tick two periods past its open means a whole bar is missing: every indicator below would
    # describe a market that no longer exists, and a generated signal would be built on it.
    duration = TIMEFRAME_DURATION.get(rows[-1].timeframe)
    if duration is not None and tick.server_time - rows[-1].time > 2 * duration:
        return _blocked((codes.BAR_TIME_TOO_OLD,), {"bar_count": len(rows)})

    closes = [bar.close for bar in rows]
    ranges = _true_ranges(rows)
    atr_window = ranges[-config.atr_period:]
    atr = sum(atr_window) / len(atr_window)
    if not math.isfinite(atr) or atr <= 0:
        return _blocked((codes.ATR_NOT_MEANINGFUL,), {"bar_count": len(rows)})

    fast = _sma(closes, config.sma_fast)
    slow = _sma(closes, config.sma_slow)
    gap = (fast - slow) / atr
    momentum_bars = min(config.sma_fast, len(closes) - 1)
    previous_close = closes[-1 - momentum_bars]
    if previous_close <= 0:
        return _blocked((codes.PRIOR_CLOSE_NON_POSITIVE,), {})
    momentum = closes[-1] / previous_close - 1.0

    spread_points = (tick.ask - tick.bid) / spec.point
    blockers: list[str] = []
    if spread_points > max_spread_points:
        blockers.append(codes.SPREAD_ABOVE_LIMIT)

    if gap >= config.min_sma_gap_atr and momentum > config.min_momentum_abs:
        direction = "BUY"
    elif gap <= -config.min_sma_gap_atr and momentum < -config.min_momentum_abs:
        direction = "SELL"
    else:
        direction = ""
        blockers.append(codes.NO_TREND_SIGNAL)

    features = {"bar_count": len(rows), "last_close": closes[-1], "sma_fast": fast,
                "sma_slow": slow, "sma_gap_atr": gap, "momentum": momentum, "atr": atr,
                "spread_points": spread_points, "direction": direction}
    if blockers or not direction:
        return _blocked(tuple(sorted(set(blockers))), features)

    # Bars and ticks reach us already converted to UTC by the backend, so the two are directly
    # comparable; a bar stamped after the quote that closed it is a clock or offset error.
    if rows[-1].time > tick.server_time + _BAR_TICK_TOLERANCE:
        return _blocked((codes.BAR_TIME_IN_FUTURE,), features)

    entry = tick.ask if direction == "BUY" else tick.bid
    stop = entry - atr * config.stop_atr_multiple if direction == "BUY" \
        else entry + atr * config.stop_atr_multiple
    risk_distance = abs(entry - stop)
    take = entry + risk_distance * config.take_profit_risk_multiple if direction == "BUY" \
        else entry - risk_distance * config.take_profit_risk_multiple
    # A stop below zero is not a level any broker accepts; ATR on a low-priced instrument can
    # push it there while every geometry comparison still looks well-formed.
    if stop <= 0 or not math.isfinite(stop):
        return _blocked((codes.STOP_DISTANCE_INVALID,), features)
    if risk_distance <= 0 or not math.isfinite(risk_distance):
        return _blocked((codes.STOP_DISTANCE_INVALID,), features)
    if take <= 0 or not math.isfinite(take):
        return _blocked((codes.TAKE_PROFIT_PRICE_INVALID,), features)
    if direction == "BUY" and not stop < entry < take:
        return _blocked((codes.BUY_GEOMETRY_INVALID,), features)
    if direction == "SELL" and not take < entry < stop:
        return _blocked((codes.SELL_GEOMETRY_INVALID,), features)
    if spec.stops_level_points:
        # The broker refuses each level individually: a stop and a target both have to clear the
        # stops level, and checking only the stop admits an order the terminal would reject.
        if risk_distance / spec.point < spec.stops_level_points:
            return _blocked((codes.STOP_INSIDE_BROKER_STOPS_LEVEL,), features)
        if abs(take - entry) / spec.point < spec.stops_level_points:
            return _blocked((codes.TARGET_INSIDE_BROKER_STOPS_LEVEL,), features)

    strength = min(abs(gap), 3.0) / 3.0 * 0.6 + min(abs(momentum) * 100.0, 1.0) * 0.4
    confidence = round(min(0.5 + 0.5 * strength, config.max_confidence), 4)
    reasons = (
        f"sma_{config.sma_fast}_minus_sma_{config.sma_slow}={gap:+.3f}*ATR",
        f"momentum_{momentum_bars}={momentum:+.5f}",
        f"atr_{config.atr_period}={atr:.8f}",
        f"spread_points={spread_points:.1f}",
        "heuristic_score_not_calibrated_probability",
    )
    proposal = Proposal(symbol=spec.symbol, timeframe=rows[-1].timeframe, direction=direction,
                        evaluated_at=rows[-1].time, entry_price=entry, stop_loss=stop,
                        take_profit=take, risk_distance=risk_distance,
                        reward_distance=abs(take - entry),
                        risk_reward=abs(take - entry) / risk_distance, atr=atr,
                        confidence=confidence, reasons=reasons)
    return SignalEvaluation(proposal, (), features)
