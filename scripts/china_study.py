#!/usr/bin/env python3
"""Test the strategies the Chinese retail and quant literature talks about most.

The four that are testable with free daily index data:

1. 双均线 dual moving average — the workhorse of Chinese retail systems.
2. 隔夜动量 overnight momentum — hold from the close to the next open.
3. 短期反转 short-term reversal — the documented A-share reversal effect.
4. 网格 grid trading — the most popular crypto/stock bot pattern in China.

Data: Shanghai Composite, CSI 300 and Shenzhen Component daily bars from Yahoo.
Costs are charged on every position change. Nothing here is a promise.
"""
from __future__ import annotations

import json
import math
import statistics
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "agent_data" / "market_data"
SYMBOLS = {"510300.SS": "csi300_etf", "000001.SS": "shanghai", "399001.SZ": "shenzhen"}
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def fetch(symbol: str, years: int = 12) -> dict:
    query = urllib.parse.urlencode({"range": f"{years}y", "interval": "1d"})
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?{query}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    result = payload["chart"]["result"][0]
    stamps = result.get("timestamp") or []
    quote = result["indicators"]["quote"][0]
    bars = []
    for index, stamp in enumerate(stamps):
        close = quote["close"][index]
        if close is None:
            continue
        bars.append({"t": int(stamp), "o": quote["open"][index] or close,
                     "h": quote["high"][index] or close, "l": quote["low"][index] or close,
                     "c": float(close)})
    return {"symbol": symbol, "bars": bars}


def load(name: str) -> list[dict]:
    payload = json.loads((OUT / f"{name}.json").read_text(encoding="utf-8"))
    return payload["bars"]


def ema(values: list[float], period: int) -> float:
    weight = 2 / (period + 1)
    result = values[0]
    for value in values[1:]:
        result = value * weight + result * (1 - weight)
    return result


def dual_ma(bars: list[dict], fast: int, slow: int, cost: float = 0.001) -> dict:
    closes = [bar["c"] for bar in bars]
    equity, peak, drawdown, trades, wins = 1.0, 1.0, 0.0, 0, 0
    position = 0
    entry = 0.0
    for index in range(slow, len(closes)):
        f = statistics.fmean(closes[index - fast + 1:index + 1])
        s = statistics.fmean(closes[index - slow + 1:index + 1])
        want = 1 if f > s else 0
        if want != position:
            if position == 1:
                equity *= closes[index] / entry * (1 - cost)
                wins += 1 if closes[index] > entry else 0
                trades += 1
            position = want
            if position == 1:
                entry = closes[index] * (1 + cost)
        if position == 1:
            peak = max(peak, equity * closes[index] / entry)
            drawdown = max(drawdown, 1 - (equity * closes[index] / entry) / peak)
    if position == 1:
        equity *= closes[-1] / entry * (1 - cost)
        wins += 1 if closes[-1] > entry else 0
        trades += 1
    years = (bars[-1]["t"] - bars[slow]["t"]) / (365 * 86400)
    return {"total_return_pct": round((equity - 1) * 100, 1),
            "annualized_pct": round(((equity) ** (1 / max(years, 0.1)) - 1) * 100, 1),
            "trades": trades, "win_rate": round(wins / trades, 3) if trades else None,
            "max_drawdown_pct": round(drawdown * 100, 1)}


def overnight(bars: list[dict], cost: float = 0.0005) -> dict:
    values = [bars[i]["o"] / bars[i - 1]["c"] - 1 - cost for i in range(1, len(bars))]
    mean = statistics.fmean(values)
    sd = statistics.pstdev(values) / math.sqrt(len(values))
    return {"mean_overnight_pct": round(mean * 100, 4),
            "t": round(mean / sd, 2) if sd else None,
            "n": len(values),
            "annualized_pct": round(mean * 252 * 100, 1),
            "wins": round(sum(1 for v in values if v > 0) / len(values), 3)}


def intraday(bars: list[dict], cost: float = 0.001) -> dict:
    """Buy at the open, sell at the close — the mirror image of overnight."""
    values = [bar["c"] / bar["o"] - 1 - cost for bar in bars if bar["o"]]
    mean = statistics.fmean(values)
    sd = statistics.pstdev(values) / math.sqrt(len(values))
    return {"mean_intraday_pct": round(mean * 100, 4),
            "t": round(mean / sd, 2) if sd else None,
            "n": len(values),
            "annualized_pct": round(mean * 252 * 100, 1),
            "wins": round(sum(1 for v in values if v > 0) / len(values), 3)}


def reversal(bars: list[dict], lookback: int = 5, hold: int = 5, cost: float = 0.001) -> dict:
    closes = [bar["c"] for bar in bars]
    returns = []
    for index in range(lookback, len(closes) - hold):
        past = closes[index] / closes[index - lookback] - 1
        if past < 0:  # buy after a fall, the A-share reversal claim
            returns.append(closes[index + hold] / closes[index] - 1 - cost)
    if len(returns) < 30:
        return {"trades": len(returns)}
    mean = statistics.fmean(returns)
    sd = statistics.pstdev(returns) / math.sqrt(len(returns))
    return {"trades": len(returns), "mean_pct": round(mean * 100, 3),
            "t": round(mean / sd, 2) if sd else None,
            "wins": round(sum(1 for v in returns if v > 0) / len(returns), 3)}


def grid(bars: list[dict], step_pct: float = 0.01, cost: float = 0.001) -> dict:
    """One-unit grid around the first price; each step crossed trades one unit."""
    capital = 100.0
    cash = capital
    units = 0.0
    base = bars[0]["c"]
    fills = 0
    for bar in bars:
        price = bar["c"]
        level = math.floor((price / base - 1) / step_pct)
        previous = math.floor((base * (1 + (level - 1) * step_pct)) / base - 1) if False else None
        target_units = max(0.0, -level)  # buy one unit per step below the base
        if target_units > units:
            buy = min(target_units - units, cash / (price * (1 + cost)))
            units += buy
            cash -= buy * price * (1 + cost)
            fills += 1
        elif target_units < units:
            sell = units - target_units
            cash += sell * price * (1 - cost)
            units -= sell
            fills += 1
    value = cash + units * bars[-1]["c"]
    return {"return_pct": round((value / capital - 1) * 100, 1),
            "fills": fills, "units_left": round(units, 3)}


def main() -> int:
    lines = ["Chinese strategies, tested on index data (costs included)", ""]
    for symbol, name in SYMBOLS.items():
        payload = fetch(symbol)
        if len(payload["bars"]) < 300:
            lines.append(f"{name}: not enough data ({len(payload['bars'])})")
            continue
        (OUT / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")
        bars = payload["bars"]
        first = bars[0]["t"] // 86400
        lines.append(f"{name}: {len(bars)} bars")
        for fast, slow in ((5, 20), (20, 60)):
            result = dual_ma(bars, fast, slow)
            lines.append(f"  dual MA {fast}/{slow}: {result}")
        lines.append(f"  overnight: {overnight(bars)}")
        lines.append(f"  intraday: {intraday(bars)}")
        lines.append(f"  reversal 5/5: {reversal(bars)}")
        lines.append(f"  grid 1%: {grid(bars)}")
    report = "\n".join(lines)
    print(report)
    (ROOT / "docs" / "evidence" / "china_strategies.txt").write_text(report, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
