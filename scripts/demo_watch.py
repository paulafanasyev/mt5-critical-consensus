"""Compact watch line: processes, money, positions, stop risk, time to the weekly close."""
from __future__ import annotations

import subprocess
from datetime import datetime, timedelta

import MetaTrader5 as mt5

TERMINAL = r"C:\mt5-team\terminal64.exe"
STOP_BUDGET = 5.0


def process_count(fragment: str) -> int:
    command = (
        "(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
        f"Where-Object {{ $_.CommandLine -like '*{fragment}*' }} | Measure-Object).Count"
    )
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", command],
                             capture_output=True, text=True, timeout=20)
        return int(out.stdout.strip() or 0)
    except Exception:
        return -1


def main() -> None:
    counts = {name: process_count(name) for name in ("agent_swarm", "swarm_supervisor", "demo_net")}
    if not mt5.initialize(path=TERMINAL, timeout=8000):
        print("API нет связи:", mt5.last_error())
        return
    info = mt5.account_info()
    positions = mt5.positions_get() or []
    risky = []
    for p in positions:
        spec = mt5.symbol_info(p.symbol)
        per_unit = spec.trade_contract_size * p.volume if spec else 0.0
        risk = abs(p.price_open - p.sl) * per_unit if p.sl else 0.0
        risky.append(f"{p.symbol} {p.volume} ри$ {risk:.2f} pnl {p.profit:+.2f}")
    naked = sum(1 for p in positions if p.sl == 0.0)
    print("процессы: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
    print(f"баланс {info.balance:.2f} | equity {info.equity:.2f} | позиций {len(positions)} | без стопа {naked}")
    for line in risky:
        print("  " + line)
    # weekly close: gold stops trading Friday 23:55 server time (server = local + 2h here)
    now = datetime.now()
    days_to_friday = (4 - now.weekday()) % 7
    close_at = (now + timedelta(days=days_to_friday)).replace(hour=23, minute=55, second=0, microsecond=0)
    if close_at < now:
        close_at += timedelta(days=7)
    minutes = int((close_at - now).total_seconds() // 60)
    print(f"до закрытия недели ~{minutes // 60} ч {minutes % 60} мин (локально)")
    mt5.shutdown()


if __name__ == "__main__":
    main()
