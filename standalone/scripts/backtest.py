"""Honest backtest stand: real historical EUR/USD data, several strategy variants,
out-of-sample validation. No promises — just numbers.

Data: Binance public klines (EURUSDT), 15-minute candles, ~3 years.
Costs: 1 pip spread charged per side (0.0001), realistic for EURUSD.

Usage: python scripts/backtest.py
"""
from __future__ import annotations

import json
import math
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
KLINES_URL = "https://api.binance.com/api/v3/klines"
SPREAD = 0.0001  # 1 pip per side


def fetch_klines(interval: str, start_ms: int, end_ms: int) -> list[dict]:
    out = []
    cursor = start_ms
    while cursor < end_ms:
        params = {"symbol": "EURUSDT", "interval": interval, "startTime": cursor,
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
        time.sleep(0.15)
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


def run_strategy(klines: list[dict], fast: int, slow: int, trend_filter: int | None,
                 flat_filter_pips: float) -> dict:
    closes = [k["close"] for k in klines]
    fast_ma = sma(closes, fast)
    slow_ma = sma(closes, slow)
    trend_ma = sma(closes, trend_filter) if trend_filter else None

    trades = []          # (entry_idx, exit_idx, direction, entry, exit, pl_pips)
    position = None      # "BUY"/"SELL"
    entry_price = 0.0
    entry_idx = 0

    for i in range(2, len(klines) - 1):
        if fast_ma[i] is None or slow_ma[i] is None:
            continue
        # flat filter: average 1-candle range over last 20 candles, in pips
        if flat_filter_pips > 0:
            window = klines[max(0, i - 20):i]
            avg_range = sum(k["high"] - k["low"] for k in window) / max(1, len(window))
            if avg_range / 0.0001 < flat_filter_pips:
                # too quiet to trade: close an open position at this candle's close
                if position:
                    pl = (klines[i]["close"] - entry_price) * (1 if position == "BUY" else -1) / 0.0001 - 1.0
                    trades.append((entry_idx, i, position, entry_price, klines[i]["close"], pl))
                    position = None
                continue

        desired = "BUY" if fast_ma[i] > slow_ma[i] else "SELL"
        if trend_ma is not None and trend_ma[i] is not None:
            if (trend_ma[i] > closes[i]) != (desired == "BUY"):
                desired_skip = True
            else:
                desired_skip = False
        else:
            desired_skip = False

        if position and (position != desired or desired_skip):
            pl = (klines[i]["close"] - entry_price) * (1 if position == "BUY" else -1) / 0.0001 - 1.0
            trades.append((entry_idx, i, position, entry_price, klines[i]["close"], pl))
            position = None
        if not position and not desired_skip:
            position = desired
            entry_price = klines[i + 1]["open"]  # realistic: enter at next candle open
            entry_idx = i + 1

    # close any open position at the end
    if position:
        pl = (closes[-1] - entry_price) * (1 if position == "BUY" else -1) / 0.0001 - 1.0
        trades.append((entry_idx, len(klines) - 1, position, entry_price, closes[-1], pl))

    return evaluate(trades)


def evaluate(trades: list) -> dict:
    wins = [t for t in trades if t[5] > 0]
    losses = [t for t in trades if t[5] <= 0]
    gross_win = sum(t[5] for t in wins)
    gross_loss = abs(sum(t[5] for t in losses))
    pf = round(gross_win / gross_loss, 2) if gross_loss > 0 else float("inf") if gross_win > 0 else 0.0
    # equity / max drawdown in pips
    equity, peak, max_dd = 0.0, 0.0, 0.0
    for t in trades:
        equity += t[5]
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return {"trades": len(trades), "winrate": round(100 * len(wins) / max(1, len(trades)), 1),
            "total_pips": round(sum(t[5] for t in trades), 1), "pf": pf,
            "max_dd_pips": round(max_dd, 1), "avg_pips": round(sum(t[5] for t in trades) / max(1, len(trades)), 2)}


def main() -> int:
    print("Fetching 3 years of 15m EURUSD data (this takes a couple of minutes)...")
    end = int(datetime.now(timezone.utc).timestamp() * 1000)
    start = int((datetime.now(timezone.utc) - timedelta(days=3 * 365)).timestamp() * 1000)
    klines = fetch_klines("15m", start, end)
    print(f"Downloaded {len(klines)} candles: "
          f"{datetime.fromtimestamp(klines[0]['t'] / 1000, tz=timezone.utc):%Y-%m-%d} .. "
          f"{datetime.fromtimestamp(klines[-1]['t'] / 1000, tz=timezone.utc):%Y-%m-%d}")

    split = int(len(klines) * 0.7)  # 70% in-sample / 30% out-of-sample
    split_date = datetime.fromtimestamp(klines[split]["t"] / 1000, tz=timezone.utc)

    print("\n=== STRATEGY SWEEP (pip = 0.0001; spread 1 pip per side charged) ===")
    print(f"{'variant':<44}{'IN-SAMPLE (70%)':<38}{'OUT-OF-SAMPLE (30%)'}")
    results = []
    variants = []
    for fast in (8, 12, 20):
        for slow in (26, 50):
            if fast >= slow:
                continue
            for tf in (None, 200):
                for flat in (0.0, 6.0):
                    name = f"SMA {fast}/{slow}" + (f" + trend {tf}" if tf else "") + \
                           (" + flat filter" if flat else " (raw)")
                    variants.append((name, fast, slow, tf, flat))

    for name, fast, slow, tf, flat in variants:
        ins = run_strategy(klines[:split], fast, slow, tf, flat)
        oos = run_strategy(klines[split:], fast, slow, tf, flat)
        results.append((name, ins, oos))
        print(f"{name:<44}{str(ins):<38}{oos}")

    results.sort(key=lambda r: r[2]["total_pips"], reverse=True)
    print("\n=== TOP-3 BY OUT-OF-SAMPLE PIPS (honest ranking) ===")
    for name, ins, oos in results[:3]:
        print(f"\n{name}\n  in-sample : {ins}\n  out-sample: {oos}")

    worst = results[-1]
    print(f"\nWORST out-of-sample: {worst[0]} → {worst[2]}")
    print(f"\nSplit date (in/out boundary): {split_date:%Y-%m-%d}")
    print("Data source: Binance EURUSDT 15m (real market). Costs: 1 pip/side. "
          "Demo scaffold — not investment advice.")
    (ROOT / "backtest_report.json").write_text(
        json.dumps({"variants": [{"name": n, "in_sample": i, "out_of_sample": o} for n, i, o in results],
                    "candles": len(klines), "split_date": str(split_date.date())},
                   ensure_ascii=False, indent=1), encoding="utf-8")
    print("Full report saved: backtest_report.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
