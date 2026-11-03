"""Unit tests for the code-first fleet: registry, sizing, storage and parsing.

These run without a terminal so CI stays green on machines with no MT5 installed.
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fleet import config, store  # noqa: E402
from fleet.broker import BrokerSymbol, normalize_volume  # noqa: E402


def test_registry_parses_accounts(tmp_path: pathlib.Path) -> None:
    target = tmp_path / "accounts.yaml"
    source = ROOT / "config" / "accounts.example.yaml"
    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    fleet = config.load(target)
    assert len(fleet.accounts) == 3
    exness = fleet.by_name("exness-real")
    assert exness is not None
    assert exness.login == 276261159
    assert exness.server == "Exness-MT5Real26"
    assert exness.is_real
    assert exness.risk.max_lots == 0.01
    assert exness.risk.daily_stop_pct == 0.5
    assert exness.symbols == ("GBPUSD", "EURUSD", "XAUUSD")
    assert exness.strategy.kinds == ("trend",)
    assert fleet.portfolio.daily_stop_pct == 2.0
    assert fleet.enabled()  # демо включён, реальные выключены


def test_shards_and_paths(tmp_path: pathlib.Path) -> None:
    fleet = config.load()
    demo = fleet.by_name("demo-metaquotes")
    assert demo is not None
    assert demo.data_dir.name.startswith("114929502-")
    assert demo.terminal_exe.name == "terminal64.exe"


def test_volume_normalization_respects_step_and_cap() -> None:
    spec = BrokerSymbol(name="EURUSD", digits=5, point=0.00001, tick_size=0.00001,
                        tick_value=1.0, volume_min=0.01, volume_max=100.0,
                        volume_step=0.01, stops_level=0, freeze_level=0, spread=10, filling=1)
    assert normalize_volume(0.004, spec, 0.10) == 0.01
    assert normalize_volume(0.017, spec, 0.10) == 0.02
    assert normalize_volume(5.0, spec, 0.10) == 0.10
    assert normalize_volume(0.5, spec, 0.005) == 0.0


def test_store_roundtrip(tmp_path: pathlib.Path) -> None:
    db = store.Store(tmp_path / "fleet.sqlite")
    db.equity("demo", 1, 100.0, 101.0, 2, 1.0)
    db.event("demo", "opened", symbol="EURUSD")
    db.save_day("demo", "2026-11-03", 100.0, 0.5, False)
    book = db.portfolio()
    assert book["equity"] == 101.0
    assert book["accounts"] == 1.0
    day = db.day("demo")
    assert day is not None and day[1] == 100.0 and day[3] is False
    assert db.recent_events(1)[0][2] == "opened"
    assert (tmp_path / "fleet.jsonl").exists()


def test_scalar_parsing() -> None:
    assert config._scalar("42") == 42
    assert config._scalar("0.5") == 0.5
    assert config._scalar("true") is True
    assert config._scalar("[EURUSD, GBPUSD]") == ["EURUSD", "GBPUSD"]
    assert config._scalar("Exness-MT5Real26") == "Exness-MT5Real26"
