"""The candidate study must be deterministic, cost-honest and free of look-ahead.

Three independent implementations of the three rules are compared to the engine trade by
trade -- including the exit reason and R, so a shared mistake in ATR, cost or exit ordering
cannot hide. The fill rules are pinned on hand-built bars, and the real cache is checked for
structural invariants only: no test asserts a performance number, because a test that encoded
a result would freeze the answer the study is supposed to discover.
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
    base = date(2020, 1, 1).toordinal()
    return [_bar(date.fromordinal(base + start + index).isoformat(),
                 value, value * 1.01, value * 0.99, value)
            for index, value in enumerate(values)]


# ------------------------------------------------- independent re-implementation, from prose

POINTS = {"XAUUSD": 0.01, "XAGUSD": 0.001, "EURUSD": 0.00001, "AUDUSD": 0.00001, "NZDUSD": 0.00001}
FLOORS = {"XAUUSD": 20, "XAGUSD": 30, "EURUSD": 2, "AUDUSD": 2, "NZDUSD": 2}
RATES = {"XAUUSD": 0.03, "XAGUSD": 0.03, "EURUSD": 0.02, "AUDUSD": 0.02, "NZDUSD": 0.02}


def _tr_atr(bars: list[dict], period: int = 14) -> list[float]:
    ranges = []
    for index, bar in enumerate(bars):
        if index == 0:
            ranges.append(bar["h"] - bar["l"])
            continue
        previous = bars[index - 1]["c"]
        ranges.append(max(bar["h"] - bar["l"], abs(bar["h"] - previous), abs(bar["l"] - previous)))
    out = []
    for index in range(len(bars)):
        first = max(0, index - period + 1)
        slice_ = ranges[first:index + 1]
        out.append(sum(slice_) / len(slice_))
    return out


def _cost(symbol: str, bar: dict, price: float, nights: int) -> float:
    spread = max(bar["spread"], FLOORS[symbol]) * POINTS[symbol]
    if symbol in ("XAUUSD", "XAGUSD"):
        slippage = price * 5.0 / 10_000.0
    else:
        slippage = 0.00005
    return spread + 2.0 * slippage + price * RATES[symbol] * nights / 252.0


def _fill(bars, direction, entry, stop, target, first, last):
    """Resolve a protective exit with signed comparisons instead of explicit branches."""
    sides = (("stop", stop, direction), ("target", target, -direction))
    for index in range(first, last + 1):
        bar = bars[index]
        for name, level, side in sides:  # a gap is resolved before any intrabar level
            if (bar["o"] - level) * side <= 0:
                return index, bar["o"], f"{name}_gap"
        for name, level, side in sides:  # inside the bar the stop is tested first
            extreme = bar["l"] if side == 1 else bar["h"]
            if (extreme - level) * side <= 0:
                return index, level, name
    return None


def _record(symbol, bars, atr, direction, signal_at, entry_at, exit_at, price, reason):
    entry = bars[entry_at]["o"]
    risk = 2.0 * atr[signal_at]
    cost = _cost(symbol, bars[entry_at], entry, exit_at - entry_at)
    gross = (price - entry) * direction
    return (bars[entry_at]["d"], bars[exit_at]["d"], direction, reason,
            round((gross - cost) / risk, 9))


def _independent_c1(bars, window) -> list[tuple]:
    start, end = window
    atr = _tr_atr(bars)
    signals = {}
    for index in range(30, len(bars)):
        prev, before = bars[index - 1], bars[index - 2]
        if not (prev["h"] <= before["h"] and prev["l"] >= before["l"]):
            continue
        if bars[index]["c"] > prev["h"]:
            signals[index] = 1
        elif bars[index]["c"] < prev["l"]:
            signals[index] = -1
    index = next(i for i, bar in enumerate(bars) if bar["date"] >= start)
    last = max(i for i, bar in enumerate(bars) if bar["date"] <= end)
    out = []
    while index <= last:
        direction = signals.get(index)
        if not direction or index + 1 > last:
            index += 1
            continue
        entry_at = index + 1
        risk = 2.0 * atr[index]
        entry = bars[entry_at]["o"]
        stop, target = entry - risk * direction, entry + 2.0 * risk * direction
        deadline = entry_at + 10
        hit = _fill(bars, direction, entry, stop, target, entry_at, min(deadline - 1, last))
        if hit is None:
            hit = ((deadline, bars[deadline]["o"], "time") if deadline <= last
                   else (last, bars[last]["c"], "end"))
        out.append(_record("XAUUSD", bars, atr, direction, index, entry_at,
                           hit[0], hit[1], hit[2]))
        index = hit[0] + 1
    return out


def _independent_c2(gold, silver, window) -> list[tuple]:
    start, end = window
    atr = _tr_atr(gold)
    silver_at = {bar["date"]: index for index, bar in enumerate(silver)}
    joint = [(index, silver_at[bar["date"]]) for index, bar in enumerate(gold)
             if bar["date"] in silver_at]
    gaps: list[float | None] = [None] * len(joint)
    for step in range(5, len(joint)):
        gold_now = joint[step][0]
        gold_then = joint[step - 5][0]
        silver_now = joint[step][1]
        silver_then = joint[step - 5][1]
        gaps[step] = ((gold[gold_now]["c"] / gold[gold_then]["c"] - 1.0)
                      - (silver[silver_now]["c"] / silver[silver_then]["c"] - 1.0))
    signals = {}
    for step in range(65, len(joint)):
        sample = gaps[step - 60:step]
        if any(value is None for value in sample):
            continue
        centre = sum(sample) / 60.0
        variance = sum((value - centre) ** 2 for value in sample) / 59.0
        if variance <= 0:
            continue
        z = (gaps[step] - centre) / variance ** 0.5
        if z <= -1.5:
            signals[joint[step][0]] = 1
        elif z >= 1.5:
            signals[joint[step][0]] = -1
    index = next(i for i, bar in enumerate(gold) if bar["date"] >= start)
    last = max(i for i, bar in enumerate(gold) if bar["date"] <= end)
    out = []
    while index <= last:
        direction = signals.get(index)
        if not direction or index + 1 > last:
            index += 1
            continue
        entry_at = index + 1
        risk = 2.0 * atr[index]
        entry = gold[entry_at]["o"]
        stop, target = entry - risk * direction, entry + 2.0 * risk * direction
        deadline = entry_at + 5
        hit = _fill(gold, direction, entry, stop, target, entry_at, min(deadline - 1, last))
        if hit is None:
            hit = ((deadline, gold[deadline]["o"], "time") if deadline <= last
                   else (last, gold[last]["c"], "end"))
        out.append(_record("XAUUSD", gold, atr, direction, index, entry_at,
                           hit[0], hit[1], hit[2]))
        index = hit[0] + 1
    return out


def _independent_c3(symbol: str, bars, window) -> list[tuple]:
    start, end = window
    atr = _tr_atr(bars)
    last = max(i for i, bar in enumerate(bars) if bar["date"] <= end)
    out = []
    for index in range(1, last + 1):
        if bars[index]["date"].month == bars[index - 1]["date"].month:
            continue
        if not (start <= bars[index]["date"] <= end):
            continue
        signal = index - 1
        if signal < 252:
            continue
        change = bars[signal]["c"] / bars[signal - 252]["c"] - 1.0
        if change == 0:
            continue
        direction = 1 if change > 0 else -1
        risk = 2.0 * atr[signal]
        entry = bars[index]["o"]
        stop, target = entry - risk * direction, entry + 2.0 * risk * direction
        month_end = index
        while (month_end + 1 <= last
               and bars[month_end + 1]["date"].month == bars[index]["date"].month):
            month_end += 1
        hit = _fill(bars, direction, entry, stop, target, index, month_end)
        if hit is None:
            following = month_end + 1
            hit = ((following, bars[following]["o"], "rebalance") if following <= last
                   else (month_end, bars[month_end]["c"], "end"))
        out.append(_record(symbol, bars, atr, direction, signal, index, hit[0], hit[1], hit[2]))
    return out


# ------------------------------------------------------------------ engine fill rules


def test_a_gap_through_the_target_is_not_later_rebooked_as_a_stop():
    study = _study()
    bars = [_bar("2020-01-01", 100, 101, 99, 100),
            _bar("2020-01-02", 120, 121, 80, 85)]  # opens above target, later trades through stop
    hit = study.protective_exit(bars, 1, 1, direction=1, stop=90, target=110)
    assert hit == (1, 120.0, "target_gap"), "the open is chronologically first and wins"


def test_protective_exit_checks_the_stop_before_the_target_inside_the_bar():
    study = _study()
    bars = [_bar("2020-01-01", 100, 101, 99, 100),
            _bar("2020-01-02", 100, 120, 80, 100)]
    assert study.protective_exit(bars, 1, 1, direction=1, stop=90, target=110) == (1, 90.0, "stop")


def test_protective_exit_fills_a_gap_through_the_stop_at_the_open():
    study = _study()
    bars = [_bar("2020-01-01", 100, 101, 99, 100),
            _bar("2020-01-02", 85, 88, 84, 86)]
    assert study.protective_exit(bars, 1, 1, direction=1, stop=90, target=110) == (1, 85.0, "stop_gap")


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
    bars[6] = _bar(bars[6]["d"], 100, 104, 99, 103)
    bars[5] = _bar(bars[5]["d"], 100, 102, 99, 100)
    bars[7] = _bar(bars[7]["d"], 105, 106, 104, 105)
    intent = [0] * len(bars)
    intent[6] = 1
    trades = study.simulate("XAUUSD", bars, {"intent": intent, "max_hold": 3},
                            (date(2020, 1, 1), date(2020, 3, 15)))
    assert len(trades) == 1
    assert trades[0]["entry_date"] == bars[7]["d"]
    assert trades[0]["entry"] == pytest.approx(105.0)


def test_the_time_exit_lands_on_the_registered_bar():
    """max_hold=3: protections on the entry bar and the next two; exit at the open of the 4th."""
    study = _study()
    bars = _walk([100.0] * 40)
    intent = [0] * len(bars)
    intent[5] = 1
    trade = study.simulate("XAUUSD", bars, {"intent": intent, "max_hold": 3},
                           (date(2020, 1, 1), date(2020, 3, 15)))[0]
    assert trade["entry_date"] == bars[6]["d"]
    assert trade["reason"] == "time"
    assert trade["exit_date"] == bars[6 + 3]["d"], "the entry bar counts as the first held bar"
    assert trade["financing_nights"] == 3


def test_no_signal_is_taken_on_the_bar_a_position_closes():
    study = _study()
    bars = _walk([100.0] * 40)
    intent = [0] * len(bars)
    intent[5] = 1
    first = study.simulate("XAUUSD", bars, {"intent": intent, "max_hold": 3},
                           (date(2020, 1, 1), date(2020, 3, 15)))
    exit_index = next(index for index, bar in enumerate(bars) if bar["d"] == first[0]["exit_date"])
    intent[exit_index] = 1  # the registered rule ignores a signal on the exit bar
    again = study.simulate("XAUUSD", bars, {"intent": intent, "max_hold": 3},
                           (date(2020, 1, 1), date(2020, 3, 15)))
    assert len(again) == 1


def test_simulate_never_overlaps_two_positions():
    study = _study()
    bars = _walk([100.0 + index * 0.1 for index in range(40)])
    trades = study.simulate("XAUUSD", bars, {"intent": [1] * len(bars), "max_hold": 3},
                            (date(2020, 1, 1), date(2020, 3, 15)))
    assert len(trades) >= 2
    for earlier, later in zip(trades, trades[1:]):
        assert later["entry_date"] > earlier["exit_date"]


def test_costs_are_positive_and_r_is_net_of_them():
    study = _study()
    bars = _walk([100.0 + index * 0.5 for index in range(40)])
    intent = [0] * len(bars)
    intent[5] = 1
    trade = study.simulate("XAUUSD", bars, {"intent": intent, "max_hold": 2},
                           (date(2020, 1, 1), date(2020, 3, 15)))[0]
    assert trade["transaction"] > 0 and trade["financing"] > 0
    assert trade["cost"] == pytest.approx(trade["transaction"] + trade["financing"])
    assert trade["r"] == pytest.approx((trade["gross_pnl"] - trade["cost"]) / trade["risk"])


def test_financing_grows_with_the_number_of_held_nights():
    study = _study()
    bars = _walk([100.0] * 40)
    intent = [0] * len(bars)
    intent[5] = 1
    short_hold = study.simulate("XAUUSD", bars, {"intent": intent, "max_hold": 2},
                                (date(2020, 1, 1), date(2020, 3, 15)))[0]
    long_hold = study.simulate("XAUUSD", bars, {"intent": intent, "max_hold": 20},
                               (date(2020, 1, 1), date(2020, 3, 15)))[0]
    assert 0 < short_hold["financing"] < long_hold["financing"]
    assert long_hold["cost"] > short_hold["cost"]


def test_cost_sweep_at_zero_cost_reproduces_the_gross_mean():
    study = _study()
    bars = _walk([100.0 + index * 0.5 for index in range(40)])
    intent = [0] * len(bars)
    intent[5] = 1
    rule = {"intent": intent, "max_hold": 2}
    free = study.simulate("XAUUSD", bars, rule, (date(2020, 1, 1), date(2020, 3, 15)),
                          cost_multiple=0.0, financing_multiple=0.0)[0]
    paid = study.simulate("XAUUSD", bars, rule, (date(2020, 1, 1), date(2020, 3, 15)))[0]
    assert free["r"] == pytest.approx(free["gross_r"])
    assert paid["r"] < free["r"]


def test_trades_cluster_by_entry_month():
    study = _study()
    bars = _walk([100.0 + index * 0.5 for index in range(40)])
    intent = [0] * len(bars)
    intent[5] = 1
    intent[25] = 1
    trades = study.simulate("XAUUSD", bars, {"intent": intent, "max_hold": 2},
                            (date(2020, 1, 1), date(2020, 3, 15)))
    assert all(trade["cluster"] == trade["entry_date"][:7] for trade in trades)


# ------------------------------------------------------------------ rule definitions


def test_c1_warm_up_suppresses_a_signal_that_would_otherwise_fire():
    study = _study()
    bars = _walk([100.0] * 40)
    shape = {26: (105, 95, 100), 27: (103, 97, 100), 28: (102, 98, 100),
             29: (104, 100, 103), 30: (104, 100, 101), 31: (107, 104, 106)}
    for index, (high, low, close) in shape.items():
        bars[index] = _bar(bars[index]["d"], 101, high, low, close)
    # Preconditions: bar 28 is inside bar 27 and bar 29 closes above 102, so a rule without
    # the 30-bar warm-up would signal at index 29.
    assert bars[28]["h"] <= bars[27]["h"] and bars[28]["l"] >= bars[27]["l"]
    assert bars[29]["c"] > bars[28]["h"]
    rule = study.rule_c1(bars)
    assert rule["intent"][29] == 0, "index 29 is inside the warm-up"
    assert rule["intent"][31] == 1, "the same shape after the warm-up must fire"


def test_c1_only_fires_after_an_inside_bar_and_only_beyond_its_range():
    study = _study()
    bars = _walk([100.0] * 40)
    levels = [(105, 95, 100), (103, 97, 101), (104, 100, 103.5),
              (104, 100, 101), (104, 100, 101), (102, 99, 99.5)]
    for offset, (high, low, close) in enumerate(levels):
        bars[28 + offset] = _bar(bars[28 + offset]["d"], 101, high, low, close)
    rule = study.rule_c1(bars)
    assert rule["intent"][30] == 1 and rule["intent"][33] == -1
    assert rule["intent"][:30] == [0] * 30


def test_c2_uses_only_joint_dates_and_prior_observations():
    study = _study()
    gold = _walk([100.0 + index for index in range(80)])
    silver = _walk([10.0 + 2 * index for index in range(80)])
    gold.pop(40)
    rule = study.rule_c2(gold, silver)
    assert len(rule["intent"]) == len(gold)
    assert rule["intent"][:65] == [0] * 65, "no signal before the 60-observation window exists"


def test_c3_reads_the_252_day_return_and_signs_it():
    study = _study()
    bars = []
    values = [100.0] * 260 + [100.0 * 1.001 ** step for step in range(1, 40)]
    for index, value in enumerate(values):
        day = date(2019, 1, 1).toordinal() + index
        bars.append(_bar(date.fromordinal(day).isoformat(), value, value, value, value))
    rule = study.rule_c3(bars)
    signals = [index for index, value in enumerate(rule["intent"]) if value]
    assert signals, "a sustained rise must produce at least one momentum signal"
    assert all(rule["intent"][index] == 1 for index in signals)


# ------------------------------------------------------------------ parity, three ways


def test_two_independent_implementations_of_c1_produce_the_same_trades():
    study = _study()
    bars = study.load("XAUUSD")
    window = study.WINDOWS["dev"]
    engine = [(trade["entry_date"], trade["exit_date"], trade["dir"], trade["reason"],
               round(trade["r"], 9))
              for trade in study.simulate("XAUUSD", bars, study.rule_c1(bars), window)]
    assert engine == _independent_c1(bars, window)


def test_two_independent_implementations_of_c2_produce_the_same_trades():
    study = _study()
    gold, silver = study.load("XAUUSD"), study.load("XAGUSD")
    window = study.WINDOWS["dev"]
    engine = [(trade["entry_date"], trade["exit_date"], trade["dir"], trade["reason"],
               round(trade["r"], 9))
              for trade in study.simulate("XAUUSD", gold, study.rule_c2(gold, silver), window)]
    assert engine == _independent_c2(gold, silver, window)


def test_two_independent_implementations_of_c3_produce_the_same_trades():
    study = _study()
    window = study.WINDOWS["dev"]
    for symbol in ("EURUSD", "AUDUSD", "NZDUSD"):
        bars = study.load(symbol)
        engine = [(trade["entry_date"], trade["exit_date"], trade["dir"], trade["reason"],
                   round(trade["r"], 9))
                  for trade in study.simulate_monthly(symbol, bars, study.rule_c3(bars), window)]
        assert engine == _independent_c3(symbol, bars, window)


# ------------------------------------------------------------------ real cache invariants


@pytest.mark.parametrize("symbol", ["XAUUSD", "XAGUSD", "EURUSD", "AUDUSD", "NZDUSD"])
def test_cached_bars_are_clean_and_within_the_registered_range(symbol):
    study = _study()
    bars = study.load(symbol)
    dates = [bar["date"] for bar in bars]
    assert len(dates) == len(set(dates)), "duplicate trading days in the cache"
    assert all(bar["l"] > 0 for bar in bars)
    assert all(bar["h"] >= bar["l"] for bar in bars)
    assert max(dates) <= study.WINDOWS["holdout"][1]
    assert all(bar["date"].weekday() < 5 for bar in bars)


@pytest.mark.parametrize("candidate", ["c1_inside_breakout", "c2_ratio_reversion", "c3_fx_tsmom"])
def test_trades_obey_the_registered_window_and_arithmetic(candidate):
    study = _study()
    start, end = study.WINDOWS["dev"]
    trades = study.build(candidate, "dev")
    assert trades, "a candidate that trades nothing cannot be evaluated"
    for trade in trades:
        assert start <= date.fromisoformat(trade["entry_date"]) <= end
        assert start <= date.fromisoformat(trade["exit_date"]) <= end
        assert trade["exit_date"] >= trade["entry_date"]
        assert trade["risk"] > 0 and trade["cost"] > 0
        assert trade["r"] == pytest.approx((trade["gross_pnl"] - trade["cost"]) / trade["risk"])
        assert trade["cluster"] == trade["entry_date"][:7]


@pytest.mark.parametrize("candidate", ["c1_inside_breakout", "c2_ratio_reversion", "c3_fx_tsmom"])
def test_truncating_the_future_changes_no_past_trade(candidate):
    study = _study()
    start, end = study.WINDOWS["dev"]
    cut = {symbol: [bar for bar in study.load(symbol) if bar["date"] <= end]
           for symbol in ("XAUUSD", "XAGUSD", "EURUSD", "AUDUSD", "NZDUSD")}
    if candidate == "c1_inside_breakout":
        full = study.simulate("XAUUSD", study.load("XAUUSD"), study.rule_c1(study.load("XAUUSD")),
                              (start, end))
        short = study.simulate("XAUUSD", cut["XAUUSD"], study.rule_c1(cut["XAUUSD"]), (start, end))
    elif candidate == "c2_ratio_reversion":
        gold, silver = study.load("XAUUSD"), study.load("XAGUSD")
        full = study.simulate("XAUUSD", gold, study.rule_c2(gold, silver), (start, end))
        short = study.simulate("XAUUSD", cut["XAUUSD"],
                               study.rule_c2(cut["XAUUSD"], cut["XAGUSD"]), (start, end))
    else:
        full, short = [], []
        for symbol in ("EURUSD", "AUDUSD", "NZDUSD"):
            bars = study.load(symbol)
            full.extend(study.simulate_monthly(symbol, bars, study.rule_c3(bars), (start, end)))
            short.extend(study.simulate_monthly(symbol, cut[symbol], study.rule_c3(cut[symbol]),
                                                (start, end)))
    assert [(t["entry_date"], t["exit_date"], round(t["r"], 9)) for t in full] == \
           [(t["entry_date"], t["exit_date"], round(t["r"], 9)) for t in short]


# ------------------------------------------------------------------ scoring, locks, verdicts


def test_cluster_bootstrap_is_seeded_and_reports_a_sign():
    study = _study()
    winners = [{"cluster": f"2020-{index:02d}" if index < 13 else f"2021-{index - 12:02d}",
                "r": 0.5} for index in range(1, 41)]
    losers = [{"cluster": f"2010-{index:02d}" if index < 13 else f"2011-{index - 12:02d}",
               "r": -0.5} for index in range(1, 41)]
    assert study.cluster_bootstrap(winners, 0.90, rounds=500) == \
           study.cluster_bootstrap(winners, 0.90, rounds=500)
    mean_r, low, high = study.cluster_bootstrap(winners, 0.90, rounds=500)
    assert mean_r == pytest.approx(0.5) and low > 0
    mean_r, low, high = study.cluster_bootstrap(losers, 0.90, rounds=500)
    assert mean_r == pytest.approx(-0.5) and high < 0


def test_the_block_bootstrap_draws_consecutive_months():
    study = _study()
    trades = [{"cluster": f"2020-{month:02d}", "r": float(month)} for month in range(1, 13)]
    mean_r, low, high = study.cluster_bootstrap(trades, 0.90, rounds=200, block=12)
    assert mean_r == pytest.approx(6.5)
    assert low == pytest.approx(6.5) and high == pytest.approx(6.5), (
        "a block as long as the sample can only return the whole sample")


def test_the_interval_is_not_degenerate():
    study = _study()
    values = [1.0, -1.0, 2.0, -2.0, 0.5, -0.5, 1.5, -1.5, 0.3, -0.3, 0.7, -0.7]
    trades = [{"cluster": f"2020-{month:02d}", "r": value}
              for month, value in enumerate(values, start=1)]
    mean_r, low, high = study.cluster_bootstrap(trades, 0.90, rounds=500)
    assert high - low > 0.5, "a degenerate [mean, mean] implementation must fail this"


def test_clustering_is_not_the_same_as_treating_trades_as_independent():
    study = _study()
    trades = []
    for month in range(10):
        trades.append({"cluster": f"2020-{month + 1:02d}", "r": 2.0})
        trades.append({"cluster": f"2020-{month + 1:02d}", "r": -2.0})
    mean_r, low, high = study.cluster_bootstrap(trades, 0.90, rounds=500)
    assert mean_r == pytest.approx(0.0)
    assert low <= 0 <= high


def test_a_holdout_cannot_be_opened_without_a_passed_development_run(tmp_path, monkeypatch):
    study = _study()
    locks = tmp_path / "locks.json"
    evidence = tmp_path / "dev.json"
    monkeypatch.setattr(study, "LOCKS", locks)
    monkeypatch.setattr(study, "DEV_EVIDENCE", evidence)
    with pytest.raises(SystemExit):
        study.open_holdout("c3_fx_tsmom")  # no evidence file
    evidence.write_text(json.dumps({"window": "dev", "candidates": {
        "c3_fx_tsmom": {"decision": {"passed": False, "status": "fail"}}}}), encoding="utf-8")
    with pytest.raises(SystemExit):
        study.open_holdout("c3_fx_tsmom")
    # A document that claims a pass with numbers the engine cannot reproduce is refused.
    trades = study.build("c3_fx_tsmom", "dev")
    real = study.summarise("c3_fx_tsmom", trades, study.DEV_CONFIDENCE)
    fabricated = {"passed": True, "n": real["n"], "clusters": real["clusters"],
                  "mean": real["mean"] + 1.0, "low": real["low"] + 1.0}
    evidence.write_text(json.dumps({"window": "dev", "candidates": {
        "c3_fx_tsmom": {"decision": fabricated}}}), encoding="utf-8")
    with pytest.raises(SystemExit):
        study.open_holdout("c3_fx_tsmom")
    truth = {"passed": True, "n": real["n"], "clusters": real["clusters"],
             "mean": real["mean"], "low": real["low"]}
    evidence.write_text(json.dumps({"window": "dev", "candidates": {
        "c3_fx_tsmom": {"decision": truth}}}), encoding="utf-8")
    study.open_holdout("c3_fx_tsmom")
    with pytest.raises(SystemExit):
        study.open_holdout("c3_fx_tsmom")  # exactly once
    lock = json.loads(locks.read_text(encoding="utf-8"))["c3_fx_tsmom"]
    assert lock["engine_sha256"] and lock["cache_manifest_sha256"]
    assert study.require_holdout_open("c3_fx_tsmom")["development_n"] == real["n"]


def test_holdout_scoring_requires_the_lock_and_unchanged_evidence(tmp_path, monkeypatch):
    study = _study()
    locks = tmp_path / "locks.json"
    evidence = tmp_path / "dev.json"
    monkeypatch.setattr(study, "LOCKS", locks)
    monkeypatch.setattr(study, "DEV_EVIDENCE", evidence)
    with pytest.raises(SystemExit):
        study.require_holdout_open("c3_fx_tsmom")
    trades = study.build("c3_fx_tsmom", "dev")
    real = study.summarise("c3_fx_tsmom", trades, study.DEV_CONFIDENCE)
    decision = {"passed": True, "n": real["n"], "clusters": real["clusters"],
                "mean": real["mean"], "low": real["low"]}
    evidence.write_text(json.dumps({"window": "dev", "candidates": {
        "c3_fx_tsmom": {"decision": decision}}}), encoding="utf-8")
    study.open_holdout("c3_fx_tsmom")
    assert study.require_holdout_open("c3_fx_tsmom")["development_evidence_sha256"]
    decision["low"] += 0.01
    evidence.write_text(json.dumps({"window": "dev", "candidates": {
        "c3_fx_tsmom": {"decision": decision}}}), encoding="utf-8")
    with pytest.raises(SystemExit):
        study.require_holdout_open("c3_fx_tsmom")


def test_verdict_refuses_an_underpowered_sample():
    study = _study()
    decision = study.verdict({"n": 10, "clusters": 10, "mean": 1.0, "low": 0.5, "high": 1.5,
                              "sd": 1.2}, "dev", double_cost_mean=0.3)
    assert decision["status"] == "no_decision" and decision["passed"] is False
    assert "below the pre-registered floor" in decision["reason"]


def test_verdict_refuses_too_few_clusters():
    study = _study()
    decision = study.verdict({"n": 200, "clusters": 12, "mean": 0.4, "low": 0.2,
                              "high": 0.6, "sd": 1.2}, "dev", double_cost_mean=0.1)
    assert decision["status"] == "no_decision"
    assert "clusters" in decision["reason"]


def test_verdict_requires_the_edge_to_survive_double_cost():
    study = _study()
    summary = {"n": 200, "clusters": 60, "mean": 0.30, "low": 0.10, "high": 0.50, "sd": 1.2}
    assert study.verdict(summary, "dev", double_cost_mean=0.05)["status"] == "pass"
    decision = study.verdict(summary, "dev", double_cost_mean=-0.02)
    assert decision["status"] == "fail" and "double cost" in decision["reason"]
