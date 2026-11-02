#!/usr/bin/env python3
"""Paper-sell a 30-day strangle on Deribit using real quoted prices.

The volatility premium measured from DVOL (+8.8 vol points for BTC) has to survive the
prices a seller actually gets. This script sells one out-of-the-money call and one
out-of-the-money put at the strikes closest to 10% away from spot, at the quoted bid, holds
to expiry, marks the position every cycle at the quoted mid, and settles at intrinsic.

No account, no key: Deribit's option chain is public. State is kept in
agent_data/vol_paper_state.json and every mark is appended to agent_data/vol_paper.jsonl.
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "agent_data" / "vol_paper_state.json"
LOG = ROOT / "agent_data" / "vol_paper.jsonl"
CURRENCIES = ("BTC", "ETH")
TARGET_DAYS = 30
WING = 0.10  # strikes 10% away from spot


def get(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "dsh"})
    with urllib.request.urlopen(request, timeout=25) as response:
        return json.loads(response.read().decode("utf-8"))


def chain(currency: str) -> tuple[float, list[dict]]:
    payload = get("https://www.deribit.com/api/v2/public/get_book_summary_by_currency"
                  f"?currency={currency}&kind=option")
    rows = []
    underlying = None
    for item in payload["result"]:
        parts = item["instrument_name"].split("-")
        if len(parts) != 4:
            continue
        _, expiry, strike, kind = parts
        try:
            expiry_time = datetime.strptime(expiry, "%d%b%y").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        underlying = underlying or item.get("underlying_price")
        rows.append({"instrument": item["instrument_name"], "expiry": expiry,
                     "expiry_time": expiry_time, "strike": float(strike), "kind": kind,
                     "bid": item.get("bid_price"), "ask": item.get("ask_price"),
                     "mid": item.get("mark_price")})
    return float(underlying or 0), rows


def pick(currency: str) -> dict | None:
    underlying, rows = chain(currency)
    if not underlying or not rows:
        return None
    now = datetime.now(timezone.utc)
    candidates = [row for row in rows
                  if 20 <= (row["expiry_time"] - now).days <= 45 and row["bid"]]
    if not candidates:
        return None
    expiry = min({row["expiry"] for row in candidates},
                 key=lambda name: next(row["expiry_time"] for row in candidates
                                       if row["expiry"] == name))
    same = [row for row in candidates if row["expiry"] == expiry]
    calls = [row for row in same if row["kind"] == "C"]
    puts = [row for row in same if row["kind"] == "P"]
    if not calls or not puts:
        return None
    call = min(calls, key=lambda row: abs(row["strike"] - underlying * (1 + WING)))
    put = min(puts, key=lambda row: abs(row["strike"] - underlying * (1 - WING)))
    return {"currency": currency, "opened_at": now.isoformat(), "expiry": expiry,
            "underlying_at_open": underlying,
            "call": {"instrument": call["instrument"], "strike": call["strike"],
                     "entry": call["bid"]},
            "put": {"instrument": put["instrument"], "strike": put["strike"],
                    "entry": put["bid"]},
            "credit": (call["bid"] or 0) + (put["bid"] or 0)}


def mark(position: dict) -> dict | None:
    underlying, rows = chain(position["currency"])
    if not underlying:
        return None
    now = datetime.now(timezone.utc)
    expiry_time = next((row["expiry_time"] for row in rows
                        if row["expiry"] == position["expiry"]), None)
    if expiry_time is None:
        return None
    by_name = {row["instrument"]: row for row in rows}
    call = by_name.get(position["call"]["instrument"])
    put = by_name.get(position["put"]["instrument"])
    if not call or not put:
        return None
    mark_price = (call["mid"] or 0) + (put["mid"] or 0)
    days_left = (expiry_time - now).total_seconds() / 86400
    result = {"ts": now.isoformat(), "currency": position["currency"],
              "underlying": underlying, "mark": mark_price,
              "entry_credit": position["credit"],
              "unrealized": position["credit"] - mark_price, "days_left": round(days_left, 2)}
    if days_left <= 0:
        intrinsic = (max(0.0, underlying - position["call"]["strike"])
                     + max(0.0, position["put"]["strike"] - underlying))
        result["settled"] = True
        result["realized"] = position["credit"] - intrinsic
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval-sec", type=int, default=3600)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)

    positions = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else None
    if not positions:
        positions = []
        for currency in CURRENCIES:
            picked = pick(currency)
            if picked:
                positions.append(picked)
                print(f"opened {currency} strangle exp {picked['expiry']} "
                      f"credit {picked['credit']:.4f} {currency}", flush=True)
        STATE.write_text(json.dumps(positions), encoding="utf-8")

    while True:
        still_open = []
        for position in positions:
            if position.get("settled"):
                still_open.append(position)
                continue
            result = mark(position)
            if result is None:
                still_open.append(position)
                continue
            with LOG.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(result) + "\n")
            print(f"[{result['ts'][11:19]}] {result['currency']} underlying "
                  f"{result['underlying']:.0f} mark {result['mark']:.4f} "
                  f"unrealized {result['unrealized']:+.4f} ({result['days_left']}d)", flush=True)
            if result.get("settled"):
                position["settled"] = True
                position["realized"] = result["realized"]
                print(f"settled {result['currency']}: {result['realized']:+.4f}", flush=True)
            still_open.append(position)
        positions = still_open
        STATE.write_text(json.dumps(positions), encoding="utf-8")
        if args.once:
            break
        time.sleep(args.interval_sec)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
