from datetime import datetime, timezone, timedelta

import pytest

from critical_consensus.market_data import (MarketBar, build_market_snapshot, build_next_bar_forecast_contract,\n                                            read_mt5_csv, retain_completed_bars, validate_bar_series)


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
