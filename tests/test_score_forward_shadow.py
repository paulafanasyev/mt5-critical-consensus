"""Tests for the forward shadow scorer: fills, charges, exits, dedupe and the decision rule."""
from __future__ import annotations

import functools
import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

UTC = timezone.utc


def _root() -> Path:
    return Path(__file__).resolve().parents[1]


def _load(name: str):
    path = _root() / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@functools.lru_cache(maxsize=1)
def _scorer():
    return _load("score_forward_shadow")


@functools.lru_cache(maxsize=1)
def _study():
    return _load("candidate_study")


score = _scorer()


def make_bars(rows, start=None):
    start = start or datetime(2026, 11, 2, 9, tzinfo=UTC)
    return [{"time": start + timedelta(hours=index), "o": o, "h": h, "l": l, "c": c, "spread": s}
            for index, (o, h, l, c, s) in enumerate(rows)]


def sell_proposal(**overrides):
    base = dict(symbol="EURUSD", timeframe="H1", evaluated_at=datetime(2026, 11, 2, 9, tzinfo=UTC),
               direction=-1, entry=1.12000, stop=1.12220, target=1.11560, risk_distance=0.00220,
               volume=0.2)
    base.update(overrides)
    return score.Proposal(**base)


def test_cost_model_matches_the_registered_study():
    study = _study()
    for symbol, spec in study.COSTS.items():
        assert score.SPREAD_FLOOR_POINTS[symbol] == spec["spread_floor_points"]
        assert score.POINT[symbol] == spec["point"]
        assert score.SLIPPAGE_BPS[symbol] == spec["slippage_bps"]
        assert score.SLIPPAGE_PRICE[symbol] == spec["slippage_price"]
    for symbol, rate in study.FINANCING_PER_YEAR.items():
        assert score.FINANCING_PER_YEAR[symbol] == rate


def test_entry_is_the_open_of_the_bar_after_the_signal_closes():
    bars = make_bars([(1.12000, 1.12100, 1.11900, 1.11950, 1),
                      (1.12005, 1.12100, 1.11900, 1.12050, 1),
                      (1.12050, 1.12150, 1.11950, 1.12000, 1)])
    # The 09:00 stamp is that bar's open; it closes at 10:00, so the fill is the 10:00 open.
    assert score.entry_index(bars, sell_proposal()) == 1


def test_stop_hit_costs_one_r_plus_the_measured_round_trip():
    bars = make_bars([(1.12000, 1.12100, 1.11900, 1.11950, 1.0),
                      (1.12005, 1.12300, 1.11900, 1.12100, 1.0)])
    trade = score.score([sell_proposal()], {"EURUSD": bars})[0]
    assert trade["reason"] == "stop"
    assert trade["entry"] == pytest.approx(1.12005)
    assert trade["exit"] == pytest.approx(1.12220)
    assert trade["gross_r"] == pytest.approx(-0.00215 / 0.00220)
    # spread floor 2 points x 0.00001 + two sides x 0.00005 slippage
    assert trade["cost_r"] == pytest.approx(0.00012 / 0.00220)
    assert trade["r"] == pytest.approx((-0.00215 - 0.00012) / 0.00220)


def test_target_hit_pays_the_reward_less_the_round_trip():
    bars = make_bars([(1.12000, 1.12100, 1.11900, 1.11950, 1.0),
                      (1.12005, 1.12100, 1.11500, 1.11600, 1.0)])
    trade = score.score([sell_proposal()], {"EURUSD": bars})[0]
    assert trade["reason"] == "target"
    assert trade["r"] == pytest.approx((0.00445 - 0.00012) / 0.00220)


def test_gap_through_the_stop_fills_at_the_open_not_at_the_level():
    bars = make_bars([(1.12000, 1.12100, 1.11900, 1.11950, 1.0),
                      (1.12005, 1.12100, 1.11900, 1.12050, 1.0),
                      (1.12500, 1.12600, 1.11900, 1.12000, 1.0)])
    trade = score.score([sell_proposal()], {"EURUSD": bars})[0]
    assert trade["reason"] == "stop_gap"
    assert trade["exit"] == pytest.approx(1.12500)
    # Worse than a full R: the gap is charged at the open, not capped at the stop.
    assert trade["r"] < -2.0


def test_time_exit_is_the_open_after_the_protected_window():
    bars = make_bars([(1.12000, 1.12100, 1.11900, 1.11950, 1.0),
                      (1.12005, 1.12100, 1.11900, 1.12050, 1.0),
                      (1.12050, 1.12150, 1.11950, 1.12000, 1.0),
                      (1.11800, 1.11900, 1.11700, 1.11750, 1.0)])
    trade = score.score([sell_proposal()], {"EURUSD": bars}, horizon=2)[0]
    assert trade["reason"] == "time"
    assert trade["exit"] == pytest.approx(1.11800)
    assert trade["exit_time"].startswith("2026-11-02T12")
    # Two bars held is 2/24 of a day, charged at the annual financing rate over 252 days.
    expected_financing = 1.12005 * 0.02 * (2 / 24.0) / 252.0
    assert trade["r"] == pytest.approx((0.00205 - 0.00012 - expected_financing) / 0.00220)


def test_run_out_of_bars_exits_at_the_last_close():
    bars = make_bars([(1.12000, 1.12100, 1.11900, 1.11950, 1.0),
                      (1.12005, 1.12100, 1.11900, 1.12050, 1.0)])
    trade = score.score([sell_proposal()], {"EURUSD": bars}, horizon=2)[0]
    assert trade["reason"] == "end_of_data"
    assert trade["exit"] == pytest.approx(1.12050)


def test_double_cost_doubles_every_charge():
    bars = make_bars([(1.12000, 1.12100, 1.11900, 1.11950, 1.0),
                      (1.12005, 1.12300, 1.11900, 1.12100, 1.0)])
    single = score.score([sell_proposal()], {"EURUSD": bars})[0]
    double = score.score([sell_proposal()], {"EURUSD": bars}, cost_multiple=2.0)[0]
    assert double["cost_r"] == pytest.approx(2.0 * single["cost_r"])
    assert double["r"] == pytest.approx(single["gross_r"] - 2.0 * single["cost_r"])


def test_a_proposal_without_bars_after_it_is_not_scored():
    bars = make_bars([(1.12000, 1.12100, 1.11900, 1.11950, 1.0)])
    assert score.score([sell_proposal()], {"EURUSD": bars}) == []


def test_journal_samples_of_one_bar_collapse_to_one_proposal(tmp_path):
    from critical_consensus.mt5_agent.journal import DecisionJournal

    path = tmp_path / "shadow.db"
    journal = DecisionJournal(path)
    first = {"status": "OBSERVED", "proposal": {"symbol": "EURUSD", "timeframe": "H1",
             "evaluated_at": "2026-11-02T09:00:00+00:00", "direction": "SELL",
             "entry_price": 1.12, "stop_loss": 1.1222, "take_profit": 1.1156,
             "risk_distance": 0.0022}, "sizing": {"volume": 0.2}}
    second = json.loads(json.dumps(first))
    second["proposal"]["evaluated_at"] = "2026-11-02T10:00:00+00:00"
    journal.record("agent_cycle", {"status": "BLOCKED", "proposal": None})
    journal.record("agent_cycle", first)
    journal.record("agent_cycle", first)
    journal.record("agent_cycle", second)

    proposals = score.proposals_from_journal(path)
    assert [p.evaluated_at for p in proposals] == [datetime(2026, 11, 2, 9, tzinfo=UTC),
                                                   datetime(2026, 11, 2, 10, tzinfo=UTC)]
    assert proposals[0].direction == -1
    assert proposals[0].volume == 0.2
    assert len(score.proposals_from_journal(path, after=datetime(2026, 11, 2, 10, tzinfo=UTC))) == 1


def test_bootstrap_is_deterministic_and_collapses_when_days_are_identical():
    trades = [{"cluster": f"2026-11-0{day}", "r": 0.5} for day in range(1, 6) for _ in range(3)]
    first = score.cluster_bootstrap(trades, score.CONFIDENCE)
    second = score.cluster_bootstrap(trades, score.CONFIDENCE)
    assert first == second
    assert first[0] == pytest.approx(0.5)
    assert first[1] == pytest.approx(0.5)
    assert first[2] == pytest.approx(0.5)


def test_verdict_needs_sample_days_interval_and_double_cost():
    base = {"n": score.MIN_PROPOSALS, "days": score.MIN_DAYS, "low_r": 0.01, "high_r": 0.4,
            "sd": 1.0, "mean_r": 0.2}
    assert score.verdict(base, 0.1)["passed"] is True
    assert score.verdict(dict(base, n=10), 0.1)["passed"] is False
    assert score.verdict(dict(base, days=5), 0.1)["passed"] is False
    assert score.verdict(dict(base, low_r=-0.01), 0.1)["passed"] is False
    assert score.verdict(base, -0.01)["passed"] is False
    empty = score.verdict({"n": 0, "days": 0, "low_r": 0.0, "high_r": 0.0, "sd": 0.0,
                           "mean_r": 0.0}, 0.0)
    assert empty["status"] == "insufficient"


def test_detectable_edge_shrinks_with_days_not_with_trades():
    summary = {"sd": 1.0, "days": 60}
    assert score.detectable_r(summary) == pytest.approx(2.39398 / 60 ** 0.5, rel=1e-3)
    assert score.detectable_r(dict(summary, days=240)) < score.detectable_r(summary)
