from datetime import datetime, timedelta, timezone
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
    DEAL_TYPE_BUY = 0
    DEAL_TYPE_SELL = 1
    DEAL_TYPE_BALANCE = 2

    def __init__(self, *, trade_mode=0, positions=None, deals=None, fail_positions=False):
        self.info = SimpleNamespace(trade_mode=trade_mode, equity=10100.0, balance=10000.0,
                                    server="Demo-Server", currency="USD")
        self.positions = positions or [SimpleNamespace(
            symbol="EURUSD", type=0, volume=0.1, price_open=1.1000, sl=1.0980)]
        self.deals = deals or [SimpleNamespace(
            type=0, profit=10.0, commission=-1.0, swap=0.0, fee=0.0)]
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
        server_utc_offset_seconds=0,
    )
    assert result["status"] == "PASS"
    assert result["account_mode"] == "demo"
    assert result["daily_pnl"] == 9.0
    assert result["daily_loss_fraction"] == 0.0
    assert result["risk_fraction"] == pytest.approx(10.0 / 10100.0)
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
        server_utc_offset_seconds=0,
    )
    assert result["status"] == "DENY"
    assert "MT5_ACCOUNT_NOT_DEMO" in result["blockers"]
    fake.fail_positions = True
    result = collect_mt5_risk_snapshot(
        symbol="EURUSD", day_start_equity=10000.0, equity_high_watermark=10200.0,
        proposed_request=proposed_request(), symbol_groups={"EURUSD": "fx-major"}, now=NOW,
        server_utc_offset_seconds=0,
    )
    assert result["status"] == "DENY"
    assert "MT5_POSITIONS_UNAVAILABLE" in result["blockers"]


def test_missing_equity_baselines_blocks_before_mt5_initializes(monkeypatch):
    fake = FakeMT5()
    monkeypatch.setitem(__import__("sys").modules, "MetaTrader5", fake)
    result = collect_mt5_risk_snapshot(
        symbol="EURUSD", day_start_equity=None, equity_high_watermark=None,
        proposed_request=proposed_request(), symbol_groups={"EURUSD": "fx-major"},
        server_utc_offset_seconds=0,
    )
    assert result["status"] == "DENY"
    assert "DAY_START_EQUITY_MISSING_OR_INVALID" in result["blockers"]
    assert not fake.shutdown_called


def test_daily_loss_uses_equity_change_and_includes_open_position_loss(monkeypatch):
    fake = FakeMT5()
    fake.info.equity = 9500.0
    fake.deals = [SimpleNamespace(type=0, profit=100.0, commission=0.0, swap=0.0, fee=0.0)]
    monkeypatch.setitem(__import__("sys").modules, "MetaTrader5", fake)
    result = collect_mt5_risk_snapshot(
        symbol="EURUSD", day_start_equity=10000.0, equity_high_watermark=10200.0,
        proposed_request=proposed_request(), symbol_groups={"EURUSD": "fx-major"}, now=NOW,
        server_utc_offset_seconds=0,
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
        server_utc_offset_seconds=0,
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
        server_utc_offset_seconds=0,
    )
    assert result["status"] == "DENY"
    assert "MT5_QUOTE_STALE" in result["blockers"]


def test_missing_server_utc_offset_fails_closed_before_initializing(monkeypatch):
    fake = FakeMT5()
    monkeypatch.setitem(__import__("sys").modules, "MetaTrader5", fake)
    result = collect_mt5_risk_snapshot(
        symbol="EURUSD", day_start_equity=10000.0, equity_high_watermark=10200.0,
        proposed_request=proposed_request(), symbol_groups={"EURUSD": "fx-major"}, now=NOW,
    )
    assert result["status"] == "DENY"
    assert "MT5_SERVER_UTC_OFFSET_MISSING_OR_INVALID" in result["blockers"]
    assert result["trade_allowed"] is False
    assert not fake.shutdown_called


def test_invalid_server_utc_offset_is_rejected(monkeypatch):
    fake = FakeMT5()
    monkeypatch.setitem(__import__("sys").modules, "MetaTrader5", fake)
    for invalid in (True, 90, 15 * 3600):
        result = collect_mt5_risk_snapshot(
            symbol="EURUSD", day_start_equity=10000.0, equity_high_watermark=10200.0,
            proposed_request=proposed_request(), symbol_groups={"EURUSD": "fx-major"}, now=NOW,
            server_utc_offset_seconds=invalid,
        )
        assert result["status"] == "DENY"
        assert "MT5_SERVER_UTC_OFFSET_MISSING_OR_INVALID" in result["blockers"]


def test_broker_server_clock_offset_is_applied_to_tick_freshness(monkeypatch):
    fake = FakeMT5()
    fake.tick.time = int(NOW.timestamp()) + 3 * 3600
    monkeypatch.setitem(__import__("sys").modules, "MetaTrader5", fake)
    kwargs = dict(symbol="EURUSD", day_start_equity=10000.0, equity_high_watermark=10200.0,
                  proposed_request=proposed_request(), symbol_groups={"EURUSD": "fx-major"},
                  now=NOW)
    naive = collect_mt5_risk_snapshot(**kwargs, server_utc_offset_seconds=0)
    assert naive["status"] == "DENY"
    assert "MT5_QUOTE_FROM_FUTURE" in naive["blockers"]
    correct = collect_mt5_risk_snapshot(**kwargs, server_utc_offset_seconds=3 * 3600)
    assert correct["status"] == "PASS"
    assert correct["quote_age_seconds"] == 0
    assert correct["server_utc_offset_seconds"] == 3 * 3600
    assert datetime.fromisoformat(correct["server_observed_at"]) == NOW + timedelta(hours=3)


def test_collector_sizes_risk_at_the_worse_reference_price(monkeypatch):
    fake = FakeMT5()
    fake.tick.ask = 1.10015
    monkeypatch.setitem(__import__("sys").modules, "MetaTrader5", fake)
    result = collect_mt5_risk_snapshot(
        symbol="EURUSD", day_start_equity=10000.0, equity_high_watermark=10200.0,
        proposed_request=proposed_request(), symbol_groups={"EURUSD": "fx-major"}, now=NOW,
        server_utc_offset_seconds=0,
    )
    assert result["status"] == "PASS"
    assert result["risk_entry_price"] == 1.10015
    assert result["risk_value_account_currency"] == pytest.approx(10.5)
    assert result["risk_fraction"] == pytest.approx(10.5 / 10100.0)


def test_collector_blocks_slippage_beyond_a_tenth_of_the_stop_distance(monkeypatch):
    fake = FakeMT5()
    fake.tick.ask = 1.1003  # 20 points: inside the point limit but 20% of the stop width
    monkeypatch.setitem(__import__("sys").modules, "MetaTrader5", fake)
    result = collect_mt5_risk_snapshot(
        symbol="EURUSD", day_start_equity=10000.0, equity_high_watermark=10200.0,
        proposed_request=proposed_request(), symbol_groups={"EURUSD": "fx-major"}, now=NOW,
        server_utc_offset_seconds=0,
    )
    assert result["status"] == "DENY"
    assert "PRICE_SLIPPAGE_EXCEEDS_STOP_DISTANCE_FRACTION" in result["blockers"]
    assert "PRICE_SLIPPAGE_LIMIT_EXCEEDED" not in result["blockers"]


def test_collector_blocks_a_reference_price_beyond_the_reward_side(monkeypatch):
    fake = FakeMT5()
    fake.tick.ask = 1.1030
    monkeypatch.setitem(__import__("sys").modules, "MetaTrader5", fake)
    result = collect_mt5_risk_snapshot(
        symbol="EURUSD", day_start_equity=10000.0, equity_high_watermark=10200.0,
        proposed_request=proposed_request(), symbol_groups={"EURUSD": "fx-major"}, now=NOW,
        server_utc_offset_seconds=0,
    )
    assert result["status"] == "DENY"
    assert "RISK_CONSERVATIVE_ENTRY_OUTSIDE_GEOMETRY" in result["blockers"]


def test_collector_blocks_a_day_start_that_cannot_reconstruct_the_balance(monkeypatch):
    fake = FakeMT5()
    monkeypatch.setitem(__import__("sys").modules, "MetaTrader5", fake)
    result = collect_mt5_risk_snapshot(
        symbol="EURUSD", day_start_equity=9000.0, equity_high_watermark=10200.0,
        proposed_request=proposed_request(), symbol_groups={"EURUSD": "fx-major"}, now=NOW,
        server_utc_offset_seconds=0,
    )
    assert result["status"] == "DENY"
    assert "DAY_START_EQUITY_BELOW_DAILY_PNL_IMPLIED_BALANCE" in result["blockers"]


def test_collector_blocks_a_peak_below_the_day_start(monkeypatch):
    fake = FakeMT5()
    monkeypatch.setitem(__import__("sys").modules, "MetaTrader5", fake)
    result = collect_mt5_risk_snapshot(
        symbol="EURUSD", day_start_equity=10000.0, equity_high_watermark=9500.0,
        proposed_request=proposed_request(), symbol_groups={"EURUSD": "fx-major"}, now=NOW,
        server_utc_offset_seconds=0,
    )
    assert result["status"] == "DENY"
    assert "EQUITY_HIGH_WATERMARK_BELOW_DAY_START" in result["blockers"]


def test_collector_accepts_a_net_loss_day_and_reports_it_as_finite(monkeypatch):
    fake = FakeMT5()
    fake.info.equity = 9800.0
    fake.info.balance = 9800.0
    fake.deals = [SimpleNamespace(type=0, profit=-200.0, commission=0.0, swap=0.0, fee=0.0)]
    monkeypatch.setitem(__import__("sys").modules, "MetaTrader5", fake)
    result = collect_mt5_risk_snapshot(
        symbol="EURUSD", day_start_equity=10000.0, equity_high_watermark=10200.0,
        proposed_request=proposed_request(), symbol_groups={"EURUSD": "fx-major"}, now=NOW,
        server_utc_offset_seconds=0,
    )
    assert result["status"] == "PASS"
    assert result["daily_pnl"] == -200.0
    assert result["daily_loss_fraction"] == pytest.approx(0.02)
    assert "MT5_RISK_METRICS_NONFINITE" not in result["blockers"]


def test_balance_cashflows_are_not_counted_as_trading_pnl(monkeypatch):
    fake = FakeMT5()
    fake.info.equity = 12000.0
    fake.info.balance = 12000.0
    fake.deals = [
        SimpleNamespace(type=0, profit=10.0, commission=0.0, swap=0.0, fee=0.0),
        SimpleNamespace(type=2, profit=2000.0, commission=0.0, swap=0.0, fee=0.0),
    ]
    monkeypatch.setitem(__import__("sys").modules, "MetaTrader5", fake)
    result = collect_mt5_risk_snapshot(
        symbol="EURUSD", day_start_equity=10000.0, equity_high_watermark=12000.0,
        proposed_request=proposed_request(), symbol_groups={"EURUSD": "fx-major"}, now=NOW,
        server_utc_offset_seconds=0,
    )
    assert result["daily_pnl"] == 10.0
    assert result["daily_deals_count"] == 1
    # The deposit is caught by the baseline reconciliation instead of refreshing the budget.
    assert result["status"] == "DENY"
    assert "DAY_START_EQUITY_BELOW_DAILY_PNL_IMPLIED_BALANCE" in result["blockers"]


def test_collector_reports_open_stop_risk_and_blocks_unknown_position_stops(monkeypatch):
    fake = FakeMT5()
    monkeypatch.setitem(__import__("sys").modules, "MetaTrader5", fake)
    result = collect_mt5_risk_snapshot(
        symbol="EURUSD", day_start_equity=10000.0, equity_high_watermark=10200.0,
        proposed_request=proposed_request(), symbol_groups={"EURUSD": "fx-major"}, now=NOW,
        server_utc_offset_seconds=0,
    )
    # |1.1000 - 1.0980| / 0.00001 * 1.0 * 0.1 = 20 account-currency units at risk.
    assert result["open_stop_risk_value_account_currency"] == pytest.approx(20.0)
    assert result["open_stop_risk_fraction"] == pytest.approx(20.0 / 10100.0)

    fake.positions = [SimpleNamespace(symbol="EURUSD", type=0, volume=0.1,
                                      price_open=1.1000, sl=0.0)]
    result = collect_mt5_risk_snapshot(
        symbol="EURUSD", day_start_equity=10000.0, equity_high_watermark=10200.0,
        proposed_request=proposed_request(), symbol_groups={"EURUSD": "fx-major"}, now=NOW,
        server_utc_offset_seconds=0,
    )
    assert result["status"] == "DENY"
    assert "OPEN_POSITION_STOP_RISK_UNKNOWN" in result["blockers"]
