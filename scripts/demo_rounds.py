"""Fresh 30-round scoreboard for the demo park, starting from a fixed marker."""
from __future__ import annotations

import json
import pathlib
from datetime import datetime

import MetaTrader5 as mt5

ROOT = pathlib.Path(__file__).resolve().parents[1]
MARK = ROOT / "agent_data" / "rounds.json"
TERMINAL = r"C:\mt5-team\terminal64.exe"


def main() -> int:
    if not mt5.initialize(path=TERMINAL, timeout=8000):
        print("API нет связи")
        return 1
    info = mt5.account_info()
    if MARK.exists():
        mark = json.loads(MARK.read_text(encoding="utf-8"))
    else:
        mark = {"started_at": datetime.now().isoformat(timespec="seconds"),
                "start_equity": round(info.equity, 2)}
        MARK.write_text(json.dumps(mark, ensure_ascii=False, indent=2), encoding="utf-8")
    started = datetime.fromisoformat(mark["started_at"])
    deals = mt5.history_deals_get(started, datetime.now()) or []
    closed = [d for d in deals if d.entry == 1]
    wins = [d for d in closed if d.profit > 0]
    losses = [d for d in closed if d.profit <= 0]
    gross_win = sum(d.profit for d in wins)
    gross_loss = abs(sum(d.profit for d in losses))
    pf = gross_win / gross_loss if gross_loss else (99.0 if gross_win else 0.0)
    print(f"СТАРТ РАУНДА: {mark['started_at']} | equity тогда {mark['start_equity']:.2f}")
    print(f"сейчас: equity {info.equity:.2f} ({info.equity - mark['start_equity']:+.2f})")
    print(f"закрыто {len(closed)}/30 | плюс {len(wins)} на {gross_win:+.2f} | "
          f"минус {len(losses)} на {-gross_loss:+.2f} | PF {pf:.2f}")
    if closed:
        print(f"винрейт {len(wins) / len(closed) * 100:.1f}% | "
              f"средний плюс {gross_win / len(wins) if wins else 0:+.2f} | "
              f"средний минус {-gross_loss / len(losses) if losses else 0:+.2f}")
    mt5.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
