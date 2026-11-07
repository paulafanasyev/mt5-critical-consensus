#!/usr/bin/env python3
"""Where in the world is the overnight move positive and where is it negative?

The Chinese data showed a strongly negative overnight drift. That raises an obvious
question: is it a Chinese property or a general one? This script splits every asset we hold
into its overnight component (close to the next open) and its intraday component (open to
close), reports the mean and t-statistic of each, and says what the mirror trade would earn
after two cost assumptions.
"""
from __future__ import annotations

import json
import math
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MARKET = ROOT / "agent_data" / "market_data"
GOLD = ROOT / "agent_data" / "gold_data"
ASSETS = {
    "US 500 (SPY)": (MARKET, "spy"),
    "Nasdaq (QQQ)": (MARKET, "qqq"),
    "Gold ETF (GLD)": (MARKET, "gld"),
    "Silver ETF (SLV)": (MARKET, "slv"),
    "Gold futures": (MARKET, "gold_futures"),
    "Oil futures": (MARKET, "oil_futures"),
    "Treasuries (TLT)": (MARKET, "tlt"),
    "EM equities (EEM)": (MARKET, "eem"),
    "Dollar index": (MARKET, "dollar_index"),
    "BTC": (MARKET, "btc_usd"),
    "ETH": (MARKET, "eth_usd"),
    "Gold token (PAXG)": (GOLD, "paxg_d1"),
    "Shanghai": (MARKET, "shanghai"),
    "Shenzhen": (MARKET, "shenzhen"),
    "CSI 300 ETF": (MARKET, "csi300_etf"),
}
COSTS = (0.02, 0.05)


def load(path: Path, name: str) -> list[dict]:
    payload = json.loads((path / f"{name}.json").read_text(encoding="utf-8"))
    bars = payload["bars"] if isinstance(payload, dict) else payload
    out = []
    for bar in bars:
        out.append({"o": float(bar["o"]), "c": float(bar["c"])})
    return out


def stats(values: list[float]) -> tuple[float, float]:
    mean = statistics.fmean(values)
    sd = statistics.pstdev(values) / math.sqrt(len(values))
    return mean, mean / sd if sd else 0.0


def main() -> int:
    rows = []
    for label, (path, name) in ASSETS.items():
        if not (path / f"{name}.json").exists():
            continue
        bars = load(path, name)
        if len(bars) < 300:
            continue
        overnight = [bars[i]["o"] / bars[i - 1]["c"] - 1 for i in range(1, len(bars))]
        intraday = [bar["c"] / bar["o"] - 1 for bar in bars if bar["o"]]
        o_mean, o_t = stats(overnight)
        i_mean, i_t = stats(intraday)
        tradable = {}
        for cost_pct in COSTS:
            # the mirror trade earns the negative of the move, less the round-trip cost
            tradable[cost_pct] = -o_mean - cost_pct / 100
        rows.append({"asset": label, "n": len(overnight),
                     "overnight_pct": o_mean * 100, "overnight_t": o_t,
                     "intraday_pct": i_mean * 100, "intraday_t": i_t,
                     "short_overnight_annual_pct_002": tradable[COSTS[0]] * 252 * 100,
                     "short_overnight_annual_pct_005": tradable[COSTS[1]] * 252 * 100})

    rows.sort(key=lambda row: row["overnight_pct"])
    lines = ["overnight and intraday drifts, daily data", ""]
    header = (f"{'asset':20} {'n':>5} {'overnight%':>11} {'t':>6} {'intraday%':>10} "
              f"{'t':>6} {'short-ovn@2bp':>14} {'short-ovn@5bp':>14}")
    lines.append(header)
    for row in rows:
        lines.append(f"{row['asset']:20} {row['n']:5} {row['overnight_pct']:>+11.4f} "
                     f"{row['overnight_t']:>6.2f} {row['intraday_pct']:>+10.4f} "
                     f"{row['intraday_t']:>6.2f} "
                     f"{row['short_overnight_annual_pct_002']:>+14.1f} "
                     f"{row['short_overnight_annual_pct_005']:>+14.1f}")
    report = "\n".join(lines)
    print(report)
    (ROOT / "docs" / "evidence" / "overnight_study.txt").write_text(report, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
