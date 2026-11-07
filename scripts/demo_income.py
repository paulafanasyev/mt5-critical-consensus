"""Income growth since a fixed point: balance, equity, and the trades behind the change."""
from __future__ import annotations

import json
import pathlib
from datetime import datetime

import MetaTrader5 as mt5

ROOT = pathlib.Path(__file__).resolve().parents[1]
MARK = ROOT / "agent_data" / "income.json"
TERMINAL = r"C:\mt5-team\terminal64.exe"


def main() -> int:
    if not mt5.initialize(path=TERMINAL, timeout=8000):
        print("API нет связи:", mt5.last_error())
        return 1
    info = mt5.account_info()
    if MARK.exists():
        mark = json.loads(MARK.read_text(encoding="utf-8"))
    else:
        mark = {"started_at": datetime.now().isoformat(timespec="seconds"),
                "start_balance": round(info.balance, 2)}
        MARK.write_text(json.dumps(mark, ensure_ascii=False, indent=2), encoding="utf-8")
    started = datetime.fromisoformat(mark["started_at"])
    deals = mt5.history_deals_get(started, datetime.now()) or []
    closed = [d for d in deals if d.entry == 1]
    wins = [d for d in closed if d.profit > 0]
    losses = [d for d in closed if d.profit <= 0]
    gross_win = sum(d.profit for d in wins)
    gross_loss = abs(sum(d.profit for d in losses))
    pf = gross_win / gross_loss if gross_loss else (99.0 if gross_win else 0.0)
    growth = info.balance - mark["start_balance"]
    print(f"точка отсчёта: {mark['started_at']} | баланс тогда {mark['start_balance']:.2f}")
    print(f"сейчас: баланс {info.balance:.2f} | equity {info.equity:.2f}")
    print(f"ПРИРОСТ: {growth:+.2f} ({growth / mark['start_balance'] * 100:+.3f}%)")
    print(f"сделок закрыто {len(closed)} | плюс {len(wins)} на {gross_win:+.2f} | "
          f"минус {len(losses)} на {-gross_loss:+.2f} | PF {pf:.2f}")
    if closed:
        print(f"винрейт {len(wins) / len(closed) * 100:.0f}% | "
              f"средний плюс {gross_win / len(wins) if wins else 0:+.2f} | "
              f"средний минус {-gross_loss / len(losses) if losses else 0:+.2f}")
    positions = mt5.positions_get() or []
    print(f"открыто сейчас: {len(positions)} | плавающий {sum(p.profit for p in positions):+.2f}")
    mt5.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
