"""Shard process: trades one group of accounts, reloads the registry, and dies with its parent.

A shard never dies silently on a single bad cycle, never trades a stale registry, and never keeps
trading after the conductor that started it is gone.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import pathlib
import sys
import time

from . import worker
from .config import DEFAULT_STORE, load
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
    parser.add_argument("--pause-file", default="")
    parser.add_argument("--parent-pid", type=int, default=0)
    args = parser.parse_args(argv)

    config_path = pathlib.Path(args.config).resolve() if args.config else None
    pause = pathlib.Path(args.pause_file) if args.pause_file else None
    store = Store(args.store)
    config = load(config_path)
    fingerprint = config_path.stat().st_mtime if config_path and config_path.exists() else 0.0

    while True:
        if args.parent_pid and not _parent_alive(args.parent_pid):
            store.event("shard", "parent_gone", shard=args.shard, parent=args.parent_pid)
            return 0
        try:
            if config_path and config_path.exists():
                stamp = config_path.stat().st_mtime
                if stamp != fingerprint:
                    config = load(config_path)
                    fingerprint = stamp
                    store.event("shard", "config_reloaded", shard=args.shard)
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
