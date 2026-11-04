"""Conductor: keeps the shards alive, owns the book, and stops the park when it must.

One process, one lock, one machine's worth of shards. Several shards run at once because each
terminal has its own channel: a shard drives its own accounts' terminals and never needs another
shard's window, so stopping the conductor terminates its shard children and leaves every terminal
alone — a terminal that is killed mid-cycle stops protecting the positions it holds.
A pause stops new entries without ever stopping exits, and removing the pause brings the park back
without a manual restart.
"""
from __future__ import annotations

import json
import os
import pathlib
import socket
import subprocess
import sys
import time

from .config import FLEET_ROOT, FleetConfig, local_host, load
from .store import Store

PAUSE_FILE = FLEET_ROOT / "PAUSE"
STATE_FILE = FLEET_ROOT / "conductor.json"
LOCK_FILE = FLEET_ROOT / "conductor.lock"
BACKOFF = (5, 10, 30, 60, 300)
# A dark shard re-announces itself this often. An hour would write two hundred identical events
# over a quiet weekend, and the operator who has to read the trail does not need that.
DAY_HOLD_REANNOUNCE_SECONDS = 6 * 3600


def parse_shards(text: str, known: list[int]) -> list[int]:
    """Turn "--shards 0,2" into a deduplicated list, refusing ids nobody would answer to.

    A typo or a shard pinned to another host would otherwise start a conductor that watches an
    empty set of children and looks healthy while trading nothing.
    """
    if not text.strip():
        return []
    wanted: list[int] = []
    for part in text.replace(" ", "").split(","):
        if not part:
            continue
        try:
            shard = int(part)
        except ValueError:
            raise ValueError(f"неверный shard {part!r}: ожидалось целое число") from None
        if shard not in wanted:
            wanted.append(shard)
    unknown = [s for s in wanted if s not in known]
    if unknown:
        raise ValueError(f"shard {unknown} нет среди счетов этой машины; доступны {known or 'ни одного'}")
    return wanted


class Conductor:
    # A shard process owns a group of terminals, and a terminal is a GUI process: the wall is the
    # Windows desktop heap, not the MetaTrader API. Eight is a deliberate default, not a discovery.
    MAX_SHARDS = 8

    def __init__(self, config: FleetConfig, store: Store, interval: int = 30,
                 dry_run: bool = False, python: str | None = None,
                 config_path: pathlib.Path | None = None,
                 shards: list[int] | None = None, live: bool = False,
                 max_shards: int | None = None) -> None:
        self.config = config
        self.store = store
        self.interval = interval
        self.dry_run = dry_run
        self.python = python or sys.executable
        self.config_path = pathlib.Path(config_path).resolve() if config_path else None
        wanted = shards if shards else config.shards()
        ceiling = self.MAX_SHARDS if max_shards is None else max_shards
        if len(wanted) > ceiling:
            raise ValueError(
                f"слишком много шардов на одной машине: {len(wanted)} > {ceiling}: каждый шард "
                "держит свои терминалы как GUI-процессы, потолок — рабочий стол Windows, не API. "
                "Подними --max-shards, если точно потянешь.")
        self.shards = wanted
        self.live = live
        self.children: dict[int, subprocess.Popen] = {}
        self.day_start_equity: float | None = None
        self.day_held: dict | None = None
        self._paused = False
        self.restarts: dict[int, int] = {}
        self.next_restart: dict[int, float] = {}

    # --- lifecycle ----------------------------------------------------------------------
    def acquire_lock(self, force: bool = False) -> bool:
        """Publish the lock atomically: a reader can never see an empty half-written file."""
        FLEET_ROOT.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            payload = json.dumps({"pid": os.getpid(), "host": socket.gethostname()})
            # One shared temp name would let a second racer overwrite ours between write and link,
            # publishing a lock that names the other pid — which the real owner then never releases.
            tmp = FLEET_ROOT / f".conductor.{os.getpid()}.{os.urandom(4).hex()}.lock.tmp"
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
        local = [a for a in self.config.enabled() if self.config.runs_here(a)]
        remote = sorted(a.name for a in self.config.enabled() if not self.config.runs_here(a))
        self.store.event("conductor", "started", shards=self.shards,
                         accounts=len(local), dry_run=self.dry_run)
        if remote:
            # The book — and with it the portfolio daily stop — is the one on THIS machine.
            # An operator must be able to see that the limit is machine-local, not fleet-wide.
            self.store.event("conductor", "portfolio_local_only",
                             local=len(local), remote=remote[:20], remote_count=len(remote))

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
        from . import network

        # Only a tunnel this machine was asked to raise: shards elsewhere keep their own VPN.
        if any(a.network == "vpn" for a in self.config.enabled() if self.config.runs_here(a)):
            killed = network.stop_tunnel()
            if killed:
                self.store.event("conductor", "tunnel_stopped", sessions=killed)

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
        mine = [a.name for a in self.config.local()]
        book = self.store.portfolio(mine)
        book_stop = self.store.portfolio(mine, fresh_only=False)
        self._remember_baseline(book)
        if book_stop.get("unpriced", 0) > 0:
            if self.store.get_meta("unpriced_warned") != "1":
                self.store.set_meta("unpriced_warned", "1")
                self.store.event("conductor", "unpriced_accounts",
                                 count=book_stop["unpriced"])
        elif self.store.get_meta("unpriced_warned") == "1":
            self.store.set_meta("unpriced_warned", "0")
            self.store.event("conductor", "unpriced_resolved")
        reason = self._portfolio_stop(book_stop)
        if reason and not PAUSE_FILE.exists():
            PAUSE_FILE.write_text(reason, encoding="utf-8")
            self.store.event("conductor", "portfolio_stop", reason=reason, **book)
        state = {"book": book, "day_start_equity": self.day_start_equity,
                 "paused": PAUSE_FILE.exists(), "shards": sorted(self.children),
                 "restarts": self.restarts, "day_held": self.day_held,
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
        host = local_host()
        # Only the shards this process actually spawns get refreshed by it. Held the day against
        # every local account, a park started with `--shards 0` would watch shard 1's rows go dark
        # and hold the day for good: correct arithmetic, and a book that never trades again.
        watched = sorted({a.name for shard in self.shards for a in self.config.by_shard(shard)})
        today = self.store.live_server_day(watched)
        stored_day, stored_value = self.store.claim_portfolio_baseline(host)
        if stored_day and today < stored_day:
            # Brokers keep different clocks (GMT+0 and GMT+3 sit a day apart late in the evening).
            # The portfolio day therefore never rolls back: a shard going dark must not rebase the
            # baseline onto equity that is already down, which would silently re-arm the daily stop.
            today = stored_day
        dark = sorted(set(watched) - set(self.store.fresh_accounts(watched)))
        held_key = f"portfolio_day_held:{host}"
        since_key = f"portfolio_day_held_since:{host}"
        held = self.store.get_meta(held_key) or ""
        if dark and stored_day and today > stored_day:
            # The same hazard in the other direction: MIN is only the slowest live clock if every
            # clock is live. With the slowest account dark, the remaining brokers look like they
            # crossed midnight early, the day rolls forward, and the baseline is taken from equity
            # that already lost yesterday's damage. The day waits, and the stop stays armed.
            now = time.time()
            last = float(self.store.get_meta(since_key) or 0.0)
            if "|".join(dark) != held or now - last >= DAY_HOLD_REANNOUNCE_SECONDS:
                self.store.set_meta(held_key, "|".join(dark))
                self.store.set_meta(since_key, f"{now:.0f}")
                self.store.event("conductor", "portfolio_day_held", day=stored_day, dark=dark,
                                 held_seconds=round(now - last) if last else 0)
            self.day_held = {"day": stored_day, "dark": dark,
                             "held_seconds": round(now - last) if last else 0}
            today = stored_day
        elif held and not dark:
            # Nothing was withheld, so there is nothing to announce: an all-dark book (a weekend)
            # must not read as a resume it never earned.
            self.store.set_meta(held_key, "")
            self.store.set_meta(since_key, "")
            self.store.event("conductor", "portfolio_day_resumed", day=today)
            self.day_held = None
        else:
            self.day_held = None
        expected = sum(len(self.config.by_shard(shard)) for shard in self.shards)
        registry = self._registry_hash()
        if self.store.portfolio_registry_hash(host) != registry:
            base = self._adjusted_equity(book) if book["equity"] > 0 else 0.0
            if base > 0:
                self.store.set_portfolio_registry_hash(host, registry)
                self.store.set_portfolio_baseline(host, today, base)
                self.day_start_equity = base
                self.store.event("conductor", "baseline_rebased", day=today,
                                 equity=round(base, 2), registry=registry)
            return
        if stored_day == today and stored_value:
            try:
                self.day_start_equity = float(stored_value)
            except ValueError:
                self.day_start_equity = None
            return  # an existing baseline is authoritative even on a partial book
        if expected and book["accounts"] < expected:
            last_known = self.store.portfolio([a.name for a in self.config.local()],
                                              fresh_only=False)
            adjusted = self._adjusted_equity(last_known)
            if adjusted > 0:
                self.store.set_portfolio_baseline(host, today, adjusted)
                self.day_start_equity = adjusted
                self.store.event("conductor", "baseline_rebased", day=today,
                                 equity=round(last_known["equity"], 2),
                                 accounts=last_known["accounts"], expected=expected)
            return  # never invent a base from three fresh rows out of a hundred
        if stored_day != today or not stored_value:
            adjusted = self._adjusted_equity(book)
            if adjusted > 0:
                self.store.set_portfolio_baseline(host, today, adjusted)
                self.day_start_equity = adjusted
                self.store.event("conductor", "new_day", day=today,
                                 equity=round(adjusted, 2))
            return
        try:
            self.day_start_equity = float(stored_value)
        except ValueError:
            self.day_start_equity = None

    def _book_flows(self) -> tuple[float, float]:
        """Sum today's deposits/withdrawals and realized P&L across this machine's accounts."""
        flow = realized = 0.0
        with self.store._connect() as conn:  # noqa: SLF001 - same package boundary
            rows = conn.execute(
                "SELECT account, server_day, balance, equity_usd FROM book").fetchall()
            for account, day, balance, equity_usd in rows:
                if account not in {a.name for a in self.config.local()}:
                    continue
                if equity_usd is None:
                    continue  # unpriced: not in equity, so its flow must not move adjusted
                row = conn.execute("SELECT value FROM meta WHERE key=?",
                                   (f"dayflows:{account}:{day}",)).fetchone()
                if not row:
                    continue
                parts = row[0].split("|")
                if len(parts) < 4:
                    continue
                try:
                    flow += float(parts[0])
                    realized += float(parts[1])
                    witness = float(parts[2])
                except ValueError:
                    continue
                if abs(witness - float(balance or 0.0)) > 0.01:  # sub-cent balances exist
                    # The payload belongs to a different balance than this book row: skip it,
                    # the next cycle will see both settled.
                    flow -= float(parts[0])
                    realized -= float(parts[1])
        return flow, realized

    def _registry_hash(self) -> str:
        """The composition of THIS machine's book.

        Hashing the whole registry would re-base the day onto today's equity every time another
        box's account was added or removed — and a re-base is exactly what lets yesterday's loss
        become tomorrow's starting money.
        """
        import hashlib

        names = sorted(a.name for a in self.config.local())
        return hashlib.sha1(",".join(names).encode()).hexdigest()[:12]

    def _adjusted_equity(self, book: dict) -> float:
        """Equity net of deposits/withdrawals only.

        Realized P&L is already inside equity, so subtracting it again would hide every closed
        loss from the portfolio stop — the exact failure the stop exists to prevent.
        """
        flow, _realized = self._book_flows()
        return book["equity"] - flow

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
            adjusted = self._adjusted_equity(book)
            change_pct = (adjusted - self.day_start_equity) / self.day_start_equity * 100
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
    parser.add_argument("--max-shards", type=int, default=None,
                        help=f"потолок шардов на машине (по умолчанию {Conductor.MAX_SHARDS})")
    parser.add_argument("--force-lock", action="store_true", help="снять чужой/устаревший lock")
    args = parser.parse_args(argv)
    config_path = pathlib.Path(args.config).resolve() if args.config else None
    config = load(config_path)
    if not config.enabled():
        print("нет включённых счетов в config/accounts.yaml")
        return 1
    shards = parse_shards(args.shards, config.shards()) or None
    if PAUSE_FILE.exists():
        print(f"файл паузы на месте ({PAUSE_FILE}); удали его или выполни resume")
        return 1
    engine = Conductor(config, Store(args.store), interval=args.interval,
                       dry_run=args.dry_run, config_path=config_path, shards=shards,
                       max_shards=args.max_shards)
    local = [a for a in config.enabled() if config.runs_here(a)]
    if not local:
        print("на этой машине нет ни одного счёта: все закреплены за другими host")
        return 1
    print(f"парк: {len(local)} счетов на этой машине, шарды {engine.shards}")
    engine.run_forever(force_lock=args.force_lock)
    return 0


if __name__ == "__main__":
    sys.exit(main())
