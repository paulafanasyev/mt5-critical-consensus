"""Test bootstrap: make the fleet importable, with or without the MetaTrader5 package.

The money tests must run on a machine that has no terminal installed (CI, a reviewer's laptop), so
when the real package is missing it is replaced by a stub that keeps module import cheap and makes
an accidental live call fail loudly instead of silently returning nothing.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import types

import pytest


def _stub_mt5() -> types.ModuleType:
    module = types.ModuleType("MetaTrader5")

    class _Unavailable:
        def __getattr__(self, name: str):
            raise RuntimeError(
                "MetaTrader5 is not installed: this code path needs a live terminal")

    for constant, value in {
        "TIMEFRAME_H1": 16385, "TIMEFRAME_H4": 16388, "TIMEFRAME_D1": 16408,
        "POSITION_TYPE_BUY": 0, "POSITION_TYPE_SELL": 1,
        "ORDER_TYPE_BUY": 0, "ORDER_TYPE_SELL": 1,
        "TRADE_ACTION_DEAL": 1, "TRADE_ACTION_SLTP": 6,
        "TRADE_RETCODE_DONE": 10009, "TRADE_RETCODE_DONE_PARTIAL": 10010,
        "TRADE_RETCODE_REQUOTE": 10004, "TRADE_RETCODE_REJECT": 10006,
        "TRADE_RETCODE_INVALID": 10013, "TRADE_RETCODE_INVALID_VOLUME": 10014,
        "TRADE_RETCODE_INVALID_PRICE": 10015, "TRADE_RETCODE_INVALID_STOPS": 10016,
        "TRADE_RETCODE_TRADE_DISABLED": 10017, "TRADE_RETCODE_MARKET_CLOSED": 10018,
        "TRADE_RETCODE_NO_MONEY": 10019, "TRADE_RETCODE_PRICE_CHANGED": 10020,
        "TRADE_RETCODE_PRICE_OFF": 10021, "TRADE_RETCODE_INVALID_EXPIRATION": 10022,
        "TRADE_RETCODE_ORDER_CHANGED": 10023, "TRADE_RETCODE_TOO_MANY_REQUESTS": 10024,
        "TRADE_RETCODE_NO_CHANGES": 10025, "TRADE_RETCODE_SERVER_DISABLES_AT": 10026,
        "TRADE_RETCODE_CLIENT_DISABLES_AT": 10027, "TRADE_RETCODE_LOCKED": 10028,
        "TRADE_RETCODE_FROZEN": 10029, "TRADE_RETCODE_CONNECTION": 10031,
        "TRADE_RETCODE_LIMIT_ORDERS": 10032, "TRADE_RETCODE_LIMIT_VOLUME": 10034,
        "TRADE_RETCODE_INVALID_FILL": 10030, "TRADE_RETCODE_ONLY_REAL": 10038,
        "TRADE_RETCODE_TIMEOUT": 10012,
        "ORDER_FILLING_FOK": 0, "ORDER_FILLING_IOC": 1, "ORDER_FILLING_RETURN": 2,
        "ORDER_TIME_GTC": 0,
        "ACCOUNT_TRADE_MODE_DEMO": 0, "ACCOUNT_TRADE_MODE_REAL": 2,
    }.items():
        setattr(module, constant, value)
    module.account_info = _Unavailable().__getattr__
    module.terminal_info = lambda *a, **k: None
    module.initialize = lambda *a, **k: False
    module.shutdown = lambda: None
    module.last_error = lambda: (-9999, "stub")
    module.symbols_get = lambda: []
    module.symbol_info = lambda *a, **k: None
    module.symbol_info_tick = lambda *a, **k: None
    module.symbol_select = lambda *a, **k: False
    module.positions_get = lambda *a, **k: []
    module.history_deals_get = lambda *a, **k: []
    module.copy_rates_from_pos = lambda *a, **k: None
    module.order_send = lambda *a, **k: None
    return module


if importlib.util.find_spec("MetaTrader5") is None:
    sys.modules["MetaTrader5"] = _stub_mt5()


_FLEET_SNAPSHOT: dict[str, float] = {}
_SHARD_SNAPSHOT: dict[str, str] = {}
_FLEET_OWNED = ("fleet.sqlite", "fleet.jsonl", "conductor.json", "conductor.lock", "PAUSE")
_BATTLE_STORE = r"C:\mt5-fleet\fleet.sqlite"


def _fleet_state(root) -> dict[str, float]:
    import pathlib as _pathlib

    state: dict[str, float] = {}
    base = _pathlib.Path(root)
    if not base.exists():
        return state
    for name in _FLEET_OWNED:
        target = base / name
        if target.exists():
            state[name] = target.stat().st_mtime
    terminals = base / "terminals"
    if terminals.exists():
        for item in terminals.iterdir():
            try:
                state[f"terminals/{item.name}"] = item.stat().st_mtime
            except OSError:
                continue
    return state


def _shards() -> dict[str, str]:
    """pid -> command line, for every python on this machine that is running a fleet.shard."""
    import subprocess

    script = ("Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
              "Where-Object { $_.CommandLine -like '*fleet.shard*' } | "
              "ForEach-Object { \"{0}|{1}\" -f $_.ProcessId, $_.CommandLine }")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                             capture_output=True, text=True, timeout=30).stdout
    except Exception:  # noqa: BLE001 - the guard must never break collection
        return {}
    rows: dict[str, str] = {}
    for line in out.splitlines():
        pid, _, command = line.partition("|")
        if pid.strip():
            rows[pid.strip()] = command.strip()
    return rows


def pytest_configure(config):
    import pathlib as _pathlib

    _FLEET_SNAPSHOT.update(_fleet_state(_pathlib.Path(r"C:\mt5-fleet")))
    _SHARD_SNAPSHOT.update(_shards())


@pytest.fixture(autouse=True)
def stand_is_a_temp_file(tmp_path, monkeypatch):
    """No test inherits this machine's kill switch, its registry, or its trading state.

    `Worker` reads the stand itself now, so a suite run on a parked machine would have refused every
    entry — and a suite that silently stops testing entries is the failure a reviewer cannot see. The
    same is true one layer down and one layer up: `livegate` answers about the stand and about the
    registry, and a gate test that inherits `config/accounts.yaml` is reading the operator's live
    account rows. `conductor.PAUSE_FILE` matters most of all, because `fleet_cli start` and `reset-day`
    unlink that file — a test that runs them here would take the production stop off the machine.

    Every one of these paths is bound at import, so each module is moved by its own name. The path
    goes to a file that is not on disk; a test that wants a stand or a registry makes one of its own,
    and its `monkeypatch` lands after this fixture and wins.
    """
    try:
        from fleet import conductor, livegate, shard, worker as fleet_worker
    except ImportError:  # a module that never puts the repo root on sys.path has no fleet to patch
        return
    stand = tmp_path / "STAND_NOT_ARMED"
    registry = tmp_path / "REGISTRY_EMPTY.yaml"
    monkeypatch.setattr(fleet_worker, "PAUSE_FILE", stand, raising=False)
    monkeypatch.setattr(conductor, "PAUSE_FILE", stand, raising=False)
    monkeypatch.setattr(shard, "PAUSE_FILE", stand, raising=False)
    monkeypatch.setattr(livegate, "PAUSE_FILE", stand, raising=False)
    monkeypatch.setattr(livegate, "CONFIG_PATH", registry, raising=False)


def pytest_sessionfinish(session, exitstatus):
    """A green suite must not leave trading processes behind, nor write into the battle journal.

    The park is now a resident of this machine, and that changes what each half of this guard can
    claim. A shard whose command line names `C:\\mt5-fleet\\fleet.sqlite` is the live park trading —
    not a leftover of this session — so a process counts against the suite only when it drives some
    other store and was not already running when the session started. The directory is the same
    problem one layer down: while the park runs, its journal's mtime moves every thirty seconds, so
    mtime proves nothing; the reading that stays true is the composition of the directory, and the
    journal files inside it. Losing `fleet.sqlite` or `fleet.jsonl` is fatal whatever else is running
    — that is the 2026-11-04 incident, and it happened to a script that thought it was reading.
    """
    live = _shards()
    ours = sorted(pid for pid, command in live.items()
                  if _BATTLE_STORE not in command and pid not in _SHARD_SNAPSHOT)
    if ours:
        raise AssertionError(f"живые fleet.shard процессы после suite: {ours}")
    park_live = any(_BATTLE_STORE in command for command in live.values())

    import pathlib as _pathlib

    after = _fleet_state(_pathlib.Path(r"C:\mt5-fleet"))
    missing = sorted(set(_FLEET_SNAPSHOT) - set(after))
    added = sorted(set(after) - set(_FLEET_SNAPSHOT))
    gone_journal = [name for name in missing if name in ("fleet.sqlite", "fleet.jsonl")]
    if gone_journal:
        raise AssertionError(rf"боевой журнал пропал во время suite: {gone_journal}")
    if (missing or added) and not park_live:
        raise AssertionError(rf"suite изменила состав боевого C:\mt5-fleet: "
                             f"нет {missing}, появилось {added}")

    touched = sorted(key for key in after if after[key] != _FLEET_SNAPSHOT.get(key))
    if not touched:
        return
    if missing or added:
        print(rf"\n[fleet guard] пока шла suite, состав C:\mt5-fleet изменил парк: "
              f"нет {missing}, появилось {added}")
    if park_live:
        print(rf"\n[fleet guard] живой парк ведёт журнал сам: {touched} — mtime не свидетельство")
    elif os.environ.get("FLEET_STRICT_GUARD") == "1":
        raise AssertionError(rf"suite изменила боевой C:\mt5-fleet: {touched}")
    else:
        print(rf"\n[fleet guard] suite изменила боевой C:\mt5-fleet: {touched} "
              r"(внешняя активность; strict-режим: FLEET_STRICT_GUARD=1)")
