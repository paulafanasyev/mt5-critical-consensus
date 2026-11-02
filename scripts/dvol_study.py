#!/usr/bin/env python3
"""Is the crypto volatility premium real, and is it large enough to matter?

Deribit publishes a volatility index (DVOL) that is the 30-day implied vol of BTC/ETH
options. If implied is systematically above the volatility that actually follows, selling
volatility earns a premium. This script measures that directly:

    premium_t = DVOL_t - realised_30d(t)

and the settlement P/L of a variance swap sold at DVOL_t and settled at the realised
variance 30 days later, in variance points. It is the honest test of a premium that
cannot be traded here without an options account, and it is reported with a bootstrap
interval rather than a story.
"""
from __future__ import annotations

import json
import math
import random
import statistics
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "agent_data" / "market_data"
BOOTSTRAP = 10_000


def _get(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "dsh"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_dvol(currency: str) -> list[dict]:
    """Paginate Deribit's 1000-row limit back to the index's first day."""
    rows: dict[int, float] = {}
    end = int(time.time() * 1000)
    for _ in range(10):
        payload = _get("https://www.deribit.com/api/v2/public/get_volatility_index_data"
                       f"?currency={currency}&start_timestamp=1600000000000"
                       f"&end_timestamp={end}&resolution=1D")
        batch = payload["result"]["data"]
        if not batch:
            break
        for row in batch:
            rows[int(row[0])] = float(row[4])
        end = int(batch[0][0]) - 1
        if int(batch[0][0]) <= 1610000000000:
            break
        time.sleep(0.2)
    return [{"t": stamp, "v": value} for stamp, value in sorted(rows.items())]


def load_prices(name: str) -> dict[str, float]:
    payload = json.loads((DATA / f"{name}.json").read_text(encoding="utf-8"))
    return {datetime.fromtimestamp(bar["t"] / 1000, tz=timezone.utc).date().isoformat(): bar["c"]
            for bar in payload["bars"]}


def realised_vol(prices: dict[str, float], days: list[str], end: int, window: int = 30) -> float | None:
    if end < window:
        return None
    window_returns = [prices[days[i]] / prices[days[i - 1]] - 1 for i in range(end - window, end)]
    if len(window_returns) < window:
        return None
    return statistics.pstdev(window_returns) * math.sqrt(365) * 100


def bootstrap_ci(values: list[float], confidence: float = 0.95) -> tuple[float, float]:
    generator = random.Random(20261101)
    n = len(values)
    means = sorted(sum(values[generator.randrange(n)] for _ in range(n)) / n
                   for _ in range(BOOTSTRAP))
    tail = (1 - confidence) / 2
    return means[int(tail * (BOOTSTRAP - 1))], means[int((1 - tail) * (BOOTSTRAP - 1))]


def main() -> int:
    prices = load_prices("btc_usd")
    days = sorted(prices)
    index_of = {day: i for i, day in enumerate(days)}

    for currency, name in (("BTC", "btc"), ("ETH", "eth")):
        series = fetch_dvol(currency)
        (DATA / f"{name}_dvol_full.json").write_text(json.dumps(series), encoding="utf-8")
        print(f"\n{currency}: {len(series)} DVOL days "
              f"{datetime.fromtimestamp(series[0]['t'] / 1000, tz=timezone.utc).date()} .. "
              f"{datetime.fromtimestamp(series[-1]['t'] / 1000, tz=timezone.utc).date()}")

        asset_prices = load_prices(f"{name}_usd") if name == "eth" else prices
        asset_days = sorted(asset_prices)
        asset_index = {day: i for i, day in enumerate(asset_days)}
        premia, settlements = [], []
        for row in series:
            day = datetime.fromtimestamp(row["t"] / 1000, tz=timezone.utc).date().isoformat()
            if day not in asset_index:
                continue
            realised = realised_vol(asset_prices, asset_days, asset_index[day])
            if realised is None:
                continue
            implied = row["v"]
            premia.append(implied - realised)
            # Variance-swap P/L of selling at implied and settling at the realised vol that
            # follows: positive when realised comes in below implied, in variance points.
            forward = asset_index[day] + 30
            if forward < len(asset_days):
                future = realised_vol(asset_prices, asset_days, forward)
                if future is not None:
                    settlements.append((implied ** 2 - future ** 2) / 100.0)

        if not premia:
            print("  no overlap with price history")
            continue
        low, high = bootstrap_ci(premia)
        print(f"  mean premium {statistics.fmean(premia):+.2f} vol points  "
              f"95% CI [{low:+.2f}, {high:+.2f}]  n={len(premia)}")
        if settlements:
            low_s, high_s = bootstrap_ci(settlements)
            wins = sum(1 for value in settlements if value > 0) / len(settlements)
            print(f"  short-variance P/L {statistics.fmean(settlements):+.2f} variance points "
                  f"95% CI [{low_s:+.2f}, {high_s:+.2f}]  wins {wins * 100:.1f}%  n={len(settlements)}")
            # A round trip in crypto options costs roughly 1.5 vol points in spread; charge
            # it by selling at implied minus that, and show the worst settlements too.
            haircut = 1.5
            net_settlements = []
            for row in series:
                day = datetime.fromtimestamp(row["t"] / 1000, tz=timezone.utc).date().isoformat()
                if day not in asset_index:
                    continue
                forward = asset_index[day] + 30
                if forward >= len(asset_days):
                    continue
                future = realised_vol(asset_prices, asset_days, forward)
                if future is None:
                    continue
                net_settlements.append(((row["v"] - haircut) ** 2 - future ** 2) / 100.0)
            low_n, high_n = bootstrap_ci(net_settlements)
            print(f"  after a {haircut:.1f}-point spread {statistics.fmean(net_settlements):+.2f} "
                  f"variance points 95% CI [{low_n:+.2f}, {high_n:+.2f}]")
            worst = sorted(settlements)[:3]
            print(f"  worst settlements: {[round(value, 1) for value in worst]}")
            print("  verdict:", "premium survives a spread haircut (options required)"
                  if low_n > 0 else "premium does not survive the spread haircut")
            by_year: dict[int, list[float]] = {}
            for row in series:
                day = datetime.fromtimestamp(row["t"] / 1000, tz=timezone.utc).date()
                if day.isoformat() not in asset_index:
                    continue
                forward = asset_index[day.isoformat()] + 30
                if forward >= len(asset_days):
                    continue
                future = realised_vol(asset_prices, asset_days, forward)
                if future is not None:
                    by_year.setdefault(day.year, []).append((row["v"] ** 2 - future ** 2) / 100.0)
            print("  by year: " + ", ".join(
                f"{year}: {statistics.fmean(values):+.1f} (n={len(values)})"
                for year, values in sorted(by_year.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
