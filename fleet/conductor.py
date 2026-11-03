"""Conductor: keeps one shard process per group of accounts alive and guards the whole book.

Workers trade individual accounts; the conductor owns what a worker cannot see — the aggregate
book. It watches the combined equity curve, trips a fleet-wide stop when the daily loss limit is
hit, restarts dead shards, and brings the network tunnel up for brokers that are blocked locally.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import time

from .config import FleetConfig, PortfolioLimits, load
from .store import Store

FLEET_ROOT = pathlib.Path(r"C:\mt5-fleet")
PAUSE_FILE = FLEET_ROOT / "PAUSE"
STATE_FILE = FLEET_ROOT / "conductor.json"


class Conductor:
    def __init__(self, config: FleetConfig, store: Store, interval: int = 30,
                 dry_run: bool = False, python: str | None = None) -> None:
        self.config = config
        self.store = store
        self.interval = interval
        self.dry_run = dry_run
        self.python = python or sys.executable
        self.children: dict[int, subprocess.Popen] = {}
        self.day_start_equity: float | None = None

    # --- lifecycle ----------------------------------------------------------------------
    def start(self) -> None:
        FLEET_ROOT.mkdir(parents=True, exist_ok=True)
        for shard in self.config.shards():
            self._spawn(shard)
        self.store.event("conductor", "started", shards=self.config.shards(),
                         accounts=len(self.config.enabled()))

    def _spawn(self, shard: int) -> None:
        command = [self.python, "-m", "fleet.shard", "--shard", str(shard),
                   "--interval", "90", "--pause-file", str(PAUSE_FILE)]
        if self.dry_run:
            command.append("--dry-run")
        child = subprocess.Popen(command, cwd=str(pathlib.Path(__file__).resolve().parents[1]),
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.children[shard] = child
        self.store.event("conductor", "spawned", shard=shard, pid=child.pid)

    def stop(self) -> None:
        for shard, child in self.children.items():
            if child.poll() is None:
                child.terminate()
                self.store.event("conductor", "stopped_shard", shard=shard)
        self.children.clear()

    # --- supervision --------------------------------------------------------------------
    def tick(self) -> dict:
        for shard, child in list(self.children.items()):
            if child.poll() is not None:
                self.store.event("conductor", "restart_shard", shard=shard,
                                 code=child.returncode)
                self._spawn(shard)
        book = self.store.portfolio()
        if self.day_start_equity is None and book["equity"] > 0:
            self.day_start_equity = book["equity"]
        reason = self._portfolio_stop(book)
        if reason:
            PAUSE_FILE.write_text(reason, encoding="utf-8")
            self.store.event("conductor", "portfolio_stop", reason=reason, **book)
        state = {"book": book, "day_start_equity": self.day_start_equity,
                 "paused": PAUSE_FILE.exists(), "shards": len(self.children)}
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        return state

    def _portfolio_stop(self, book: dict) -> str:
        limits = self.config.portfolio
        if self.day_start_equity and self.day_start_equity > 0 and book["equity"] > 0:
            change_pct = (book["equity"] - self.day_start_equity) / self.day_start_equity * 100
            if change_pct <= -limits.daily_stop_pct:
                return f"дневной стоп портфеля: {change_pct:.2f}%"
        return ""

    def run_forever(self) -> None:
        self.start()
        self.store.event("conductor", "running", interval=self.interval)
        try:
            while True:
                self.tick()
                time.sleep(self.interval)
        except KeyboardInterrupt:
            self.store.event("conductor", "interrupted")
        finally:
            self.stop()


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Fleet conductor")
    parser.add_argument("--config", default="")
    parser.add_argument("--store", default=str(FLEET_ROOT / "fleet.sqlite"))
    parser.add_argument("--interval", type=int, default=30)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    config = load(pathlib.Path(args.config) if args.config else None)
    if not config.enabled():
        print("нет включённых счетов в config/accounts.yaml")
        return 1
    if PAUSE_FILE.exists():
        print(f"файл паузы на месте ({PAUSE_FILE}); удали его, чтобы запустить")
        return 1
    conductor = Conductor(config, Store(args.store), interval=args.interval,
                          dry_run=args.dry_run)
    print(f"парк: {len(config.enabled())} счетов, шардов: {config.shards()}")
    conductor.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
