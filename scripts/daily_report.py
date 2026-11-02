#!/usr/bin/env python3
"""One daily report for every running scheme.

Collects the live state of each paper or demo stream in one place: funding carry, the
scenario board, the option strangles, the MT5 session rule and every deal on the broker
account. It is the monitoring page the operator asked for: what each scheme is doing, what
it has earned or lost, and whether it is still inside its own rules.

Read-only. Run it any time: python scripts/daily_report.py
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "agent_data"


def tail_jsonl(path: Path, count: int = 200) -> list[dict]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-count:]
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def main() -> int:
    print("=" * 78)
    print(f"ОТЧЁТ ПО ВСЕМ СХЕМАМ · {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 78)

    # 1. funding carry
    funding = tail_jsonl(DATA / "funding_carry_live.jsonl", 1)
    if funding:
        last = funding[-1]
        print(f"\nFUNDING CARRY (дельта-нейтрально, бумага)")
        print(f"  итог ${last.get('total_net', 0):+.2f} на ${last.get('capital', 0):.0f}")
        for symbol, data in last.get("assets", {}).items():
            print(f"  {symbol:9} ${data['net']:+.2f}  периодов {data['periods']}  "
                  f"базис {data['basis_bps']:+.1f} bps")
    else:
        print("\nFUNDING CARRY: нет данных")

    # 2. scenario board
    scenarios = tail_jsonl(DATA / "scenarios.jsonl", 1)
    if scenarios:
        board = scenarios[-1]
        print("\nДОСКА СЦЕНАРИЕВ")
        for name, value in board.get("scenarios", {}).items():
            if isinstance(value, dict) and "annualized_pct" in value:
                print(f"  {name:26} {value['annualized_pct']:+.2f}%/год")
            elif isinstance(value, dict) and "premium" in value:
                print(f"  {name:26} премия {value['premium']:+.2f} vol пункта")
            elif isinstance(value, dict) and name.startswith("S2"):
                parts = ", ".join(f"{k.replace('USDT','')} {v:+.1f}" for k, v in value.items())
                print(f"  {name:26} {parts}")
        if board.get("halted"):
            print(f"  остановлены стражем: {', '.join(board['halted'])}")

    # 3. option strangles
    positions = json.loads((DATA / "vol_paper_state.json").read_text(encoding="utf-8")) \
        if (DATA / "vol_paper_state.json").exists() else []
    marks = tail_jsonl(DATA / "vol_paper.jsonl", 2)
    if positions:
        print("\nОПЦИОНЫ (продажа стрэнглов, бумага)")
        for position in positions:
            realized = position.get("realized")
            print(f"  {position['currency']:4} exp {position['expiry']:9} "
                  f"credit {position['credit']:.4f}"
                  + (f"  рассчитано {realized:+.4f}" if realized is not None else ""))
        for mark in marks:
            print(f"  разметка {mark['currency']}: unrealized {mark['unrealized']:+.4f} "
                  f"({mark['days_left']} дн.)")

    # 4. broker account
    try:
        import MetaTrader5 as mt5
    except ImportError:
        mt5 = None
    if mt5 and mt5.initialize():
        try:
            account = mt5.account_info()
            print("\nMT5 ДЕМО-СЧЁТ")
            if account:
                print(f"  баланс {account.balance:.2f}  средства {account.equity:.2f}  "
                      f"маржа {account.margin:.2f}")
            positions = mt5.positions_get() or []
            print(f"  открытых позиций: {len(positions)}")
            for position in positions:
                side = "SELL" if position.type == 1 else "BUY"
                print(f"    {position.symbol} {side} {position.volume} @ {position.price_open} "
                      f"SL {position.sl} TP {position.tp} P/L {position.profit:+.2f} "
                      f"magic {position.magic}")
            deals = mt5.history_deals_get(datetime.now() - timedelta(days=1),
                                          datetime.now() + timedelta(days=1)) or []
            closed = [deal for deal in deals if deal.profit != 0]
            print(f"  сделок за сутки: {len(deals)}, с результатом: {len(closed)}, "
                  f"реализовано {sum(deal.profit for deal in closed):+.2f}")
        finally:
            mt5.shutdown()
    print("\n" + "=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
