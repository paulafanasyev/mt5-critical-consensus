"""Shard process: the unit the conductor starts once per group of accounts."""
from __future__ import annotations

import argparse
import dataclasses
import json
import pathlib
import sys
import time

from . import worker
from .config import load
from .store import Store


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="One fleet shard")
    parser.add_argument("--shard", type=int, required=True)
    parser.add_argument("--config", default="")
    parser.add_argument("--store", default=str(pathlib.Path(r"C:\mt5-fleet") / "fleet.sqlite"))
    parser.add_argument("--interval", type=int, default=90)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--pause-file", default="")
    args = parser.parse_args(argv)

    config = load(pathlib.Path(args.config) if args.config else None)
    store = Store(args.store)
    pause = pathlib.Path(args.pause_file) if args.pause_file else None
    while True:
        results = worker.run_shard(config, store, args.shard, dry_run=args.dry_run,
                                   pause_file=pause)
        for result in results:
            store.event(result.account, "cycle", status=result.status,
                        detail=result.detail, opened=result.opened, closed=result.closed)
        if args.once:
            print(json.dumps([dataclasses.asdict(r) for r in results], ensure_ascii=False))
            return 0
        if pause is not None and pause.exists():
            print("пауза: файл существует", flush=True)
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
