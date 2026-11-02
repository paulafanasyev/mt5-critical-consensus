#!/usr/bin/env python3
"""All known structural scenarios on one board, with a guard against drawdowns.

Scenarios (all market-neutral, none predicts direction):
  S1 bybit funding carry BTC        short perp + long spot, collect funding
  S2 bybit funding across assets    BTC ETH SOL XRP DOGE LINK
  S3 funding threshold              hold only while the last rate is positive
  S4 implied-vol premium            DVOL against 30-day realised
  S5 dated-futures basis            spot against a 20-100 day future
  S6 OKX funding                    a second venue for the same carry
  S7 Binance funding                a third venue
  S8 cross-venue basis              Kraken price against Binance price
  S9 calendar spread                near future against far future

Guards: each scenario carries a pre-declared rule that says when to stop, so a drawdown is
answered with a flat position instead of hope:
  - funding: HALT if the last three periods sum below zero; WARN on one negative print
  - basis: HALT if the annualised basis is negative
  - implied vol: HALT if implied is below realised (no premium left)
  - calendar: HALT if the curve inverts
  - perp basis: WARN above 30 bps, where crowded longs make the short leg fragile
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
BYBIT_ASSETS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "LINKUSDT")
PERIODS_PER_YEAR = 3 * 365


def get(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "dsh"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def bybit_funding(symbol: str, limit: int = 200) -> list[float]:
    payload = get(f"https://api.bybit.com/v5/market/funding/history?category=linear"
                  f"&symbol={symbol}&limit={limit}")
    rows = sorted(payload["result"]["list"], key=lambda row: int(row["fundingRateTimestamp"]))
    return [float(row["fundingRate"]) for row in rows]


def okx_funding(inst: str) -> list[float]:
    data = get(f"https://www.okx.com/api/v5/public/funding-rate-history?instId={inst}&limit=100")["data"]
    return [float(row["fundingRate"]) for row in sorted(data, key=lambda row: int(row["fundingTime"]))]


def binance_funding(symbol: str = "BTCUSDT", limit: int = 200) -> list[float]:
    data = get(f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={symbol}&limit={limit}")
    return [float(row["fundingRate"]) for row in data]


def spot_perp(symbol: str) -> tuple[float, float]:
    spot = get(f"https://api.bybit.com/v5/market/tickers?category=spot&symbol={symbol}")
    perp = get(f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={symbol}")
    return (float(spot["result"]["list"][0]["lastPrice"]),
            float(perp["result"]["list"][0]["lastPrice"]))


def annualise(rates: list[float]) -> float:
    return statistics.fmean(rates) * PERIODS_PER_YEAR * 100


def guard_funding(rates: list[float]) -> dict:
    recent = rates[-3:]
    if sum(recent) < 0:
        return {"state": "HALT", "reason": "last three funding periods sum below zero"}
    if recent and recent[-1] < 0:
        return {"state": "WARN", "reason": "last funding period was negative"}
    return {"state": "OK", "reason": "funding positive"}


def realised_vol_30d() -> float:
    data = get("https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1d&limit=31")
    closes = [float(row[4]) for row in data]
    returns = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
    return statistics.pstdev(returns) * math.sqrt(365) * 100


def dvol_now() -> tuple[float, float]:
    data = get("https://www.deribit.com/api/v2/public/get_volatility_index_data"
               "?currency=BTC&start_timestamp=0&end_timestamp="
               f"{int(time.time() * 1000)}&resolution=1D")["result"]["data"]
    implied = float(data[-1][4])
    return implied, implied - realised_vol_30d()


def futures_curve() -> list[tuple[str, float, float]]:
    spot = float(get("https://www.deribit.com/api/v2/public/get_index_price"
                     "?index_name=btc_usd")["result"]["index_price"])
    instruments = get("https://www.deribit.com/api/v2/public/get_instruments"
                      "?currency=BTC&kind=future&expired=false")["result"]
    out = []
    for instrument in instruments:
        name = instrument["instrument_name"]
        if "PERPETUAL" in name:
            continue
        ticker = get(f"https://www.deribit.com/api/v2/public/ticker?instrument_name={name}")["result"]
        price = ticker.get("last") or ticker.get("mark_price")
        days = (instrument["expiration_timestamp"] / 1000 - time.time()) / 86400
        if price and days > 5:
            out.append((name, days, (float(price) / spot - 1) * 365 / days * 100))
    return sorted(out, key=lambda row: row[1])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval-sec", type=int, default=600)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)

    while True:
        board = {"ts": datetime.now(timezone.utc).isoformat(), "scenarios": {}, "guards": {}}
        scenario = board["scenarios"]
        guards = board["guards"]

        try:
            btc = bybit_funding("BTCUSDT")
            scenario["S1_funding_carry_btc"] = {"annualized_pct": round(annualise(btc), 2)}
            guards["S1"] = guard_funding(btc)
            threshold = sum(r for r in btc if r > 0) * PERIODS_PER_YEAR / len(btc) * 100
            scenario["S3_funding_threshold_btc"] = {"annualized_pct": round(threshold, 2)}
            guards["S3"] = guard_funding(btc)
        except Exception as exc:  # noqa: BLE001
            scenario["S1_funding_carry_btc"] = {"error": type(exc).__name__}

        try:
            wide = {}
            for symbol in BYBIT_ASSETS:
                rates = bybit_funding(symbol)
                wide[symbol] = round(annualise(rates), 2) if rates else None
            scenario["S2_funding_wide"] = wide
        except Exception as exc:  # noqa: BLE001
            scenario["S2_funding_wide"] = {"error": type(exc).__name__}

        try:
            implied, premium = dvol_now()
            scenario["S4_vol_premium_btc"] = {"implied": round(implied, 2),
                                              "premium": round(premium, 2)}
            guards["S4"] = ({"state": "OK", "reason": "implied above realised"}
                            if premium > 0 else
                            {"state": "HALT", "reason": "implied below realised, no premium"})
        except Exception as exc:  # noqa: BLE001
            scenario["S4_vol_premium_btc"] = {"error": type(exc).__name__}

        try:
            curve = futures_curve()
            dated = [row for row in curve if 20 <= row[1] <= 100]
            if dated:
                name, days, annual = dated[0]
                scenario["S5_basis_carry_btc"] = {"contract": name,
                                                  "annualized_pct": round(annual, 2),
                                                  "days": round(days, 1)}
                guards["S5"] = ({"state": "OK", "reason": "positive basis"} if annual > 0
                                else {"state": "HALT", "reason": "negative basis"})
            if len(curve) >= 2:
                near, far = curve[0], curve[-1]
                scenario["S9_calendar_spread"] = {
                    "near": near[0], "near_annual_pct": round(near[2], 2),
                    "far": far[0], "far_annual_pct": round(far[2], 2),
                    "spread_pct": round(far[2] - near[2], 2)}
                guards["S9"] = ({"state": "OK", "reason": "contango"} if far[2] > near[2]
                                else {"state": "HALT", "reason": "curve inverted"})
        except Exception as exc:  # noqa: BLE001
            scenario["S5_basis_carry_btc"] = {"error": type(exc).__name__}

        try:
            okx_rates = okx_funding("BTC-USDT-SWAP")
            scenario["S6_okx_funding_btc"] = {"annualized_pct": round(annualise(okx_rates), 2)}
            guards["S6"] = guard_funding(okx_rates)
        except Exception as exc:  # noqa: BLE001
            scenario["S6_okx_funding_btc"] = {"error": type(exc).__name__}

        try:
            binance_rates = binance_funding("BTCUSDT")
            scenario["S7_binance_funding_btc"] = {"annualized_pct": round(annualise(binance_rates), 2)}
            guards["S7"] = guard_funding(binance_rates)
        except Exception as exc:  # noqa: BLE001
            scenario["S7_binance_funding_btc"] = {"error": type(exc).__name__}

        try:
            binance_btc = float(get("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT")["price"])
            kraken = get("https://api.kraken.com/0/public/Ticker?pair=XBTUSD")
            kraken_btc = float(next(iter(kraken["result"].values()))["c"][0])
            gap_bps = (kraken_btc / binance_btc - 1) * 10_000
            scenario["S8_cross_venue_basis"] = {"kraken_vs_binance_bps": round(gap_bps, 1)}
            guards["S8"] = ({"state": "OK", "reason": "gap within 30 bps"}
                            if abs(gap_bps) <= 30 else
                            {"state": "WARN", "reason": "gap wider than 30 bps"})
        except Exception as exc:  # noqa: BLE001
            scenario["S8_cross_venue_basis"] = {"error": type(exc).__name__}

        try:
            btc_spot, btc_perp = spot_perp("BTCUSDT")
            basis_bps = (btc_perp / btc_spot - 1) * 10_000
            scenario["perp_basis_bps_btc"] = round(basis_bps, 1)
            guards["perp"] = ({"state": "OK", "reason": "basis within 30 bps"}
                              if abs(basis_bps) <= 30 else
                              {"state": "WARN", "reason": "basis wider than 30 bps"})
        except Exception:  # noqa: BLE001
            pass

        board["halted"] = [key for key, guard in guards.items() if guard.get("state") == "HALT"]
        with LOG.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(board) + "\n")
        print(json.dumps(board, ensure_ascii=False), flush=True)
        if args.once:
            break
        time.sleep(args.interval_sec)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
