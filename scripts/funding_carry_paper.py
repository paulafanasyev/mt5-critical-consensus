#!/usr/bin/env python3
"""Paper the delta-neutral funding carry and show what it actually earned.

Mechanics: hold spot and short the perpetual for the same notional. The price cancels;
the position collects (or pays) the funding rate every eight hours. This script replays
the real funding history on Bybit for BTC and ETH, charges taker fees on both legs at
entry and exit, and reports the net curve. Nothing is sent to an exchange.
"""
from __future__ import annotations

import json
import statistics
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "evidence" / "funding_carry_paper.txt"
NOTIONAL = 1_000.0        # USD per leg, per asset
SPOT_TAKER = 0.0010       # Bybit spot taker
PERP_TAKER = 0.00055      # Bybit linear taker
ROUND_TRIP_FEES = 2 * (SPOT_TAKER + PERP_TAKER) * NOTIONAL


def get(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "dsh"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def funding_history(symbol: str, limit: int = 200) -> list[float]:
    payload = get(f"https://api.bybit.com/v5/market/funding/history?category=linear"
                  f"&symbol={symbol}&limit={limit}")
    rows = payload["result"]["list"]
    rows.sort(key=lambda row: int(row["fundingRateTimestamp"]))
    return [float(row["fundingRate"]) for row in rows]


def main() -> int:
    lines = [f"delta-neutral funding carry, notional ${NOTIONAL:.0f} per leg, "
             f"fees ${ROUND_TRIP_FEES:.2f} per asset round trip", ""]
    total_net = 0.0
    for symbol in ("BTCUSDT", "ETHUSDT"):
        rates = funding_history(symbol)
        if not rates:
            lines.append(f"{symbol}: no funding history")
            continue
        funding_pnl = [NOTIONAL * rate for rate in rates]
        equity = 0.0
        peak = 0.0
        drawdown = 0.0
        curve = []
        for pnl in funding_pnl:
            equity += pnl
            peak = max(peak, equity)
            drawdown = max(drawdown, peak - equity)
            curve.append(equity)
        net = equity - ROUND_TRIP_FEES
        total_net += net
        periods_per_year = 3 * 365
        gross_annual = statistics.fmean(rates) * periods_per_year * 100
        net_annual = gross_annual - (ROUND_TRIP_FEES / NOTIONAL * 100)
        negative = sum(1 for rate in rates if rate < 0) / len(rates)
        lines.append(f"{symbol}: {len(rates)} periods")
        lines.append(f"  funding P/L  ${equity:+.2f}   after fees ${net:+.2f}   "
                     f"max drawdown ${drawdown:.2f}")
        lines.append(f"  gross annualized {gross_annual:+.2f}%   net of fees {net_annual:+.2f}%   "
                     f"negative periods {negative * 100:.0f}%")
        lines.append(f"  worst 8h {min(rates) * 100:+.4f}%   best {max(rates) * 100:+.4f}%")
    lines.append("")
    lines.append(f"combined net over the sample for two assets: ${total_net:+.2f} "
                 f"on ${2 * NOTIONAL:.0f} of capital")
    report = "\n".join(lines)
    print(report)
    OUT.write_text(report, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
