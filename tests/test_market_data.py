from datetime import datetime, timezone, timedelta

import pytest

from critical_consensus.market_data import MarketBar, build_market_snapshot, read_mt5_csv, validate_bar_series


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
