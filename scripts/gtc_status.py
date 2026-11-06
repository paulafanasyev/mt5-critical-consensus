"""One-line status of the real GTC micro account and the loop (tiny, for minute checks)."""
from __future__ import annotations

import json
import pathlib

import MetaTrader5 as mt5

ROOT = pathlib.Path(__file__).resolve().parents[1]
MICRO = ROOT / "agent_data" / "gtc_micro.jsonl"
TERMINAL = r"C:\Program Files\GTC Global SA MetaTrader 5\terminal64.exe"


def last_event_age() -> str:
    try:
        line = MICRO.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-1]
        stamp = json.loads(line)["ts"]
        from datetime import datetime, timezone

        when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - when).total_seconds()
        return f"{int(age)}s"
    except Exception:
        return "n/a"


if not mt5.initialize(path=TERMINAL, timeout=6000):
    print("API нет связи")
    raise SystemExit(1)
info = mt5.account_info()
positions = mt5.positions_get() or []
naked = sum(1 for p in positions if p.sl == 0.0)
pnl = sum(p.profit for p in positions)
detail = ", ".join(f"{p.symbol} {p.profit:+.2f}" for p in positions) or "позиций нет"
print(f"equity {info.equity:.2f} | свободно {info.margin_free:.2f} | {detail} | "
      f"без SL {naked} | петля {last_event_age()} назад")
mt5.shutdown()
