#!/usr/bin/env python3
"""Replay the strategy families on Binance spot data, the second venue.

Crypto trades around the clock, so a rule that only looked good on a market with
weekends gets a cleaner, faster sample here. Klines come from the public REST API
(no key), and the round-trip taker fee is modelled as the spread, because the engine
has no separate commission input - a backtest without that cost is a backtest of a
different strategy.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from market_matrix import FAMILY_PARAMS, _yaml_number  # noqa: E402

UTC = timezone.utc
KLINES = "https://api.binance.com/api/v3/klines"
# One tick per unit of the base asset, so a price move of one tick is worth one tick of
# quote currency per lot; the exact contract does not matter for a price-relative rule.
TICK = {"BTCUSDT": 0.01, "ETHUSDT": 0.01, "SOLUSDT": 0.001, "XRPUSDT": 0.0001,
        "BNBUSDT": 0.01, "ADAUSDT": 0.0001, "DOGEUSDT": 0.00001}


def _get_json(url: str, params: dict) -> list:
    query = urllib.parse.urlencode(params)
    request = urllib.request.Request(f"{url}?{query}", headers={"User-Agent": "dsh"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_bars(symbol: str, bars: int) -> list:
    from critical_consensus.market_data import MarketBar

    rows: list = []
    end_time = None
    while len(rows) < bars:
        params = {"symbol": symbol, "interval": "1h", "limit": 1000}
        if end_time is not None:
            params["endTime"] = end_time
        batch = _get_json(KLINES, params)
        if not batch:
            break
        rows = [
            MarketBar(symbol=symbol, timeframe="H1",
                      time=datetime.fromtimestamp(item[0] / 1000, tz=UTC),
                      open=float(item[1]), high=float(item[2]), low=float(item[3]),
                      close=float(item[4]), tick_volume=int(float(item[5])), spread_points=2)
            for item in batch
        ] + rows
        end_time = batch[0][0] - 1
    rows.sort(key=lambda bar: bar.time)
    return rows[-bars:]


def build_spec(symbol: str, price: float) -> dict:
    tick = TICK.get(symbol, 0.01)
    return {
        "symbol": symbol, "point": tick, "digits": max(0, len(f"{tick:.10f}".rstrip("0").split(".")[1])),
        "tick_size": tick, "tick_value": tick, "volume_min": 0.001, "volume_max": 1000.0,
        "volume_step": 0.001, "spread_points": max(1, round(price * 0.001 / tick)),
        "stops_level_points": 0,
    }


def build_policy(symbol: str, spec: dict, family: str):
    from critical_consensus.mt5_agent import load_policy

    params = dict(FAMILY_PARAMS[family])
    lines = [f"  family: {family}", "  min_bars: 250"]
    lines += [f"  {key}: {value}" for key, value in params.items() if key != "min_bars"]
    lines += ["  atr_period: 14", "  stop_atr_multiple: 2.0",
              "  take_profit_risk_multiple: 2.0", "  min_stop_spread_multiple: 3.0",
              "  min_sma_gap_atr: 0.10", "  min_momentum_abs: 0.0",
              "  max_confidence: 0.95"]
    spec_lines = "\n".join(
        f"  {key}: {_yaml_number(value) if isinstance(value, float) else value}"
        for key, value in spec.items())
    text = f"""version: 1
mode: paper
symbol: {symbol}
timeframe: H1
history_bars: 10000
server_utc_offset_seconds: 0
execution_mode: auto
ui_language: en

account:
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
  reason: "crypto matrix run"
"""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "policy.yaml"
        path.write_text(text, encoding="utf-8")
        return load_policy(str(path))


def replay(bars: list, spec: dict, policy, *, balance: float, offset_seconds: int = 0) -> dict:
    from critical_consensus.mt5_agent import MT5Agent, PaperBroker, SymbolSpec

    broker = PaperBroker(bars={policy.symbol: bars}, spec=SymbolSpec(**spec),
                         starting_balance=balance, server_utc_offset_seconds=offset_seconds)
    agent = MT5Agent(backend=broker, policy=policy, clock=broker.now)
    for bar in bars[policy.strategy.min_bars:]:
        broker.seek(bar.time)
        agent.run_cycle()
    agent.close_positions("END_OF_RUN")
    trades = list(broker.trades)
    wins = sum(1 for trade in trades if trade.pnl > 0)
    return {"trades": len(trades), "wins": wins,
            "pnl": round(broker.realized_pnl, 2),
            "return": round(broker.realized_pnl / balance, 4)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default="BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT")
    parser.add_argument("--families", default="sma,ema,bollinger,donchian")
    parser.add_argument("--bars", type=int, default=6000)
    parser.add_argument("--balance", type=float, default=10_000.0)
    parser.add_argument("--report", default="")
    args = parser.parse_args(argv)

    symbols = [item.strip().upper() for item in args.symbols.split(",") if item.strip()]
    families = [item.strip().lower() for item in args.families.split(",") if item.strip()]
    report: dict[str, dict] = {}
    header = f"{'symbol':12}" + "".join(f"{family:>13}" for family in families)
    print(header)
    print("-" * len(header))
    for symbol in symbols:
        try:
            bars = fetch_bars(symbol, args.bars)
        except Exception as exc:  # noqa: BLE001 - the venue can refuse a symbol
            print(f"{symbol:12} fetch failed: {type(exc).__name__}")
            continue
        if len(bars) < 400:
            print(f"{symbol:12} only {len(bars)} bars")
            continue
        spec = build_spec(symbol, bars[-1].close)
        cells = []
        report[symbol] = {}
        for family in families:
            policy = build_policy(symbol, spec, family)
            outcome = replay(bars, spec, policy, balance=args.balance)
            report[symbol][family] = outcome
            cells.append(f"{outcome['return'] * 100:12.2f}%")
        print(f"{symbol:12}" + "".join(cells))
        print(f"{'':12}" + "".join(f"{report[symbol][f]['trades']:>8} trades" for f in families))
    if args.report:
        Path(args.report).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"raw report: {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
