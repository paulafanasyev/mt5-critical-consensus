"""The candidate study must be deterministic, cost-honest and free of look-ahead.

Two independent implementations of the two non-calendar rules are compared trade by trade,
the engine's fill rules are pinned on hand-built bars, and the real cache is checked for
structural invariants only -- the tests never assert a performance number, because a test
that encodes a result would freeze the answer the study is supposed to discover.
"""
from __future__ import annotations

import importlib.util
import json
from datetime import date
from pathlib import Path

import pytest


def _root() -> Path:
    return Path(__file__).resolve().parents[1]


def _study():
    path = _root() / "scripts" / "candidate_study.py"
    spec = importlib.util.spec_from_file_location("candidate_study", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _bar(day: str, o: float, h: float, l: float, c: float, spread: int = 0) -> dict:
    return {"t": 0, "d": day, "date": date.fromisoformat(day), "o": o, "h": h, "l": l,
            "c": c, "v": 1, "spread": spread}


def _walk(values: list[float], start: int = 0) -> list[dict]:
    """One bar per value, no gaps; enough shape for the engine, not a price series."""
    base = date(2020, 1, 1).toordinal()
    return [_bar(date.fromordinal(base + start + index).isoformat(),
                 value, value * 1.01, value * 0.99, value)
            for index, value in enumerate(values)]


# ------------------------------------------------------------------ engine fill rules


def test_protective_exit_checks_the_stop_before_the_target():
    study = _study()
    bars = [_bar("2020-01-01", 100, 101, 99, 100),
            _bar("2020-01-02", 100, 120, 80, 100)]  # both stop and target inside one bar
    hit = study.protective_exit(bars, 1, 1, direction=1, stop=90, target=110)
    assert hit == (1, 90.0, "stop"), "a bar containing both levels must be scored as the stop"


def test_protective_exit_fills_a_gap_through_the_stop_at_the_open():
    study = _study()
    bars = [_bar("2020-01-01", 100, 101, 99, 100),
            _bar("2020-01-02", 85, 88, 84, 86)]  # opens below the stop
    hit = study.protective_exit(bars, 1, 1, direction=1, stop=90, target=110)
    assert hit == (1, 85.0, "stop_gap")


def test_protective_exit_fills_a_gap_through_the_target_at_the_open():
    study = _study()
    bars = [_bar("2020-01-01", 100, 101, 99, 100),
            _bar("2020-01-02", 115, 116, 114, 115)]
    hit = study.protective_exit(bars, 1, 1, direction=1, stop=90, target=110)
    assert hit == (1, 115.0, "target_gap")


def test_protective_exit_mirrors_for_a_short():
    study = _study()
    bars = [_bar("2020-01-01", 100, 101, 99, 100),
            _bar("2020-01-02", 100, 111, 89, 100)]
    assert study.protective_exit(bars, 1, 1, direction=-1, stop=110, target=90) == (1, 110.0, "stop")
    bars = [_bar("2020-01-01", 100, 101, 99, 100),
            _bar("2020-01-02", 100, 101, 85, 90)]
    assert study.protective_exit(bars, 1, 1, direction=-1, stop=110, target=90) == (1, 90.0, "target")


def test_simulate_enters_at_the_next_open_never_at_the_signal_close():
    study = _study()
    bars = _walk([100.0] * 40)
    bars[6] = _bar(bars[6]["d"], 100, 104, 99, 103)   # closes above bar 5's high -> signal at 6
    bars[5] = _bar(bars[5]["d"], 100, 102, 99, 100)
    bars[7] = _bar(bars[7]["d"], 105, 106, 104, 105)  # next open, the fill this test expects
    intent = [0] * len(bars)
    intent[6] = 1
    trades = study.simulate("XAUUSD", bars, {"intent": intent, "max_hold": 3}, (date(2020, 1, 1), date(2020, 3, 15)))
    assert len(trades) == 1
    assert trades[0]["entry_date"] == bars[7]["d"]
    assert trades[0]["entry"] == pytest.approx(105.0)


def test_simulate_never_overlaps_two_positions():
    study = _study()
    bars = _walk([100.0 + index * 0.1 for index in range(40)])
    intent = [1] * len(bars)  # a signal on every bar
    trades = study.simulate("XAUUSD", bars, {"intent": intent, "max_hold": 3},
                            (date(2020, 1, 1), date(2020, 3, 15)))
    assert len(trades) >= 2
    for earlier, later in zip(trades, trades[1:]):
        assert later["entry_date"] > earlier["exit_date"], "a trade may not start before the last one ended"


def test_simulate_charges_a_positive_round_trip_cost_and_keeps_the_r_identity():
    study = _study()
    bars = _walk([100.0 + index * 0.5 for index in range(40)])
    intent = [0] * len(bars)
    intent[5] = 1
    trades = study.simulate("XAUUSD", bars, {"intent": intent, "max_hold": 2},
                            (date(2020, 1, 1), date(2020, 3, 15)))
    trade = trades[0]
    assert trade["cost"] > 0
    assert trade["r"] == pytest.approx((trade["gross_pnl"] - trade["cost"]) / trade["risk"])
    assert trade["cost_r"] == pytest.approx(trade["cost"] / trade["risk"])


def test_financing_grows_with_the_number_of_held_nights():
    study = _study()
    bars = _walk([100.0] * 40)  # flat: no stop, no target, so holding time decides
    intent = [0] * len(bars)
    intent[5] = 1
    short_hold = study.simulate("XAUUSD", bars, {"intent": intent, "max_hold": 2},
                                (date(2020, 1, 1), date(2020, 3, 15)))[0]
    long_hold = study.simulate("XAUUSD", bars, {"intent": intent, "max_hold": 20},
                               (date(2020, 1, 1), date(2020, 3, 15)))[0]
    assert short_hold["financing"] > 0
    assert long_hold["financing"] > short_hold["financing"]
    assert long_hold["cost"] > short_hold["cost"]


def test_cost_sweep_at_zero_cost_reproduces_the_gross_mean():
    study = _study()
    bars = _walk([100.0 + index * 0.5 for index in range(40)])
    intent = [0] * len(bars)
    intent[5] = 1
    rule = {"intent": intent, "max_hold": 2}
    free = study.simulate("XAUUSD", bars, rule, (date(2020, 1, 1), date(2020, 3, 15)),
                          cost_multiple=0.0, financing_multiple=0.0)[0]
    paid = study.simulate("XAUUSD", bars, rule, (date(2020, 1, 1), date(2020, 3, 15)),
                          cost_multiple=1.0)[0]
    assert free["r"] == pytest.approx(free["gross_r"])
    assert paid["r"] < free["r"]


# ------------------------------------------------------------------ rule definitions


def test_c1_only_fires_after_an_inside_bar_and_only_beyond_its_range():
    study = _study()
    bars = _walk([100.0] * 40)
    levels = [
        (105, 95, 100),    # 28: wide bar
        (103, 97, 101),    # 29: inside bar
        (104, 100, 103.5), # 30: close beyond 103 -> long
        (104, 100, 101),   # 31: inside bar again
        (104, 100, 101),   # 32: inside bar, no break either way
        (102, 99, 99.5),   # 33: close below 100 -> short
    ]
    for offset, (high, low, close) in enumerate(levels):
        bars[28 + offset] = _bar(bars[28 + offset]["d"], 101, high, low, close)
    rule = study.rule_c1(bars)
    assert rule["intent"][:28] == [0] * 28, "the warm-up must not trade"
    assert rule["intent"][28:34] == [0, 0, 1, 0, 0, -1]


def test_c2_uses_only_joint_dates_and_prior_observations():
    study = _study()
    gold = _walk([100.0 + index for index in range(80)])
    silver = _walk([10.0 + 2 * index for index in range(80)])
    gold.pop(40)  # a date missing from gold must be skipped, not interpolated
    rule = study.rule_c2(gold, silver)
    assert len(rule["intent"]) == len(gold)
    # The first 60 joint observations can only ever produce a warm-up of zeros.
    assert rule["intent"][:60] == [0] * 60


def test_c3_reads_the_252_day_return_and_holds_one_month():
    study = _study()
    bars = []
    rising = [100.0] * 260 + [100.0 * 1.001 ** step for step in range(1, 40)]
    for index, value in enumerate(rising):
        day = date(2019, 1, 1).toordinal() + index
        bars.append(_bar(date.fromordinal(day).isoformat(), value, value, value, value))
    rule = study.rule_c3(bars)
    signals = [index for index, value in enumerate(rule["intent"]) if value]
    assert signals, "a sustained rise must produce at least one momentum signal"
    assert all(rule["intent"][index] == 1 for index in signals)


# ------------------------------------------------------------------ parity: two implementations


def _naive_c1(study, bars: list[dict], window) -> list[tuple]:
    """Independently written: scan signals, then resolve each trade against a calendar."""
    start, end = window
    in_window = [index for index, bar in enumerate(bars) if start <= bar["date"] <= end]
    if not in_window:
        return []
    ranges = study.atr(bars, 14)
    out = []
    busy_until = -1
    for index in range(in_window[0], in_window[-1] + 1):
        if index < 30 or index <= busy_until:
            continue
        previous, before = bars[index - 1], bars[index - 2]
        inside = previous["h"] <= before["h"] and previous["l"] >= before["l"]
        direction = 0
        if inside and bars[index]["c"] > previous["h"]:
            direction = 1
        elif inside and bars[index]["c"] < previous["l"]:
            direction = -1
        if not direction:
            continue
        entry_at = index + 1
        if entry_at > in_window[-1]:
            continue
        entry = bars[entry_at]["o"]
        risk = 2.0 * ranges[index]
        stop, target = entry - risk * direction, entry + 2.0 * risk * direction
        exit_at, price, why = in_window[-1], bars[in_window[-1]]["c"], "end"
        for forward in range(entry_at, min(index + 11, in_window[-1] + 1)):
            bar = bars[forward]
            if direction == 1 and bar["l"] <= stop:
                exit_at, price, why = forward, min(bar["o"], stop), "stop"
                break
            if direction == 1 and bar["h"] >= target:
                exit_at, price, why = forward, max(bar["o"], target) if bar["o"] >= target else target, "target"
                break
            if direction == -1 and bar["h"] >= stop:
                exit_at, price, why = forward, max(bar["o"], stop), "stop"
                break
            if direction == -1 and bar["l"] <= target:
                exit_at, price, why = forward, min(bar["o"], target) if bar["o"] <= target else target, "target"
                break
        if index + 11 <= in_window[-1] and why == "end":
            exit_at, price, why = index + 11, bars[index + 11]["o"], "time"
        out.append((bars[entry_at]["date"].isoformat(), direction, round(entry, 6),
                    bars[exit_at]["date"].isoformat(), round(price, 6)))
        busy_until = exit_at
    return out


def _naive_c3(study, bars: list[dict], symbol: str, window) -> list[tuple]:
    start, end = window
    ranges = study.atr(bars, 14)
    month_starts = [index for index in range(1, len(bars))
                    if bars[index]["date"].month != bars[index - 1]["date"].month]
    out = []
    for position, entry in enumerate(month_starts):
        if not (start <= bars[entry]["date"] <= end):
            continue
        signal = entry - 1
        if signal < 253:
            continue
        change = bars[signal]["c"] / bars[signal - 252]["c"] - 1.0
        if change == 0:
            continue
        direction = 1 if change > 0 else -1
        price = bars[entry]["o"]
        risk = 2.0 * ranges[signal]
        stop, target = price - risk * direction, price + 2.0 * risk * direction
        following = month_starts[position + 1] if position + 1 < len(month_starts) else len(bars)
        exit_at, exit_price = None, None
        for forward in range(entry, min(following, len(bars))):
            if bars[forward]["date"] > end:
                break
            bar = bars[forward]
            if direction == 1 and bar["l"] <= stop:
                exit_at, exit_price = forward, (bar["o"] if bar["o"] < stop else stop)
                break
            if direction == 1 and bar["h"] >= target:
                exit_at, exit_price = forward, (bar["o"] if bar["o"] > target else target)
                break
            if direction == -1 and bar["h"] >= stop:
                exit_at, exit_price = forward, (bar["o"] if bar["o"] > stop else stop)
                break
            if direction == -1 and bar["l"] <= target:
                exit_at, exit_price = forward, (bar["o"] if bar["o"] < target else target)
                break
        if exit_at is None:
            if following < len(bars) and bars[following]["date"] <= end:
                exit_at, exit_price = following, bars[following]["o"]
            else:
                exit_at = max(index for index, bar in enumerate(bars) if bar["date"] <= end)
                exit_price = bars[exit_at]["c"]
        out.append((bars[entry]["date"].isoformat(), direction, round(price, 6),
                    bars[exit_at]["date"].isoformat(), round(exit_price, 6)))
    return out


def _naive_c2(study, gold: list[dict], silver: list[dict], window) -> list[tuple]:
    """Independently written: align first, then walk two parallel arrays."""
    start, end = window
    lookup = {bar["date"]: bar for bar in silver}
    common = [(index, bar, lookup[bar["date"]]) for index, bar in enumerate(gold)
              if bar["date"] in lookup]
    gaps, positions = [], []
    for step, (index, bar, other) in enumerate(common):
        if step < 5:
            gaps.append(None)
            positions.append(index)
            continue
        gold_return = bar["c"] / common[step - 5][1]["c"] - 1.0
        silver_return = other["c"] / common[step - 5][2]["c"] - 1.0
        gaps.append(gold_return - silver_return)
        positions.append(index)
    ranges = study.atr(gold, 14)
    out = []
    busy_until = -1
    for step, index in enumerate(positions):
        if step < 65 or index <= busy_until:
            continue
        if not (start <= gold[index]["date"] <= end):
            continue
        window_values = [value for value in gaps[step - 60:step] if value is not None]
        if len(window_values) < 60:
            continue
        centre = sum(window_values) / 60.0
        spread = (sum((value - centre) ** 2 for value in window_values) / 59.0) ** 0.5
        if spread <= 0:
            continue
        z = (gaps[step] - centre) / spread
        direction = 1 if z <= -1.5 else (-1 if z >= 1.5 else 0)
        if not direction:
            continue
        entry_at = index + 1
        if entry_at > max(position for position in positions if gold[position]["date"] <= end):
            continue
        entry = gold[entry_at]["o"]
        risk = 2.0 * ranges[index]
        stop, target = entry - risk * direction, entry + 2.0 * risk * direction
        cap = min(index + 6, max(position for position in positions if gold[position]["date"] <= end))
        exit_at, price = None, None
        for forward in range(entry_at, cap - 1 + 1):
            bar = gold[forward]
            if direction == 1 and bar["l"] <= stop:
                exit_at, price = forward, (bar["o"] if bar["o"] < stop else stop)
                break
            if direction == 1 and bar["h"] >= target:
                exit_at, price = forward, (bar["o"] if bar["o"] > target else target)
                break
            if direction == -1 and bar["h"] >= stop:
                exit_at, price = forward, (bar["o"] if bar["o"] > stop else stop)
                break
            if direction == -1 and bar["l"] <= target:
                exit_at, price = forward, (bar["o"] if bar["o"] < target else target)
                break
        if exit_at is None:
            last = max(position for position in positions if gold[position]["date"] <= end)
            if index + 6 <= last:
                exit_at, price = index + 6, gold[index + 6]["o"]
            else:
                exit_at, price = last, gold[last]["c"]
        out.append((gold[entry_at]["date"].isoformat(), direction, round(entry, 6),
                    gold[exit_at]["date"].isoformat(), round(price, 6)))
        busy_until = exit_at
    return out


def test_two_independent_implementations_of_c2_produce_the_same_trades():
    study = _study()
    gold, silver = study.load("XAUUSD"), study.load("XAGUSD")
    window = study.WINDOWS["dev"]
    trades = study.simulate("XAUUSD", gold, study.rule_c2(gold, silver), window)
    mine = [(trade["entry_date"], trade["dir"], round(trade["entry"], 6),
             trade["exit_date"], round(trade["exit"], 6)) for trade in trades]
    assert mine == _naive_c2(study, gold, silver, window)


def test_two_independent_implementations_of_c1_produce_the_same_trades():
    study = _study()
    bars = study.load("XAUUSD")
    window = study.WINDOWS["dev"]
    trades = study.simulate("XAUUSD", bars, study.rule_c1(bars), window)
    mine = [(trade["entry_date"], trade["dir"], round(trade["entry"], 6),
             trade["exit_date"], round(trade["exit"], 6)) for trade in trades]
    assert mine == _naive_c1(study, bars, window)


def test_two_independent_implementations_of_c3_produce_the_same_trades():
    study = _study()
    for symbol in ("EURUSD", "AUDUSD", "NZDUSD"):
        bars = study.load(symbol)
        window = study.WINDOWS["dev"]
        trades = study.simulate_monthly(symbol, bars, study.rule_c3(bars), window)
        mine = [(trade["entry_date"], trade["dir"], round(trade["entry"], 6),
                 trade["exit_date"], round(trade["exit"], 6)) for trade in trades]
        assert mine == _naive_c3(study, bars, symbol, window)


# ------------------------------------------------------------------ real cache invariants


@pytest.mark.parametrize("symbol", ["XAUUSD", "XAGUSD", "EURUSD", "AUDUSD", "NZDUSD"])
def test_cached_bars_are_clean_and_within_the_registered_range(symbol):
    study = _study()
    bars = study.load(symbol)
    dates = [bar["date"] for bar in bars]
    assert len(dates) == len(set(dates)), "duplicate trading days in the cache"
    assert all(low > 0 for low in (bar["l"] for bar in bars))
    assert all(bar["h"] >= bar["l"] for bar in bars)
    assert max(dates) <= study.WINDOWS["holdout"][1]
    assert all(bar["date"].weekday() < 5 for bar in bars)


@pytest.mark.parametrize("candidate", ["c1_inside_breakout", "c2_silver_lead", "c3_fx_tsmom"])
def test_trades_obey_the_registered_window_and_geography(candidate):
    study = _study()
    start, end = study.WINDOWS["dev"]
    trades = study.build(candidate, "dev")
    assert trades, "a candidate that trades nothing cannot be evaluated"
    for trade in trades:
        assert start <= date.fromisoformat(trade["entry_date"]) <= end
        assert start <= date.fromisoformat(trade["exit_date"]) <= end
        assert trade["exit_date"] >= trade["entry_date"]
        assert trade["risk"] > 0
        assert trade["cost"] > 0
        assert trade["r"] == pytest.approx((trade["gross_pnl"] - trade["cost"]) / trade["risk"])


@pytest.mark.parametrize("candidate", ["c1_inside_breakout", "c2_silver_lead", "c3_fx_tsmom"])
def test_truncating_the_future_changes_no_past_trade(candidate):
    """No look-ahead: bars after the development window cannot influence its trades."""
    study = _study()
    start, end = study.WINDOWS["dev"]
    truncated: dict[str, list[dict]] = {}
    for symbol in ("XAUUSD", "XAGUSD", "EURUSD", "AUDUSD", "NZDUSD"):
        bars = study.load(symbol)
        cut = [bar for bar in bars if bar["date"] <= end]
        truncated[symbol] = cut
    if candidate == "c1_inside_breakout":
        full = study.simulate("XAUUSD", study.load("XAUUSD"), study.rule_c1(study.load("XAUUSD")), (start, end))
        short = study.simulate("XAUUSD", truncated["XAUUSD"],
                               study.rule_c1(truncated["XAUUSD"]), (start, end))
    elif candidate == "c2_silver_lead":
        gold, silver = study.load("XAUUSD"), study.load("XAGUSD")
        full = study.simulate("XAUUSD", gold, study.rule_c2(gold, silver), (start, end))
        short = study.simulate("XAUUSD", truncated["XAUUSD"],
                               study.rule_c2(truncated["XAUUSD"], truncated["XAGUSD"]), (start, end))
    else:
        full, short = [], []
        for symbol in ("EURUSD", "AUDUSD", "NZDUSD"):
            bars = study.load(symbol)
            full.extend(study.simulate_monthly(symbol, bars, study.rule_c3(bars), (start, end)))
            cut = truncated[symbol]
            short.extend(study.simulate_monthly(symbol, cut, study.rule_c3(cut), (start, end)))
    assert [(t["entry_date"], t["exit_date"], round(t["r"], 9)) for t in full] == \
           [(t["entry_date"], t["exit_date"], round(t["r"], 9)) for t in short]


# ------------------------------------------------------------------ scoring and locks


def test_cluster_bootstrap_is_seeded_and_reports_a_sign():
    study = _study()
    winners = [{"cluster": f"c{index}", "r": 0.5} for index in range(40)]
    losers = [{"cluster": f"d{index}", "r": -0.5} for index in range(40)]
    first = study.cluster_bootstrap(winners, 0.90, rounds=500)
    second = study.cluster_bootstrap(winners, 0.90, rounds=500)
    assert first == second
    assert first[1] > 0 and first[3] < 0.05
    losing = study.cluster_bootstrap(losers, 0.90, rounds=500)
    assert losing[2] < 0 and losing[3] > 0.95


def test_clustering_is_not_the_same_as_treating_trades_as_independent():
    study = _study()
    # Ten months, each with one good and one bad trade that cancel: a trade-level bootstrap
    # would see twenty independent draws, a cluster bootstrap sees ten zero-mean months.
    trades = []
    for month in range(10):
        trades.append({"cluster": f"m{month}", "r": 2.0})
        trades.append({"cluster": f"m{month}", "r": -2.0})
    mean_r, low, high, p_value = study.cluster_bootstrap(trades, 0.90, rounds=500)
    assert mean_r == pytest.approx(0.0)
    assert low <= 0 <= high
    assert p_value > 0.2


def test_a_holdout_is_opened_exactly_once(tmp_path, monkeypatch):
    study = _study()
    monkeypatch.setattr(study, "LOCKS", tmp_path / "locks.json")
    study.holdout_lock("c1_inside_breakout")
    with pytest.raises(SystemExit):
        study.holdout_lock("c1_inside_breakout")
    locks = json.loads((tmp_path / "locks.json").read_text(encoding="utf-8"))
    assert "c1_inside_breakout" in locks


def test_verdict_refuses_to_call_an_underpowered_sample_a_pass():
    study = _study()
    summary = {"n": 10, "mean": 1.0, "low": 0.5, "high": 1.5}
    decision = study.verdict(summary, "dev")
    assert decision["passed"] is False
    assert "below the pre-registered floor" in decision["reason"]
