"""Score the shadow journal's forward proposals against the bars that came after them.

The observation run (``agent_data/observe_market.py``) journals what the live H1 pipeline would
have opened, at what price, stop and target, while sending nothing. This turns those journal
entries into R multiples using the bars the broker printed afterwards, and charges exactly what
the pre-registered daily study charged: the same spread floor, the same per-side slippage, the
same financing, the same moving-block cluster bootstrap. ``tests/test_score_forward_shadow.py``
binds those constants to ``scripts/candidate_study.py`` so the two cannot drift apart.

Nothing here chooses a rule, a window or a threshold. The rules are the ones the policy file
already runs; this only measures what they did.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import NormalDist
from typing import Any, Iterable, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from critical_consensus.mt5_agent.journal import DecisionJournal  # noqa: E402

UTC = timezone.utc

# Copied from the registered study (scripts/candidate_study.py) rather than imported, because a
# forward score must not depend on that file's data-loading side effects. The test suite asserts
# every shared symbol matches, so a silent edit there breaks a test here.
SPREAD_FLOOR_POINTS = {"XAUUSD": 20, "XAGUSD": 30, "EURUSD": 2, "GBPUSD": 2, "AUDUSD": 2,
                       "NZDUSD": 2}
POINT = {"XAUUSD": 0.01, "XAGUSD": 0.001, "EURUSD": 0.00001, "GBPUSD": 0.00001,
         "AUDUSD": 0.00001, "NZDUSD": 0.00001}
SLIPPAGE_BPS = {"XAUUSD": 5.0, "XAGUSD": 5.0, "EURUSD": 0.0, "GBPUSD": 0.0, "AUDUSD": 0.0,
                "NZDUSD": 0.0}
SLIPPAGE_PRICE = {"XAUUSD": 0.0, "XAGUSD": 0.0, "EURUSD": 0.00005, "GBPUSD": 0.00005,
                  "AUDUSD": 0.00005, "NZDUSD": 0.00005}
FINANCING_PER_YEAR = {"XAUUSD": 0.03, "XAGUSD": 0.03, "EURUSD": 0.02, "GBPUSD": 0.02,
                      "AUDUSD": 0.02, "NZDUSD": 0.02}

HORIZON_BARS = 24
ROUNDS = 10_000
SEED = 20261102
BLOCK_DAYS = 3
CONFIDENCE = 1.0 - 0.05 / 3
MIN_PROPOSALS = 200
MIN_DAYS = 60

BARS_DIR = Path("agent_data/forward_bars")


@dataclass(frozen=True)
class Proposal:
    symbol: str
    timeframe: str
    evaluated_at: datetime
    direction: int
    entry: float
    stop: float
    target: float
    risk_distance: float
    volume: float

    @property
    def day(self) -> str:
        return self.evaluated_at.astimezone(UTC).date().isoformat()


def _aware(value: str) -> datetime:
    moment = datetime.fromisoformat(value)
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def proposals_from_journal(db_path: Path, *, after: datetime | None = None,
                            before: datetime | None = None) -> list[Proposal]:
    """Distinct (symbol, bar) proposals the shadow journal recorded, oldest first.

    The watch samples every 20 seconds, so one bar is journalled many times; the first record for
    a bar wins and the rest are the same decision repeated.
    """
    journal = DecisionJournal(Path(db_path))
    seen: dict[tuple[str, datetime], Proposal] = {}
    events = journal.events(limit=None, event_type="agent_cycle")
    for event in reversed(events):  # events come newest first; the earliest sample of a bar wins
        payload = event["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        if payload.get("status") != "OBSERVED":
            continue
        raw = payload.get("proposal")
        if not raw:
            continue
        evaluated_at = _aware(str(raw["evaluated_at"]))
        if after is not None and evaluated_at < after:
            continue
        if before is not None and evaluated_at >= before:
            continue
        key = (str(raw["symbol"]), evaluated_at)
        if key in seen:
            continue
        seen[key] = Proposal(
            symbol=str(raw["symbol"]), timeframe=str(raw["timeframe"]), evaluated_at=evaluated_at,
            direction=1 if raw["direction"] == "BUY" else -1,
            entry=float(raw["entry_price"]), stop=float(raw["stop_loss"]),
            target=float(raw["take_profit"]), risk_distance=float(raw["risk_distance"]),
            volume=float((payload.get("sizing") or {}).get("volume") or 0.0))
    return [seen[key] for key in sorted(seen, key=lambda k: (k[1], k[0]))]


def bars_from_rows(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalise cached or terminal rows to the study's bar shape, oldest first."""
    bars = []
    for row in rows:
        moment = row.get("time") or row.get("t")
        if isinstance(moment, str):
            moment = _aware(moment)
        elif isinstance(moment, (int, float)):
            moment = datetime.fromtimestamp(int(moment), UTC)
        bars.append({"time": moment, "o": float(row["o"] if "o" in row else row["open"]),
                     "h": float(row["h"] if "h" in row else row["high"]),
                     "l": float(row["l"] if "l" in row else row["low"]),
                     "c": float(row["c"] if "c" in row else row["close"]),
                     "spread": float(row.get("spread") or row.get("spread_points") or 0.0)})
    bars.sort(key=lambda bar: bar["time"])
    return bars


def bars_from_terminal(terminal: Any, symbol: str, count: int = 2000) -> list[dict[str, Any]]:
    """Closed H1 bars straight from the terminal; read-only, no order surface."""
    return bars_from_rows([{"time": bar.time, "o": bar.open, "h": bar.high, "l": bar.low,
                            "c": bar.close, "spread": bar.spread_points}
                           for bar in terminal.bars(symbol, "H1", count)])


def cost_price(symbol: str, bar: dict[str, Any], price: float) -> float:
    spread = max(bar["spread"], SPREAD_FLOOR_POINTS[symbol]) * POINT[symbol]
    slippage = SLIPPAGE_PRICE[symbol] + price * SLIPPAGE_BPS[symbol] / 10_000.0
    return spread + 2.0 * slippage


def protective_exit(bars: Sequence[dict[str, Any]], entry_index: int, last_index: int, *,
                    direction: int, stop: float, target: float) -> tuple[int, float, str] | None:
    """Same walk as the registered engine: a gap fills at the bar's open, inside the bar the
    stop wins over the target."""
    for index in range(entry_index, last_index + 1):
        bar = bars[index]
        if direction > 0:
            if bar["o"] <= stop:
                return index, bar["o"], "stop_gap"
            if bar["o"] >= target:
                return index, bar["o"], "target_gap"
            if bar["l"] <= stop:
                return index, stop, "stop"
            if bar["h"] >= target:
                return index, target, "target"
        else:
            if bar["o"] >= stop:
                return index, bar["o"], "stop_gap"
            if bar["o"] <= target:
                return index, bar["o"], "target_gap"
            if bar["h"] >= stop:
                return index, stop, "stop"
            if bar["l"] <= target:
                return index, target, "target"
    return None


def entry_index(bars: Sequence[dict[str, Any]], proposal: Proposal) -> int | None:
    """The first bar that opens after the signal bar closes.

    ``evaluated_at`` is the MT5 bar stamp, which is that bar's open time, so the signal bar ends
    one timeframe later and the fill can only happen on the next bar's open.
    """
    step = {"H1": timedelta(hours=1), "H4": timedelta(hours=4), "D1": timedelta(days=1)}.get(
        proposal.timeframe)
    if step is None:
        raise ValueError(f"unsupported timeframe {proposal.timeframe}")
    signal_close = proposal.evaluated_at + step
    for index, bar in enumerate(bars):
        if bar["time"] >= signal_close:
            return index
    return None


def score(proposals: Iterable[Proposal], bars_by_symbol: dict[str, list[dict[str, Any]]], *,
          cost_multiple: float = 1.0,
          horizon: int = HORIZON_BARS) -> list[dict[str, Any]]:
    trades = []
    for proposal in proposals:
        bars = bars_by_symbol.get(proposal.symbol) or []
        start = entry_index(bars, proposal)
        if start is None or start >= len(bars):
            continue
        entry = bars[start]["o"]
        last = min(len(bars) - 1, start + horizon - 1)
        hit = protective_exit(bars, start, last, direction=proposal.direction, stop=proposal.stop,
                              target=proposal.target)
        if hit is not None:
            exit_index, exit_price, reason = hit
        elif start + horizon < len(bars):
            # The registered definition: protections are live on bars E .. E+horizon-1 and the
            # time exit is the open of the bar after them.
            exit_index, exit_price, reason = start + horizon, bars[start + horizon]["o"], "time"
        else:
            exit_index, exit_price, reason = last, bars[last]["c"], "end_of_data"
        gross = (exit_price - entry) * proposal.direction
        nights = (exit_index - start) / 24.0
        financing = entry * FINANCING_PER_YEAR[proposal.symbol] * nights / 252.0 * cost_multiple
        transaction = cost_price(proposal.symbol, bars[start], entry) * cost_multiple
        risk = proposal.risk_distance
        trades.append({
            "symbol": proposal.symbol, "dir": proposal.direction, "day": proposal.day,
            "evaluated_at": proposal.evaluated_at.isoformat(), "entry_time":
                bars[start]["time"].isoformat(), "exit_time": bars[exit_index]["time"].isoformat(),
            "entry": entry, "exit": exit_price, "risk": risk, "reason": reason,
            "bars_held": exit_index - start + 1, "volume": proposal.volume,
            "gross_r": gross / risk, "cost_r": (transaction + financing) / risk,
            "r": (gross - transaction - financing) / risk, "cluster": proposal.day,
        })
    return trades


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def stdev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    centre = mean(values)
    return (sum((value - centre) ** 2 for value in values) / (len(values) - 1)) ** 0.5


def cluster_bootstrap(trades: Sequence[dict[str, Any]], confidence: float, *, rounds: int = ROUNDS,
                      seed: int = SEED, block: int = BLOCK_DAYS) -> tuple[float, float, float]:
    """Moving-block bootstrap over entry days, identical in mechanism to the study's version."""
    clusters: dict[str, list[float]] = {}
    for trade in trades:
        clusters.setdefault(trade["cluster"], []).append(trade["r"])
    keys = sorted(clusters)
    sums = [sum(clusters[key]) for key in keys]
    counts = [len(clusters[key]) for key in keys]
    total = sum(counts)
    if total == 0:
        return 0.0, 0.0, 0.0
    observed = sum(sums) / total

    width = max(1, min(block, len(keys)))
    generator = random.Random(seed)
    draws: list[float] = []
    for _ in range(rounds):
        picked_sum = 0.0
        picked_count = 0
        while picked_count < total:
            start = generator.randrange(len(keys))
            for offset in range(width):
                choice = (start + offset) % len(keys)
                picked_sum += sums[choice]
                picked_count += counts[choice]
        draws.append(picked_sum / picked_count)
    draws.sort()
    tail = (1.0 - confidence) / 2.0
    return observed, draws[int(tail * (rounds - 1))], draws[int((1.0 - tail) * (rounds - 1))]


def group(trades: Sequence[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    buckets: dict[str, list[float]] = {}
    for trade in trades:
        buckets.setdefault(str(trade[key]), []).append(trade["r"])
    return {name: {"n": len(values), "mean": mean(values)} for name, values in sorted(buckets.items())}


def summary(name: str, trades: Sequence[dict[str, Any]]) -> dict[str, Any]:
    values = [trade["r"] for trade in trades]
    observed, low, high = cluster_bootstrap(trades, CONFIDENCE)
    return {
        "name": name, "n": len(values), "days": len({trade["cluster"] for trade in trades}),
        "mean_r": observed, "low_r": low, "high_r": high, "win_rate":
            sum(1 for value in values if value > 0) / len(values) if values else 0.0,
        "sd": stdev(values), "mean_gross_r": mean([trade["gross_r"] for trade in trades]),
        "mean_cost_r": mean([trade["cost_r"] for trade in trades]),
        "exits": {reason: sum(1 for trade in trades if trade["reason"] == reason)
                  for reason in sorted({str(trade["reason"]) for trade in trades})},
        "by_symbol": group(trades, "symbol"), "by_direction": group(trades, "dir"),
        "by_day": group(trades, "day"),
    }


def detectable_r(summary: dict[str, Any]) -> float:
    """The smallest mean this sample could prove at the pre-registered confidence."""
    z = NormalDist().inv_cdf(1.0 - (1.0 - CONFIDENCE) / 2.0)
    return z * summary.get("sd", 0.0) / max(1, summary.get("days", 1)) ** 0.5


def verdict(summary: dict[str, Any], double_mean: float) -> dict[str, Any]:
    reasons = []
    if summary["n"] < MIN_PROPOSALS:
        reasons.append(f"n {summary['n']} below the pre-registered {MIN_PROPOSALS} proposals")
    if summary["days"] < MIN_DAYS:
        reasons.append(f"{summary['days']} days below the pre-registered {MIN_DAYS}")
    if not summary["n"]:
        return {"status": "insufficient", "passed": False, "reasons": reasons or ["no scored trades"]}
    if summary["low_r"] <= 0.0:
        reasons.append(f"{CONFIDENCE * 100:.2f}% interval [{summary['low_r']:+.4f}, "
                       f"{summary['high_r']:+.4f}] covers zero")
    if double_mean <= 0.0:
        reasons.append(f"mean at double cost {double_mean:+.4f} R is not positive")
    return {"status": "pass" if not reasons else "fail", "passed": not reasons,
            "reasons": reasons, "detectable_r": detectable_r(summary),
            "confidence": CONFIDENCE, "threshold_n": MIN_PROPOSALS, "threshold_days": MIN_DAYS}


def load_bars(bars_dir: Path) -> dict[str, list[dict[str, Any]]]:
    out = {}
    for path in sorted(Path(bars_dir).glob("*.json")):
        out[path.stem.upper()] = bars_from_rows(json.loads(path.read_text(encoding="utf-8")))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", default="agent_data/shadow_live.db", type=Path)
    parser.add_argument("--bars-dir", default=BARS_DIR, type=Path)
    parser.add_argument("--after", default="2026-11-02T12:00:00+00:00")
    parser.add_argument("--before", default=None)
    parser.add_argument("--out", default=None, type=Path)
    parser.add_argument("--live", action="store_true", help="refresh cached bars from the terminal")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    after = _aware(args.after) if args.after else None
    before = _aware(args.before) if args.before else None
    proposals = proposals_from_journal(args.journal, after=after, before=before)
    if not proposals:
        print(f"no shadow proposals at or after {args.after} in {args.journal}")
        return 1

    if args.live:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from run_mt5_demo_trading import DemoTerminal

        terminal = DemoTerminal(server_utc_offset_seconds=7200, out=sys.stderr, language="ru")
        args.bars_dir.mkdir(parents=True, exist_ok=True)
        for symbol in sorted({proposal.symbol for proposal in proposals}):
            rows = bars_from_terminal(terminal, symbol)
            (args.bars_dir / f"{symbol}.json").write_text(
                json.dumps([{"time": bar["time"].isoformat(), "o": bar["o"], "h": bar["h"],
                             "l": bar["l"], "c": bar["c"], "spread": bar["spread"]} for bar in rows]),
                encoding="utf-8")
            print(f"cached {len(rows)} H1 bars for {symbol}")

    bars_by_symbol = load_bars(args.bars_dir)
    scored = score(proposals, bars_by_symbol)
    doubled = score(proposals, bars_by_symbol, cost_multiple=2.0)
    report = summary("forward_shadow", scored)
    report["double_cost_mean"] = mean([trade["r"] for trade in doubled])
    report["verdict"] = verdict(report, report["double_cost_mean"])
    report["proposals"] = len(proposals)
    report["unscored"] = len(proposals) - len(scored)
    report["after"] = after.isoformat() if after else None
    report["horizon_bars"] = HORIZON_BARS
    report["seed"] = SEED
    report["rounds"] = ROUNDS

    print(f"{report['n']} scored of {report['proposals']} proposals over {report['days']} days "
          f"(unscored for lack of bars: {report['unscored']})")
    verdict_line = f"    verdict: {report['verdict']['status'].upper()}"
    if report["verdict"]["reasons"]:
        verdict_line += " - " + "; ".join(report["verdict"]["reasons"])
    print(f"    mean net R {report['mean_r']:+.4f}, "
          f"{CONFIDENCE * 100:.2f}% interval [{report['low_r']:+.4f}, {report['high_r']:+.4f}], "
          f"double cost {report['double_cost_mean']:+.4f}")
    print(verdict_line)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
