"""The strategy families share one geometry and differ only in the rule that names a side.

Each case below feeds a series that only the family under test can read as a signal,
so a variant that silently falls back to the SMA rule would fail instead of passing
with the same answer as the default.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from critical_consensus.market_data import MarketBar
from critical_consensus.mt5_agent import SymbolSpec, Tick, evaluate_signal
from critical_consensus.mt5_agent.policy import StrategyConfig

UTC = timezone.utc
SPEC = SymbolSpec(symbol="EURUSD", point=0.00001, digits=5, tick_size=0.00001, tick_value=1.0,
                  volume_min=0.01, volume_max=100.0, volume_step=0.01, spread_points=10,
                  stops_level_points=0)
START = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)


def bars_from(closes: list[float], symbol: str = "EURUSD") -> list[MarketBar]:
    rows = []
    for index, close in enumerate(closes):
        previous = closes[index - 1] if index else close
        rows.append(MarketBar(
            symbol=symbol, timeframe="H1", time=START + timedelta(hours=index),
            open=previous, high=max(previous, close) + 0.0001,
            low=min(previous, close) - 0.0001, close=close,
            tick_volume=1_000, spread_points=10))
    return rows


def tick_at_last(rows: list[MarketBar]) -> Tick:
    last = rows[-1].close
    return Tick(symbol="EURUSD", bid=last, ask=last + 0.0001,
                server_time=rows[-1].time + timedelta(seconds=1), server_utc_offset_seconds=0)


def evaluate(closes: list[float], **strategy):
    rows = bars_from(closes)
    config = StrategyConfig(**strategy)
    return evaluate_signal(rows, SPEC, tick_at_last(rows), config,
                           max_spread_points=25, expected_timeframe="H1")


def alternating(count: int) -> list[float]:
    return [1.0995 if index % 2 else 1.1005 for index in range(count)]


def test_an_unknown_family_is_refused():
    with pytest.raises(ValueError):
        StrategyConfig(family="supertrend")


def test_min_bars_must_cover_the_family_lookback():
    with pytest.raises(ValueError):
        StrategyConfig(family="bollinger", bollinger_period=200, min_bars=120)
    with pytest.raises(ValueError):
        StrategyConfig(family="donchian", donchian_period=200, min_bars=120)


@pytest.mark.parametrize("family", ["sma", "ema", "bollinger", "donchian"])
def test_every_family_reports_itself_in_the_features(family):
    closes = [1.10 + 0.0005 * index for index in range(120)]
    evaluation = evaluate(closes, family=family)
    assert evaluation.features["family"] == family


def test_ema_family_signals_a_rising_series():
    closes = [1.10 + 0.0004 * index for index in range(120)]
    evaluation = evaluate(closes, family="ema", ema_fast=20, ema_slow=50)
    assert evaluation.proposal is not None, evaluation.blockers
    assert evaluation.proposal.direction == "BUY"
    assert any(reason.startswith("ema.ema_gap_atr=") for reason in evaluation.proposal.reasons)


def test_bollinger_family_buys_a_close_below_the_lower_band():
    closes = alternating(120) + [1.0970]
    evaluation = evaluate(closes, family="bollinger", bollinger_period=20, bollinger_dev=2.0)
    assert evaluation.proposal is not None, evaluation.blockers
    assert evaluation.proposal.direction == "BUY"
    assert evaluation.features["family_features"]["band_distance"] < -1.0


def test_bollinger_family_sells_a_close_above_the_upper_band():
    closes = alternating(120) + [1.1030]
    evaluation = evaluate(closes, family="bollinger", bollinger_period=20, bollinger_dev=2.0)
    assert evaluation.proposal is not None, evaluation.blockers
    assert evaluation.proposal.direction == "SELL"


def test_bollinger_family_stays_inside_the_bands():
    closes = alternating(120)
    evaluation = evaluate(closes, family="bollinger")
    assert evaluation.proposal is None
    assert "NO_TREND_SIGNAL" in evaluation.blockers


def test_donchian_family_buys_a_new_high():
    closes = alternating(120) + [1.1015]
    evaluation = evaluate(closes, family="donchian", donchian_period=20)
    assert evaluation.proposal is not None, evaluation.blockers
    assert evaluation.proposal.direction == "BUY"


def test_donchian_family_sells_a_new_low():
    closes = alternating(120) + [1.0980]
    evaluation = evaluate(closes, family="donchian", donchian_period=20)
    assert evaluation.proposal is not None, evaluation.blockers
    assert evaluation.proposal.direction == "SELL"


def test_donchian_family_stays_inside_the_channel():
    closes = alternating(120)
    evaluation = evaluate(closes, family="donchian", donchian_period=20)
    assert evaluation.proposal is None
    assert "NO_TREND_SIGNAL" in evaluation.blockers


def test_a_stop_is_never_inside_the_spread():
    """A quiet hour can leave an ATR smaller than the spread; the stop is floored at it."""
    closes = [1.10 + 0.00005 * index for index in range(120)]
    rows = bars_from(closes)
    wide = Tick(symbol="EURUSD", bid=rows[-1].close, ask=rows[-1].close + 0.0005,
                server_time=rows[-1].time + timedelta(seconds=1), server_utc_offset_seconds=0)
    evaluation = evaluate_signal(rows, SPEC, wide, StrategyConfig(family="ema"),
                                 max_spread_points=1_000, expected_timeframe="H1")
    assert evaluation.proposal is not None, evaluation.blockers
    assert evaluation.proposal.risk_distance >= 3 * 0.0005 - 1e-12
