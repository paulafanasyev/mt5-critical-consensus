#!/usr/bin/env python3
"""Run several paper scenarios side by side and print one board.

Each scenario is a measurable, market-neutral or structural source of return, evaluated on
live public data (no orders, no account):

  S1  funding carry           short perp + long spot, collect the funding rate
  S2  funding, wide universe  the same on SOL, XRP, DOGE, LINK, TON
  S3  funding threshold       hold only while the last rate is positive, flat otherwise
  S4  implied-vol premium     sell 30-day variance at DVOL, mark against realised vol
  S5  basis carry             spot against the dated future, annualised

The board is appended to agent_data/scenarios.jsonl each cycle so the numbers form a time
series rather than a single snapshot.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / "agent_data" / "scenarios.jsonl"
FUNDING_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "LINKUSDT")
NOTIONAL = 1_000.0
PERIODS_PER_YEAR = 3 * 365


def get(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "dsh"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def funding_rates(symbol: str, limit: int = 200) -> list[float]:
    payload = get(f"https://api.bybit.com/v5/market/funding/history?category=linear"
                  f"&symbol={symbol}&limit={limit}")
    rows = sorted(payload["result"]["list"], key=lambda row: int(row["fundingRateTimestamp"]))
    return [float(row["fundingRate"]) for row in rows]


def spot_and_perp(symbol: str) -> tuple[float, float]:
    spot = get(f"https://api.bybit.com/v5/market/tickers?category=spot&symbol={symbol}")
    perp = get(f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={symbol}")
    return (float(spot["result"]["list"][0]["lastPrice"]),
            float(perp["result"]["list"][0]["lastPrice"]))


def scenario_funding(rate: list[float]) -> dict:
    annual = statistics.fmean(rate) * PERIODS_PER_YEAR * 100
    return {"annualized_pct": round(annual, 2),
            "negative_share": round(sum(1 for r in rate if r < 0) / len(rate), 3)}


def scenario_threshold(rate: list[float]) -> dict:
    held = [r for r in rate if r > 0]
    annual = sum(r for r in rate if r > 0) * PERIODS_PER_YEAR / len(rate) * 100
    return {"annualized_pct": round(annual, 2), "held_share": round(len(held) / len(rate), 3)}


def realised_vol_30d() -> float | None:
    payload = get("https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1d&limit=31")
    closes = [float(row[4]) for row in payload]
    returns = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
    return statistics.pstdev(returns) * math.sqrt(365) * 100


def scenario_dvol() -> dict:
    data = get("https://www.deribit.com/api/v2/public/get_volatility_index_data"
               "?currency=BTC&start_timestamp=0&end_timestamp="
               f"{int(time.time() * 1000)}&resolution=1D")["result"]["data"]
    implied = float(data[-1][4])
    realised = realised_vol_30d()
    if realised is None:
        return {"implied": implied, "premium": None}
    return {"implied": round(implied, 2), "realised_30d": round(realised, 2),
            "premium": round(implied - realised, 2)}


def scenario_basis() -> dict:
    spot = get("https://www.deribit.com/api/v2/public/get_index_price?index_name=btc_usd")
    spot_price = float(spot["result"]["index_price"])
    instruments = get("https://www.deribit.com/api/v2/public/get_instruments"
                      "?currency=BTC&kind=future&expired=false")["result"]
    best = None
    for instrument in instruments:
        name = instrument["instrument_name"]
        ticker = get(f"https://www.deribit.com/api/v2/public/ticker?instrument_name={name}")["result"]
        expiry = instrument["expiration_timestamp"] / 1000
        days = (expiry - time.time()) / 86400
        if 20 <= days <= 100:
            price = ticker.get("last") or ticker.get("mark_price")
            if price:
                annual = (float(price) / spot_price - 1) * 365 / days * 100
                if best is None or abs(annual) < abs(best[1]):
                    best = (name, annual, days)
    if best is None:
        return {"annualized_pct": None}
    return {"contract": best[0], "annualized_pct": round(best[1], 2), "days": round(best[2], 1)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval-sec", type=int, default=600)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)

    while True:
        board = {"ts": datetime.now(timezone.utc).isoformat(), "scenarios": {}}
        try:
            btc = funding_rates("BTCUSDT")
            board["scenarios"]["S1_funding_carry_btc"] = scenario_funding(btc)
            board["scenarios"]["S3_funding_threshold_btc"] = scenario_threshold(btc)
        except Exception as exc:  # noqa: BLE001
            board["scenarios"]["S1_funding_carry_btc"] = {"error": type(exc).__name__}
        try:
            wide = {}
            for symbol in FUNDING_SYMBOLS:
                rates = funding_rates(symbol)
                wide[symbol] = scenario_funding(rates)["annualized_pct"] if rates else None
            board["scenarios"]["S2_funding_wide"] = wide
        except Exception as exc:  # noqa: BLE001
            board["scenarios"]["S2_funding_wide"] = {"error": type(exc).__name__}
        try:
            board["scenarios"]["S4_vol_premium_btc"] = scenario_dvol()
        except Exception as exc:  # noqa: BLE001
            board["scenarios"]["S4_vol_premium_btc"] = {"error": type(exc).__name__}
        try:
            board["scenarios"]["S5_basis_carry_btc"] = scenario_basis()
        except Exception as exc:  # noqa: BLE001
            board["scenarios"]["S5_basis_carry_btc"] = {"error": type(exc).__name__}
        try:
            btc_spot, btc_perp = spot_and_perp("BTCUSDT")
            board["scenarios"]["basis_bps_btc"] = round((btc_perp / btc_spot - 1) * 10_000, 2)
        except Exception:  # noqa: BLE001
            pass

        with LOG.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(board) + "\n")
        print(json.dumps(board, ensure_ascii=False), flush=True)
        if args.once:
            break
        time.sleep(args.interval_sec)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
