"""Multi-asset honest backtest: crypto majors, gold token, FX — 3 years of real data.

Same methodology as backtest.py, percentage-based so assets are comparable:
  - strategies: SMA cross (12/50), + trend filter (200), + trend & flat filter;
  - entry at next candle open, exit on signal flip or filter;
  - costs: percent per side (FX ~1 pip, crypto ~5 bps taker);
  - 70% in-sample / 30% out-of-sample, results reported for both honestly.

Data: Binance public klines, cached to data_cache/*.json after first download.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "data_cache"
CACHE.mkdir(exist_ok=True)
KLINES_URL = "https://api.binance.com/api/v3/klines"

ASSETS = {
    "EURUSD": {"symbol": "EURUSDT", "cost_pct": 0.01},   # ~1 pip on 1.13 = 0.009%
    "BTCUSD":  {"symbol": "BTCUSDT", "cost_pct": 0.05},
    "ETHUSD":  {"symbol": "ETHUSDT", "cost_pct": 0.05},
    "SOLUSD":  {"symbol": "SOLUSDT", "cost_pct": 0.05},
    "XRPUSD":  {"symbol": "XRPUSDT", "cost_pct": 0.05},
    "GOLD":    {"symbol": "PAXGUSDT", "cost_pct": 0.05},
}
VARIANTS = [("SMA 12/50 raw", 12, 50, None, 0.0),
            ("SMA 12/50 + trend 200", 12, 50, 200, 0.0),
            ("SMA 12/50 + trend + flat", 12, 50, 200, 0.08)]  # flat: avg 1m range < 0.08% of price


def fetch_klines(symbol: str, interval: str, start_ms: int, end_ms: int) -> list[dict]:
    cache_file = CACHE / f"{symbol}_{interval}_{start_ms // 86400000}.json"
    if cache_file.exists():
        return json.loads(cache_file.read_text())
    out, cursor = [], start_ms
    while cursor < end_ms:
        params = {"symbol": symbol, "interval": interval, "startTime": cursor,
                  "endTime": end_ms, "limit": 1000}
        for attempt in range(3):
            try:
                response = httpx.get(KLINES_URL, params=params, timeout=20)
                response.raise_for_status()
                batch = response.json()
                break
            except httpx.HTTPError:
                if attempt == 2:
                    raise
                time.sleep(2)
        if not batch:
            break
        out.extend({"t": int(k[0]), "open": float(k[1]), "high": float(k[2]),
                    "low": float(k[3]), "close": float(k[4])} for k in batch)
        cursor = batch[-1][0] + 1
        if len(batch) < 1000:
            break
        time.sleep(0.12)
    cache_file.write_text(json.dumps(out))
    return out


def sma(closes: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    s = 0.0
    for i, c in enumerate(closes):
        s += c
        if i >= period:
            s -= closes[i - period]
        if i >= period - 1:
            out[i] = s / period
    return out


def run_strategy(klines: list[dict], fast: int, slow: int, trend_period: int | None,
                 flat_pct: float, cost_pct: float) -> dict:
    closes = [k["close"] for k in klines]
    fast_ma = sma(closes, fast)
    slow_ma = sma(closes, slow)
    trend_ma = sma(closes, trend_period) if trend_period else None

    trades = []          # pl in % of entry
    position = None
    entry_price = 0.0

    for i in range(2, len(klines) - 1):
        if fast_ma[i] is None or slow_ma[i] is None:
            continue
        if flat_pct > 0:
            window = klines[max(0, i - 20):i]
            avg_range = sum(k["high"] - k["low"] for k in window) / max(1, len(window))
            if avg_range / closes[i] * 100 < flat_pct:
                if position:
                    pl = (klines[i]["close"] - entry_price) / entry_price * 100 * (1 if position == "BUY" else -1) - cost_pct
                    trades.append(pl)
                    position = None
                continue

        desired = "BUY" if fast_ma[i] > slow_ma[i] else "SELL"
        if trend_ma is not None and trend_ma[i] is not None:
            if (trend_ma[i] > closes[i]) != (desired == "BUY"):
                if position:
                    pl = (klines[i]["close"] - entry_price) / entry_price * 100 * (1 if position == "BUY" else -1) - cost_pct
                    trades.append(pl)
                    position = None
                continue

        if position and position != desired:
            pl = (klines[i]["close"] - entry_price) / entry_price * 100 * (1 if position == "BUY" else -1) - cost_pct
            trades.append(pl)
            position = None
        if not position:
            position = desired
            entry_price = klines[i + 1]["open"]

    if position:
        pl = (closes[-1] - entry_price) / entry_price * 100 * (1 if position == "BUY" else -1) - cost_pct
        trades.append(pl)

    wins = [t for t in trades if t > 0]
    losses = [t for t in trades if t <= 0]
    gross_win, gross_loss = sum(wins), abs(sum(losses))
    equity, peak, max_dd = 0.0, 0.0, 0.0
    for t in trades:
        equity += t
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    pf = round(gross_win / gross_loss, 2) if gross_loss > 0 else (round(gross_win, 1) if gross_win else 0.0)
    return {"trades": len(trades), "winrate": round(100 * len(wins) / max(1, len(trades)), 1),
            "total_pct": round(sum(trades), 1), "pf": pf, "max_dd_pct": round(max_dd, 1)}


def main() -> int:
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=int(3 * 365))
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)

    report = {}
    for asset, cfg in ASSETS.items():
        print(f"\n=== {asset} ({cfg['symbol']}) — downloading 3y of 15m data... ===", flush=True)
        klines = fetch_klines(cfg["symbol"], "15m", start_ms, end_ms)
        split = int(len(klines) * 0.7)
        split_date = datetime.fromtimestamp(klines[split]["t"] / 1000, tz=timezone.utc)
        print(f"    {len(klines)} candles {datetime.fromtimestamp(klines[0]['t']/1000, tz=timezone.utc):%Y-%m-%d} .. "
              f"{datetime.fromtimestamp(klines[-1]['t']/1000, tz=timezone.utc):%Y-%m-%d}")
        report[asset] = {"candles": len(klines), "variants": []}
        for name, fast, slow, tf, flat in VARIANTS:
            ins = run_strategy(klines[:split], fast, slow, tf, flat, cfg["cost_pct"])
            oos = run_strategy(klines[split:], fast, slow, tf, flat, cfg["cost_pct"])
            report[asset]["variants"].append({"name": name, "in_sample": ins, "out_of_sample": oos})
            print(f"    {name:<28} IN: {ins}   OOS: {oos}", flush=True)

    print("\n=== GRAND SUMMARY: out-of-sample total % by asset (best variant) ===", flush=True)
    for asset, data in report.items():
        best = max(data["variants"], key=lambda v: v["out_of_sample"]["total_pct"])
        b = best["out_of_sample"]
        print(f"  {asset:<8} best: {best['name']:<26} {b['total_pct']:+.1f}%  "
              f"({b['trades']} trades, WR {b['winrate']}%, PF {b['pf']}, maxDD {b['max_dd_pct']}%)")

    (ROOT / "backtest_multi_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\nSaved: backtest_multi_report.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
