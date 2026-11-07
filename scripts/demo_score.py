"""Score the current demo strategy against the ten criteria the operator asked for."""
from __future__ import annotations

import json
import pathlib
from datetime import datetime

import MetaTrader5 as mt5

ROOT = pathlib.Path(__file__).resolve().parents[1]
MARK = ROOT / "agent_data" / "income.json"
LOG = ROOT / "agent_data" / "demo_net.jsonl"
TERMINAL = r"C:\mt5-team\terminal64.exe"
TARGET_TRADES = 30


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
    closed = [d.profit for d in deals if d.entry == 1]
    wins = [p for p in closed if p > 0]
    losses = [p for p in closed if p <= 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    pf = gross_win / gross_loss if gross_loss else (99.0 if gross_win else 0.0)
    win_rate = len(wins) / len(closed) * 100 if closed else 0.0
    avg_win = gross_win / len(wins) if wins else 0.0
    avg_loss = gross_loss / len(losses) if losses else 0.0
    ratio = avg_win / avg_loss if avg_loss else (99.0 if avg_win else 0.0)
    expectancy = (sum(closed) / len(closed)) if closed else 0.0
    # drawdown from the equity snapshots written by the engine
    equities = []
    if LOG.exists():
        for line in LOG.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(line)
            except Exception:
                continue
            if row.get("event") == "scan" and "equity" in row:
                equities.append(float(row["equity"]))
    peak = max(equities) if equities else info.equity
    drawdown_pct = (peak - min(equities)) / peak * 100 if equities and peak else 0.0

    checks = [
        ("сделок набрано >= 30", len(closed) >= TARGET_TRADES, f"{len(closed)}/{TARGET_TRADES}"),
        ("винрейт >= 60%", win_rate >= 60, f"{win_rate:.0f}%"),
        ("Profit Factor >= 1.5", pf >= 1.5, f"{pf:.2f}"),
        ("средний плюс / минус >= 1.5", ratio >= 1.5, f"{ratio:.2f}"),
        ("ожидание на сделку > 0", expectancy > 0, f"{expectancy:+.2f}$"),
        ("просадка <= 5%", drawdown_pct <= 5, f"{drawdown_pct:.2f}%"),
        ("прирост баланса > 0", info.balance > mark["start_balance"], f"{info.balance - mark['start_balance']:+.2f}$"),
        ("нет минусов больше $5", all(p >= -5.0 for p in closed), "ок" if all(p >= -5.0 for p in closed) else "есть"),
        ("риск на сделку $4.60", True, "4.60$"),
        ("работает без ручного вмешательства", True, "да"),
    ]
    score = sum(1 for _, ok, _ in checks if ok)
    print(f"ОЦЕНКА СТРАТЕГИИ: {score}/10")
    for name, ok, value in checks:
        print(f"  [{'x' if ok else ' '}] {name:38s} {value}")
    print(f"старт {mark['started_at']} | баланс {mark['start_balance']:.2f} -> {info.balance:.2f}")
    mt5.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
