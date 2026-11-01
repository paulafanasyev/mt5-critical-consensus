#!/usr/bin/env python3
"""Score the demo forward run against criteria fixed before it started.

The point of this file is to make "are we doing well?" a number instead of a feeling,
and to be able to answer "not yet", "no edge" or "not decided". It reads the broker's
own deal history for the demo magic, so it counts what the broker filled, not what the
agent intended.

The statistic is the mean per-trade result with a 90% bootstrap confidence interval,
because a t-test on fat-tailed 2R trades overstates significance and a point estimate
hides the fact that a small edge takes hundreds of trades to separate from zero.

Verdicts and exit codes:
  0 PASS          the interval is entirely above zero and the drawdown gate holds
  1 FAIL          the interval is entirely below zero, or a kill criterion is hit
  2 INSUFFICIENT  fewer than --min-trades closed trades
  3 INCONCLUSIVE  the interval contains zero: the data cannot tell yet
  4 error

Drawdown here is closed-trade drawdown: the curve is built from realised P/L in exit
order, so a losing trade open across a peak is invisible. That is stated, not hidden.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
from datetime import datetime, timedelta, timezone

# MT5 deal entry codes: 0 in, 1 out, 2 in/out (partial close that also opens), 3 out by.
CLOSING_ENTRIES = {1, 2, 3}
BOOTSTRAP_ROUNDS = 10_000


def closed_positions(mt5, *, symbol: str, magic: int, days: int) -> tuple[list[dict], int]:
    """Return (closed rows in exit order, number of still-open positions seen)."""
    start = datetime.now(timezone.utc) - timedelta(days=days)
    deals = mt5.history_deals_get(start, datetime.now(timezone.utc)) or []
    by_position: dict[int, dict] = {}
    for deal in deals:
        if getattr(deal, "magic", 0) != magic:
            continue
        if symbol and getattr(deal, "symbol", "") != symbol:
            continue
        position = getattr(deal, "position_id", 0)
        row = by_position.setdefault(position, {"net": 0.0, "exit_time": None,
                                                "closed": False, "volume": 0.0})
        row["net"] += float(deal.profit) + float(deal.commission) + float(deal.swap)
        if deal.entry == 0:
            row["volume"] += float(deal.volume)
        if deal.entry in CLOSING_ENTRIES:
            row["closed"] = True
            row["exit_time"] = max(row["exit_time"] or deal.time, deal.time)
    closed = sorted((row for row in by_position.values() if row["closed"]),
                    key=lambda row: row["exit_time"])
    open_count = sum(1 for row in by_position.values() if not row["closed"])
    return closed, open_count


def bootstrap_interval(values: list[float], rounds: int = BOOTSTRAP_ROUNDS,
                       confidence: float = 0.90) -> tuple[float, float]:
    generator = random.Random(20261101)
    means = []
    n = len(values)
    for _ in range(rounds):
        means.append(sum(values[generator.randrange(n)] for _ in range(n)) / n)
    means.sort()
    tail = (1.0 - confidence) / 2.0
    low = means[int(tail * (rounds - 1))]
    high = means[int((1.0 - tail) * (rounds - 1))]
    return low, high


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="XAUUSD")
    parser.add_argument("--magic", type=int, default=8172026)
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--balance", type=float, default=100_000.0)
    parser.add_argument("--login", type=int, default=0,
                        help="refuse to score any other account than this demo login")
    parser.add_argument("--criteria", default="",
                        help="JSON file with the frozen thresholds; printed and hashed")
    parser.add_argument("--min-trades", type=int, default=100)
    parser.add_argument("--kill-fraction", type=float, default=0.05,
                        help="cumulative loss that halts the test, as a fraction of balance")
    parser.add_argument("--dd-fraction", type=float, default=0.15,
                        help="closed-trade drawdown that halts the test")
    args = parser.parse_args(argv)

    criteria = {"min_trades": args.min_trades, "kill_fraction": args.kill_fraction,
                "dd_fraction": args.dd_fraction}
    if args.criteria:
        criteria.update(json.loads(open(args.criteria, encoding="utf-8").read()))
    import hashlib

    digest = hashlib.sha256(json.dumps(criteria, sort_keys=True).encode()).hexdigest()[:16]
    print(f"criteria {json.dumps(criteria, sort_keys=True)} sha256:{digest}")

    import MetaTrader5 as mt5

    if not mt5.initialize():
        print("MetaTrader5.initialize failed", file=sys.stderr)
        return 4
    try:
        account = mt5.account_info()
        if account is None:
            print("no account information from the terminal", file=sys.stderr)
            return 4
        if getattr(account, "trade_mode", None) != mt5.ACCOUNT_TRADE_MODE_DEMO:
            print("REFUSED: the terminal is not on a demo account; nothing is scored")
            return 4
        if args.login and getattr(account, "login", 0) != args.login:
            print(f"REFUSED: terminal login {getattr(account, 'login', 0)} is not "
                  f"the expected {args.login}")
            return 4
        rows, open_count = closed_positions(mt5, symbol=args.symbol, magic=args.magic,
                                            days=args.days)
    finally:
        mt5.shutdown()

    n = len(rows)
    if n == 0:
        print(f"{args.symbol} magic {args.magic}: no closed positions "
              f"({open_count} open) — INSUFFICIENT")
        print("A forward run has not traded yet; a replay cannot fill this in.")
        return 2

    pnls = [row["net"] for row in rows]
    mean = sum(pnls) / n
    low, high = bootstrap_interval(pnls)
    wins = sum(1 for value in pnls if value > 0)
    cumulative = sum(pnls)
    equity, peak, max_dd = args.balance, args.balance, 0.0
    for value in pnls:
        equity += value
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / args.balance)
    worst = min(pnls)

    print(f"{args.symbol} magic {args.magic}: closed trades {n} ({open_count} still open)")
    print(f"  win rate          {wins / n * 100:6.1f}%")
    print(f"  mean per trade    {mean:+9.2f}   90% bootstrap CI [{low:+.2f}, {high:+.2f}]")
    print(f"  cumulative        {cumulative:+9.2f}   ({cumulative / args.balance * 100:+.2f}%)")
    print(f"  worst trade       {worst:+9.2f}")
    print(f"  closed-trade DD   {max_dd * 100:6.2f}%   (halt at "
          f"{criteria['dd_fraction'] * 100:.1f}%)")

    if n < criteria["min_trades"]:
        print(f"VERDICT: INSUFFICIENT — {n} closed trades, need {criteria['min_trades']}")
        return 2
    if cumulative <= -criteria["kill_fraction"] * args.balance:
        print("VERDICT: FAIL — cumulative loss hit the kill level")
        return 1
    if max_dd >= criteria["dd_fraction"]:
        print("VERDICT: FAIL — closed-trade drawdown hit the halt level")
        return 1
    if high < 0:
        print("VERDICT: FAIL — the whole interval is below zero")
        return 1
    if low > 0:
        print("VERDICT: PASS — the interval is entirely above zero")
        return 0
    print("VERDICT: INCONCLUSIVE — the interval contains zero; more trades decide, not more analysis")
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
