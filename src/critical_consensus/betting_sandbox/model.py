"""A small, explainable Poisson baseline for 1X2 football probabilities.

The model is deliberately simple: time-decayed attack/defence ratios plus a
home boost, combined as two independent Poisson counts. It is a benchmark, not
a proven edge, and it must be evaluated walk-forward out of sample.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from math import exp, isfinite, sqrt
from typing import Iterable, Mapping, Protocol

LEAGUE_AVERAGE_PRIOR_MATCHES = 4.0
MAX_GOALS = 10
HALF_LIFE_DAYS = 180.0


class _MatchLike(Protocol):
    date: date
    home: str
    away: str
    home_goals: int
    away_goals: int


@dataclass(frozen=True)
class Prediction:
    p_home: float
    p_draw: float
    p_away: float
    lambda_home: float
    lambda_away: float
    matches_used: int

    def as_dict(self) -> dict[str, float | int]:
        return {
            "p_home": self.p_home,
            "p_draw": self.p_draw,
            "p_away": self.p_away,
            "lambda_home": self.lambda_home,
            "lambda_away": self.lambda_away,
            "matches_used": self.matches_used,
        }


@dataclass(frozen=True)
class PoissonModel:
    league_average_goals: float
    home_boost: float
    attacks: Mapping[str, float] = field(default_factory=dict)
    defences: Mapping[str, float] = field(default_factory=dict)
    matches_used: int = 0

    def expected_goals(self, home: str, away: str) -> tuple[float, float]:
        home_attack = self.attacks.get(home, 1.0)
        away_attack = self.attacks.get(away, 1.0)
        home_defence = self.defences.get(home, 1.0)
        away_defence = self.defences.get(away, 1.0)
        base = self.league_average_goals
        lambda_home = base * home_attack * away_defence * self.home_boost
        lambda_away = base * away_attack * home_defence / self.home_boost
        return _clamp(lambda_home, 0.05, 6.0), _clamp(lambda_away, 0.05, 6.0)

    def predict(self, home: str, away: str) -> Prediction:
        lambda_home, lambda_away = self.expected_goals(home, away)
        home_goals = _poisson_pmf(lambda_home)
        away_goals = _poisson_pmf(lambda_away)
        p_home = p_draw = p_away = 0.0
        for home_score, home_prob in enumerate(home_goals):
            for away_score, away_prob in enumerate(away_goals):
                joint = home_prob * away_prob
                if home_score > away_score:
                    p_home += joint
                elif home_score == away_score:
                    p_draw += joint
                else:
                    p_away += joint
        total = p_home + p_draw + p_away
        if total <= 0 or not isfinite(total):
            return Prediction(1 / 3, 1 / 3, 1 / 3, lambda_home, lambda_away, self.matches_used)
        return Prediction(p_home / total, p_draw / total, p_away / total,
                          lambda_home, lambda_away, self.matches_used)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _poisson_pmf(mean: float) -> list[float]:
    probabilities = [exp(-mean)]
    for goals in range(1, MAX_GOALS + 1):
        probabilities.append(probabilities[-1] * mean / goals)
    return probabilities


def fit_model(matches: Iterable[_MatchLike], *, as_of: date | None = None,
              half_life_days: float = HALF_LIFE_DAYS,
              prior_matches: float = LEAGUE_AVERAGE_PRIOR_MATCHES) -> PoissonModel:
    """Fit time-decayed goal ratings using only matches strictly before ``as_of``."""
    if half_life_days <= 0 or prior_matches < 0:
        raise ValueError("half_life_days must be positive and prior_matches non-negative")
    history = [match for match in matches if as_of is None or match.date < as_of]
    history = [match for match in history if _valid_result(match)]
    if not history:
        return PoissonModel(league_average_goals=1.35, home_boost=1.15)

    newest = max(match.date for match in history)
    decay = 0.6931471805599453 / half_life_days

    total_weight = 0.0
    home_goals = 0.0
    away_goals = 0.0
    per_team: dict[str, list[float]] = {}

    for match in history:
        age_days = max(0, (newest - match.date).days)
        weight = exp(-decay * age_days)
        total_weight += weight
        home_goals += weight * match.home_goals
        away_goals += weight * match.away_goals
        _accumulate(per_team, match.home, weight, match.home_goals, match.away_goals)
        _accumulate(per_team, match.away, weight, match.away_goals, match.home_goals)

    if total_weight <= 0:
        return PoissonModel(league_average_goals=1.35, home_boost=1.15)

    league_average = (home_goals + away_goals) / (2.0 * total_weight)
    league_average = _clamp(league_average, 0.3, 4.0)
    if away_goals > 0 and home_goals > 0:
        home_boost = _clamp(sqrt(home_goals / away_goals), 1.0, 1.6)
    else:
        home_boost = 1.15

    attacks: dict[str, float] = {}
    defences: dict[str, float] = {}
    for team, (weight, scored, conceded) in per_team.items():
        denom = (weight + prior_matches) * league_average
        attacks[team] = _clamp((scored + prior_matches * league_average) / denom, 0.3, 3.0)
        defences[team] = _clamp((conceded + prior_matches * league_average) / denom, 0.3, 3.0)

    return PoissonModel(league_average, home_boost, attacks, defences, len(history))


def _accumulate(store: dict[str, list[float]], team: str, weight: float,
                scored: int, conceded: int) -> None:
    entry = store.setdefault(team, [0.0, 0.0, 0.0])
    entry[0] += weight
    entry[1] += weight * scored
    entry[2] += weight * conceded


def _valid_result(match: _MatchLike) -> bool:
    try:
        home = int(match.home_goals)
        away = int(match.away_goals)
    except (TypeError, ValueError):
        return False
    return home >= 0 and away >= 0
