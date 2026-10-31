from datetime import datetime, timedelta, timezone
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace

import pytest

from critical_consensus.market_data import MarketBar


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sync_mt5_market_data.py"
SPEC = spec_from_file_location("sync_mt5_market_data", SCRIPT)
MODULE = module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class FakeRepository:
    def __init__(self, existing):
        self.existing = existing
        self.migrated = False
        self.appended = []
        self.resolved = []

    def migrate(self):
        self.migrated = True

    def get_database_clock(self):
        return {
            "now_utc": datetime.now(timezone.utc),
            "session_timezone": "UTC",
        }

    def get_recent_market_bars(self, symbol, timeframe, limit):
        assert limit == 5000
        return self.existing

    def append_market_bars(self, bars):
        self.appended.extend(bars)
        return {"inserted": len(bars), "identical_duplicates": 0, "total": len(bars)}

    def resolve_due_market_forecasts(self, **kwargs):
        self.resolved.append(kwargs)
        return {"resolved": 0, "pending": 0}


def test_incremental_sync_with_no_new_bars_is_a_safe_noop(monkeypatch, capsys):
    latest = datetime.now(timezone.utc) - timedelta(hours=2)
    repo = FakeRepository([MarketBar(
        "EURUSD", "H1", latest, 1.0, 1.1, 0.9, 1.05,
    )])
    monkeypatch.setattr(MODULE, "PostgresRepository", lambda: repo)
    monkeypatch.setattr(MODULE, "fetch_mt5_bars", lambda *args, **kwargs: [])
    monkeypatch.setattr(MODULE, "retain_completed_bars", lambda bars, **kwargs: list(bars))
    monkeypatch.setattr("sys.argv", [
        "sync_mt5_market_data.py", "--symbol", "eurusd", "--timeframe", "H1",
    ])

    assert MODULE.main() == 0
    payload = __import__("json").loads(capsys.readouterr().out)
    assert repo.migrated is True
    assert repo.appended == []
    assert payload["received_bars"] == 0
    assert payload["ingestion"]["inserted"] == 0
    assert payload["clock"]["finalization_clock_source"] == "postgresql_server_clock"
    assert payload["clock"]["database_session_timezone"] == "UTC"
    assert payload["clock"]["application_database_skew_seconds"] >= 0
    assert len(repo.resolved) == 1
    assert repo.resolved[0]["symbol"] == "EURUSD"
    assert payload["trade_allowed"] is False


def test_mt5_outage_fails_closed_without_resolving_or_writing(monkeypatch):
    latest = datetime.now(timezone.utc) - timedelta(hours=2)
    repo = FakeRepository([MarketBar(
        "EURUSD", "H1", latest, 1.0, 1.1, 0.9, 1.05,
    )])
    monkeypatch.setattr(MODULE, "PostgresRepository", lambda: repo)

    def unavailable(*args, **kwargs):
        raise RuntimeError("MT5 terminal unavailable")

    monkeypatch.setattr(MODULE, "fetch_mt5_bars", unavailable)
    monkeypatch.setattr("sys.argv", [
        "sync_mt5_market_data.py", "--symbol", "EURUSD", "--timeframe", "H1",
    ])

    with pytest.raises(RuntimeError, match="MT5 terminal unavailable"):
        MODULE.main()
    assert repo.appended == []
    assert repo.resolved == []


def test_invalid_resolve_limit_is_rejected_before_database_access(monkeypatch):
    def unexpected_repository():
        raise AssertionError("invalid CLI input must be rejected before database access")

    monkeypatch.setattr(MODULE, "PostgresRepository", unexpected_repository)
    monkeypatch.setattr("sys.argv", [
        "sync_mt5_market_data.py", "--symbol", "EURUSD", "--timeframe", "H1",
        "--resolve-limit", "0",
    ])
    with pytest.raises(SystemExit) as error:
        MODULE.main()
    assert error.value.code == 2


def test_empty_symbol_is_rejected_before_database_access(monkeypatch):
    def unexpected_repository():
        raise AssertionError("empty symbol must be rejected before database access")

    monkeypatch.setattr(MODULE, "PostgresRepository", unexpected_repository)
    monkeypatch.setattr("sys.argv", [
        "sync_mt5_market_data.py", "--symbol", "   ", "--timeframe", "H1",
    ])
    with pytest.raises(SystemExit) as error:
        MODULE.main()
    assert error.value.code == 2


def test_parse_aware_normalizes_offsets_to_utc():
    parsed = MODULE.parse_aware("2026-10-31T10:00:00+07:00")
    assert parsed == datetime(2026, 10, 31, 3, 0, tzinfo=timezone.utc)



def test_sync_reports_potential_gap_but_does_not_backfill_automatically(monkeypatch, capsys):
    previous = MarketBar(
        "EURUSD", "H1", datetime(2026, 10, 26, 10, tzinfo=timezone.utc),
        1.0, 1.1, 0.9, 1.05,
    )
    last_persisted = MarketBar(
        "EURUSD", "H1", datetime(2026, 10, 26, 11, tzinfo=timezone.utc),
        1.05, 1.15, 0.95, 1.1,
    )
    next_bar = MarketBar(
        "EURUSD", "H1", datetime(2026, 10, 26, 13, tzinfo=timezone.utc),
        1.1, 1.2, 1.0, 1.15,
    )
    repo = FakeRepository([previous, last_persisted])
    monkeypatch.setattr(MODULE, "PostgresRepository", lambda: repo)
    monkeypatch.setattr(MODULE, "fetch_mt5_bars", lambda *args, **kwargs: [next_bar])
    monkeypatch.setattr(MODULE, "retain_completed_bars", lambda bars, **kwargs: list(bars))
    monkeypatch.setattr("sys.argv", [
        "sync_mt5_market_data.py", "--symbol", "EURUSD", "--timeframe", "H1",
    ])

    assert MODULE.main() == 0
    payload = __import__("json").loads(capsys.readouterr().out)
    integrity = payload["market_history_integrity"]
    assert integrity["bars_scanned"] == 3
    assert len(integrity["potential_gaps"]) == 1
    assert integrity["potential_gaps"][0]["status"] == "POTENTIAL_GAP_REQUIRES_SESSION_REVIEW"
    assert integrity["automatic_backfill"] is False
    assert repo.appended == [next_bar]



def test_weekend_gap_suppression_requires_and_honors_explicit_flag(monkeypatch, capsys):
    friday = MarketBar(
        "EURUSD", "H1", datetime(2026, 10, 23, 20, tzinfo=timezone.utc),
        1.0, 1.1, 0.9, 1.05,
    )
    monday = MarketBar(
        "EURUSD", "H1", datetime(2026, 10, 26, 0, tzinfo=timezone.utc),
        1.05, 1.15, 0.95, 1.1,
    )
    repo = FakeRepository([friday, monday])
    seen = []
    monkeypatch.setattr(MODULE, "PostgresRepository", lambda: repo)
    monkeypatch.setattr(MODULE, "fetch_mt5_bars", lambda *args, **kwargs: [])
    monkeypatch.setattr(MODULE, "retain_completed_bars", lambda bars, **kwargs: list(bars))

    def detector(bars, *, ignore_weekend_closures):
        seen.append(ignore_weekend_closures)
        return []

    monkeypatch.setattr(MODULE, "detect_potential_bar_gaps", detector)
    monkeypatch.setattr("sys.argv", [
        "sync_mt5_market_data.py", "--symbol", "EURUSD", "--timeframe", "H1",
        "--ignore-weekend-closures",
    ])

    assert MODULE.main() == 0
    payload = __import__("json").loads(capsys.readouterr().out)
    assert seen == [True]
    assert payload["market_history_integrity"]["weekend_gaps_ignored"] is True
