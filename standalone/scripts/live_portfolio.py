"""Multi-strategy live portfolio: 4 classic strategy families from the trading
literature, each on its own market with its own $200 sub-balance ($1000 total).

Strategy families (real, from the literature):
  TURTLES  — Donchian channel breakout (Richard Dennis's Turtle rules, simplified)
  BOLLINGER— Bollinger band mean-reversion (John Bollinger)
  EMATREND — EMA 20/50 trend following
  MOMENTUM — Rate-of-change momentum

Markets: BTC, ETH, SOL, EUR, GOLD (PAXG) — real prices from a public feed.
Each slot: SL 1% / TP 2% from entry, position tracked against live price.
Balance changes at every close — that is the point.

No order is ever sent anywhere. Demo money, real data, real-time dynamics.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
LOG_PATH = ROOT / "portfolio_log.json"
KLINES_URL = "https://api.binance.com/api/v3/klines"


def log_write(entry: dict) -> None:
    entry["ts"] = datetime.now(timezone.utc).isoformat()
    history = []
    if LOG_PATH.exists():
        try:
            history = json.loads(LOG_PATH.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            history = []
    history.append(entry)
    LOG_PATH.write_text(json.dumps(history, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[{entry['ts'][11:19]}] {entry['event']}", flush=True)


LOG_PATH = ROOT / "portfolio_log.json"


def fetch_candles(symbol: str, limit: int = 30, interval: str = "1m") -> list[dict]:
    response = httpx.get(KLINES_URL, params={"symbol": symbol, "interval": interval, "limit": limit}, timeout=15)
    response.raise_for_status()
    return [{"t": int(k[0]), "open": float(k[1]), "high": float(k[2]),
             "low": float(k[3]), "close": float(k[4])} for k in response.json()]


def ema(values: list[float], period: int) -> float:
    k = 2 / (period + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


def sma_last(values: list[float], period: int) -> float:
    return sum(values[-period:]) / period


class Slot:
    """One strategy on one market with its own sub-balance."""

    def __init__(self, name: str, market: str, family: str, balance: float, tf: str = "1m"):
        self.name = name          # e.g. "TURTLES/BTC"
        self.market = market      # Binance symbol
        self.family = family
        self.balance = balance
        self.start_balance = balance
        self.tf = tf              # working timeframe of the strategy
        self.position = None      # {"direction","entry","sl","tp","notional"}
        self.closed = 0
        self.wins = 0
        self.frozen = False       # drawdown stop: slot stopped trading


    def equity(self, price: float | None) -> float:
        if not self.position or price is None:
            return self.balance
        sign = 1.0 if self.position["direction"] == "BUY" else -1.0
        floating = (price - self.position["entry"]) * sign * self.position["notional"] / self.position["entry"]
        return self.balance + floating


def atr(candles: list[dict], period: int = 14) -> float:
    """Average True Range over the last `period` candles (Wilder approximation)."""
    trs = []
    for i in range(max(1, len(candles) - period), len(candles)):
        h, l = candles[i]["high"], candles[i]["low"]
        prev_close = candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - prev_close), abs(l - prev_close)))
    return sum(trs) / max(1, len(trs))


def signal_turtles(candles: list[dict]) -> str | None:
    """Donchian breakout: long if close > max(high[-21:-1]), short if close < min(low[-21:-1])."""
    closes = [c["close"] for c in candles]
    if len(closes) < 22:
        return None
    high20 = max(c["high"] for c in candles[-21:-1])
    low20 = min(c["low"] for c in candles[-21:-1])
    price = closes[-1]
    if price > high20:
        return "BUY"
    if price < low20:
        return "SELL"
    return None


def signal_bollinger(candles: list[dict]) -> str | None:
    """Mean reversion: buy below lower band, sell above upper band, exit at mid."""
    closes = [c["close"] for c in candles]
    if len(closes) < 21:
        return None
    mid = sum(closes[-20:]) / 20
    var = sum((c - mid) ** 2 for c in closes[-20:]) / 20
    sd = math.sqrt(var)
    price = closes[-1]
    if price < mid - 2 * sd:
        return "BUY"
    if price > mid + 2 * sd:
        return "SELL"
    return "HOLD"  # inside bands: keep/neutral


def signal_ematrend(candles: list[dict], fast: int = 12, slow: int = 26) -> str | None:
    e_fast = ema([c["close"] for c in candles], fast)
    e_slow = ema([c["close"] for c in candles], slow)
    if e_fast > e_slow:
        return "BUY"
    if e_fast < e_slow:
        return "SELL"
    return None


def signal_momentum(candles: list[dict]) -> str | None:
    closes = [c["close"] for c in candles]
    if len(closes) < 11:
        return None
    roc = (closes[-1] - closes[-11]) / closes[-11] * 100
    if roc > 0.15:
        return "BUY"
    if roc < -0.15:
        return "SELL"
    return None


STRATEGY_FN = {"TURTLES": signal_turtles, "BOLLINGER": signal_bollinger,
               "EMATREND": signal_ematrend, "MOMENTUM": signal_momentum}


def entry_price(price: float, sl_distance: float, tp_distance: float, direction: str) -> tuple[float, float]:
    """Stop/take geometry from distances (ATR-based)."""
    if direction == "BUY":
        return price - sl_distance, price + tp_distance
    return price + sl_distance, price - tp_distance


def close_position_at(slot, price: float, reason: str) -> None:
    """Close the slot's open position at the given price (used by drawdown stop)."""
    if not slot.position:
        return
    pos = slot.position
    sign = 1.0 if pos["direction"] == "BUY" else -1.0
    pl = (price - pos["entry"]) * sign * pos["notional"] / pos["entry"]
    slot.balance += pl
    slot.closed += 1
    slot.wins += 1 if pl > 0 else 0
    slot.position = None
    log_write({"event": f"{slot.name} CLOSE ({reason}): @ {price:.5g} — P/L ${pl:+.2f} "
                        f"| slot ${slot.balance:.2f}", "slot": slot.name,
               "pl": round(pl, 2)})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minutes", type=int, default=0, help="0 = run until stopped")
    parser.add_argument("--interval-sec", type=int, default=30)
    parser.add_argument("--balance", type=float, default=1000.0)
    parser.add_argument("--log-name", default="portfolio_log.json",
                        help="log file name (lets several processes run in parallel)")
    parser.add_argument("--mode", choices=("auto", "evidence"), default="evidence",
                        help="evidence = walk-forward weighted allocation (default); "
                             "auto = auto-configure from capital_tiers")
    args = parser.parse_args()

    global LOG_PATH
    LOG_PATH = ROOT / args.log_name

    # v6: slot allocation. "evidence" (default) = walk-forward weighted; "auto" =
    # capital_tiers.configure(balance) builds the allocation for any balance.
    tier_cfg = None
    if args.mode == "auto":
        sys.path.insert(0, str(ROOT / "scripts"))
        from capital_tiers import configure as configure_tier
        tier_cfg = configure_tier(args.balance)
        tier_slots = tier_cfg.get("slots", [])
        slots = []
        for ts in tier_slots:
            family = "TURTLES" if ts["family"] == "DONCHIAN" else ts["family"]
            tf = ts["tf"]
            slot = Slot(f"{family}/{ts['market'].replace('USDT', '')}", ts["market"], family,
                        args.balance * ts["weight"], tf)
            if family == "EMATREND":
                slot.ema_fast, slot.ema_slow = \
                    (50, 200) if ts["market"] == "PAXGUSDT" else (12, 26)
            slots.append(slot)
    else:
        slots = [
            # v5 evidence-weighted allocation (walkforward_report.json, gold_research_report.txt):
            #  - Bollinger mean-reversion on EUR 1h: +7.1% chained OOS
            #  - Donchian breakout on EUR 1h: +2.4% chained OOS
            #  - EMA 50/200 trend on GOLD 1h: +84.5% chained OOS, 7/8 folds positive
            Slot("BOLLINGER/EUR", "EURUSDT", "BOLLINGER", args.balance * 0.3, "1h"),
            Slot("DONCHIAN/EUR", "EURUSDT", "TURTLES", args.balance * 0.3, "1h"),
            Slot("EMATREND/GOLD", "PAXGUSDT", "EMATREND", args.balance * 0.25, "1h"),
            Slot("BOLLINGER/ETH", "ETHUSDT", "BOLLINGER", args.balance * 0.1, "1h"),
            Slot("TURTLES/BTC", "BTCUSDT", "TURTLES", args.balance * 0.05, "1h"),
        ]
        for s in slots:
            if s.family == "EMATREND":
                s.ema_fast, s.ema_slow = (50, 200) if "GOLD" in s.name else (12, 26)
    # v6 additions: ATR stops, risk sizing, per-slot drawdown stop (v3) are below;
    # new: gold hour filter + per-slot loss circuit breaker.

    deadline = time.monotonic() + args.minutes * 60 if args.minutes > 0 else None

    def total_equity() -> float:
        # uses last known prices cache
        t = 0.0
        for s in slots:
            price = price_cache.get(s.market)
            t += s.equity(price)
        return t

    price_cache: dict[str, float] = {}
    start_equity = args.balance

    log_write({"event": f"PORTFOLIO v6 START: {len(slots)} slots, mode {args.mode}, "
                        f"tier {tier_cfg['tier'] if tier_cfg else 'evidence'}, "
                        f"circuit breaker 3 losses -> 30 min pause, "
                        f"gold NY-open hour filter.", "balance": round(start_equity, 2)})

    try:
        while deadline is None or time.monotonic() < deadline:
            for slot in slots:
                try:
                    # The slot's timeframe is the one the rule was researched on: fetching
                    # 1-minute candles for a 1-hour rule changes the strategy silently.
                    candles = fetch_candles(slot.market, limit=400, interval=slot.tf)
                    price = candles[-1]["close"]
                    price_cache[slot.market] = price
                except Exception as exc:
                    log_write({"event": f"{slot.name}: feed error ({type(exc).__name__})"})
                    continue

                # manage open position
                if slot.position:
                    pos = slot.position
                    sign = 1.0 if pos["direction"] == "BUY" else -1.0
                    pl = (price - pos["entry"]) * sign * pos["notional"] / pos["entry"]
                    hit = None
                    if pos["direction"] == "BUY":
                        if price <= pos["sl"]:
                            hit = "SL"
                        elif price >= pos["tp"]:
                            hit = "TP"
                    else:
                        if price >= pos["sl"]:
                            hit = "SL"
                        elif price <= pos["tp"]:
                            hit = "TP"
                    if hit:
                        slot.balance += pl
                        slot.closed += 1
                        slot.wins += 1 if pl > 0 else 0
                        log_write({"event": f"{slot.name} CLOSE {hit}: {pos['direction']} @ {price:.5g} — "
                                            f"P/L ${pl:+.2f} | slot ${slot.balance:.2f}",
                                   "slot": slot.name, "pl": round(pl, 2),
                                   "balance": round(total_equity(), 2)})
                        slot.position = None
                        # v6 circuit breaker: 3 consecutive losses -> 30 min pause
                        if pl <= 0:
                            slot.consec_losses = getattr(slot, "consec_losses", 0) + 1
                            if slot.consec_losses >= 3:
                                slot.consec_losses = 0
                                slot.pause_until = time.monotonic() + 1800
                                log_write({"event": f"{slot.name} CIRCUIT BREAKER: 3 losses in a row — "
                                                    f"new entries paused for 30 min",
                                           "slot": slot.name, "balance": round(total_equity(), 2)})
                        else:
                            slot.consec_losses = 0

                # v6 circuit breaker: 3 consecutive losses -> slot pauses new entries 30 min
                if getattr(slot, "pause_until", 0) > time.monotonic():
                    continue
                # v6 gold hour filter: statistically the worst hour for gold is 13 UTC
                # (NY open sell-off, research: avg -0.0083%/h) — no NEW gold entries there
                hour_utc = datetime.now(timezone.utc).hour
                if "GOLD" in slot.name and hour_utc == 13 and slot.position is None:
                    continue

                # new signal? The EMA slot carries its own fast/slow pair, researched as 50/200
                # on gold; calling the generic 12/26 rule would trade a different strategy.
                if slot.family == "EMATREND":
                    sig = signal_ematrend(candles, getattr(slot, "ema_fast", 12),
                                          getattr(slot, "ema_slow", 26))
                else:
                    sig = STRATEGY_FN[slot.family](candles)
                if slot.position is None and sig in ("BUY", "SELL") and not slot.frozen:
                    # v3 (applied from the MT5 agent repo): ATR-based stop geometry and
                    # risk-based sizing — a stop hit costs exactly RISK_FRACTION of the slot.
                    atr_value = max(atr(candles, 14), price * 0.0005)   # floor: 0.05% of price
                    sl_distance = 2.0 * atr_value                       # stop at 2×ATR
                    tp_distance = 2.0 * sl_distance                     # RR 2.0
                    risk_dollars = slot.balance * 0.005                 # 0.5% of slot per stop
                    notional = risk_dollars / (sl_distance / price)
                    if sig == "BUY":
                        sl, tp = entry_price(price, sl_distance, tp_distance, "BUY")
                    else:
                        sl, tp = entry_price(price, sl_distance, tp_distance, "SELL")
                    slot.position = {"direction": sig, "entry": price, "sl": sl, "tp": tp,
                                     "notional": notional}
                    log_write({"event": f"{slot.name} OPEN {sig} @ {price:.5g} "
                                        f"(SL {sl:.5g}, TP {tp:.5g}, ATR {atr_value:.5g}, "
                                        f"notional ${notional:.0f}, risk ${risk_dollars:.2f})",
                               "slot": slot.name, "balance": round(total_equity(), 2)})
                elif slot.position and sig in ("BUY", "SELL") and sig != slot.position["direction"]:
                    # strategy flipped: close and reverse
                    pos = slot.position
                    sign = 1.0 if pos["direction"] == "BUY" else -1.0
                    pl = (price - pos["entry"]) * sign * pos["notional"] / pos["entry"]
                    slot.balance += pl
                    slot.closed += 1
                    slot.wins += 1 if pl > 0 else 0
                    slot.position = None
                    log_write({"event": f"{slot.name} FLIP: closed {pos['direction']} (${pl:+.2f}) → {sig}",
                               "slot": slot.name, "pl": round(pl, 2),
                               "balance": round(total_equity(), 2)})
                    atr_value = max(atr(candles, 14), price * 0.0005)
                    sl_distance = 2.0 * atr_value
                    tp_distance = 2.0 * sl_distance
                    risk_dollars = slot.balance * 0.005
                    notional = risk_dollars / (sl_distance / price)
                    if sig == "BUY":
                        sl, tp = entry_price(price, sl_distance, tp_distance, "BUY")
                    else:
                        sl, tp = entry_price(price, sl_distance, tp_distance, "SELL")
                    slot.position = {"direction": sig, "entry": price, "sl": sl, "tp": tp,
                                     "notional": notional}

                # drawdown stop (from the MT5 agent repo): slot frozen at −6% of its start
                if not slot.frozen and slot.balance < slot.start_balance * 0.94:
                    slot.frozen = True
                    if slot.position:
                        price_now = price_cache.get(slot.market, price)
                        close_position_at(slot, price_now, "DRAWDOWN STOP (−6%)")
                    log_write({"event": f"{slot.name} FROZEN: drawdown stop at −6% of start balance. "
                                        f"Slot stops trading. Balance ${slot.balance:.2f}",
                               "slot": slot.name, "balance": round(total_equity(), 2)})

            equity = total_equity()
            pos_str = ", ".join(f"{s.name}:{s.position['direction']}" for s in slots if s.position) or "none"
            log_write({"event": f"EQUITY ${equity:.2f} | P/L ${equity - args.balance:+.2f} | "
                                f"positions: {pos_str}",
                       "balance": round(equity, 2)})
            time.sleep(args.interval_sec)
    except KeyboardInterrupt:
        pass

    # force close all
    for slot in slots:
        if slot.position:
            price = price_cache.get(slot.market)
            if price:
                pos = slot.position
                sign = 1.0 if pos["direction"] == "BUY" else -1.0
                pl = (price - pos["entry"]) * sign * pos["notional"] / pos["entry"]
                slot.balance += pl
                slot.closed += 1
                slot.wins += 1 if pl > 0 else 0
                slot.position = None
                log_write({"event": f"{slot.name} FORCE CLOSE at session end: ${pl:+.2f}",
                           "slot": slot.name, "pl": round(pl, 2), "balance": round(total_equity(), 2)})
    equity = total_equity()
    stats = ", ".join(f"{s.name.split('/')[0]}: {s.closed} сделок ({s.wins}W)" for s in slots)
    log_write({"event": f"PORTFOLIO STOP: equity ${equity:.2f} "
                        f"({'+' if equity >= args.balance else ''}{equity - args.balance:.2f} vs start). {stats}",
               "balance": round(equity, 2)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
