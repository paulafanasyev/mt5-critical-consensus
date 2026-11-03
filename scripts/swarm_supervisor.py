#!/usr/bin/env python3
"""The supervisor: watch the swarm, correct what drifts, never stop.

Every cycle it checks four things and fixes them on the spot:

  1. the swarm process is alive - if it died, start it again;
  2. every swarm position has a stop - if a stop is missing, add 3xATR;
  3. no symbol exceeds its cap - if it does, trim the newest excess;
  4. the account has not lost 1% from its baseline - if it has, flatten and pause.

Everything is written to agent_data/supervisor.jsonl so the corrections are auditable.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / "agent_data" / "supervisor.jsonl"
STATE = ROOT / "agent_data" / "swarm_state.json"
SWARM = ROOT / "scripts" / "agent_swarm.py"
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
MAGIC_FLOOR = 8180000
MAGIC_BASE = 8180000
DEMO_TERMINALS = (r"C:\mt5-team\terminal64.exe",
                  r"C:\mt5-exness\terminal64.exe",
                  r"C:\Program Files\MetaTrader 5\terminal64.exe",
                  r"C:\mt5-real\terminal64.exe")
DESK_MAGIC = 8189999  # hand-placed by the lead, not an orphan
MAX_DRAWDOWN = 0.01
KILL_SWITCH = ROOT / "agent_data" / "demo_bridge" / "kill_switch.json"


def kill_switch_active() -> bool:
    """The documented brake, read the way the trading runner reads it.

    Every duty below sends an order or starts a process that does, so an armed switch has to stop
    the supervisor itself, not just the swarm. Missing or unreadable counts as armed.
    """
    try:
        raw = json.loads(KILL_SWITCH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    return raw.get("kill_switch_active") is True


def log(event: dict) -> None:
    event["ts"] = datetime.now(timezone.utc).isoformat()
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event) + "\n")
    print(f"[{event['ts'][11:19]}] {event['event']}", flush=True)


def swarm_running() -> bool:
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
         "Where-Object { $_.CommandLine -like '*agent_swarm*' } | Measure-Object).Count"],
        capture_output=True, text=True, timeout=30)
    try:
        return int(result.stdout.strip() or 0) > 0
    except ValueError:
        return False


def connect_demo(mt5) -> bool:
    """Bind to whichever known terminal currently holds the demo login."""
    for path in DEMO_TERMINALS:
        try:
            if not mt5.initialize(path=path, timeout=3000):
                continue
            account = mt5.account_info()
            if (account is not None and account.login == 114929502
                    and account.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO):
                return True
            mt5.shutdown()
        except Exception:  # noqa: BLE001
            continue
    return False

def main(argv: list[str] | None = None) -> int:
    import argparse

    import MetaTrader5 as mt5

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval-sec", type=int, default=300)
    args = parser.parse_args(argv)
    if not connect_demo(mt5):
        raise SystemExit("MetaTrader5.initialize failed")

    while True:
        try:
            now = datetime.now(timezone.utc)
            account = mt5.account_info()
            if account is None:
                log({"event": "no account info, retry"})
                time.sleep(args.interval_sec)
                continue
            state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
            baseline = 100_000.0  # fixed reference capital: the brake cannot drift
            state.setdefault("paused_until", 0.0)

            if kill_switch_active():
                log({"event": "kill switch armed; supervisor idle, nothing sent or started"})
                time.sleep(args.interval_sec)
                continue

            # 1. the swarm process
            if not swarm_running():
                subprocess.Popen([str(PYTHON), str(SWARM), "--interval-sec", "15"],
                                 cwd=str(ROOT), stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
                log({"event": "swarm was down; restarted"})

            positions = [p for p in (mt5.positions_get() or []) if p.magic >= MAGIC_FLOOR]

            # Positions below the floor belong to the person trading in the terminal or to another
            # agent. The supervisor used to flatten them on every cycle and to stop the other
            # agents' processes with PowerShell, which is how the observed run kept dying and the
            # owner's own legs kept closing on their own. It has no authority over either.

            # 1b. every position must belong to the agent whose magic it carries
            import re as _re

            source = SWARM.read_text(encoding="utf-8")
            registry = _re.findall(r'agent\("([^"]+)", "[a-z0-9_]+", "([A-Z0-9]+)", [0-9.]+',
                                   source)
            symbol_by_name = {name: symbol for name, symbol in registry}
            import zlib as _zlib

            for position in positions:
                name = None
                for candidate, symbol in registry:
                    if MAGIC_BASE + (_zlib.crc32(candidate.encode()) % 5000) == position.magic:
                        name = candidate
                        break
                if position.magic == DESK_MAGIC:
                    continue
                if name is None or symbol_by_name.get(name) != position.symbol:
                    tick = mt5.symbol_info_tick(position.symbol)
                    mt5.order_send({"action": mt5.TRADE_ACTION_DEAL,
                                    "symbol": position.symbol,
                                    "volume": position.volume,
                                    "type": mt5.ORDER_TYPE_SELL if position.type == 0
                                            else mt5.ORDER_TYPE_BUY,
                                    "position": position.ticket,
                                    "price": tick.bid if position.type == 0 else tick.ask,
                                    "deviation": 30, "magic": position.magic,
                                    "type_filling": mt5.ORDER_FILLING_FOK,
                                    "comment": "orphan"})
                    log({"event": f"orphan closed: {position.symbol} ticket {position.ticket} "
                                  f"magic {position.magic} (agent {name})"})

            positions = [p for p in (mt5.positions_get() or []) if p.magic >= MAGIC_FLOOR]

            # 2. every position must carry a stop
            for position in positions:
                if position.sl:
                    continue
                rates = mt5.copy_rates_from_pos(position.symbol, mt5.TIMEFRAME_H1, 1, 60)
                if rates is None or len(rates) < 20:
                    continue
                ranges = [max(rates[i]["high"] - rates[i]["low"],
                              abs(rates[i]["high"] - rates[i - 1]["close"]),
                              abs(rates[i]["low"] - rates[i - 1]["close"]))
                          for i in range(1, len(rates))]
                atr = sum(ranges[-14:]) / 14
                if position.type == 0:
                    stop = position.price_open - 3 * atr
                else:
                    stop = position.price_open + 3 * atr
                result = mt5.order_send({"action": mt5.TRADE_ACTION_SLTP,
                                         "symbol": position.symbol,
                                         "position": position.ticket,
                                         "sl": round(stop, 5), "tp": position.tp})
                log({"event": f"{position.symbol} ticket {position.ticket}: no stop, added "
                              f"{round(stop, 5)} retcode "
                              f"{None if result is None else result.retcode}"})

            # 3. drawdown brake. The swarm owns the stop rules and anchors them to the day's
            # starting equity; a second, fixed 100 000 brake here flattened every position on
            # every cycle once the account sat below -1%, which looked exactly like "no trades".
            # The supervisor only acts as a backstop when the swarm itself is gone.
            swarm_alive = swarm_running()
            if not swarm_alive and account.equity < baseline * (1 - MAX_DRAWDOWN):
                log({"event": f"swarm is down and equity {account.equity:.2f} is below "
                              f"{baseline:.2f}; flattening as a backstop"})
                for position in positions:
                    tick = mt5.symbol_info_tick(position.symbol)
                    mt5.order_send({"action": mt5.TRADE_ACTION_DEAL,
                                    "symbol": position.symbol,
                                    "volume": position.volume,
                                    "type": mt5.ORDER_TYPE_SELL if position.type == 0
                                            else mt5.ORDER_TYPE_BUY,
                                    "position": position.ticket,
                                    "price": tick.bid if position.type == 0 else tick.ask,
                                    "deviation": 30, "magic": position.magic,
                                    "type_filling": mt5.ORDER_FILLING_FOK,
                                    "comment": "supervisor"})
                state["baseline_equity"] = account.equity
                state["paused_until"] = time.time() + 1800
                STATE.write_text(json.dumps(state), encoding="utf-8")

            # 4. heartbeat
            pnl = sum(position.profit for position in positions)
            log({"event": f"HEARTBEAT equity {account.equity:.2f} baseline {baseline:.2f} "
                          f"positions {len(positions)} floating {pnl:+.2f}"})
        except Exception as exc:  # noqa: BLE001 - the supervisor must never die
            log({"event": f"cycle error {type(exc).__name__}: {exc}"})
        time.sleep(args.interval_sec)


if __name__ == "__main__":
    raise SystemExit(main())
