"""Regression tests for the defects the architecture critic proved by running the code.

Each test reproduces a bug that shipped once: a book computed from one random account, shards
launched with the wrong registry, a pause that respawned shards forever, and inline YAML comments
swallowed into values.
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fleet import conductor, config  # noqa: E402
from fleet.store import Store  # noqa: E402


# --- 1. портфель обязан суммировать книгу, а не брать один счёт ---------------------------

def test_portfolio_sums_every_account(tmp_path: pathlib.Path) -> None:
    store = Store(tmp_path / "fleet.sqlite")
    store.equity("a", 1, 1000.0, 1010.0, 1, 10.0)
    store.equity("b", 2, 2000.0, 2020.0, 2, 20.0)
    book = store.portfolio()
    assert book["balance"] == 3000.0
    assert book["equity"] == 3030.0
    assert book["positions"] == 3.0
    assert book["floating"] == 30.0
    assert book["accounts"] == 2.0
    rows = store.latest_equity()
    assert [row[0] for row in rows] == ["a", "b"]


def test_portfolio_uses_latest_row_per_account(tmp_path: pathlib.Path) -> None:
    store = Store(tmp_path / "fleet.sqlite")
    store.equity("a", 1, 1000.0, 1000.0, 0, 0.0)
    store.equity("a", 1, 1000.0, 900.0, 1, -100.0)
    book = store.portfolio()
    assert book["equity"] == 900.0
    positions = store.open_positions()
    assert positions == []  # nothing written yet, but aggregation must not invent rows


# --- 2. дирижёр передаёт шардам свой реестр и хранилище ------------------------------------

def test_conductor_passes_config_and_store(tmp_path: pathlib.Path, monkeypatch) -> None:
    cfg = config.FleetConfig()
    store = Store(tmp_path / "book.sqlite")
    captured: list[list[str]] = []

    class FakePopen:
        pid = 1

        def __init__(self, command, **kwargs):
            captured.append(command)

        def poll(self):
            return 0

    monkeypatch.setattr(conductor.subprocess, "Popen", FakePopen)
    engine = conductor.Conductor(cfg, store, config_path=tmp_path / "accounts.yaml")
    engine._spawn(0)
    command = captured[0]
    assert "--store" in command and str(store.path) in command
    assert "--config" in command and str(tmp_path / "accounts.yaml") in command


# --- 3. пауза останавливает парк и не перезапускает шарды ----------------------------------

def test_pause_stops_shards_instead_of_respawning(tmp_path: pathlib.Path, monkeypatch) -> None:
    monkeypatch.setattr(conductor, "PAUSE_FILE", tmp_path / "PAUSE")
    monkeypatch.setattr(conductor, "STATE_FILE", tmp_path / "conductor.json")
    store = Store(tmp_path / "book.sqlite")
    spawned: list[list[str]] = []

    class FakePopen:
        pid = 4242

        def __init__(self, command, **kwargs):
            spawned.append(command)
            self.terminated = False

        def poll(self):
            return None if not self.terminated else 0

        def terminate(self):
            self.terminated = True

    monkeypatch.setattr(conductor.subprocess, "Popen", FakePopen)
    engine = conductor.Conductor(config.FleetConfig(), store)
    engine._spawn(0)
    conductor.PAUSE_FILE.write_text("стоп", encoding="utf-8")
    state = engine.tick()
    assert state["paused"] is True
    assert engine.children == {}
    spawned_before = len(spawned)
    engine.tick()
    engine.tick()
    assert len(spawned) == spawned_before  # пауза не плодит респавны


# --- 4. инлайн-комментарии в YAML не попадают в значения ----------------------------------

def test_inline_yaml_comment_is_stripped(tmp_path: pathlib.Path) -> None:
    target = tmp_path / "accounts.yaml"
    target.write_text(
        "accounts:\n"
        "  - name: demo\n"
        "    login: 1\n"
        "    server: Demo-Server\n"
        "    network: direct   # или vpn, если брокер блокируется\n"
        "    seed_config: C:\\data\\cfg  # сохранённая сессия\n"
        "    symbols: [EURUSD, GBPUSD]\n",
        encoding="utf-8")
    fleet = config.load(target)
    account = fleet.by_name("demo")
    assert account is not None
    assert account.network == "direct"
    assert account.seed_config == r"C:\data\cfg"
    assert account.symbols == ("EURUSD", "GBPUSD")
