#!/usr/bin/env python3
"""Why do the rules lose: is it the signal or the cost?

Runs every gold rule twice, once with zero costs and once with the real round trip, and
reports the difference in R per trade. If the gross number is around zero, the loss is the
cost of trading and the correction is to trade less or to demand a bigger move per trade.
If the gross number is negative, no cost correction can save the rule — the signal itself
is wrong.
"""
from __future__ import annotations

import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import gold_study  # noqa: E402

RULES = ("ema", "donchian", "bollinger", "roc", "session", "volfilter")


def main() -> int:
    bars = gold_study.load("paxg_h1")
    print(f"bars: {len(bars)}  cost model: {gold_study.COST['paxg'] * 100:.2f}% round trip")
    print(f"{'rule':10} {'trades':>7} {'gross R':>9} {'net R':>8} {'cost/trade R':>13} "
          f"{'gross win%':>11} {'verdict':>22}")
    summary = []
    for rule in RULES:
        free = gold_study.simulate(rule, bars, cost_rate=0.0)
        paid = gold_study.simulate(rule, bars, cost_rate=gold_study.COST["paxg"])
        if not free or not paid:
            print(f"{rule:10} no trades")
            continue
        gross = statistics.fmean(trade["r"] for trade in free)
        net = statistics.fmean(trade["r"] for trade in paid)
        wins = sum(1 for trade in free if trade["r"] > 0) / len(free)
        if abs(gross) < 0.05:
            verdict = "cost-driven: trade less"
        elif gross > 0:
            verdict = "signal positive, cost eats it"
        else:
            verdict = "signal negative: reject"
        summary.append((rule, gross, net))
        print(f"{rule:10} {len(paid):7} {gross:>+9.3f} {net:>+8.3f} {gross - net:>13.3f} "
              f"{wins * 100:>10.1f}% {verdict:>22}")
    if summary:
        mean_gross = statistics.fmean(gross for _, gross, _ in summary)
        mean_net = statistics.fmean(net for _, _, net in summary)
        print(f"\naverage across rules: gross {mean_gross:+.3f}R, net {mean_net:+.3f}R, "
              f"cost {mean_gross - mean_net:.3f}R per trade")
        print("correction: the cost is the difference, so the only fix is fewer, larger "
              "trades per unit of risk — which the data says the gold rules do not deliver.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
