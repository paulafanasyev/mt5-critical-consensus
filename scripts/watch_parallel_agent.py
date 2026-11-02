#!/usr/bin/env python3
"""Watch what the other agent does on the shared demo account.

Every cycle it records the account state, every open position and every deal that appeared
since the previous cycle. The point is an evidence trail of the parallel process's trading:
which symbols, which sizes, which magic number, and what it costs.

Read-only: this script never sends an order.
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / "agent_data" / "watch_parallel.jsonl"
SEEN = ROOT / "agent_data" / "watch_parallel_seen.json"


def main(argv: list[str] | None = None) -> int:
    import MetaTrader5 as mt5

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval-sec", type=int, default=300)
    args = parser.parse_args(argv)
    if not mt5.initialize():
        raise SystemExit("MetaTrader5.initialize failed")
    seen = set(json.loads(SEEN.read_text(encoding="utf-8"))) if SEEN.exists() else set()
    print(f"watching from {datetime.now(timezone.utc).isoformat()}", flush=True)

    while True:
        record = {"ts": datetime.now(timezone.utc).isoformat(), "positions": [], "new_deals": []}
        try:
            account = mt5.account_info()
            if account:
                record["balance"] = round(account.balance, 2)
                record["equity"] = round(account.equity, 2)
            for position in mt5.positions_get() or []:
                record["positions"].append({
                    "ticket": position.ticket, "symbol": position.symbol,
                    "side": "SELL" if position.type == 1 else "BUY",
                    "volume": position.volume, "open": position.price_open,
                    "sl": position.sl, "tp": position.tp, "profit": round(position.profit, 2),
                    "magic": position.magic, "time": position.time})
            since = datetime.now() - timedelta(days=2)
            deals = mt5.history_deals_get(since, datetime.now() + timedelta(days=1)) or []
            for deal in deals:
                key = f"{deal.ticket}"
                if key in seen:
                    continue
                seen.add(key)
                record["new_deals"].append({
                    "ticket": deal.ticket, "time": deal.time, "symbol": deal.symbol,
                    "type": deal.type, "volume": deal.volume, "price": deal.price,
                    "profit": round(deal.profit, 2), "magic": deal.magic,
                    "comment": deal.comment})
        except Exception as exc:  # noqa: BLE001
            record["error"] = f"{type(exc).__name__}: {exc}"
        with LOG.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
        SEEN.write_text(json.dumps(sorted(seen)), encoding="utf-8")
        for deal in record["new_deals"]:
            print(f"[{record['ts'][11:19]}] DEAL {deal['symbol']} type={deal['type']} "
                  f"vol={deal['volume']} price={deal['price']} P/L {deal['profit']:+.2f} "
                  f"magic={deal['magic']}", flush=True)
        print(f"[{record['ts'][11:19]}] balance {record.get('balance')} "
              f"positions {len(record['positions'])}", flush=True)
        time.sleep(args.interval_sec)


if __name__ == "__main__":
    raise SystemExit(main())
