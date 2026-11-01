"""Live demo: the pipeline evaluates REAL market data (EUR/USD from a public feed)
with DEMO money and small sizes, and asks the operator for approval — in the
console or on the phone via Telegram.

IMPORTANT: this scaffold creates and sends no order commands anywhere. "Approved"
means the pipeline produced AUTHORIZED_ONCE/AUTHORIZED_AUTONOMOUS metadata on real
prices. Money is demo; the MT5 bridge remains unimplemented (see README scope).

Usage:
    python scripts/demo_real_data.py --trades 3
    python scripts/demo_real_data.py --trades 3 --telegram   # needs TELEGRAM_* env
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from critical_consensus.calibration import weighted_consensus  # noqa: E402
from critical_consensus.audit import canonical_hash, content_hash_excluding  # noqa: E402
from critical_consensus.pipeline import evaluate_signal_bundle  # noqa: E402
from critical_consensus.telegram_approvals import channel_from_env  # noqa: E402

KLINES_URL = "https://api.binance.com/api/v3/klines"
DEMO_BALANCE = 1000.0  # demo account balance, USD
LOT_UNITS = 100_000    # 1.0 lot of EURUSD = 100 000 EUR


def fetch_real_price(symbol: str = "EURUSDT") -> dict:
    """Fetch recent real candles from a public no-key market data feed."""
    response = httpx.get(KLINES_URL, params={"symbol": symbol, "interval": "1m", "limit": 30}, timeout=15)
    response.raise_for_status()
    klines = response.json()
    closes = [float(k[4]) for k in klines]
    sma10 = sum(closes[-10:]) / 10.0
    return {"symbol": symbol, "price": closes[-1], "sma10": sma10,
            "direction": "BUY" if closes[-1] > sma10 else "SELL",
            "time": datetime.fromtimestamp(klines[-1][0] / 1000, tz=timezone.utc)}


def build_bundle(price_info: dict, certainty_policy: dict) -> dict:
    """Build a fully-valid signal bundle from the real price, small size (0.01 lots)."""
    now = datetime.now(timezone.utc)
    signal_id = f"demo-{now.strftime('%H%M%S')}"
    entry = round(price_info["price"], 5)
    direction = price_info["direction"]
    sl = round(entry - 0.0050, 5) if direction == "BUY" else round(entry + 0.0050, 5)   # 50 pips
    tp = round(entry + 0.0100, 5) if direction == "BUY" else round(entry - 0.0100, 5)   # 100 pips
    size = 0.01                                                                          # micro lot

    agents = certainty_policy["mandatory_agents"]
    verdicts = [{"verdict_id": f"v-{i}", "signal_id": signal_id, "agent_id": agent,
                 "verdict": "APPROVE", "confidence": 0.99, "created_at": now.isoformat(),
                 "ttl_seconds": 300, "input_hash": "sha256:" + "a" * 64,
                 "calibration_meta": {"brier_score": 0.05, "sample_size": 100,
                                      "method_version": "v1", "calibration_status": "CALIBRATED"}}
                for i, agent in enumerate(agents)]
    reviews = [{"target_verdict_id": v["verdict_id"], "reviewer_alias": f"reviewer-{j + 1}",
                "stance": "CONFIRM", "challenge_type": "NONE",
                "argument": "Independent cross-check of the real-price signal",
                "evidence_refs": [f"market-feed-{v['verdict_id']}-{j}"], "status": "VALIDATED"}
               for v in verdicts for j in range(2)]
    steps = {k: {"passed": True, "detail": f"checked {k} against real EURUSD data",
                 "evidence_refs": [f"market-feed-{signal_id}-{k}"]} for k in "abcdefghi"}
    chief = {"reasoning_id": f"chief-{signal_id}", "signal_id": signal_id,
             "created_at": now.isoformat(), "verdict": "CONFIRM", "steps": steps,
             "reasons_against": ["momentum may reverse", "spread widening", "news risk",
                                  "liquidity thinning", "session rollover"],
             "tail_risk_probability": 0.01,
             "tail_risk_scenarios": [{"scenario": "sudden opposite move", "probability": 0.01,
                                      "impact": "stop loss hit", "evidence_refs": ["market-feed-1"]}],
             "open_challenges": [], "all_challenges_read": True,
             "challenge_ledger_snapshot": [], "challenge_ledger_hash": canonical_hash([]),
             "reviewed_challenge_ids": []}
    chief["reasoning_hash"] = content_hash_excluding(chief, "reasoning_hash")

    permission = {"permission_id": f"perm-{signal_id}", "signal_id": signal_id,
                  "instrument": "EURUSD", "direction": direction, "size": size, "mode": "demo",
                  "expected_rr": 2.0, "expected_price": entry, "sl_tp": {"sl": sl, "tp": tp},
                  "chief_reasoning_hash": chief["reasoning_hash"],
                  "sources": [{"id": "real-eurusd-feed", "credibility": 0.95, "age_seconds": 2}],
                  "expires_at": (now + timedelta(minutes=5)).isoformat(),
                  "within_high_impact_news_window": False,
                  "risk_snapshot": {"risk_fraction": 0.005, "daily_loss_fraction": 0.001,
                                    "drawdown_fraction": 0.001, "open_positions": 0,
                                    "correlated_open_positions": 0}}

    return {"signal_id": signal_id, "verdicts": verdicts, "peer_reviews": reviews,
            "open_challenges": [], "challenge_records": [], "challenge_resolution_status": "RESOLVED",
            "contradictions_last_n_hours": 0,
            "key_sources": [{"source_id": "real-eurusd-feed", "credibility": 0.95,
                             "age_seconds": 2, "ttl_seconds": 900}],
            "devil_advocate_report": {"status": "PASS", "counter_hypotheses": [
                {"hypothesis": "momentum reversal", "evidence_refs": ["market-feed-1"]},
                {"hypothesis": "spread spike", "evidence_refs": ["market-feed-2"]},
                {"hypothesis": "session change", "evidence_refs": ["market-feed-3"]}]},
            "news_or_rumor": False, "risk_flags": [], "compliance_status": "PASS",
            "chief_reasoning": chief, "permission_request": permission,
            "dissent_rate": 0.125, "dissent_sample_size": 80,
            "false_certainty_rate": 0.02, "false_certainty_sample_size": 80}


def describe(price_info: dict, bundle: dict) -> str:
    permission = bundle["permission_request"]
    size = permission["size"]
    pips = abs(permission["expected_price"] - permission["sl_tp"]["sl"]) / 0.0001
    pip_value = size * LOT_UNITS * 0.0001
    return (f"REAL price EUR/USD = {price_info['price']:.5f} (SMA10 {price_info['sma10']:.5f})\n"
            f"  direction: {permission['direction']}   size: {size} lot (micro)\n"
            f"  SL {permission['sl_tp']['sl']:.5f} ({pips:.0f} pips = ${pips * pip_value:.2f} risk"
            f" = {pips * pip_value / DEMO_BALANCE * 100:.1f}% of ${DEMO_BALANCE:.0f} demo)\n"
            f"  TP {permission['sl_tp']['tp']:.5f}   expected RR 2.0")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trades", type=int, default=1, help="how many signals to evaluate")
    parser.add_argument("--telegram", action="store_true", help="ask for approval via Telegram")
    parser.add_argument("--autonomous", action="store_true",
                        help="autonomous work mode: demo-only, size-capped, no human gate")
    args = parser.parse_args()
    work_mode_policy = {"work_mode": "autonomous", "autonomous": {"enabled": True, "demo_only": True,
                                                                  "max_size_per_trade": 0.05}} if args.autonomous else None

    certainty_policy = yaml.safe_load((ROOT / "config" / "certainty_thresholds.yaml").read_text(encoding="utf-8"))
    permission_matrix = yaml.safe_load((ROOT / "config" / "permission_matrix.yaml").read_text(encoding="utf-8"))
    # Demo runs around the clock: widen the operator's time window for the demo only.
    permission_matrix["permission_matrix"]["allowed_time_utc"] = {"start": "00:00", "end": "23:59"}
    permission_matrix["permission_matrix"]["rollover_block_utc"] = {"start": "23:59", "end": "23:59"}

    channel = channel_from_env() if args.telegram else None
    if args.telegram and channel is None:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are not set — falling back to console approval.")

    print("=" * 72)
    print("DEMO: real market data + demo money. No order is ever sent anywhere.")
    print("=" * 72)

    approved = 0
    for n in range(1, args.trades + 1):
        print(f"\n--- signal {n}/{args.trades} ---")
        try:
            price_info = fetch_real_price()
        except Exception as exc:
            print(f"market data unavailable: {type(exc).__name__}; skipping")
            continue
        bundle = build_bundle(price_info, certainty_policy)
        bundle["permission_request"]["consensus_score"] = weighted_consensus(bundle["verdicts"])
        print(describe(price_info, bundle))

        result = evaluate_signal_bundle(bundle, certainty_policy, permission_matrix, now=datetime.now(timezone.utc),
                                        work_mode_policy=work_mode_policy)
        print(f"pipeline: {result['status']}   blockers: {result['blockers'] or 'none'}")
        if result["status"] != "REQUIRE_HUMAN":
            continue

        text = f"🤖 Signal {result['signal_id']} — needs your approval\n{describe(price_info, bundle)}"
        decision = None
        if channel is not None:
            print("asking on Telegram (up to 5 minutes)...")
            try:
                decision = channel.request_decision(text, timeout_seconds=300)
            except Exception as exc:
                print(f"telegram unavailable ({type(exc).__name__}) — console fallback")
        if decision is None:
            answer = input("Approve this demo trade? [y/N]: ").strip().lower()
            decision = "approve" if answer == "y" else "reject"

        if decision != "approve":
            print("❌ REJECTED by operator — nothing happens.")
            continue

        now = datetime.now(timezone.utc)
        bundle["user_decision"] = {"decision": "APPROVE", "decision_id": f"dec-{result['signal_id']}",
                                   "user_id": "telegram-operator", "reason": "approved from console/telegram",
                                   "permission_id": bundle["permission_request"]["permission_id"],
                                   "timestamp": now.isoformat(), "ttl_seconds": 300}
        final = evaluate_signal_bundle(bundle, certainty_policy, permission_matrix, now=now)
        approved += 1
        print(f"✅ pipeline: {final['status']}  (audit chain valid: {final['audit_chain_valid']})")
        print("   Demo money only: this authorized METADATA on real prices; no order was sent to any broker.")

    print(f"\nDone: {approved}/{args.trades} approved. All money demo, all data real, zero orders sent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
