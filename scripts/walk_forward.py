#!/usr/bin/env python3
"""Split a symbol's history into folds and replay the same rule in each one.

One long replay hides regime changes: a rule can be carried by a single good stretch.
This runs the same paper engine per fold, with the warm-up bars prepended so the
indicators start where they would in a live run, and prints how many folds were
positive and how the folds disagree.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from market_matrix import FAMILY_PARAMS, _yaml_number, terminal_specs  # noqa: E402

UTC = timezone.utc
SERVER_OFFSET = 7200


def load_bars(symbol: str, bars: int) -> list:
    import MetaTrader5 as mt5

    from critical_consensus.market_data import MarketBar

    if not mt5.initialize():
        raise SystemExit("MetaTrader5.initialize failed")
    try:
        mt5.symbol_select(symbol, True)
        rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 0, bars)
    finally:
        mt5.shutdown()
    rows = []
    names = rates.dtype.names or ()
    for rate in rates:
        stamp = datetime.fromtimestamp(int(rate["time"]), tz=UTC) - timedelta(seconds=SERVER_OFFSET)
        spread = int(rate["spread"]) if "spread" in names and int(rate["spread"]) > 0 else 10
        rows.append(MarketBar(symbol=symbol, timeframe="H1", time=stamp,
                              open=float(rate["open"]), high=float(rate["high"]),
                              low=float(rate["low"]), close=float(rate["close"]),
                              tick_volume=int(rate["tick_volume"]), spread_points=spread))
    return rows


def build_policy(symbol: str, spec: dict, family: str, *, balance: float):
    from critical_consensus.mt5_agent import load_policy

    params = dict(FAMILY_PARAMS[family])
    lines = [f"  family: {family}", "  min_bars: 250"]
    lines += [f"  {key}: {value}" for key, value in params.items() if key != "min_bars"]
    lines += ["  atr_period: 14", "  stop_atr_multiple: 2.0",
              "  take_profit_risk_multiple: 2.0", "  min_sma_gap_atr: 0.10",
              "  min_momentum_abs: 0.0", "  max_confidence: 0.95"]
    spec_lines = "\n".join(
        f"  {key}: {_yaml_number(value) if isinstance(value, float) else value}"
        for key, value in spec.items())
    text = f"""version: 1
mode: paper
symbol: {symbol}
timeframe: H1
history_bars: 10000
server_utc_offset_seconds: {SERVER_OFFSET}
execution_mode: auto
ui_language: en

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
{chr(10).join(lines)}

schedule:
  window_start_minute_utc: 0
  window_end_minute_utc: 1440
  blocked_weekdays_utc: []
  blackout_windows_utc: []

kill_switch:
  active: false
  reason: "walk-forward run"
"""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "policy.yaml"
        path.write_text(text, encoding="utf-8")
        return load_policy(str(path))


def run_fold(bars: list, spec: dict, policy, *, balance: float) -> dict:
    from critical_consensus.mt5_agent import MT5Agent, PaperBroker

    warmup = policy.strategy.min_bars
    series = bars
    broker = PaperBroker(bars={policy.symbol: series}, spec=_spec(spec),
                         login=policy.expected_login or 1_000_000_000,
                         server=policy.expected_server or "PAPER-LOCAL",
                         starting_balance=balance, server_utc_offset_seconds=SERVER_OFFSET)
    agent = MT5Agent(backend=broker, policy=policy, clock=broker.now)
    cycles = 0
    for bar in series[warmup:]:
        broker.seek(bar.time)
        agent.run_cycle()
        cycles += 1
    agent.close_positions("END_OF_FOLD")
    trades = list(broker.trades)
    wins = sum(1 for trade in trades if trade.pnl > 0)
    return {"cycles": cycles, "trades": len(trades), "wins": wins,
            "pnl": round(broker.realized_pnl, 2),
            "return": round(broker.realized_pnl / balance, 4)}


def _spec(raw: dict):
    from critical_consensus.mt5_agent import SymbolSpec

    return SymbolSpec(**raw)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--families", default="sma,ema,bollinger,donchian")
    parser.add_argument("--folds", type=int, default=6)
    parser.add_argument("--bars", type=int, default=10000)
    parser.add_argument("--balance", type=float, default=10_000.0)
    parser.add_argument("--report", default="")
    args = parser.parse_args(argv)

    symbol = args.symbol.strip().upper()
    spec = terminal_specs([symbol]).get(symbol)
    if spec is None:
        print(f"{symbol}: no contract data from the terminal", file=sys.stderr)
        return 2
    bars = load_bars(symbol, args.bars)
    families = [item.strip().lower() for item in args.families.split(",") if item.strip()]
    warmup = 250
    usable = bars[warmup:]
    fold_size = len(usable) // args.folds
    if fold_size < 50:
        print("not enough history for that many folds", file=sys.stderr)
        return 2

    report: dict[str, list] = {}
    print(f"{symbol}: {len(bars)} bars, {args.folds} folds of ~{fold_size} hours")
    for family in families:
        policy = build_policy(symbol, spec, family, balance=args.balance)
        rows = []
        for index in range(args.folds):
            start = index * fold_size
            end = start + fold_size if index < args.folds - 1 else len(usable)
            chunk = usable[max(0, start - warmup):end]
            rows.append(run_fold(chunk, spec, policy, balance=args.balance))
        report[family] = rows
        cells = " ".join(f"{row['return'] * 100:+6.1f}%" for row in rows)
        positive = sum(1 for row in rows if row["return"] > 0)
        print(f"{family:10} folds: {cells}   positive {positive}/{len(rows)}")

    if args.report:
        Path(args.report).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"raw report: {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
