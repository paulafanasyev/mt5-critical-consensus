"""Open the same trades on the demo account and keep them: the operator wants both books in step.

The real mirror appends each executed trade to `demo_mirror_queue.jsonl`; this loop replays new
entries on the demo terminal and never closes them (so the demo keeps the position after the real
book takes its stop or target, exactly as the operator asked).
"""
from __future__ import annotations

import json
import pathlib
import time

import MetaTrader5 as mt5

ROOT = pathlib.Path(__file__).resolve().parents[1]
QUEUE = ROOT / "agent_data" / "demo_mirror_queue.jsonl"
CURSOR = ROOT / "agent_data" / "demo_mirror.cursor"
EVENTS = ROOT / "agent_data" / "demo_mirror.jsonl"
TERMINAL = r"C:\mt5-team\terminal64.exe"
DEMO_LOGIN = 114929502  # refuse to trade if the API answered from another account


def log(event: str, **fields) -> None:
    row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, **fields}
    with EVENTS.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def main() -> None:
    while True:
        try:
            if not QUEUE.exists():
                time.sleep(5)
                continue
            done = int(CURSOR.read_text().strip() or 0) if CURSOR.exists() else 0
            rows = [json.loads(line) for line in QUEUE.read_text(encoding="utf-8").splitlines() if line.strip()]
            fresh = rows[done:]
            if not fresh:
                time.sleep(5)
                continue
            if not mt5.initialize(path=TERMINAL, timeout=8000):
                log("init_failed", error=str(mt5.last_error()))
                time.sleep(10)
                continue
            info = mt5.account_info()
            if info is None or info.login != DEMO_LOGIN:
                log("wrong_account", login=None if info is None else info.login,
                    why="API ответил чужим счётом — демо-ордера не отправляю")
                mt5.shutdown()
                time.sleep(10)
                continue
            for order in fresh:
                mt5.symbol_select(order["symbol"], True)
                spec = mt5.symbol_info(order["symbol"])
                tick = mt5.symbol_info_tick(order["symbol"])
                res = mt5.order_send({"action": mt5.TRADE_ACTION_DEAL, "symbol": order["symbol"],
                                      "volume": order["lot"],
                                      "type": mt5.ORDER_TYPE_BUY if order["side"] == "buy" else mt5.ORDER_TYPE_SELL,
                                      "price": tick.ask if order["side"] == "buy" else tick.bid,
                                      "sl": order["sl"], "tp": order["tp"], "deviation": 30,
                                      "magic": 7000009, "comment": "demo-keep",
                                      "type_time": mt5.ORDER_TIME_GTC,
                                      "type_filling": mt5.ORDER_FILLING_IOC})
                log("demo_trade", symbol=order["symbol"], side=order["side"], lot=order["lot"],
                    retcode=None if res is None else res.retcode,
                    comment=None if res is None else res.comment)
            CURSOR.write_text(str(len(rows)), encoding="utf-8")
            mt5.shutdown()
        except Exception as exc:  # noqa: BLE001 - a bad queue line must not kill the mirror
            log("error", error=f"{type(exc).__name__}: {exc}")
        time.sleep(5)


if __name__ == "__main__":
    main()
