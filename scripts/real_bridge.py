"""Real-account bridge: the EA in the real terminal and this process share two files.

The MetaTrader5 Python API can only attach to one terminal at a time, and the demo swarm owns
that channel. So the real account is driven by the inverse arrangement: an Expert Advisor lives
inside the real terminal and exchanges files through the common MQL5\\Files folder.

    EA  -> dsh_real_state.json   (account, equity, positions) every second
    EA  <- dsh_real_cmd.json     (one command: open / close), read and deleted

This process reads the state, mirrors only what survives the operator's rules, and refuses to
send anything unless the terminal reports an Exness real account with money on it.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / "agent_data" / "real_bridge.jsonl"
STATE = ROOT / "agent_data" / "real_bridge_state.json"
COMMON = Path(r"C:\Users\Administrator\AppData\Roaming\MetaQuotes\Terminal\Common\Files")
EA_STATE = COMMON / "dsh_real_state.json"
EA_CMD = COMMON / "dsh_real_cmd.json"
DEMO_TERMINAL = r"C:\Program Files\MetaTrader 5\terminal64.exe"
REAL_SERVER = "Exness-MT5Real26"
REAL_LOGIN = 276261159
DAILY_STOP_PCT = 0.5
REAL_LOTS = 0.01
TP_USD = 3.0
MAX_HOLD_MIN = 90


def log(event: dict) -> None:
    event["ts"] = datetime.now(timezone.utc).isoformat()
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event) + "\n")
    print(f"[{event['ts'][11:19]}] {event['event']}", flush=True)


def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def save_state(state: dict) -> None:
    STATE.write_text(json.dumps(state), encoding="utf-8")


def read_ea_state() -> dict | None:
    if not EA_STATE.exists():
        return None
    try:
        return json.loads(EA_STATE.read_text(encoding="utf-8", errors="replace"))
    except (json.JSONDecodeError, OSError):
        return None


def send_command(command: dict) -> None:
    EA_CMD.write_text(json.dumps(command), encoding="utf-8")
    log({"event": f"command to EA: {command}"})


def demo_book() -> list[dict] | None:
    import MetaTrader5 as mt5

    if not mt5.initialize(path=DEMO_TERMINAL, timeout=4000):
        return None
    try:
        account = mt5.account_info()
        if account is None or account.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
            return None
        positions = mt5.positions_get() or []
        return [{"ticket": p.ticket, "symbol": p.symbol, "type": p.type,
                 "volume": p.volume, "profit": p.profit, "sl": p.sl, "tp": p.tp}
                for p in positions]
    finally:
        mt5.shutdown()


def hourly_uptrend(symbol: str) -> bool:
    import MetaTrader5 as mt5

    if not mt5.initialize(path=DEMO_TERMINAL, timeout=4000):
        return False
    try:
        rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 1, 60)
        if rates is None or len(rates) < 50:
            return False
        closes = [float(r["close"]) for r in rates]

        def ema(values, period):
            weight = 2 / (period + 1)
            result = values[0]
            for value in values[1:]:
                result = value * weight + result * (1 - weight)
            return result

        return closes[-1] > ema(closes, 20) > ema(closes, 50)
    finally:
        mt5.shutdown()


def main() -> int:
    state = load_state()
    log({"event": "REAL BRIDGE START (file channel, EA in the real terminal)"})
    while True:
        try:
            ea = read_ea_state()
            if ea is None:
                log({"event": "no EA state yet — is DSHRealBridge attached in the real terminal?"})
                time.sleep(10)
                continue

            # hard guard: only an Exness real account with money may receive orders
            if ea.get("server") != REAL_SERVER or int(ea.get("login") or 0) != REAL_LOGIN:
                log({"event": f"EA reports {ea.get('login')} {ea.get('server')} — not the real "
                              f"account, bridge idle"})
                time.sleep(15)
                continue
            if float(ea.get("balance") or 0) <= 0:
                log({"event": f"real balance {ea.get('balance')} — no funds, bridge idle"})
                time.sleep(30)
                continue

            today = datetime.now(timezone.utc).date().isoformat()
            if state.get("day") != today:
                state["day"] = today
                state["day_start_equity"] = float(ea["equity"])
                save_state(state)
            day_start = float(state.get("day_start_equity") or ea["equity"])
            if float(ea["equity"]) < day_start * (1 - DAILY_STOP_PCT / 100) and not state.get("halted"):
                for position in ea.get("positions") or []:
                    send_command({"action": "close", "ticket": position["ticket"]})
                state["halted"] = True
                save_state(state)
                log({"event": f"DAILY STOP: equity {ea['equity']} below {day_start:.2f}"})
            if state.get("halted"):
                time.sleep(30)
                continue

            book = demo_book()
            if book is None:
                log({"event": "demo book unavailable this cycle"})
                time.sleep(10)
                continue
            real_positions = {p["symbol"]: p for p in (ea.get("positions") or [])}
            demo_symbols = {item["symbol"] for item in book}

            # close the real copy when the demo closed its own
            for symbol, position in list(real_positions.items()):
                if symbol not in demo_symbols:
                    send_command({"action": "close", "ticket": position["ticket"]})
                    time.sleep(2)

            # mirror only profitable demo buys in a confirmed uptrend
            opened = state.setdefault("opened", {})
            for item in book:
                if item["type"] != 0 or item["symbol"] in real_positions:
                    continue
                if item["profit"] <= 0:
                    continue
                if not hourly_uptrend(item["symbol"]):
                    continue
                send_command({"action": "open", "symbol": item["symbol"], "side": 0,
                              "lots": REAL_LOTS})
                opened[item["symbol"]] = time.time()
                save_state(state)
                time.sleep(2)
            save_state(state)
            log({"event": f"HEARTBEAT real {ea.get('login')} equity {ea.get('equity')} "
                          f"positions {len(ea.get('positions') or [])} demo {len(book)}"})
        except Exception as exc:  # noqa: BLE001
            log({"event": f"cycle error {type(exc).__name__}: {exc}"})
        time.sleep(10)


if __name__ == "__main__":
    raise SystemExit(main())
