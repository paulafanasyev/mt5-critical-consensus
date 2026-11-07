#!/usr/bin/env python3
"""Replay several strategy variants over the same real history and print one table.

Each variant is a policy YAML that differs only in its ``strategy`` section. The
replay is the same paper engine the live demo uses, over bars read from the running
MT5 terminal, so the numbers compare rules rather than two different simulators.

Nothing here can place an order: the run happens inside ``run_mt5_agent.py run``,
whose backend has no order surface.

Example:
    python scripts/compare_strategies.py --bars 10000 --balance 10000
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY_DIR = ROOT / "config" / "forward"


def run_one(policy: Path, *, bars: int, balance: float, python: str) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "compare.db"
        command = [
            python, str(ROOT / "scripts" / "run_mt5_agent.py"), "run",
            "--policy", str(policy), "--db", str(db),
            "--source", "live", "--live-bars", str(bars),
            "--balance", str(balance), "--execution", "auto", "--language", "en",
        ]
        completed = subprocess.run(command, capture_output=True, text=True,
                                   cwd=str(ROOT), timeout=3600)
    text = completed.stdout + completed.stderr
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise RuntimeError(f"{policy.name}: the replay printed no report\n{text[-2000:]}")
    return json.loads(text[start:end + 1])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bars", type=int, default=10_000, help="bars to read from the terminal")
    parser.add_argument("--balance", type=float, default=10_000.0, help="starting paper balance")
    parser.add_argument("--policy-dir", default=str(DEFAULT_POLICY_DIR))
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--report", default="", help="write the raw per-variant JSON here")
    args = parser.parse_args(argv)

    policies = sorted(Path(args.policy_dir).glob("*.yaml"))
    if not policies:
        print(f"no policy files under {args.policy_dir}", file=sys.stderr)
        return 2

    header = (f"{'variant':30} {'trades':>6} {'wins':>5} {'win%':>6} {'pnl':>10} "
              f"{'ret%':>7} {'maxDD':>8}")
    print(header)
    print("-" * len(header))
    report = {}
    for policy in policies:
        try:
            data = run_one(policy, bars=args.bars, balance=args.balance, python=args.python)
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            print(f"{policy.stem:30} FAILED: {exc}")
            continue
        report[policy.stem] = data
        trades = data.get("paper_trades_closed", 0)
        wins = data.get("paper_wins", 0)
        rate = (wins / trades * 100) if trades else 0.0
        print(f"{policy.stem:30} {trades:6} {wins:5} {rate:5.1f}% "
              f"{data.get('realized_pnl', 0):10.2f} {data.get('return_fraction', 0) * 100:6.2f}% "
              f"{data.get('max_drawdown', 0):8.2f}")

    if args.report:
        Path(args.report).write_text(json.dumps(report, indent=2, ensure_ascii=False),
                                     encoding="utf-8")
        print(f"\nraw report: {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
