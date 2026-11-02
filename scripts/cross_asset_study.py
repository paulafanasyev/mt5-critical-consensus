#!/usr/bin/env python3
"""Cross-asset relationships on the data we actually hold.

Daily returns of gold (PAXG), Bitcoin, Ethereum and EUR/USD from Binance, aligned by
date: a correlation matrix, the range of the rolling 90-day correlation, and a lead-lag
scan of gold against Bitcoin. The point is to see whether any pair offers a stable,
tradable relationship — not to find a story that fits one period.
"""
from __future__ import annotations

import json
import math
import statistics
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "agent_data" / "gold_data"
SERIES = {"PAXG (gold)": "paxg_d1", "BTC": "btc_d1", "ETH": "eth_d1", "EUR/USD": "eur_d1"}


def daily_returns(name: str) -> dict[str, float]:
    rows = json.loads((DATA / f"{name}.json").read_text(encoding="utf-8"))
    closes = {}
    for row in rows:
        day = datetime.fromtimestamp(row["t"] / 1000, tz=timezone.utc).date().isoformat()
        closes[day] = float(row["c"])
    days = sorted(closes)
    return {days[i]: closes[days[i]] / closes[days[i - 1]] - 1.0 for i in range(1, len(days))}


def corr(a: list[float], b: list[float]) -> float:
    n = len(a)
    ma, mb = sum(a) / n, sum(b) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b)) / n
    sa = math.sqrt(sum((x - ma) ** 2 for x in a) / n)
    sb = math.sqrt(sum((y - mb) ** 2 for y in b) / n)
    return cov / (sa * sb) if sa and sb else 0.0


def main() -> int:
    returns = {label: daily_returns(name) for label, name in SERIES.items()}
    common = sorted(set.intersection(*(set(value) for value in returns.values())))
    print(f"aligned days: {len(common)} ({common[0]} .. {common[-1]})")
    labels = list(SERIES)
    print("\nfull-sample correlation of daily returns")
    print("            " + "".join(f"{label:>12}" for label in labels))
    for a in labels:
        row = "".join(f"{corr([returns[a][d] for d in common], [returns[b][d] for d in common]):12.2f}"
                      for b in labels)
        print(f"{a:12}" + row)

    gold = [returns["PAXG (gold)"][d] for d in common]
    print("\nrolling 90-day correlation with gold (min / median / max)")
    for label in labels[1:]:
        other = [returns[label][d] for d in common]
        window = [corr(gold[i:i + 90], other[i:i + 90]) for i in range(0, len(common) - 90, 15)]
        print(f"  gold vs {label:10} {min(window):+.2f}  {statistics.median(window):+.2f}  {max(window):+.2f}")

    print("\nlead-lag: correlation of gold return today with the other asset's return k days ago")
    btc = [returns["BTC"][d] for d in common]
    for lag in range(-5, 6):
        if lag >= 0:
            a_series, b_series = gold[lag:], btc[:len(btc) - lag]
        else:
            a_series, b_series = gold[:len(gold) + lag], btc[-lag:]
        print(f"  lag {lag:+d}: {corr(a_series, b_series):+.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
