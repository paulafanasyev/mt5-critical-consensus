"""Deep gold research v2: maximum history (PAXG 1h since 2020), microstructure
profile and walk-forward tests of gold strategies. Trades are timestamped.
"""
from __future__ import annotations

import json
import math
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "data_cache"
COST = 0.05
HOURS = ["00 Asia", "01 Asia", "02 Asia", "03 Asia", "04 Asia", "05 Asia", "06 Asia",
         "07 London", "08 London", "09 London", "10 London", "11 London", "12 London",
         "13 NY", "14 NY", "15 NY", "16 NY", "17 NY", "18 NY", "19 NY",
         "20 NY-late", "21 NY-late", "22 rollover", "23 rollover"]
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def fetch_hourly(symbol: str, start_ms: int) -> list[dict]:
    cache = CACHE / f"{symbol}_1h_max.json"
    if cache.exists():
        return json.loads(cache.read_text())
    hosts = ["api.binance.com", "api1.binance.com", "api2.binance.com", "api3.binance.com"]
    out, cursor = [], start_ms
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    while cursor < end_ms:
        batch = None
        for attempt in range(10):
            host = hosts[attempt % len(hosts)]
            try:
                r = httpx.get(f"https://{host}/api/v3/klines",
                              params={"symbol": symbol, "interval": "1h", "startTime": cursor,
                                      "endTime": end_ms, "limit": 1000}, timeout=20)
                r.raise_for_status()
                batch = r.json()
                break
            except (httpx.HTTPError, ValueError):
                time.sleep(8 + attempt * 2)
        if not batch:
            raise RuntimeError("feed down")
        out.extend({"t": int(k[0]), "open": float(k[1]), "high": float(k[2]),
                    "low": float(k[3]), "close": float(k[4])} for k in batch)
        new_cursor = batch[-1][0] + 1
        if new_cursor <= cursor:
            cursor += 3600000
            continue
        cursor = new_cursor
        if len(batch) < 1000:
            break
        time.sleep(0.1)
    cache.write_text(json.dumps(out))
    return out


def main() -> int:
    start_ms = int(datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    print("Fetching PAXG 1h since 2020...", flush=True)
    klines = fetch_hourly("PAXGUSDT", start_ms)
    print(f"    {len(klines)} hourly bars "
          f"{datetime.fromtimestamp(klines[0]['t']/1000, tz=timezone.utc):%Y-%m-%d} .. "
          f"{datetime.fromtimestamp(klines[-1]['t']/1000, tz=timezone.utc):%Y-%m-%d}\n", flush=True)

    closes = [k["close"] for k in klines]
    times = [k["t"] for k in klines]

    # ---------- Part 1: microstructure ----------
    hour_ret = {h: [] for h in range(24)}
    hour_rng = {h: [] for h in range(24)}
    wd_ret = {d: [] for d in range(7)}
    for i in range(1, len(klines)):
        prev, k = klines[i - 1], klines[i]
        dt = datetime.fromtimestamp(k["t"] / 1000, tz=timezone.utc)
        ret = (k["close"] - prev["close"]) / prev["close"] * 100
        rng = (k["high"] - k["low"]) / k["close"] * 100
        hour_ret[dt.hour].append(ret)
        hour_rng[dt.hour].append(rng)
        if dt.weekday() < 5:
            wd_ret[dt.weekday()].append(ret)

    lines = ["=== HOURLY PROFILE (avg return %, avg range %, t-stat, UTC hours) ==="]
    for h in range(24):
        r = statistics.mean(hour_ret[h]); sd = statistics.stdev(hour_ret[h]); n = len(hour_ret[h])
        rg = statistics.mean(hour_rng[h])
        t = r / (sd / math.sqrt(n)) if n > 2 and sd > 0 else 0
        marker = "  <== significant" if abs(t) > 2 else ""
        lines.append(f"  {HOURS[h]:<12} avg {r:+.4f}%  range {rg:.3f}%  t={t:+.2f}{marker}")
    lines.append("\n=== WEEKDAY ===")
    for d in range(5):
        r = statistics.mean(wd_ret[d]); sd = statistics.stdev(wd_ret[d]); n = len(wd_ret[d])
        t = r / (sd / math.sqrt(n)) if n > 2 and sd > 0 else 0
        lines.append(f"  {WEEKDAYS[d]:<5} avg {r:+.4f}%  t={t:+.2f}  n={n}")

    pairs = []
    for i in range(24, len(closes) - 24, 24):
        pairs.append(((closes[i] - closes[i - 24]) / closes[i - 24],
                      (closes[i + 24] - closes[i]) / closes[i]))
    mx = statistics.mean(p[0] for p in pairs); my = statistics.mean(p[1] for p in pairs)
    cov = sum((x - mx) * (y - my) for x, y in pairs) / len(pairs)
    sx = statistics.stdev(p[0] for p in pairs); sy = statistics.stdev(p[1] for p in pairs)
    corr = cov / (sx * sy) if sx > 0 and sy > 0 else 0
    lines.append("\n=== TREND PERSISTENCE (24h return -> next 24h) ===")
    lines.append(f"  corr = {corr:+.3f}  (>0 trends continue, <0 mean reversion)")

    # ---------- Part 2: strategies with timestamped trades ----------
    def ema_series(vals, p):
        k = 2 / (p + 1); e = vals[0]; out = [e]
        for v in vals[1:]:
            e = v * k + e * (1 - k); out.append(e)
        return out

    def strat_ema(cl, tm, fast, slow):
        f, s = ema_series(cl, fast), ema_series(cl, slow)
        pos, tr, entry = None, [], 0.0
        for i in range(slow, len(cl) - 1):
            want = "BUY" if f[i] > s[i] else "SELL"
            if pos and pos != want:
                pl = (cl[i] - entry) / entry * 100 * (1 if pos == "BUY" else -1) - COST
                tr.append((tm[i], pl)); pos = None
            if not pos and want:
                pos, entry = want, cl[i + 1]
        if pos:
            pl = (cl[-1] - entry) / entry * 100 * (1 if pos == "BUY" else -1) - COST
            tr.append((tm[-1], pl))
        return tr

    def strat_boll(cl, tm, period, k):
        pos, tr, entry = None, [], 0.0
        for i in range(period, len(cl) - 1):
            w = cl[i - period:i]
            m = sum(w) / period
            sd = (sum((c - m) ** 2 for c in w) / period) ** 0.5
            if pos == "BUY" and cl[i] >= m:
                pl = (cl[i] - entry) / entry * 100 - COST; tr.append((tm[i], pl)); pos = None
            elif pos == "SELL" and cl[i] <= m:
                pl = (entry - cl[i]) / entry * 100 - COST; tr.append((tm[i], pl)); pos = None
            elif not pos:
                if cl[i] < m - k * sd:
                    pos, entry = "BUY", cl[i + 1]
                elif cl[i] > m + k * sd:
                    pos, entry = "SELL", cl[i + 1]
        if pos:
            pl = (cl[-1] - entry) / entry * 100 * (1 if pos == "BUY" else -1) - COST
            tr.append((tm[-1], pl))
        return tr

    def strat_mom(cl, tm, roc_n, filt_n):
        filt = [None] * len(cl)
        for i in range(filt_n, len(cl)):
            filt[i] = sum(cl[i - filt_n:i]) / filt_n
        pos, tr, entry = None, [], 0.0
        for i in range(filt_n + roc_n, len(cl) - 1):
            roc = (cl[i] - cl[i - roc_n]) / cl[i - roc_n] * 100
            want = None
            if roc > 0 and cl[i] > filt[i]:
                want = "BUY"
            elif roc < 0 and cl[i] < filt[i]:
                want = "SELL"
            if pos and pos != want:
                pl = (cl[i] - entry) / entry * 100 * (1 if pos == "BUY" else -1) - COST
                tr.append((tm[i], pl)); pos = None
            if want and not pos:
                pos, entry = want, cl[i + 1]
        if pos:
            pl = (cl[-1] - entry) / entry * 100 * (1 if pos == "BUY" else -1) - COST
            tr.append((tm[-1], pl))
        return tr

    strat = {"EMA trend (20/100)": lambda c, t: strat_ema(c, t, 20, 100),
             "EMA trend (50/200)": lambda c, t: strat_ema(c, t, 50, 200),
             "Bollinger (20, 2.5)": lambda c, t: strat_boll(c, t, 20, 2.5),
             "Momentum (24, 200)": lambda c, t: strat_mom(c, t, 24, 200)}

    # ---------- Part 2: walk-forward 8 folds ----------
    lines2 = ["\n=== WALK-FORWARD: 8 folds (IS 3/4 of history span -> OOS 1/4), chained OOS ==="]
    gold_report = {}
    start_t, end_t = times[0], times[-1]
    span = end_t - start_t
    fold_span = span / (n_folds := 8)
    is_ms = fold_span * 3 / 4
    oos_ms = fold_span / 4
    for name, fn in strat.items():
        trades = fn(closes, times)
        chain, picks = [], []
        for fold in range(n_folds):
            f_start = start_t + fold * fold_span
            f_end = f_start + fold_span
            pls = [pl for tt, pl in trades if f_start <= tt < f_end]
            chain.extend(pls)
            picks.append(f"{sum(pls):+.1f}%")
        wins = sum(1 for p in chain if p > 0)
        gold_report[name] = {"chained_oos_pct": round(sum(chain), 1), "oos_trades": len(chain),
                             "winrate": round(100 * wins / max(1, len(chain)), 1)}
        lines2.append(f"  {name:<22} chained OOS: {sum(chain):+.1f}% over {len(chain)} trades "
                      f"(WR {gold_report[name]['winrate']}%)  folds: {picks}")

    full = "\n".join(lines) + "\n" + "\n".join(lines2)
    (ROOT / "gold_research_report.txt").write_text(full, encoding="utf-8")
    (ROOT / "gold_research_report.json").write_text(json.dumps(gold_report, indent=1), encoding="utf-8")
    print("\n".join(lines2), flush=True)
    print("\nSaved: gold_research_report.txt / .json", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
