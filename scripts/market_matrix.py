#!/usr/bin/env python3
"""Replay every strategy family on every requested symbol and print one matrix.

The specs come from the running terminal, so gold, an index and a crypto CFD are
sized with their own contract rather than a guessed one. Each cell is the return of
one (symbol, family) replay over the same history with the paper engine.

A positive cell is a candidate, not a discovery: running dozens of combinations is
exactly how a random walk starts to look like a system, so the report also prints how
many cells would be positive by chance if every family had zero edge.
"""
from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FAMILIES = ("sma", "ema", "bollinger", "donchian")
FAMILY_PARAMS = {
    "sma": {"sma_fast": 5, "sma_slow": 20},
    "ema": {"ema_fast": 20, "ema_slow": 50},
    "bollinger": {"bollinger_period": 20, "bollinger_dev": 2.0},
    "donchian": {"donchian_period": 20},
}


def terminal_specs(symbols: list[str]) -> dict[str, dict]:
    import MetaTrader5 as mt5

    if not mt5.initialize():
        raise SystemExit("MetaTrader5.initialize failed; is the terminal running?")
    specs = {}
    try:
        for symbol in symbols:
            if not mt5.symbol_select(symbol, True):
                print(f"{symbol}: the broker does not offer it", file=sys.stderr)
                continue
            info = mt5.symbol_info(symbol)
            if info is None or not info.trade_tick_value:
                print(f"{symbol}: no usable contract data", file=sys.stderr)
                continue
            specs[symbol] = {
                "symbol": symbol, "point": info.point, "digits": info.digits,
                "tick_size": info.trade_tick_size, "tick_value": info.trade_tick_value,
                "volume_min": info.volume_min, "volume_max": info.volume_max,
                "volume_step": info.volume_step,
                "spread_points": min(max(int(info.spread), 10), 60),
                "stops_level_points": max(int(info.trade_stops_level), 0),
            }
            # Only a broker that publishes it: SymbolSpec rejects a declared non-positive
            # contract_size, and an unset field reads as 0.0 rather than as "unknown".
            contract_size = getattr(info, "trade_contract_size", 0.0)
            if contract_size > 0:
                specs[symbol]["contract_size"] = float(contract_size)
    finally:
        mt5.shutdown()
    return specs


def _yaml_number(value: float) -> str:
    """Plain decimal, because YAML 1.1 reads ``1e-05`` as a string and the spec rejects it."""
    text = format(float(value), ".10f").rstrip("0").rstrip(".")
    return text or "0"


def policy_yaml(symbol: str, spec: dict, family: str, *, bars: int) -> str:
    params = dict(FAMILY_PARAMS[family])
    params["min_bars"] = 250
    strategy_lines = [f"  family: {family}", "  min_bars: 250"]
    strategy_lines += [f"  {key}: {value}" for key, value in params.items() if key != "min_bars"]
    strategy_lines += [
        "  atr_period: 14", "  stop_atr_multiple: 2.0",
        "  take_profit_risk_multiple: 2.0", "  min_sma_gap_atr: 0.10",
        "  min_momentum_abs: 0.0", "  max_confidence: 0.95",
    ]
    spec_lines = "\n".join(
        f"  {key}: {_yaml_number(value) if isinstance(value, float) else value}"
        for key, value in spec.items())
    return f"""version: 1
mode: paper
symbol: {symbol}
timeframe: H1
history_bars: {bars}
server_utc_offset_seconds: 7200
execution_mode: auto
ui_language: ru

account:
  expected_login: 114929502
  expected_server: MetaQuotes-Demo
  require_demo: true

symbol_spec:
{spec_lines}

risk:
  risk_per_trade_fraction: 0.005
  max_open_positions: 1
  max_daily_loss_fraction: 0.25
  max_drawdown_fraction: 0.5
  min_risk_reward: 1.5
  max_spread_points: 10000
  max_quote_age_seconds: 300

strategy:
{chr(10).join(strategy_lines)}

schedule:
  window_start_minute_utc: 0
  window_end_minute_utc: 1440
  blocked_weekdays_utc: []
  blackout_windows_utc: []

kill_switch:
  active: false
  reason: "market matrix run"
"""


def run_replay(policy_path: Path, *, bars: int, balance: float, python: str) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        command = [
            python, str(ROOT / "scripts" / "run_mt5_agent.py"), "run",
            "--policy", str(policy_path), "--db", str(Path(tmp) / "matrix.db"),
            "--source", "live", "--live-bars", str(bars),
            "--balance", str(balance), "--execution", "auto", "--language", "en",
        ]
        completed = subprocess.run(command, capture_output=True, text=True,
                                   cwd=str(ROOT), timeout=7200)
    text = completed.stdout + completed.stderr
    start, end = text.find("{"), text.rfind("}")
    if start == -1:
        raise RuntimeError(text[-1500:])
    return json.loads(text[start:end + 1])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", required=True, help="comma-separated broker symbols")
    parser.add_argument("--families", default=",".join(FAMILIES))
    parser.add_argument("--bars", type=int, default=10_000)
    parser.add_argument("--balance", type=float, default=10_000.0)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--report", default="")
    args = parser.parse_args(argv)

    symbols = [item.strip().upper() for item in args.symbols.split(",") if item.strip()]
    families = [item.strip().lower() for item in args.families.split(",") if item.strip()]
    unknown = [family for family in families if family not in FAMILIES]
    if unknown:
        print(f"unknown families: {unknown}", file=sys.stderr)
        return 2

    specs = terminal_specs(symbols)
    if not specs:
        print("no symbol had usable contract data", file=sys.stderr)
        return 2

    results: dict[str, dict[str, dict]] = {}
    with tempfile.TemporaryDirectory() as tmp:
        for symbol, spec in specs.items():
            results[symbol] = {}
            for family in families:
                policy_path = Path(tmp) / f"{symbol}_{family}.yaml"
                policy_path.write_text(policy_yaml(symbol, spec, family, bars=args.bars),
                                       encoding="utf-8")
                try:
                    results[symbol][family] = run_replay(
                        policy_path, bars=args.bars, balance=args.balance, python=args.python)
                except (RuntimeError, subprocess.TimeoutExpired) as exc:
                    print(f"{symbol}/{family}: FAILED {exc}", file=sys.stderr)
                    results[symbol][family] = {"error": str(exc)}
                print(f"  {symbol}/{family} done", file=sys.stderr)

    header = f"{'symbol':10}" + "".join(f"{family:>14}" for family in families)
    print(header)
    print("-" * len(header))
    positives = 0
    total = 0
    for symbol, by_family in results.items():
        cells = []
        for family in families:
            data = by_family[family]
            if "error" in data:
                cells.append(f"{'ERR':>14}")
                continue
            total += 1
            ret = data.get("return_fraction", 0.0) * 100
            positives += ret > 0
            cells.append(f"{ret:13.2f}%")
        print(f"{symbol:10}" + "".join(cells))

    if total:
        expected = total * 0.5
        print(f"\npositive cells: {positives}/{total}; "
              f"a zero-edge world would average about {expected:.0f} by chance")
    if args.report:
        Path(args.report).write_text(json.dumps(results, indent=2, ensure_ascii=False),
                                     encoding="utf-8")
        print(f"raw report: {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
