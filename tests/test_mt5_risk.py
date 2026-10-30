from datetime import datetime, timezone
from types import SimpleNamespace

from critical_consensus.mt5_risk import collect_mt5_risk_snapshot


class FakeMT5:
    ACCOUNT_TRADE_MODE_DEMO = 0
    ACCOUNT_TRADE_MODE_CONTEST = 1

    def __init__(self, *, trade_mode=0, positions=None, deals=None, fail_positions=False):
        self.info = SimpleNamespace(trade_mode=trade_mode, equity=10100.0, balance=10000.0,
                                    server="Demo-Server", currency="USD")
        self.positions = positions or [SimpleNamespace(symbol="EURUSD")]
        self.deals = deals or [SimpleNamespace(profit=10.0, commission=-1.0, swap=0.0, fee=0.0)]
        self.fail_positions = fail_positions
        self.shutdown_called = False

    def initialize(self, **kwargs):
        return True

    def account_info(self):
        return self.info

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


def test_read_only_demo_risk_snapshot(monkeypatch):
    fake = FakeMT5()
    monkeypatch.setitem(__import__("sys").modules, "MetaTrader5", fake)
    result = collect_mt5_risk_snapshot(
        symbol="EURUSD", day_start_equity=10000.0, equity_high_watermark=10200.0,
        proposed_risk_fraction=0.002, symbol_groups={"EURUSD": "fx-major"},
        now=datetime(2026, 10, 30, 1, 0, tzinfo=timezone.utc),
    )
    assert result["status"] == "PASS"
    assert result["account_mode"] == "demo"
    assert result["daily_pnl"] == 9.0
    assert result["open_positions"] == 1
    assert result["correlated_open_positions"] == 1
    assert result["trade_allowed"] is False
    assert fake.shutdown_called


def test_real_account_and_missing_server_snapshot_fail_closed(monkeypatch):
    fake = FakeMT5(trade_mode=2)
    monkeypatch.setitem(__import__("sys").modules, "MetaTrader5", fake)
    result = collect_mt5_risk_snapshot(
        symbol="EURUSD", day_start_equity=10000.0, equity_high_watermark=10200.0,
        proposed_risk_fraction=0.002, symbol_groups={"EURUSD": "fx-major"},
        now=datetime(2026, 10, 30, 1, 0, tzinfo=timezone.utc),
    )
    assert result["status"] == "DENY"
    assert "MT5_ACCOUNT_NOT_DEMO" in result["blockers"]
    fake.fail_positions = True
    result = collect_mt5_risk_snapshot(
        symbol="EURUSD", day_start_equity=10000.0, equity_high_watermark=10200.0,
        proposed_risk_fraction=0.002, symbol_groups={"EURUSD": "fx-major"},
        now=datetime(2026, 10, 30, 1, 0, tzinfo=timezone.utc),
    )
    assert result["status"] == "DENY"
    assert "MT5_POSITIONS_UNAVAILABLE" in result["blockers"]


def test_missing_equity_baselines_blocks_before_mt5_initializes(monkeypatch):
    fake = FakeMT5()
    monkeypatch.setitem(__import__("sys").modules, "MetaTrader5", fake)
    result = collect_mt5_risk_snapshot(
        symbol="EURUSD", day_start_equity=None, equity_high_watermark=None,
        proposed_risk_fraction=0.002, symbol_groups={"EURUSD": "fx-major"},
    )
    assert result["status"] == "DENY"
    assert "DAY_START_EQUITY_MISSING_OR_INVALID" in result["blockers"]
    assert not fake.shutdown_called
