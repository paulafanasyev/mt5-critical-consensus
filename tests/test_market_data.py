from datetime import datetime, timezone, timedelta
from types import SimpleNamespace

import pytest

import critical_consensus.market_data as market_data

from critical_consensus.market_data import (
    MarketBar, build_market_snapshot, build_next_bar_forecast_contract,
    detect_potential_bar_gaps, read_mt5_csv, retain_completed_bars, validate_bar_series,
)
from critical_consensus.persistence import PostgresRepository


def bars(n=25):
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    output = []
    for i in range(n):
        close = 1.0 + i * 0.001
        output.append(MarketBar("EURUSD", "H1", start + timedelta(hours=i),
                                close - 0.0002, close + 0.0005, close - 0.0005,
                                close, tick_volume=100 + i, spread_points=10))
    return output


def test_snapshot_is_descriptive_and_deterministic():
    snapshot = build_market_snapshot(bars())
    assert snapshot["symbol"] == "EURUSD"
    assert snapshot["timeframe"] == "H1"
    assert snapshot["bar_count"] == 25
    assert snapshot["return_20"] > 0
    assert snapshot["atr_14"] > 0
    assert "not a calibrated forecast" in snapshot["evidence_note"]


def test_bar_series_rejects_duplicate_and_out_of_order_times():
    data = bars()
    with pytest.raises(ValueError, match="strictly increasing"):
        validate_bar_series([data[0], data[0]])
    with pytest.raises(ValueError, match="strictly increasing"):
        validate_bar_series([data[1], data[0]])


def test_bar_rejects_impossible_ohlc_and_nonfinite_values():
    stamp = datetime(2026, 10, 1, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="high"):
        MarketBar("EURUSD", "H1", stamp, 1.2, 1.1, 1.0, 1.15)
    with pytest.raises(ValueError, match="finite"):
        MarketBar("EURUSD", "H1", stamp, 1.0, float("nan"), 0.9, 0.95)


def test_naive_mt5_export_requires_explicit_server_timezone(tmp_path):
    path = tmp_path / "mt5.csv"
    path.write_text("Date,Time,Open,High,Low,Close,TickVol,Volume,Spread\n"
                    "2026.10.01,10:00:00,1.0,1.1,0.9,1.05,100,0,10\n", encoding="utf-8")
    with pytest.raises(ValueError, match="explicit source_timezone"):
        read_mt5_csv(path, symbol="EURUSD", timeframe="H1")
    loaded = read_mt5_csv(path, symbol="EURUSD", timeframe="H1", source_timezone="UTC")
    assert loaded[0].time == datetime(2026, 10, 1, 10, tzinfo=timezone.utc)


def test_mt5_export_rejects_duplicate_timestamps(tmp_path):
    path = tmp_path / "mt5.csv"
    path.write_text("Date,Time,Open,High,Low,Close\n"
                    "2026.10.01,10:00:00,1.0,1.1,0.9,1.05\n"
                    "2026.10.01,10:00:00,1.0,1.1,0.9,1.05\n", encoding="utf-8")
    with pytest.raises(ValueError, match="strictly increasing"):
        read_mt5_csv(path, symbol="EURUSD", timeframe="H1", source_timezone="UTC")


def test_forecast_snapshot_excludes_a_still_forming_bar():
    data = bars(25)
    now = data[-1].time + timedelta(hours=1, seconds=1)
    closed = retain_completed_bars(data, now=now, finalization_grace_seconds=2)
    assert closed[-1].time == data[-2].time
    assert len(closed) == 24


def test_forecast_snapshot_includes_bar_after_finalization_grace():
    data = bars(25)
    now = data[-1].time + timedelta(hours=1, seconds=3)
    closed = retain_completed_bars(data, now=now, finalization_grace_seconds=2)
    assert closed[-1].time == data[-1].time


def test_objective_forecast_contract_binds_symbol_timeframe_and_reference_close():
    snapshot = build_market_snapshot(bars(25))
    contract = build_next_bar_forecast_contract(snapshot)
    assert contract["target_event"] == "NEXT_AVAILABLE_BAR_CLOSE_ABOVE_REFERENCE"
    assert contract["target_policy_version"] == "next-available-bar-close-above-reference-v1"
    assert contract["symbol"] == "EURUSD"
    assert contract["timeframe"] == "H1"
    assert contract["reference_at"] == snapshot["as_of"]
    assert contract["reference_close"] == snapshot["last_close"]


def test_objective_forecast_contract_rejects_unsupported_monthly_timeframe():
    snapshot = build_market_snapshot(bars(25))
    snapshot["timeframe"] = "MN1"
    with pytest.raises(ValueError, match="does not support this timeframe"):
        build_next_bar_forecast_contract(snapshot)




def test_persistence_rejects_a_still_forming_market_bar_before_database_access():
    def unexpected_connect(_dsn):
        raise AssertionError("forming bars must be rejected before database access")

    repo = PostgresRepository("postgresql://unused/unused", connect_fn=unexpected_connect)
    forming = MarketBar(
        "EURUSD", "H1", datetime.now(timezone.utc) - timedelta(minutes=30),
        1.0, 1.2, 0.9, 1.1,
    )
    with pytest.raises(ValueError, match="insufficient completed bars"):
        repo.append_market_bars([forming])



def test_forecast_rejects_reference_bar_not_finalized_before_issue_without_database():
    def unexpected_connect(_dsn):
        raise AssertionError("reference timing must be validated before database access")

    repo = PostgresRepository("postgresql://unused/unused", connect_fn=unexpected_connect)
    issued = datetime(2026, 10, 2, 10, 0, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="reference market bar must be fully finalized"):
        repo.record_forecasts([{
            "model_id": "model-a", "signal_id": "signal-a",
            "issued_at": issued, "probability": 0.61,
            "input_sha256": "sha256:" + "a" * 64,
            "forecast_kind": "NEXT_BAR_MARKET_EVENT",
            "target_policy_version": "next-available-bar-close-above-reference-v1",
            "target_event": "NEXT_AVAILABLE_BAR_CLOSE_ABOVE_REFERENCE",
            "symbol": "EURUSD", "timeframe": "H1",
            "reference_at": issued - timedelta(hours=1),
            "reference_close": 1.1,
        }])





def test_live_mt5_adapter_uses_utc_read_only_calls_and_always_shuts_down(monkeypatch):
    calls = []
    fake = SimpleNamespace(
        TIMEFRAME_H1=60,
        initialize=lambda **kwargs: calls.append(("initialize", kwargs)) or True,
        terminal_info=lambda: SimpleNamespace(connected=True),
        symbol_select=lambda symbol, enabled: calls.append(("symbol_select", symbol, enabled)) or True,
        copy_rates_range=lambda symbol, timeframe, start, end: (
            calls.append(("copy_rates_range", symbol, timeframe, start, end)) or [
                {"time": 1790848800, "open": 1.0, "high": 1.2, "low": 0.9,
                 "close": 1.1, "tick_volume": 100, "spread": 8},
                {"time": 1790852400, "open": 1.1, "high": 1.3, "low": 1.0,
                 "close": 1.2, "tick_volume": 110, "spread": 9},
            ]
        ),
        shutdown=lambda: calls.append(("shutdown",)),
        last_error=lambda: (-1, "mock error"),
    )
    monkeypatch.setattr(market_data.importlib, "import_module", lambda name: fake)
    start = datetime(2026, 10, 31, 0, tzinfo=timezone.utc)
    end = datetime(2026, 10, 31, 3, tzinfo=timezone.utc)
    result = market_data.fetch_mt5_bars("eurusd", "H1", start, end, terminal_path="C:/MT5/terminal64.exe")

    assert len(result) == 2
    assert result[0].symbol == "EURUSD"
    assert result[0].source == "MetaTrader5.copy_rates_range"
    assert ("initialize", {"path": "C:/MT5/terminal64.exe"}) in calls
    request = next(call for call in calls if call[0] == "copy_rates_range")
    assert request[1:3] == ("EURUSD", 60)
    assert request[3].utcoffset().total_seconds() == 0
    assert request[4].utcoffset().total_seconds() == 0
    assert calls[-1] == ("shutdown",)
    assert not any("order" in name.lower() for name, *_ in calls)


def test_live_mt5_adapter_shuts_down_when_rate_fetch_fails(monkeypatch):
    calls = []
    fake = SimpleNamespace(
        TIMEFRAME_M1=1,
        initialize=lambda **kwargs: True,
        terminal_info=lambda: SimpleNamespace(connected=True),
        symbol_select=lambda symbol, enabled: True,
        copy_rates_range=lambda *args: None,
        shutdown=lambda: calls.append("shutdown"),
        last_error=lambda: (-2, "history unavailable"),
    )
    monkeypatch.setattr(market_data.importlib, "import_module", lambda name: fake)
    with pytest.raises(RuntimeError, match="copy_rates_range failed"):
        market_data.fetch_mt5_bars(
            "EURUSD", "M1",
            datetime(2026, 10, 31, tzinfo=timezone.utc),
            datetime(2026, 11, 1, tzinfo=timezone.utc),
        )
    assert calls == ["shutdown"]





def test_live_mt5_adapter_shutdowns_after_initialize_failure(monkeypatch):
    calls = []
    fake = SimpleNamespace(
        initialize=lambda **kwargs: False,
        shutdown=lambda: calls.append("shutdown"),
        last_error=lambda: (-10004, "no connection"),
    )
    monkeypatch.setattr(market_data.importlib, "import_module", lambda name: fake)
    with pytest.raises(RuntimeError, match="initialize failed"):
        market_data.fetch_mt5_bars(
            "EURUSD", "M1",
            datetime(2026, 10, 31, tzinfo=timezone.utc),
            datetime(2026, 11, 1, tzinfo=timezone.utc),
        )
    assert calls == ["shutdown"]


def test_live_mt5_adapter_rechecks_terminal_connection_before_reading(monkeypatch):
    calls = []
    fake = SimpleNamespace(
        initialize=lambda **kwargs: True,
        terminal_info=lambda: SimpleNamespace(connected=False),
        symbol_select=lambda *args: calls.append("symbol_select") or True,
        shutdown=lambda: calls.append("shutdown"),
        last_error=lambda: (-10004, "disconnected"),
    )
    monkeypatch.setattr(market_data.importlib, "import_module", lambda name: fake)
    with pytest.raises(RuntimeError, match="not connected"):
        market_data.fetch_mt5_bars(
            "EURUSD", "M1",
            datetime(2026, 10, 31, tzinfo=timezone.utc),
            datetime(2026, 11, 1, tzinfo=timezone.utc),
        )
    assert "symbol_select" not in calls
    assert calls == ["shutdown"]


def test_potential_gap_detector_flags_weekday_missing_interval():
    data = [
        MarketBar("EURUSD", "H1", datetime(2026, 10, 26, 10, tzinfo=timezone.utc),
                  1.0, 1.1, 0.9, 1.05),
        MarketBar("EURUSD", "H1", datetime(2026, 10, 26, 11, tzinfo=timezone.utc),
                  1.05, 1.15, 0.95, 1.1),
        MarketBar("EURUSD", "H1", datetime(2026, 10, 26, 13, tzinfo=timezone.utc),
                  1.1, 1.2, 1.0, 1.15),
    ]
    gaps = detect_potential_bar_gaps(data)
    assert len(gaps) == 1
    assert gaps[0]["previous_bar_utc"] == data[1].time.isoformat()
    assert gaps[0]["next_bar_utc"] == data[2].time.isoformat()
    assert gaps[0]["nominal_periods_elapsed"] == 2
    assert gaps[0]["status"] == "POTENTIAL_GAP_REQUIRES_SESSION_REVIEW"


def test_potential_gap_detector_ignores_weekend_market_closure():
    data = [
        MarketBar("EURUSD", "H1", datetime(2026, 10, 30, 21, tzinfo=timezone.utc),
                  1.0, 1.1, 0.9, 1.05),
        MarketBar("EURUSD", "H1", datetime(2026, 11, 2, 0, tzinfo=timezone.utc),
                  1.05, 1.15, 0.95, 1.1),
    ]
    assert detect_potential_bar_gaps(data) == []
    assert len(detect_potential_bar_gaps(data, ignore_weekend_closures=False)) == 1


def test_potential_gap_detector_rejects_invalid_tolerance():
    with pytest.raises(ValueError, match="gap_tolerance_seconds"):
        detect_potential_bar_gaps(bars(2), gap_tolerance_seconds=float("nan"))
