#!/usr/bin/env python3
"""Score the demo forward run against the criteria fixed before it started.

The point of this file is to make "are we doing well?" a number instead of a feeling,
and to be able to answer "not yet" or "no". It reads the broker's own deal history for
the demo magic, so it counts what the broker filled, not what the agent intended.

Verdicts: INSUFFICIENT (not enough closed trades), FAIL (a kill criterion is hit),
PASS (every criterion holds). Exit code: 0 PASS, 1 FAIL, 2 INSUFFICIENT, 3 error.
"""
from __future__ import annotations

import argparse
import math
import sys
from datetime import datetime, timedelta, timezone


def closed_positions(mt5, *, symbol: str, magic: int, days: int) -> list[dict]:
    start = datetime.now(timezone.utc) - timedelta(days=days)
    deals = mt5.history_deals_get(start, datetime.now(timezone.utc)) or []
    by_position: dict[int, dict] = {}
    for deal in deals:
        if getattr(deal, "magic", 0) != magic:
            continue
        if symbol and getattr(deal, "symbol", "") != symbol:
            continue
        position = getattr(deal, "position_id", 0)
        row = by_position.setdefault(position, {"net": 0.0, "entry": None, "entry_time": None,
                                                "exit_time": None, "volume": 0.0})
        row["net"] += float(deal.profit) + float(deal.commission) + float(deal.swap)
        if deal.entry == 0:  # DEAL_ENTRY_IN
            row["entry"] = float(deal.price)
            row["entry_time"] = deal.time
            row["volume"] += float(deal.volume)
        else:
            row["exit_time"] = deal.time
    return [row for row in by_position.values() if row["exit_time"] is not None]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="XAUUSD")
    parser.add_argument("--magic", type=int, default=8172026)
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--balance", type=float, default=100_000.0)
    parser.add_argument("--min-trades", type=int, default=100)
    parser.add_argument("--min-t", type=float, default=2.0)
    parser.add_argument("--min-pf", type=float, default=1.1)
    parser.add_argument("--assumed-dd", type=float, default=0.15,
                        help="in-sample max drawdown the forward run is allowed to exceed by 1.5x")
    args = parser.parse_args(argv)

    import MetaTrader5 as mt5

    if not mt5.initialize():
        print("MetaTrader5.initialize failed", file=sys.stderr)
        return 3
    try:
        rows = closed_positions(mt5, symbol=args.symbol, magic=args.magic, days=args.days)
    finally:
        mt5.shutdown()

    n = len(rows)
    if n == 0:
        print(f"{args.symbol} magic {args.magic}: no closed positions — INSUFFICIENT")
        return 2

    pnls = [row["net"] for row in rows]
    wins = [value for value in pnls if value > 0]
    losses = [value for value in pnls if value <= 0]
    mean = sum(pnls) / n
    variance = sum((value - mean) ** 2 for value in pnls) / (n - 1) if n > 1 else 0.0
    stdev = math.sqrt(variance)
    t_stat = mean / (stdev / math.sqrt(n)) if stdev > 0 else 0.0
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    profit_factor = (gross_win / gross_loss) if gross_loss else float("inf")

    equity, peak, max_dd = args.balance, args.balance, 0.0
    for value in pnls:
        equity += value
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / args.balance)

    print(f"{args.symbol} magic {args.magic}: closed trades {n}")
    print(f"  win rate       {len(wins) / n * 100:6.1f}%")
    print(f"  mean per trade {mean:+8.2f} (account currency)")
    print(f"  t-statistic    {t_stat:+6.2f}  (need ≥ {args.min_t})")
    print(f"  profit factor  {profit_factor:6.2f}  (need > {args.min_pf})")
    print(f"  max drawdown   {max_dd * 100:6.2f}%  (cap {args.assumed_dd * 1.5 * 100:.1f}%)")

    reasons = []
    if n < args.min_trades:
        reasons.append(f"{n} closed trades, need {args.min_trades}")
    if t_stat < args.min_t:
        reasons.append(f"t={t_stat:.2f} below {args.min_t}")
    if profit_factor <= args.min_pf:
        reasons.append(f"profit factor {profit_factor:.2f} not above {args.min_pf}")
    if max_dd > args.assumed_dd * 1.5:
        reasons.append(f"drawdown {max_dd * 100:.1f}% above the 1.5x cap")

    if n < args.min_trades:
        print("VERDICT: INSUFFICIENT — " + "; ".join(reasons))
        return 2
    if reasons:
        print("VERDICT: FAIL — " + "; ".join(reasons))
        return 1
    print("VERDICT: PASS — every pre-registered criterion holds")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
