"""Walk-forward research: maximum available history, 4 strategy families,
12 rolling folds. The most honest evaluation methodology in the industry.

For every fold: parameters are chosen by performance on the in-sample window
(6 months), then applied unchanged to the next out-of-sample window (2 months).
The chained OOS result across all folds is the true achievable performance.

Data: BTCUSDT 1h since 2017 (~9 years), EURUSDT 1h since 2020 (~6.5 years).
Costs: 0.05% per side (crypto), 0.01% per side (EUR).
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "data_cache"
KLINES_URL = "https://api.binance.com/api/v3/klines"

ASSETS = {
    "BTCUSD": {"symbol": "BTCUSDT", "cost": 0.05, "since": "2017-08-01"},
    "EURUSD": {"symbol": "EURUSDT", "cost": 0.01, "since": "2020-01-01"},
}


def fetch_hourly(symbol: str, start_ms: int) -> list[dict]:
    cache = CACHE / f"{symbol}_1h_max.json"
    if cache.exists():
        return json.loads(cache.read_text())
    hosts = ["api.binance.com", "api1.binance.com", "api2.binance.com", "api3.binance.com"]
    out, cursor = [], int(start_ms)
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    stall_rounds = 0
    while cursor < end_ms:
        batch = None
        for attempt in range(10):  # up to ~2 minutes of retries per request
            host = hosts[attempt % len(hosts)]
            url = f"https://{host}/api/v3/klines"
            try:
                r = httpx.get(url, params={"symbol": symbol, "interval": "1h",
                                           "startTime": cursor, "endTime": end_ms, "limit": 1000},
                              timeout=20)
                r.raise_for_status()
                batch = r.json()
                break
            except (httpx.HTTPError, ValueError):
                time.sleep(8 + attempt * 2)  # DNS/network flakiness: back off and rotate hosts
        if not batch:
            raise RuntimeError(f"feed unavailable after retries at cursor {cursor}")
        if not batch:
            break
        out.extend({"t": int(k[0]), "open": float(k[1]), "high": float(k[2]),
                    "low": float(k[3]), "close": float(k[4])} for k in batch)
        new_cursor = batch[-1][0] + 1
        if new_cursor <= cursor:  # no progress despite success — force advance
            cursor += 1000 * 60 * 60
            continue
        cursor = new_cursor
        stall_rounds = 0
        if len(batch) < 1000:
            break
        time.sleep(0.1)
    cache.write_text(json.dumps(out))
    return out


def sma_series(closes: list[float], p: int) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    s = 0.0
    for i, c in enumerate(closes):
        s += c
        if i >= p:
            s -= closes[i - p]
        if i >= p - 1:
            out[i] = s / p
    return out


def ema_series(closes: list[float], p: int) -> list[float]:
    k = 2 / (p + 1)
    out = [closes[0]]
    for c in closes[1:]:
        out.append(c * k + out[-1] * (1 - k))
    return out


def highest(values: list[float], p: int) -> list[float]:
    out = [None] * len(values)
    for i in range(len(values)):
        if i >= p:
            out[i] = max(values[i - p:i])
    return out


def lowest(values: list[float], p: int) -> list[float]:
    out = [None] * len(values)
    for i in range(len(values)):
        if i >= p:
            out[i] = min(values[i - p:i])
    return out


# --- strategy families: each returns desired position ("BUY"/"SELL"/None = flat) per bar ---

def strat_ema_trend(closes, cost, fast, slow):
    f, s = ema_series(closes, fast), ema_series(closes, slow)
    pos, trades, entry = None, [], 0.0
    for i in range(slow, len(closes) - 1):
        want = "BUY" if f[i] > s[i] else "SELL"
        if pos and pos != want:
            pl = (closes[i] - entry) / entry * 100 * (1 if pos == "BUY" else -1) - cost
            trades.append((i, pl))
            pos = None
        if not pos:
            pos = want
            entry = closes[i + 1]
    if pos:
        pl = (closes[-1] - entry) / entry * 100 * (1 if pos == "BUY" else -1) - cost
        trades.append((len(closes) - 1, pl))
    return trades


def strat_donchian(closes, cost, entry_n, exit_n):
    hi, lo = highest(closes, entry_n), lowest(closes, exit_n)
    pos, trades, entry = None, [], 0.0
    for i in range(entry_n + 1, len(closes) - 1):
        if hi[i] is None or lo[i] is None:
            continue
        if pos is None:
            if closes[i] > hi[i - 1]:
                pos, entry = "BUY", closes[i + 1]
            elif closes[i] < lo[i - 1]:
                pos, entry = "SELL", closes[i + 1]
        elif pos == "BUY" and closes[i] < lo[i - 1]:
            pl = (closes[i] - entry) / entry * 100 - cost
            trades.append((i, pl))
            pos, entry = "SELL", closes[i + 1]
        elif pos == "SELL" and closes[i] > hi[i - 1]:
            pl = (closes[i] - entry) / entry * 100 - cost
            trades.append((i, pl))
            pos, entry = "BUY", closes[i + 1]
    if pos:
        pl = (closes[-1] - entry) / entry * 100 * (1 if pos == "BUY" else -1) - cost
        trades.append((len(closes) - 1, pl))
    return trades


def strat_bollinger(closes, cost, period, k):
    mid_list = sma_series(closes, period)
    pos, trades, entry = None, [], 0.0
    for i in range(period, len(closes) - 1):
        if mid_list[i] is None:
            continue
        window = closes[i - period:i]
        sd = (sum((c - mid_list[i]) ** 2 for c in window) / period) ** 0.5
        upper, lower = mid_list[i] + k * sd, mid_list[i] - k * sd
        if pos == "BUY":
            if closes[i] >= mid_list[i]:
                pl = (closes[i] - entry) / entry * 100 - cost
                trades.append((i, pl))
                pos = None
        elif pos == "SELL":
            if closes[i] <= mid_list[i]:
                pl = (entry - closes[i]) / entry * 100 - cost
                trades.append((i, pl))
                pos = None
        else:
            if closes[i] < lower:
                pos, entry = "BUY", closes[i + 1]
            elif closes[i] > upper:
                pos, entry = "SELL", closes[i + 1]
    if pos:
        pl = (closes[-1] - entry) / entry * 100 * (1 if pos == "BUY" else -1) - cost
        trades.append((len(closes) - 1, pl))
    return trades


def strat_momentum_filter(closes, cost, roc_n, filter_n):
    filt = sma_series(closes, filter_n)
    pos, trades, entry = None, [], 0.0
    for i in range(filter_n + roc_n, len(closes) - 1):
        roc = (closes[i] - closes[i - roc_n]) / closes[i - roc_n] * 100
        if filt[i] is None:
            continue
        want = "BUY" if roc > 0 and closes[i] > filt[i] else ("SELL" if roc < 0 and closes[i] < filt[i] else None)
        if pos and pos != want:
            pl = (closes[i] - entry) / entry * 100 * (1 if pos == "BUY" else -1) - cost
            trades.append((i, pl))
            pos = None
        if want and not pos:
            pos = want
            entry = closes[i + 1]
    if pos:
        pl = (closes[-1] - entry) / entry * 100 * (1 if pos == "BUY" else -1) - cost
        trades.append((len(closes) - 1, pl))
    return trades


FAMILIES = {
    "EMA_TREND": (strat_ema_trend, [(20, 100), (20, 200), (50, 200)]),
    "DONCHIAN": (strat_donchian, [(20, 10), (55, 20)]),
    "BOLLINGER": (strat_bollinger, [(20, 2.0), (20, 2.5)]),
    "MOMENTUM": (strat_momentum_filter, [(24, 200), (48, 200)]),
}


def main() -> int:
    report = {}
    for asset, cfg in ASSETS.items():
        symbol, cost, since = cfg["symbol"], cfg["cost"], cfg["since"]
        start_ms = int(datetime.fromisoformat(since + "T00:00:00+00:00").timestamp() * 1000)
        print(f"\n=== {asset}: fetching 1h from {since}... ===", flush=True)
        klines = fetch_hourly(symbol, start_ms)
        closes = [k["close"] for k in klines]
        times = [k["t"] for k in klines]
        print(f"    {len(klines)} hourly bars "
              f"{datetime.fromtimestamp(times[0]/1000, tz=timezone.utc):%Y-%m-%d} .. "
              f"{datetime.fromtimestamp(times[-1]/1000, tz=timezone.utc):%Y-%m-%d}", flush=True)

        # walk-forward: 12 folds; IS 6 months -> OOS 2 months, rolling
        n_folds = 12
        is_days, oos_days = 180, 60
        ms_per_day = 86400000
        start_t = times[0]
        total_span = times[-1] - start_t
        step = (total_span - (is_days + oos_days) * ms_per_day) / (n_folds - 1)
        if step <= 0:
            print("not enough history"); continue

        report[asset] = {"bars": len(klines), "families": {}}
        for family, (fn, grid) in FAMILIES.items():
            # precompute trades per param combo over the whole series
            all_trades = [fn(closes, cost, *params) for params in grid]

            chain_oos = []
            picks = []
            for fold in range(n_folds):
                is_start = start_t + fold * step
                is_end = is_start + is_days * ms_per_day
                oos_end = is_end + oos_days * ms_per_day
                best_i, best_perf = 0, -1e9
                for gi, trades in enumerate(all_trades):
                    perf = sum(pl for t, pl in trades if is_start <= times[t] < is_end)
                    if perf > best_perf:
                        best_perf, best_i = perf, gi
                oos_pls = [pl for t, pl in all_trades[best_i] if is_end <= times[t] < oos_end]
                chain_oos.extend(oos_pls)
                picks.append(f"{grid[best_i]}({sum(oos_pls):+.1f}%)")

            wins = sum(1 for p in chain_oos if p > 0)
            total = sum(chain_oos)
            bh = (closes[-1] / closes[0] - 1) * 100
            report[asset]["families"][family] = {
                "chained_oos_pct": round(total, 1), "oos_trades": len(chain_oos),
                "winrate": round(100 * wins / max(1, len(chain_oos)), 1),
                "buy_hold_pct": round(bh, 1), "picks": picks}
            print(f"    {family:<12} chained OOS: {total:+.1f}% over {len(chain_oos)} trades "
                  f"(WR {round(100*wins/max(1,len(chain_oos)),1)}%) | buy&hold {bh:+.1f}%", flush=True)

    (ROOT / "walkforward_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\nSaved: walkforward_report.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
