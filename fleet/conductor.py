"""Conductor: keeps the shards alive, owns the book, and stops the park when it must.

One process, one lock, one machine's worth of shards: MT5 answers the Python API on a single port
per Windows session, so a conductor refuses to run two shard processes against the same channel
unless the operator explicitly asks for it. A pause stops new entries without ever stopping exits,
and removing the pause brings the park back without a manual restart.
"""
from __future__ import annotations

import json
import os
import pathlib
import socket
import subprocess
import sys
import time

from . import terminals
from .config import FLEET_ROOT, FleetConfig, load
from .store import Store

PAUSE_FILE = FLEET_ROOT / "PAUSE"
STATE_FILE = FLEET_ROOT / "conductor.json"
LOCK_FILE = FLEET_ROOT / "conductor.lock"
BACKOFF = (5, 10, 30, 60, 300)


class Conductor:
    def __init__(self, config: FleetConfig, store: Store, interval: int = 30,
                 dry_run: bool = False, python: str | None = None,
                 config_path: pathlib.Path | None = None,
                 shards: list[int] | None = None,
                 allow_multi_shard: bool = False, live: bool = False) -> None:
        self.config = config
        self.store = store
        self.interval = interval
        self.dry_run = dry_run
        self.python = python or sys.executable
        self.config_path = pathlib.Path(config_path).resolve() if config_path else None
        wanted = shards if shards else config.shards()
        if not allow_multi_shard and len(wanted) > 1:
            raise ValueError(
                "на одной машине допустим один шард: один порт API на сессию Windows. "
                f"Укажи --shards с одним номером из {config.shards()} "
                "или --allow-multi-shard, если счета не пересекаются по порту.")
        self.shards = wanted
        self.allow_multi_shard = allow_multi_shard
        self.live = live
        self.children: dict[int, subprocess.Popen] = {}
        self.day_start_equity: float | None = None
        self._paused = False
        self.restarts: dict[int, int] = {}
        self.next_restart: dict[int, float] = {}

    # --- lifecycle ----------------------------------------------------------------------
    def acquire_lock(self, force: bool = False) -> bool:
        """Publish the lock atomically: a reader can never see an empty half-written file."""
        FLEET_ROOT.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            payload = json.dumps({"pid": os.getpid(), "host": socket.gethostname()})
            tmp = LOCK_FILE.with_suffix(".lock.tmp")
            tmp.write_text(payload, encoding="utf-8")
            try:
                os.link(tmp, LOCK_FILE)
                tmp.unlink(missing_ok=True)
                return True
            except FileExistsError:
                tmp.unlink(missing_ok=True)
                try:
                    current = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    current = {}
                pid = int(current.get("pid", 0) or 0)
                if pid and _pid_alive(pid) and not force:
                    return False
                LOCK_FILE.unlink(missing_ok=True)
        return False

    def release_lock(self) -> None:
        try:
            payload = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = {}
        if int(payload.get("pid", 0) or 0) == os.getpid():
            LOCK_FILE.unlink(missing_ok=True)

    def start(self) -> None:
        FLEET_ROOT.mkdir(parents=True, exist_ok=True)
        for shard in self.shards:
            self._spawn(shard)
        self.store.event("conductor", "started", shards=self.shards,
                         accounts=len(self.config.enabled()), dry_run=self.dry_run)

    def _spawn(self, shard: int) -> None:
        command = [self.python, "-m", "fleet.shard", "--shard", str(shard),
                   "--interval", str(self.interval), "--pause-file", str(PAUSE_FILE),
                   "--store", str(self.store.path), "--parent-pid", str(os.getpid())]
        if self.config_path is not None:
            command.extend(["--config", str(self.config_path)])
        if self.dry_run:
            command.append("--dry-run")
        if self.live:
            command.append("--live")
        child = subprocess.Popen(command, cwd=str(pathlib.Path(__file__).resolve().parents[1]),
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.children[shard] = child
        self.store.event("conductor", "spawned", shard=shard, pid=child.pid)

    def stop(self) -> None:
        for shard, child in list(self.children.items()):
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                self.store.event("conductor", "stopped_shard", shard=shard)
        self.children.clear()
        terminals.stop_other_fleet()  # fleet terminals only: never someone else's terminal
        from . import network

        if any(a.network == "vpn" for a in self.config.enabled()):
            network.stop_tunnel()

    # --- supervision --------------------------------------------------------------------
    def tick(self) -> dict:
        if PAUSE_FILE.exists():
            self._hold_on_pause()
            self._reap_and_restart()  # a shard must be restarted even while entries are paused
        else:
            if self._paused:
                self._paused = False
                self.store.event("conductor", "resumed")
            self._reap_and_restart()
        book = self.store.portfolio([a.name for a in self.config.enabled()])
        book_stop = self.store.portfolio([a.name for a in self.config.enabled()],
                                         fresh_only=False)
        self._remember_baseline(book)
        reason = self._portfolio_stop(book_stop)
        if reason and not PAUSE_FILE.exists():
            PAUSE_FILE.write_text(reason, encoding="utf-8")
            self.store.event("conductor", "portfolio_stop", reason=reason, **book)
        state = {"book": book, "day_start_equity": self.day_start_equity,
                 "paused": PAUSE_FILE.exists(), "shards": sorted(self.children),
                 "restarts": self.restarts,
                 "positions_known": len(self.store.open_positions())}
        _atomic_write(STATE_FILE, json.dumps(state, ensure_ascii=False, indent=2))
        return state

    def _reap_and_restart(self) -> None:
        now = time.time()
        for shard in self.shards:
            child = self.children.get(shard)
            if child is not None and child.poll() is None:
                continue
            if child is not None:
                self.store.event("conductor", "shard_died", shard=shard, code=child.returncode)
                self.restarts[shard] = self.restarts.get(shard, 0) + 1
            count = self.restarts.get(shard, 0)
            if count:
                if now < self.next_restart.get(shard, 0):
                    continue
                delay = BACKOFF[min(count - 1, len(BACKOFF) - 1)]
                self.next_restart[shard] = now + delay
                if count > len(BACKOFF):
                    self.store.event("conductor", "alarm", shard=shard,
                                     reason="шард падает слишком часто", restarts=count)
                self._spawn(shard)
            else:
                self._spawn(shard)

    def _hold_on_pause(self) -> None:
        """Pause blocks new entries only: shards keep running so stops and targets still fire."""
        if self._paused:
            return
        self._paused = True
        self.store.event("conductor", "paused",
                         reason=PAUSE_FILE.read_text(encoding="utf-8")[:200])

    def _remember_baseline(self, book: dict) -> None:
        with self.store._connect() as conn:  # noqa: SLF001 - same package boundary
            row = conn.execute("SELECT MAX(server_day) FROM book").fetchone()
        today = (row[0] if row and row[0] else "") or ""
        stored_day = self.store.get_meta("portfolio_day")
        stored_value = self.store.get_meta("portfolio_day_start_equity")
        expected = sum(len(self.config.by_shard(shard)) for shard in self.shards)
        if stored_day == today and stored_value:
            try:
                self.day_start_equity = float(stored_value)
            except ValueError:
                self.day_start_equity = None
            return  # an existing baseline is authoritative even on a partial book
        if expected and book["accounts"] < expected:
            last_known = self.store.portfolio([a.name for a in self.config.enabled()],
                                              fresh_only=False)
            if last_known["equity"] > 0:
                self.store.set_meta("portfolio_day", today)
                self.store.set_meta("portfolio_day_start_equity",
                                    f"{last_known['equity']:.2f}")
                self.day_start_equity = last_known["equity"]
                self.store.event("conductor", "baseline_rebased", day=today,
                                 equity=round(last_known["equity"], 2),
                                 accounts=last_known["accounts"], expected=expected)
            return  # never invent a base from three fresh rows out of a hundred
        if stored_day != today or not stored_value:
            if book["equity"] > 0:
                self.store.set_meta("portfolio_day", today)
                self.store.set_meta("portfolio_day_start_equity", f"{book['equity']:.2f}")
                self.day_start_equity = book["equity"]
                self.store.event("conductor", "new_day", day=today,
                                 equity=round(book["equity"], 2))
            return
        try:
            self.day_start_equity = float(stored_value)
        except ValueError:
            self.day_start_equity = None

    def _portfolio_stop(self, book: dict) -> str:
        limits = self.config.portfolio
        expected = sum(len(self.config.by_shard(shard)) for shard in self.shards)
        if expected and book["accounts"] < expected:
            # Alarm, then judge on the equity we do see: silence here would disable the last line.
            if self.store.get_meta("book_incomplete") != "1":
                self.store.set_meta("book_incomplete", "1")
                self.store.event("conductor", "book_incomplete",
                                 accounts=book["accounts"], expected=expected)
        elif self.store.get_meta("book_incomplete") == "1":
            self.store.set_meta("book_incomplete", "0")
            self.store.event("conductor", "book_complete", accounts=book["accounts"])
        if self.day_start_equity and self.day_start_equity > 0 and book["equity"] > 0:
            change_pct = (book["equity"] - self.day_start_equity) / self.day_start_equity * 100
            if change_pct <= -limits.daily_stop_pct:
                return f"дневной стоп портфеля: {change_pct:.2f}% ({book['equity']:.2f} USD)"
        return ""

    def run_forever(self, force_lock: bool = False) -> None:
        if not self.acquire_lock(force=force_lock):
            raise RuntimeError(f"дирижёр уже работает: {LOCK_FILE}")
        self.start()
        self.store.event("conductor", "running", interval=self.interval)
        try:
            while True:
                self.tick()
                self.store.prune([a.name for a in self.config.enabled()])
                time.sleep(self.interval)
        except KeyboardInterrupt:
            self.store.event("conductor", "interrupted")
        finally:
            self.stop()
            self.release_lock()


def _pid_alive(pid: int) -> bool:
    script = (f"Get-Process -Id {pid} -ErrorAction SilentlyContinue | "
              "Select-Object -First 1 -ExpandProperty Id")
    result = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                            capture_output=True, text=True)
    return result.stdout.strip().isdigit()


def _atomic_write(path: pathlib.Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Fleet conductor")
    parser.add_argument("--config", default="")
    parser.add_argument("--store", default=str(FLEET_ROOT / "fleet.sqlite"))
    parser.add_argument("--interval", type=int, default=30)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--shards", default="", help="например 0 или 0,2")
    parser.add_argument("--allow-multi-shard", action="store_true")
    parser.add_argument("--force-lock", action="store_true", help="снять чужой/устаревший lock")
    args = parser.parse_args(argv)
    config_path = pathlib.Path(args.config).resolve() if args.config else None
    config = load(config_path)
    if not config.enabled():
        print("нет включённых счетов в config/accounts.yaml")
        return 1
    shards = [int(part) for part in args.shards.replace(" ", "").split(",") if part] or None
    if PAUSE_FILE.exists():
        print(f"файл паузы на месте ({PAUSE_FILE}); удали его или выполни resume")
        return 1
    engine = Conductor(config, Store(args.store), interval=args.interval,
                       dry_run=args.dry_run, config_path=config_path, shards=shards,
                       allow_multi_shard=args.allow_multi_shard)
    print(f"парк: {len(config.enabled())} счетов, шарды {engine.shards}")
    engine.run_forever(force_lock=args.force_lock)
    return 0


if __name__ == "__main__":
    sys.exit(main())
