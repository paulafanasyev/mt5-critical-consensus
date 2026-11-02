"""Continuous autonomous trading demo on REAL market data with DEMO money. v2

v2 improvements over v1:
  - flat-market filter: no entries when the recent price range is too tight
    (the classic SMA-strategy killer — chop stops);
  - breakeven move: once a position is +5 pips, SL moves to entry;
  - profit lock: at +10 pips floating the position closes at market,
    so realized profit does not evaporate before the 20-pip TP;
  - force-close on stop: an open position is closed at the current price
    when the session ends, no dangling positions.

The loop still runs the FULL fail-closed pipeline (5 agents -> certainty gate
-> autonomous mode) on real EUR/USD prices, and still sends no orders anywhere.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from critical_consensus.calibration import weighted_consensus  # noqa: E402
from critical_consensus.audit import canonical_hash, content_hash_excluding  # noqa: E402
from critical_consensus.pipeline import evaluate_signal_bundle  # noqa: E402

from demo_real_data import build_bundle  # noqa: E402

KLINES_URL = "https://api.binance.com/api/v3/klines"
LOG_PATH = ROOT / "live_log.json"
LOT_UNITS = 100_000
PIP = 0.0001
BREAKEVEN_PIPS = 5    # move SL to entry at +5 pips
LOCK_PIPS = 10        # close at market at +10 pips (lock the profit)


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


def fetch_market() -> dict:
    response = httpx.get(KLINES_URL, params={"symbol": "EURUSDT", "interval": "1m", "limit": 30}, timeout=15)
    response.raise_for_status()
    klines = response.json()
    closes = [float(k[4]) for k in klines]
    highs = [float(k[2]) for k in klines]
    lows = [float(k[3]) for k in klines]
    # average 1-minute range of the last 10 candles, in pips — the flat-market meter
    ranges = [(h - l) / PIP for h, l in zip(highs[-10:], lows[-10:])]
    return {"price": closes[-1], "sma10": sum(closes[-10:]) / 10.0,
            "avg_range_pips": sum(ranges) / len(ranges)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minutes", type=int, default=0, help="0 = run until stopped (Ctrl+C)")
    parser.add_argument("--interval-sec", type=int, default=45, help="seconds between market checks")
    parser.add_argument("--sl-pips", type=float, default=10, help="stop loss, pips (demo-scale)")
    parser.add_argument("--tp-pips", type=float, default=20, help="take profit, pips (demo-scale)")
    parser.add_argument("--min-range-pips", type=float, default=5.0,
                        help="flat-market filter: minimum average 1m range to allow entries")
    parser.add_argument("--balance", type=float, default=1000.0, help="starting demo balance, USD")
    args = parser.parse_args()

    certainty_policy = yaml.safe_load((ROOT / "config" / "certainty_thresholds.yaml").read_text(encoding="utf-8"))
    permission_matrix = yaml.safe_load((ROOT / "config" / "permission_matrix.yaml").read_text(encoding="utf-8"))
    permission_matrix["permission_matrix"]["allowed_time_utc"] = {"start": "00:00", "end": "23:59"}
    permission_matrix["permission_matrix"]["rollover_block_utc"] = {"start": "23:59", "end": "23:59"}
    work_mode_policy = {"work_mode": "autonomous",
                        "autonomous": {"enabled": True, "demo_only": True, "max_size_per_trade": 0.05}}

    balance = args.balance
    position = None          # {"direction","entry","sl","tp","size","signal_id","be_moved"}
    closed_trades = 0
    wins = 0
    losses = 0
    deadline = time.monotonic() + args.minutes * 60 if args.minutes > 0 else None

    log_write({"event": f"START v2: real EURUSD, demo ${balance:.2f}, "
                        f"SL {args.sl_pips:.0f} / TP {args.tp_pips:.0f} pips, micro lot 0.01, "
                        f"flat-filter ≥ {args.min_range_pips:.0f} pips, breakeven +{BREAKEVEN_PIPS}, "
                        f"lock +{LOCK_PIPS}",
               "balance": round(balance, 2)})

    def close_position(price: float, reason: str) -> None:
        nonlocal balance, closed_trades, wins, losses, position
        sign = 1.0 if position["direction"] == "BUY" else -1.0
        pl = (price - position["entry"]) * sign * position["size"] * LOT_UNITS
        balance += pl
        closed_trades += 1
        if pl > 0:
            wins += 1
        else:
            losses += 1
        log_write({"event": f"CLOSE ({reason}): {position['direction']} {position['size']} lot "
                            f"@ {price:.5f} — P/L ${pl:+.2f} | balance ${balance:.2f} "
                            f"| trades {closed_trades}: {wins}W/{losses}L",
                   "balance": round(balance, 2), "pl": round(pl, 2), "reason": reason})
        position = None

    try:
        while deadline is None or time.monotonic() < deadline:
            try:
                market = fetch_market()
                price = market["price"]
            except Exception as exc:
                log_write({"event": f"feed unavailable ({type(exc).__name__}) — retrying"})
                time.sleep(args.interval_sec)
                continue

            # --- 1. manage the open position: trailing/lock/SL/TP on the live price ---
            if position:
                sign = 1.0 if position["direction"] == "BUY" else -1.0
                floating_pips = (price - position["entry"]) * sign / PIP

                if not position["be_moved"] and floating_pips >= BREAKEVEN_PIPS:
                    position["sl"] = position["entry"]
                    position["be_moved"] = True
                    log_write({"event": f"BREAKEVEN: SL moved to entry {position['entry']:.5f} "
                                        f"(+{floating_pips:.1f} pips floating)"})

                hit = None
                if position["direction"] == "BUY":
                    if price <= position["sl"]:
                        hit = "BE/SL" if position["be_moved"] else "SL"
                    elif price >= position["tp"]:
                        hit = "TP"
                else:
                    if price >= position["sl"]:
                        hit = "BE/SL" if position["be_moved"] else "SL"
                    elif price <= position["tp"]:
                        hit = "TP"

                if floating_pips >= LOCK_PIPS:
                    close_position(price, f"LOCK +{floating_pips:.1f} pips")
                elif hit:
                    close_position(price, hit)

            # --- 2. no position -> flat filter -> pipeline decides autonomously ---
            if position is None:
                if market["avg_range_pips"] < args.min_range_pips:
                    log_write({"event": f"FLAT MARKET: avg 1m range {market['avg_range_pips']:.1f} pips "
                                        f"< {args.min_range_pips:.0f} — no entry (chop filter), "
                                        f"price {price:.5f}, balance ${balance:.2f}"})
                else:
                    price_info = {"price": price, "sma10": market["sma10"],
                                  "direction": "BUY" if price > market["sma10"] else "SELL"}
                    bundle = build_bundle(price_info, certainty_policy)
                    permission = bundle["permission_request"]
                    entry = permission["expected_price"]
                    sl_dist = args.sl_pips * PIP
                    tp_dist = args.tp_pips * PIP
                    if permission["direction"] == "BUY":
                        permission["sl_tp"] = {"sl": round(entry - sl_dist, 5), "tp": round(entry + tp_dist, 5)}
                    else:
                        permission["sl_tp"] = {"sl": round(entry + sl_dist, 5), "tp": round(entry - tp_dist, 5)}
                    permission["expected_rr"] = round(tp_dist / sl_dist, 2)
                    permission["consensus_score"] = weighted_consensus(bundle["verdicts"])
                    bundle["chief_reasoning"]["reasoning_hash"] = content_hash_excluding(
                        bundle["chief_reasoning"], "reasoning_hash")
                    permission["chief_reasoning_hash"] = bundle["chief_reasoning"]["reasoning_hash"]

                    result = evaluate_signal_bundle(bundle, certainty_policy, permission_matrix,
                                                    now=datetime.now(timezone.utc),
                                                    work_mode_policy=work_mode_policy)
                    if result["status"] == "AUTHORIZED_AUTONOMOUS":
                        position = {"direction": permission["direction"], "entry": entry,
                                    "sl": permission["sl_tp"]["sl"], "tp": permission["sl_tp"]["tp"],
                                    "size": permission["size"], "signal_id": result["signal_id"],
                                    "be_moved": False}
                        log_write({"event": f"OPEN {position['direction']} {position['size']} lot @ {entry:.5f} "
                                            f"(SL {position['sl']:.5f}, TP {position['tp']:.5f}) — "
                                            f"price {price:.5f}, range {market['avg_range_pips']:.1f} pips, "
                                            f"balance ${balance:.2f}",
                                   "balance": round(balance, 2), "signal_id": position["signal_id"]})
                    else:
                        log_write({"event": f"pipeline: {result['status']} "
                                            f"({', '.join(result['blockers'][:2]) or 'no blockers'}) "
                                            f"— price {price:.5f}"})

            # --- 3. status line ---
            if position:
                sign = 1.0 if position["direction"] == "BUY" else -1.0
                floating = (price - position["entry"]) * sign * position["size"] * LOT_UNITS
                log_write({"event": f"price {price:.5f} | {position['direction']} @ {position['entry']:.5f} "
                                    f"SL {position['sl']:.5f} floating ${floating:+.2f} "
                                    f"| balance ${balance:.2f}",
                           "balance": round(balance, 2), "floating": round(floating, 2)})
            time.sleep(min(args.interval_sec, 15))
    except KeyboardInterrupt:
        pass

    # --- force-close on stop: no dangling positions ---
    if position:
        try:
            price = fetch_market()["price"]
        except Exception:
            price = position["entry"]
        close_position(price, "FORCE CLOSE at session stop")

    log_write({"event": f"STOP: balance ${balance:.2f}, closed trades {closed_trades} "
                        f"({wins}W/{losses}L), winrate {round(100 * wins / max(1, closed_trades))}%"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
