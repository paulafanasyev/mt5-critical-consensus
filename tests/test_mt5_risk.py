from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from critical_consensus.mt5_risk import collect_mt5_risk_snapshot
from critical_consensus.risk_controls import risk_request_binding_hash


NOW = datetime(2026, 10, 30, 1, 0, tzinfo=timezone.utc)


def proposed_request():
    return {
        "instrument": "EURUSD", "direction": "BUY", "size": 0.1,
        "expected_price": 1.1001, "sl_tp": {"sl": 1.0991, "tp": 1.1020},
    }


class FakeMT5:
    ACCOUNT_TRADE_MODE_DEMO = 0
    ACCOUNT_TRADE_MODE_CONTEST = 1

    def __init__(self, *, trade_mode=0, positions=None, deals=None, fail_positions=False):
        self.info = SimpleNamespace(trade_mode=trade_mode, equity=10100.0, balance=10000.0,
                                    server="Demo-Server", currency="USD")
        self.positions = positions or [SimpleNamespace(symbol="EURUSD")]
        self.deals = deals or [SimpleNamespace(profit=10.0, commission=-1.0, swap=0.0, fee=0.0)]
        self.spec = SimpleNamespace(trade_tick_size=0.00001, trade_tick_value_loss=1.0,
                                    trade_tick_value=1.0, point=0.00001)
        self.tick = SimpleNamespace(bid=1.1000, ask=1.1001, time=int(NOW.timestamp()))
        self.fail_positions = fail_positions
        self.shutdown_called = False

    def initialize(self, **kwargs):
        return True

    def account_info(self):
        return self.info

    def symbol_select(self, symbol, selected):
        return symbol == "EURUSD" and selected is True

    def symbol_info(self, symbol):
        return self.spec if symbol == "EURUSD" else None

    def symbol_info_tick(self, symbol):
        return self.tick if symbol == "EURUSD" else None

    def positions_get(self):
        if self.fail_positions:
            return None
        return self.positions

    def history_deals_get(self, start, end):
        return self.deals

    def shutdown(self):
        self.shutdown_called = True

    def last_error(self):
        return (0, "OK")


def test_read_only_demo_risk_snapshot_computes_broker_tick_value_risk(monkeypatch):
    fake = FakeMT5()
    monkeypatch.setitem(__import__("sys").modules, "MetaTrader5", fake)
    request = proposed_request()
    result = collect_mt5_risk_snapshot(
        symbol="EURUSD", day_start_equity=10000.0, equity_high_watermark=10200.0,
        proposed_request=request, symbol_groups={"EURUSD": "fx-major"}, now=NOW,
    )
    assert result["status"] == "PASS"
    assert result["account_mode"] == "demo"
    assert result["daily_pnl"] == 9.0
    assert result["daily_loss_fraction"] == 0.0
    assert result["risk_fraction"] == pytest.approx(10.0 / 10100.0)
    assert result["spread_points"] == pytest.approx(10.0)
    assert result["max_spread_points"] == 20
    assert result["request_binding_sha256"] == risk_request_binding_hash(request)
    assert result["open_positions"] == 1
    assert result["correlated_open_positions"] == 1
    assert result["trade_allowed"] is False
    assert fake.shutdown_called


def test_real_account_and_missing_server_snapshot_fail_closed(monkeypatch):
    fake = FakeMT5(trade_mode=2)
    monkeypatch.setitem(__import__("sys").modules, "MetaTrader5", fake)
    result = collect_mt5_risk_snapshot(
        symbol="EURUSD", day_start_equity=10000.0, equity_high_watermark=10200.0,
        proposed_request=proposed_request(), symbol_groups={"EURUSD": "fx-major"}, now=NOW,
    )
    assert result["status"] == "DENY"
    assert "MT5_ACCOUNT_NOT_DEMO" in result["blockers"]
    fake.fail_positions = True
    result = collect_mt5_risk_snapshot(
        symbol="EURUSD", day_start_equity=10000.0, equity_high_watermark=10200.0,
        proposed_request=proposed_request(), symbol_groups={"EURUSD": "fx-major"}, now=NOW,
    )
    assert result["status"] == "DENY"
    assert "MT5_POSITIONS_UNAVAILABLE" in result["blockers"]


def test_missing_equity_baselines_blocks_before_mt5_initializes(monkeypatch):
    fake = FakeMT5()
    monkeypatch.setitem(__import__("sys").modules, "MetaTrader5", fake)
    result = collect_mt5_risk_snapshot(
        symbol="EURUSD", day_start_equity=None, equity_high_watermark=None,
        proposed_request=proposed_request(), symbol_groups={"EURUSD": "fx-major"},
    )
    assert result["status"] == "DENY"
    assert "DAY_START_EQUITY_MISSING_OR_INVALID" in result["blockers"]
    assert not fake.shutdown_called


def test_daily_loss_uses_equity_change_and_includes_open_position_loss(monkeypatch):
    fake = FakeMT5()
    fake.info.equity = 9500.0
    fake.deals = [SimpleNamespace(profit=100.0, commission=0.0, swap=0.0, fee=0.0)]
    monkeypatch.setitem(__import__("sys").modules, "MetaTrader5", fake)
    result = collect_mt5_risk_snapshot(
        symbol="EURUSD", day_start_equity=10000.0, equity_high_watermark=10200.0,
        proposed_request=proposed_request(), symbol_groups={"EURUSD": "fx-major"}, now=NOW,
    )
    # Realised P/L is positive in the fixture but equity is down 5%; the equity-loss signal wins.
    assert result["daily_pnl"] == 100.0
    assert result["daily_loss_fraction"] == 0.05


def test_missing_group_for_an_open_position_blocks_snapshot(monkeypatch):
    fake = FakeMT5(positions=[SimpleNamespace(symbol="USDJPY")])
    monkeypatch.setitem(__import__("sys").modules, "MetaTrader5", fake)
    result = collect_mt5_risk_snapshot(
        symbol="EURUSD", day_start_equity=10000.0, equity_high_watermark=10200.0,
        proposed_request=proposed_request(), symbol_groups={"EURUSD": "fx-major"}, now=NOW,
    )
    assert result["status"] == "DENY"
    assert "OPEN_POSITION_GROUP_UNKNOWN" in result["blockers"]

def test_stale_mt5_tick_blocks_risk_snapshot(monkeypatch):
    fake = FakeMT5()
    fake.tick.time = int((NOW.timestamp()) - 60)
    monkeypatch.setitem(__import__("sys").modules, "MetaTrader5", fake)
    result = collect_mt5_risk_snapshot(
        symbol="EURUSD", day_start_equity=10000.0, equity_high_watermark=10200.0,
        proposed_request=proposed_request(), symbol_groups={"EURUSD": "fx-major"}, now=NOW,
    )
    assert result["status"] == "DENY"
    assert "MT5_QUOTE_STALE" in result["blockers"]



def test_wide_live_spread_blocks_risk_snapshot(monkeypatch):
    fake = FakeMT5()
    fake.tick.ask = 1.1010
    monkeypatch.setitem(__import__("sys").modules, "MetaTrader5", fake)
    result = collect_mt5_risk_snapshot(
        symbol="EURUSD", day_start_equity=10000.0, equity_high_watermark=10200.0,
        proposed_request=proposed_request(), symbol_groups={"EURUSD": "fx-major"}, now=NOW,
        max_spread_points=20,
    )
    assert result["status"] == "DENY"
    assert "MT5_SPREAD_LIMIT_EXCEEDED" in result["blockers"]
    assert result["spread_points"] == pytest.approx(100.0)


# Persistence readback integrity: DB metadata and JSON payload must remain bound.
import hashlib
import json

from critical_consensus.persistence import PostgresRepository


def _snapshot_digest(snapshot):
    encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False, default=str)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class _RiskSnapshotCursor:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class _RiskSnapshotConnection:
    def __init__(self, row):
        self.row = row

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, query, args=None):
        assert "FROM mt5_risk_snapshots" in query
        assert args == ("EURUSD",)
        return _RiskSnapshotCursor(self.row)


def _risk_snapshot_repository(row):
    return PostgresRepository(
        "postgresql://unused",
        connect_fn=lambda _dsn: _RiskSnapshotConnection(row),
    )


def _risk_snapshot_payload():
    return {
        "observed_at": "2026-10-30T10:30:00+00:00",
        "account_mode": "demo",
        "symbol": "EURUSD",
        "status": "DENY",
        "trade_allowed": False,
        "blockers": ["TEST_FIXTURE"],
    }


def test_latest_risk_snapshot_verifies_hash_and_database_columns():
    snapshot = _risk_snapshot_payload()
    digest = _snapshot_digest(snapshot)
    row = (datetime(2026, 10, 30, 10, 30, tzinfo=timezone.utc),
           "demo", "EURUSD", digest, snapshot)

    result = _risk_snapshot_repository(row).get_latest_mt5_risk_snapshot("eurusd")

    assert result["snapshot_sha256"] == digest
    assert result["status"] == "DENY"
    assert result["trade_allowed"] is False


def test_latest_risk_snapshot_rejects_tampered_json_payload():
    snapshot = _risk_snapshot_payload()
    digest = _snapshot_digest(snapshot)
    tampered = dict(snapshot, trade_allowed=True)
    row = (datetime(2026, 10, 30, 10, 30, tzinfo=timezone.utc),
           "demo", "EURUSD", digest, tampered)

    with pytest.raises(RuntimeError, match="hash mismatch"):
        _risk_snapshot_repository(row).get_latest_mt5_risk_snapshot("EURUSD")


@pytest.mark.parametrize(
    "column_patch, expected_error",
    [
        ({"account_mode": "real"}, "account-mode column mismatch"),
        ({"symbol": "GBPUSD"}, "symbol column mismatch"),
        ({"observed_at": "2026-10-30T10:31:00+00:00"}, "timestamp column mismatch"),
    ],
)
def test_latest_risk_snapshot_rejects_json_database_column_divergence(column_patch, expected_error):
    snapshot = _risk_snapshot_payload()
    patched = dict(snapshot, **column_patch)
    row = (datetime(2026, 10, 30, 10, 30, tzinfo=timezone.utc),
           "demo", "EURUSD", _snapshot_digest(patched), patched)

    with pytest.raises(RuntimeError, match=expected_error):
        _risk_snapshot_repository(row).get_latest_mt5_risk_snapshot("EURUSD")


def test_latest_risk_snapshot_rejects_non_object_json_payload():
    row = (datetime(2026, 10, 30, 10, 30, tzinfo=timezone.utc),
           "demo", "EURUSD", _snapshot_digest([]), [])

    with pytest.raises(RuntimeError, match="not a JSON object"):
        _risk_snapshot_repository(row).get_latest_mt5_risk_snapshot("EURUSD")

def test_collector_rejects_quote_age_policy_above_server_enforced_maximum(monkeypatch):
    fake = FakeMT5()
    monkeypatch.setitem(__import__("sys").modules, "MetaTrader5", fake)
    result = collect_mt5_risk_snapshot(
        symbol="EURUSD", day_start_equity=10000.0, equity_high_watermark=10200.0,
        proposed_request=proposed_request(), symbol_groups={"EURUSD": "fx-major"}, now=NOW,
        max_quote_age_seconds=16,
    )
    assert result["status"] == "DENY"
    assert "MAX_QUOTE_AGE_POLICY_INVALID" in result["blockers"]
    assert not fake.shutdown_called

