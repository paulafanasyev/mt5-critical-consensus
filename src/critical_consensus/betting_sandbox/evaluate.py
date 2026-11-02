"""Walk-forward evaluation and honest performance metrics for the sandbox."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Iterable, Sequence

from .data import Match
from .model import Prediction, fit_model
from .pricing import implied_probabilities
from .staking import StakingPlan, decide_stake

SELECTION_INDEX = {"HOME": 0, "DRAW": 1, "AWAY": 2}
SELECTION_RESULT = {"HOME": "H", "DRAW": "D", "AWAY": "A"}


@dataclass(frozen=True)
class BetRecord:
    match_id: str
    match_date: date
    division: str
    home: str
    away: str
    selection: str
    odds: float
    closing_odds: float | None
    stake_units: int
    model_probability: float
    market_probability: float
    edge: float
    won: bool
    payout_units: int
    clv: float | None


@dataclass(frozen=True)
class WalkForwardResult:
    bets: list[BetRecord]
    bankroll_curve: list[int]
    initial_bankroll_units: int
    final_bankroll_units: int
    matches_considered: int
    warmup_matches: int


def walk_forward(matches: Sequence[Match], *, initial_bankroll_units: int,
                 plan: StakingPlan, warmup_matches: int = 80,
                 half_life_days: float = 180.0,
                 odds_method: str = "proportional") -> WalkForwardResult:
    """Bet one selection per match using only data available before that match."""
    if initial_bankroll_units < 0:
        raise ValueError("initial_bankroll_units must be non-negative")
    if warmup_matches < 0:
        raise ValueError("warmup_matches must be non-negative")
    ordered = sorted(matches, key=lambda match: (match.date, match.match_id))
    bankroll = initial_bankroll_units
    curve = [bankroll]
    records: list[BetRecord] = []
    model_cache: dict[date, object] = {}

    for index, match in enumerate(ordered):
        if index < warmup_matches or bankroll <= 0:
            curve.append(bankroll)
            continue
        model = model_cache.get(match.date)
        if model is None:
            model = fit_model(ordered[:index], as_of=match.date, half_life_days=half_life_days)
            model_cache[match.date] = model
        prediction: Prediction = model.predict(match.home, match.away)  # type: ignore[attr-defined]
        model_probabilities = (prediction.p_home, prediction.p_draw, prediction.p_away)
        market_probabilities = implied_probabilities(
            [float(value) for value in match.odds], method=odds_method)
        closing = match.closing_odds

        candidates = []
        for selection, position in SELECTION_INDEX.items():
            decision = decide_stake(plan, model_probabilities[position],
                                    float(match.odds[position]), bankroll)
            if decision.stake_units > 0:
                candidates.append((decision.edge, selection, position, decision.stake_units))
        if candidates:
            edge, selection, position, stake = max(candidates, key=lambda item: (item[0], item[1]))
            stake = min(stake, bankroll)
            won = SELECTION_RESULT[selection] == match.result
            payout = int(round(float(match.odds[position]) * stake)) if won else 0
            bankroll += payout - stake
            close_price = float(closing[position]) if closing is not None else None
            clv = (float(match.odds[position]) / close_price - 1.0) if close_price else None
            records.append(BetRecord(
                match_id=match.match_id, match_date=match.date, division=match.division,
                home=match.home, away=match.away, selection=selection,
                odds=float(match.odds[position]), closing_odds=close_price,
                stake_units=stake, model_probability=model_probabilities[position],
                market_probability=market_probabilities[position], edge=edge,
                won=won, payout_units=payout, clv=clv,
            ))
        curve.append(bankroll)

    return WalkForwardResult(records, curve, initial_bankroll_units, bankroll,
                             len(ordered), min(warmup_matches, len(ordered)))


def summarize(bets: Iterable[BetRecord], *, initial_bankroll_units: int,
              bankroll_curve: Sequence[int] | None = None) -> dict[str, object]:
    records = list(bets)
    staked = sum(record.stake_units for record in records)
    returned = sum(record.payout_units for record in records)
    wins = sum(1 for record in records if record.won)
    n = len(records)
    brier = (sum((record.model_probability - (1.0 if record.won else 0.0)) ** 2
                 for record in records) / n) if n else None
    log_loss = None
    if n:
        total = 0.0
        for record in records:
            probability = min(max(record.model_probability, 1e-9), 1.0 - 1e-9)
            total -= math.log(probability) if record.won else math.log(1.0 - probability)
        log_loss = total / n
    clv_values = [record.clv for record in records if record.clv is not None]
    curve = list(bankroll_curve) if bankroll_curve is not None else [initial_bankroll_units]
    max_drawdown = 0
    peak = curve[0] if curve else initial_bankroll_units
    for value in curve:
        peak = max(peak, value)
        max_drawdown = max(max_drawdown, peak - value)
    final_bankroll = curve[-1] if curve else initial_bankroll_units
    return {
        "bets": n,
        "staked_units": staked,
        "returned_units": returned,
        "profit_units": returned - staked,
        "roi": ((returned - staked) / staked) if staked else 0.0,
        "wins": wins,
        "hit_rate": (wins / n) if n else 0.0,
        "hit_rate_ci95": wilson_interval(wins, n) if n else [0.0, 0.0],
        "brier_selection": brier,
        "log_loss_selection": log_loss,
        "mean_clv": (sum(clv_values) / len(clv_values)) if clv_values else None,
        "clv_samples": len(clv_values),
        "clv_positive_share": (sum(1 for value in clv_values if value > 0) / len(clv_values))
        if clv_values else None,
        "max_drawdown_units": max_drawdown,
        "initial_bankroll_units": initial_bankroll_units,
        "final_bankroll_units": final_bankroll,
        "note": ("Ket qua mo phong tren du lieu lich su; khong bao dam loi nhuan. "
                 "Khong phai tien that."),
    }


def wilson_interval(successes: int, trials: int, *, z: float = 1.96) -> list[float]:
    if trials <= 0:
        return [0.0, 0.0]
    if not 0 <= successes <= trials:
        raise ValueError("successes must be between 0 and trials")
    p = successes / trials
    denominator = 1.0 + z * z / trials
    centre = (p + z * z / (2 * trials)) / denominator
    margin = z * math.sqrt(p * (1 - p) / trials + z * z / (4 * trials * trials)) / denominator
    return [max(0.0, centre - margin), min(1.0, centre + margin)]
