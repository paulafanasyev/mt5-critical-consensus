"""One-screen status of the metals demo lab: money, positions, per-tactic results."""
from __future__ import annotations

import json
import pathlib
from collections import defaultdict

import MetaTrader5 as mt5

ROOT = pathlib.Path(__file__).resolve().parents[1]
LOG = ROOT / "agent_data" / "demo_net.jsonl"
TERMINAL = r"C:\mt5-team\terminal64.exe"


def main() -> None:
    entries = []
    exits = []
    if LOG.exists():
        for line in LOG.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(line)
            except Exception:
                continue
            if row.get("event") == "entry":
                entries.append(row)
            elif row.get("event") == "exit" and row.get("retcode") in (10009, None):
                exits.append(row)
    by_tactic: dict[str, list[float]] = defaultdict(list)
    for row in entries:
        by_tactic[row.get("tactic", "?")].append(0.0)
    if not mt5.initialize(path=TERMINAL, timeout=8000):
        print("API нет связи")
        return
    info = mt5.account_info()
    positions = mt5.positions_get() or []
    naked = sum(1 for p in positions if p.sl == 0.0)
    metals = [p for p in positions if p.symbol.startswith(("XAU", "XAG"))]
    print(f"equity {info.equity:.2f} | позиций {len(positions)} (металлы {len(metals)}, без стопа {naked}) | "
          f"плавающий {sum(p.profit for p in positions):+.2f}")
    profits = [row.get("profit", 0.0) for row in exits]
    wins = [p for p in profits if p > 0]
    losses = [p for p in profits if p <= 0]
    print(f"закрыто {len(profits)} | плюс {len(wins)} на {sum(wins):+.2f} | минус {len(losses)} на {sum(losses):+.2f}")
    tactic_rows = []
    for row in exits:
        tactic_rows.append((row.get("symbol", "?"), row.get("profit", 0.0)))
    print("входов по тактикам: " + ", ".join(f"{k}={len(v)}" for k, v in sorted(by_tactic.items())))
    for symbol, profit in tactic_rows[-6:]:
        print(f"  {symbol}: {profit:+.2f}")
    mt5.shutdown()


if __name__ == "__main__":
    main()
