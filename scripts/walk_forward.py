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
        # Position 0 is the bar still forming: its close is not a close yet, so a replay
        # that reads it looks one hour into the future.
        rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 1, bars)
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


def run_fold(bars: list, spec: dict, policy, *, balance: float, warmup: int) -> dict:
    from critical_consensus.mt5_agent import MT5Agent, PaperBroker

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
    # No forced close at the boundary: closing the last position at the boundary bid makes a
    # fold's sign depend on where the fold happened to end. The position is marked to market
    # instead, and the fold's result is the equity change, open trade included.
    equity = broker.equity()
    trades = list(broker.trades)
    wins = sum(1 for trade in trades if trade.pnl > 0)
    return {"cycles": cycles, "trades": len(trades), "wins": wins,
            "pnl": round(equity - balance, 2),
            "return": round((equity - balance) / balance, 4),
            "open_at_end": len(broker.positions())}


def _spec(raw: dict):
    from critical_consensus.mt5_agent import SymbolSpec

    return SymbolSpec(**raw)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--families", default="sma,ema,bollinger,donchian")
    parser.add_argument("--policy", default="",
                        help="use this policy file verbatim instead of a generated one")
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
    fixed_policy = None
    if args.policy:
        from critical_consensus.mt5_agent import load_policy

        fixed_policy = load_policy(args.policy)
        families = [fixed_policy.strategy.family]
    report: dict[str, list] = {}
    for family in families:
        policy = fixed_policy or build_policy(symbol, spec, family, balance=args.balance)
        # The warm-up has to leave the seed of an EMA far behind: at 250 bars an EMA(200)
        # still carries ~8% of its first value, so each fold would be judging a different
        # rule. Three lookbacks, never less than 300 bars.
        warmup = max(300, 3 * policy.strategy.lookback)
        usable_len = len(bars) - warmup
        fold_size = usable_len // args.folds
        if fold_size < 50:
            print("not enough history for that many folds", file=sys.stderr)
            return 2
        rows = []
        for index in range(args.folds):
            # Each fold gets its own warm-up prefix taken from the bars before it, so fold 0
            # is not shorter than the rest and no fold sees a bar from its own future.
            fold_start = warmup + index * fold_size
            fold_end = warmup + usable_len if index == args.folds - 1 \
                else fold_start + fold_size
            chunk = bars[fold_start - warmup:fold_end]
            rows.append(run_fold(chunk, spec, policy, balance=args.balance, warmup=warmup))
        report[family] = rows
        cells = " ".join(f"{row['return'] * 100:+6.1f}%" for row in rows)
        positive = sum(1 for row in rows if row["return"] > 0)
        print(f"{family:10} warmup={warmup:4} folds: {cells}   positive {positive}/{len(rows)}")

    if args.report:
        Path(args.report).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"raw report: {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
