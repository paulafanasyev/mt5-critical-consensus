"""Shard process: trades one group of accounts, reloads the registry, and dies with its parent.

A shard never dies silently on a single bad cycle, never trades a stale registry, and never keeps
trading after the conductor that started it is gone. A registry it cannot read is treated as the
worst kind of stale: the shard stops cycling at all rather than spend money on the config it
loaded before the file broke.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import pathlib
import sys
import time

from . import worker
from .config import CONFIG_PATH, DEFAULT_STORE, PAUSE_FILE, load
from .store import Store


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
    pause = pathlib.Path(args.pause_file) if args.pause_file else None
    store = Store(args.store)
    config = load(config_path)
    fingerprint = config_path.stat().st_mtime if config_path and config_path.exists() else 0.0
    halted = ""  # why this shard will not trade; empty means the registry on disk is trusted

    while True:
        if args.parent_pid and not _parent_alive(args.parent_pid):
            store.event("shard", "parent_gone", shard=args.shard, parent=args.parent_pid)
            return 0
        stamp = config_path.stat().st_mtime if config_path and config_path.exists() else None
        if config_path and stamp != fingerprint:
            # The file changed under this process. Until the new one is readable, the config in
            # memory is a registry we know to be wrong: the accounts, caps or modes on disk are not
            # the ones these numbers describe, and trading them is the stale registry this module
            # promises never to trade. Halt, say it once per broken version, wait for a good one.
            try:
                config = load(config_path)
            except Exception as exc:  # noqa: BLE001 - a bad registry stops entries, not the shard
                halted = f"{type(exc).__name__}: {exc}"[:400]
                store.event("shard", "config_rejected", shard=args.shard, path=str(config_path),
                            error=halted)
            else:
                store.event("shard", "config_reloaded", shard=args.shard, recovered_from=halted)
                halted = ""
            fingerprint = stamp
        if halted:
            time.sleep(args.interval)
            continue
        try:
            results = worker.run_shard(config, store, args.shard, dry_run=args.dry_run,
                                       pause_file=pause, allow_live=args.live)
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
