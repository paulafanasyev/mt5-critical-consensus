"""Real-account bridge: the EA in the real terminal and this process share two files.

The MetaTrader5 Python API can only attach to one terminal at a time, and the demo swarm owns
that channel. So the real account is driven by the inverse arrangement: an Expert Advisor lives
inside the real terminal and exchanges files through the common MQL5\\Files folder.

    EA  -> dsh_real_state.json   (account, equity, positions with their magic) every second
    EA  <- dsh_real_cmd.json     (one command: open / close), read and deleted

Nothing in this file sends by default. `send_command` is the only door, and it asks `fleet.livegate`
on every knock: the stand (`C:\\mt5-fleet\\PAUSE`) must be gone, this invocation must carry `--live`,
and the registry must hold this login as a real account with `enabled: true`. The account is not
hardcoded here any more than the money is — an earlier version carried
`276261159@Exness-MT5Real26` in the source, which made any copy of this file a loaded gun pointed at
one specific live account.

And it closes only the lots it recognises as its own: the EA stamps `REAL_MAGIC` on what it opens,
and a position with any other magic is the operator's money, which this process may count but must
never close.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from fleet import livegate  # noqa: E402

LOG = ROOT / "agent_data" / "real_bridge.jsonl"
STATE = ROOT / "agent_data" / "real_bridge_state.json"
COMMON = Path(r"C:\Users\Administrator\AppData\Roaming\MetaQuotes\Terminal\Common\Files")
EA_STATE = COMMON / "dsh_real_state.json"
EA_CMD = COMMON / "dsh_real_cmd.json"
DEMO_TERMINAL = r"C:\Program Files\MetaTrader 5\terminal64.exe"
# The magic DSHRealBridge.mq5 stamps on everything it opens. A position with any other magic is the
# operator's own lot: this process may count it in the risk it reports, but it must never close it.
REAL_MAGIC = 8272026
DAILY_STOP_PCT = 0.5
REAL_LOTS = 0.01


def log(event: dict) -> None:
    event["ts"] = datetime.now(timezone.utc).isoformat()
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    print(f"[{event['ts'][11:19]}] {event['event']}", flush=True)


def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def save_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state), encoding="utf-8")


def read_ea_state(path: pathlib.Path = EA_STATE) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (json.JSONDecodeError, OSError):
        return None


def ours(ea: dict) -> list[dict]:
    """The real positions this bridge opened — and only those.

    A state file written by an EA that predates the `magic` field yields nothing here: no field, no
    close. Guessing that an unlabelled lot was ours would mean closing the operator's trade.
    """
    return [p for p in (ea.get("positions") or []) if int(p.get("magic") or 0) == REAL_MAGIC]


def send_command(command: dict, gate, cmd_path: pathlib.Path = EA_CMD) -> bool:
    """The only door to a real account. `gate()` answers why it may not be walked right now."""
    reason = gate()
    if reason:
        log({"event": f"NOT SENT {command}: {reason}"})
        return False
    cmd_path.parent.mkdir(parents=True, exist_ok=True)
    cmd_path.write_text(json.dumps(command), encoding="utf-8")
    log({"event": f"command to EA: {command}"})
    return True


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


def run_cycle(state: dict, gate, *, ea_path: pathlib.Path = EA_STATE,
              cmd_path: pathlib.Path = EA_CMD) -> str:
    """One pass over the two files; returns a sentence for the log."""
    refusal = gate()
    if refusal:
        # Asked before anything else: a cycle nobody permitted must not even open the demo terminal.
        return f"bridge idle: {refusal}"
    ea = read_ea_state(ea_path)
    if ea is None:
        return "no EA state yet — is DSHRealBridge attached in the real terminal?"
    if float(ea.get("balance") or 0) <= 0:
        return f"real balance {ea.get('balance')} — no funds, bridge idle"

    today = datetime.now(timezone.utc).date().isoformat()
    if state.get("day") != today:
        state["day"] = today
        state["day_start_equity"] = float(ea.get("equity") or 0)
        state["halted"] = False
        save_state(state)
    day_start = float(state.get("day_start_equity") or ea.get("equity") or 0)
    equity = float(ea.get("equity") or 0)
    if day_start and equity < day_start * (1 - DAILY_STOP_PCT / 100) and not state.get("halted"):
        for position in ours(ea):
            send_command({"action": "close", "ticket": position["ticket"]}, gate, cmd_path)
        state["halted"] = True
        save_state(state)
        return f"DAILY STOP: equity {equity} below {day_start:.2f}"
    if state.get("halted"):
        return "halted for the day"

    book = demo_book()
    if book is None:
        return "demo book unavailable this cycle"
    our_positions = ours(ea)
    all_positions = ea.get("positions") or []
    if len(our_positions) < len(all_positions):
        log({"event": f"{len(all_positions) - len(our_positions)} лот(ов) на реальном счёте не наши "
                      f"(magic != {REAL_MAGIC}): не закрыты и не тронуты"})
    held = {p["symbol"] for p in all_positions}
    demo_symbols = {item["symbol"] for item in book}

    # close our own copy when the demo closed its own
    for position in our_positions:
        if position["symbol"] not in demo_symbols:
            send_command({"action": "close", "ticket": position["ticket"]}, gate, cmd_path)
            time.sleep(2)

    # mirror only profitable demo buys in a confirmed uptrend
    opened = state.setdefault("opened", {})
    for item in book:
        if item["type"] != 0 or item["symbol"] in held or item["profit"] <= 0:
            continue
        if not hourly_uptrend(item["symbol"]):
            continue
        if send_command({"action": "open", "symbol": item["symbol"], "side": 0,
                         "lots": REAL_LOTS}, gate, cmd_path):
            opened[item["symbol"]] = time.time()
            save_state(state)
            time.sleep(2)
    return f"HEARTBEAT real {ea.get('login')} equity {equity} демо {len(book)}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Real-account bridge (шлёт только с --live)")
    parser.add_argument("--live", action="store_true",
                        help="единственный способ разрешить этому запуску тронуть реальные деньги")
    parser.add_argument("--config", default="",
                        help="реестр счетов; по умолчанию config/accounts.yaml")
    parser.add_argument("--interval-sec", type=int, default=10)
    parser.add_argument("--once", action="store_true", help="один цикл и выйти")
    args = parser.parse_args(argv)
    registry = pathlib.Path(args.config).resolve() if args.config else None

    def gate() -> str:
        ea = read_ea_state() or {}
        return livegate.refusal(ea.get("login"), live=args.live,
                                server=str(ea.get("server") or ""), config_path=registry)

    state = load_state()
    log({"event": "REAL BRIDGE START (file channel, EA in the real terminal)"})
    if not args.once:
        reason = gate()
        if reason:
            log({"event": f"стоп, ничего не отправлено: {reason}"})
            return 1
    while True:
        try:
            log({"event": run_cycle(state, gate)})
        except Exception as exc:  # noqa: BLE001 - the bridge must never die
            log({"event": f"cycle error {type(exc).__name__}: {exc}"})
        if args.once:
            return 0
        time.sleep(args.interval_sec)


if __name__ == "__main__":
    raise SystemExit(main())
