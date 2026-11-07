"""Shard process: trades one group of accounts, reloads the registry, and dies with its parent.

A shard never dies silently on a single bad cycle, never trades a stale registry, and never keeps
trading after the conductor that started it is gone. A registry it cannot read is treated as the
worst kind of stale: the shard stops cycling at all rather than spend money on the config it
loaded before the file broke.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import pathlib
import sys
import time

from . import worker
from .config import CONFIG_PATH, DEFAULT_STORE, PAUSE_FILE, load
from .store import Store


def _digest(path: pathlib.Path) -> str | None:
    """The registry's bytes, hashed: a version stamp that cannot miss a same-second edit.

    An mtime is a coarse clock. Two saves inside one tick leave the second one invisible, and the
    shard then trades the config it loaded before the operator changed the caps, the modes or the
    accounts — exactly the stale registry this module promises never to trade. The hash also says
    what a touch does not: identical bytes are the same registry, so a reload is not an event.
    """
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _parent_alive(pid: int) -> bool:
    if pid <= 0:
        return True
    import subprocess

    script = f"Get-Process -Id {pid} -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Id"
    result = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                            capture_output=True, text=True)
    return result.stdout.strip().isdigit()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="One fleet shard")
    parser.add_argument("--shard", type=int, required=True)
    parser.add_argument("--config", default="")
    parser.add_argument("--store", default=str(DEFAULT_STORE))
    parser.add_argument("--interval", type=int, default=90)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--live", action="store_true", help="разрешить реальные ордера")
    # The stand is not a flag an operator has to remember to pass. A shard started by hand obeys the
    # same file the conductor writes, so `PAUSE` on disk stops entries in every process on this box.
    parser.add_argument("--pause-file", default=str(PAUSE_FILE))
    parser.add_argument("--parent-pid", type=int, default=0)
    args = parser.parse_args(argv)

    # The registry this process watches must be the registry it trades. `load(None)` reads
    # CONFIG_PATH, so watching `None` because the operator used the documented `run --shards 0`
    # would leave the halt inert while a live file was edited under the shard.
    config_path = (pathlib.Path(args.config).resolve() if args.config
                   else CONFIG_PATH.resolve())
    # An empty `--pause-file` is the machine's stand, not the absence of one: a caller that clears the
    # flag must not get a process that trades while `PAUSE` sits on disk.
    pause = pathlib.Path(args.pause_file or PAUSE_FILE)
    store = Store(args.store)
    try:
        config = load(config_path)
    except Exception as exc:  # noqa: BLE001 - a shard that never started must say why out loud
        # Nothing was trusted and nothing will be traded, so this is not the mid-run case where the
        # process keeps cycling on a registry it can still prove: here the only honest answer is a
        # journal row, a sentence on stdout and a non-zero exit the conductor can see. A traceback
        # that dies in a window nobody watches is how a park goes quiet without saying so.
        store.event("shard", "config_rejected", shard=args.shard, path=str(config_path),
                    error=f"{type(exc).__name__}: {exc}"[:400])
        print(f"shard {args.shard}: реестр {config_path} не читается — {exc}; "
              "ни один счёт не торгован")
        return 2
    fingerprint = _digest(config_path)
    halted = ""  # why this shard will not trade; empty means the registry on disk is trusted

    while True:
        if args.parent_pid and not _parent_alive(args.parent_pid):
            store.event("shard", "parent_gone", shard=args.shard, parent=args.parent_pid)
            return 0
        stamp = _digest(config_path)
        if stamp != fingerprint:
            # The file changed under this process. Until the new one is readable, the config in
            # memory is a registry we know to be wrong: the accounts, caps or modes on disk are not
            # the ones these numbers describe, and trading them is the stale registry this module
            # promises never to trade. So: no openings from a registry nobody can vouch for.
            try:
                config = load(config_path)
            except Exception as exc:  # noqa: BLE001 - a bad registry stops entries, not the shard
                halted = f"{type(exc).__name__}: {exc}"[:400]
                store.event("shard", "config_rejected", shard=args.shard, path=str(config_path),
                            error=halted, entries="blocked",
                            exits="the last registry this process could read still manages its own "
                                  "lots: a file nobody can parse is not an order to leave a lot in "
                                  "the market")
            else:
                store.event("shard", "config_reloaded", shard=args.shard, recovered_from=halted)
                halted = ""
            fingerprint = stamp
        try:
            # `halted` closes the entry door only. Freezing the closer with it is the defect this
            # project removed from `worker.py` in 4027e77 after 8aa8db1 put it there: the lot does not
            # care that the file changed, and it is still the park's own money at risk.
            results = worker.run_shard(config, store, args.shard, dry_run=args.dry_run,
                                       pause_file=pause, allow_live=args.live,
                                       allow_entries=not halted)
            for result in results:
                store.event(result.account, "cycle", status=result.status,
                            detail=result.detail, opened=result.opened, closed=result.closed)
            if args.once:
                print(json.dumps([dataclasses.asdict(r) for r in results], ensure_ascii=False))
                return 0
        except Exception as exc:  # noqa: BLE001 - a shard survives any single cycle
            store.event("shard", "cycle_error", shard=args.shard, error=str(exc)[:400])
            time.sleep(15)
            continue
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
