"""Risk, margin and lots must be priced from the contract, not from a broker's tick field.

Every number here was read off MetaQuotes-Demo on 2026-11-02, on the account this repo runs
against: the terminal's own ``order_calc_profit`` and the margin it actually blocked. The tick
fields disagree with both for metals by a factor of ten, and sizing built on them sent a gold
order ten times the risk budget allowed.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from critical_consensus.mt5_agent import SymbolSpec
from critical_consensus.mt5_agent.sizing import (MINIMUM_LOT_EXCEEDS_RISK_BUDGET, size_position)

# XAUUSD exactly as the server publishes it: tick_value 0.1 at tick_size 0.01 claims 10 $ per
# price unit per lot, while contract_size 100 and the terminal's own profit math say 100 $.
GOLD = SymbolSpec(symbol="XAUUSD", point=0.01, digits=2, tick_size=0.01, tick_value=0.1,
                  contract_size=100.0, volume_min=0.01, volume_max=100.0, volume_step=0.01,
                  spread_points=20, stops_level_points=0)

EURUSD = SymbolSpec(symbol="EURUSD", point=0.00001, digits=5, tick_size=0.00001, tick_value=1.0,
                    contract_size=100000.0, volume_min=0.01, volume_max=500.0, volume_step=0.01,
                    spread_points=3, stops_level_points=0)

# mt5.order_calc_profit(ORDER_TYPE_BUY, "XAUUSD", 1.0, 3902.27, 3947.07) == 4480.0
GOLD_STOP = 3947.07 - 3902.27
GOLD_RISK_PER_LOT = 4480.0
# mt5.order_calc_profit(ORDER_TYPE_BUY, "EURUSD", 1.0, 1.11563, 1.11871) == 308.0
EUR_STOP = 1.11871 - 1.11563
EUR_RISK_PER_LOT = 308.0


def gold_size(**overrides):
    kwargs = dict(spec=GOLD, equity=100_000.0, risk_fraction_limit=0.0005,
                  stop_distance=GOLD_STOP, leverage=100, margin_available=99_851.0,
                  price=3902.27)
    kwargs.update(overrides)
    return size_position(**kwargs)


def test_the_contract_size_prices_a_gold_lot_at_what_the_broker_pays():
    assert GOLD.cash_per_price_unit == 100.0
    assert GOLD.value_per_price_unit(1.0) == 100.0


def test_a_lot_sized_from_the_contract_carries_the_brokers_own_profit_figure():
    result = gold_size()
    assert result.accepted
    assert result.order.risk_per_lot == pytest.approx(GOLD_RISK_PER_LOT)
    # The same number the terminal computes for the whole position, at the lot it allowed.
    assert result.order.risk_value == pytest.approx(result.order.volume * GOLD_RISK_PER_LOT)


def test_the_tick_field_would_have_sized_gold_ten_times_over_budget():
    """The regression this exists for: 0.0002 of equity bought 0.04 lots, ~179 $ at the stop.

    Priced from tick_value/tick_size the same command sized 0.04 lots, because 44.8 points
    looked like 448 $ of risk per lot instead of 4480 $. The budget was 20 $ and the real
    exposure was 179 $, both recorded on the live account.
    """
    tick_only = SymbolSpec(**{**GOLD.__dict__, "contract_size": None})
    assert tick_only.cash_per_price_unit == 10.0
    assert gold_size(spec=tick_only, risk_fraction_limit=0.0002).order.volume == 0.04
    assert gold_size(risk_fraction_limit=0.0002).order is None


def test_the_smallest_gold_lot_does_not_fit_a_twenty_dollar_budget():
    """0.01 lot over a 44.8-point stop costs 44.8 $: a refusal, not a silent oversize."""
    result = gold_size(risk_fraction_limit=0.0002)
    assert not result.accepted
    assert result.denial == MINIMUM_LOT_EXCEEDS_RISK_BUDGET


def test_a_budget_that_fits_the_minimum_lot_sizes_exactly_one_minimum_lot():
    result = gold_size(risk_fraction_limit=0.0005)
    assert result.accepted
    assert result.order.volume == 0.01
    assert result.order.risk_value <= 100_000.0 * 0.0005 + 1e-9


def test_margin_is_what_the_broker_actually_blocked():
    # The terminal blocked 156.09 $ for the 0.04-lot gold position that was really opened.
    assert GOLD.margin_requirement(0.04, 100, 3902.27) == pytest.approx(156.09, abs=0.01)


def test_fx_pair_sizing_is_unchanged_by_the_contract_source():
    assert EURUSD.cash_per_price_unit == pytest.approx(100_000.0)
    assert EURUSD.cash_per_price_unit == pytest.approx(EURUSD.tick_value / EURUSD.tick_size)
    assert EURUSD.cash_estimate_divergence == pytest.approx(0.0, abs=1e-9)
    result = size_position(spec=EURUSD, equity=100_000.0, risk_fraction_limit=0.0002,
                           stop_distance=EUR_STOP, leverage=100, margin_available=99_000.0,
                           price=1.11563)
    assert result.accepted
    assert result.order.risk_per_lot == pytest.approx(EUR_RISK_PER_LOT)
    assert result.order.volume == 0.06


def test_a_spec_without_a_contract_size_falls_back_to_the_tick_ratio():
    """Brokers that leave trade_contract_size at zero still trade; the fallback is their only
    number, so it must stay, but it is not allowed to override a published contract."""
    legacy = SymbolSpec(**{**GOLD.__dict__, "contract_size": None})
    assert legacy.cash_per_price_unit == legacy.tick_value / legacy.tick_size
    assert legacy.cash_estimate_divergence is None


def terminal_info(**overrides):
    row = dict(name="XAUUSD", point=0.01, digits=2, trade_tick_size=0.01,
               trade_tick_value_loss=0.1, trade_tick_value=0.1, trade_contract_size=100.0,
               volume_min=0.01, volume_max=100.0, volume_step=0.01, spread=20,
               trade_stops_level=0, currency_profit="USD")
    row.update(overrides)
    return SimpleNamespace(**row)


def test_from_terminal_keeps_the_contract_size_and_reports_the_disagreement():
    spec = SymbolSpec.from_terminal(terminal_info(), "USD")
    assert spec.contract_size == 100.0
    assert spec.cash_per_price_unit == 100.0
    # 10 $ claimed by the tick field against 100 $ of contract: 90% of the contract value.
    assert spec.cash_estimate_divergence == pytest.approx(0.9)


def test_from_terminal_treats_a_zero_contract_size_as_unknown():
    spec = SymbolSpec.from_terminal(terminal_info(trade_contract_size=0.0), "USD")
    assert spec.contract_size is None
    assert spec.cash_per_price_unit == 10.0
    assert spec.cash_estimate_divergence is None


def test_a_negative_contract_size_is_rejected_rather_than_silently_dropped():
    with pytest.raises(ValueError):
        SymbolSpec(**{**GOLD.__dict__, "contract_size": -100.0})


# --- the rest of the risk stack has to price the lot the same way -------------------------
#
# The collector and the independent verifier run in a different process from the sizing code.
# While both still read the tick pair, a correctly sized gold order was reported to the human
# as costing 4.48 $ and the verifier agreed with the 4.48 $, so the gate that exists to catch a
# tenfold understatement was itself the understatement.

from datetime import datetime, timezone  # noqa: E402

from critical_consensus.mt5_risk import collect_mt5_risk_snapshot  # noqa: E402
from critical_consensus.risk_controls import risk_snapshot_matches_order  # noqa: E402

OBSERVED = datetime(2026, 11, 2, 2, 0, tzinfo=timezone.utc)
GOLD_ENTRY = 3902.27
GOLD_STOP_DISTANCE = 44.80  # 2 x ATR on the H1 bars the runner was fed
# 44.8 points x 100 $ per unit x 0.01 lot -- the loss the broker would actually have paid.
GOLD_RISK_VALUE = 44.80


class GoldTerminal:
    """MetaQuotes-Demo's own XAUUSD fields: contract 100 against a 10 $ tick estimate."""

    ACCOUNT_TRADE_MODE_DEMO = 0
    DEAL_TYPE_BUY = 0
    DEAL_TYPE_SELL = 1

    def __init__(self, positions=()):
        self.spec = terminal_info()
        self.tick = SimpleNamespace(bid=GOLD_ENTRY, ask=GOLD_ENTRY,
                                   time=int(OBSERVED.timestamp()))
        self.info = SimpleNamespace(trade_mode=0, login=114929502, equity=100_000.0,
                                    balance=100_000.0, server="MetaQuotes-Demo", currency="USD")
        self.positions = list(positions)

    def initialize(self, **kwargs):
        return True

    def account_info(self):
        return self.info

    def symbol_select(self, symbol, selected):
        return symbol == "XAUUSD"

    def symbol_info(self, symbol):
        return self.spec if symbol == "XAUUSD" else None

    def symbol_info_tick(self, symbol):
        return self.tick if symbol == "XAUUSD" else None

    def positions_get(self):
        return self.positions

    def history_deals_get(self, start, end):
        return []

    def shutdown(self):
        pass

    def last_error(self):
        return (0, "OK")


def gold_request():
    return {"instrument": "XAUUSD", "direction": "BUY", "size": 0.01,
            "expected_price": GOLD_ENTRY,
            "sl_tp": {"sl": GOLD_ENTRY - GOLD_STOP_DISTANCE,
                      "tp": GOLD_ENTRY + 2 * GOLD_STOP_DISTANCE}}


def collect(terminal, monkeypatch, request=None):
    monkeypatch.setitem(__import__("sys").modules, "MetaTrader5", terminal)
    request = request or gold_request()
    return collect_mt5_risk_snapshot(
        symbol="XAUUSD", day_start_equity=100_000.0, equity_high_watermark=100_000.0,
        proposed_request=request, symbol_groups={"XAUUSD": "precious-metals"},
        now=OBSERVED, server_utc_offset_seconds=0)


def test_the_collector_prices_risk_from_the_contract_and_publishes_it(monkeypatch):
    snapshot = collect(GoldTerminal(), monkeypatch)
    assert snapshot["status"] == "PASS"
    assert snapshot["risk_cash_per_price_unit"] == 100.0
    assert snapshot["risk_value_account_currency"] == pytest.approx(GOLD_RISK_VALUE)
    assert snapshot["risk_fraction"] == pytest.approx(GOLD_RISK_VALUE / 100_000.0)


def test_the_verifier_reconciles_a_contract_priced_gold_snapshot(monkeypatch):
    """The two processes must land on the same number or every gold order is a false denial."""
    snapshot = collect(GoldTerminal(), monkeypatch)
    assert risk_snapshot_matches_order(gold_request(), snapshot) == []


def test_the_snapshot_records_the_contract_size_it_used(monkeypatch):
    snapshot = collect(GoldTerminal(), monkeypatch)
    assert snapshot["risk_contract_size"] == 100.0


def test_a_row_priced_from_the_smaller_tick_estimate_is_denied(monkeypatch):
    """A snapshot that agrees with its own wrong number must not pass the cross-check.

    This is exactly what the old collector wrote for this order from these terminal fields:
    4.48 $ against the 44.80 $ the broker would have paid.
    """
    snapshot = collect(GoldTerminal(), monkeypatch)
    snapshot["risk_cash_per_price_unit"] = 10.0
    snapshot["risk_value_account_currency"] = GOLD_RISK_VALUE / 10.0
    snapshot["risk_fraction"] = GOLD_RISK_VALUE / 10.0 / 100_000.0
    blockers = risk_snapshot_matches_order(gold_request(), snapshot)
    assert "RISK_CASH_PER_PRICE_UNIT_NOT_THE_CONTRACT_SIZE" in blockers


def test_an_open_gold_positions_stop_risk_is_contract_priced(monkeypatch):
    terminal = GoldTerminal(positions=[SimpleNamespace(
        symbol="XAUUSD", type=0, volume=0.01, price_open=GOLD_ENTRY,
        sl=GOLD_ENTRY - 10.0)])
    snapshot = collect(terminal, monkeypatch)
    assert snapshot["status"] == "PASS"
    # 10 points x 100 $ x 0.01 lot = 10 $; the tick pair would have booked 1.00 $.
    assert snapshot["open_stop_risk_value_account_currency"] == pytest.approx(10.0)


def test_a_snapshot_without_the_contract_field_still_reconciles_on_ticks(monkeypatch):
    """Orders journalled before this change must stay verifiable, so the fallback is kept."""
    terminal = GoldTerminal()
    terminal.spec = SimpleNamespace(**{**terminal.spec.__dict__, "trade_contract_size": 0.0})
    snapshot = collect(terminal, monkeypatch)
    assert snapshot["risk_cash_per_price_unit"] == 10.0
    assert snapshot["risk_value_account_currency"] == pytest.approx(GOLD_RISK_VALUE / 10.0)
    assert risk_snapshot_matches_order(gold_request(), snapshot) == []


def test_an_account_paid_in_another_currency_falls_back_to_the_tick_value(monkeypatch):
    """A contract size counts base units, not dollars -- only same-currency profit may use it.

    A gold lot is 100 ounces for anyone, but its cash value is 100 dollars only while the
    account is paid in dollars. The tick value is what the broker states in account currency,
    so the collector uses that and publishes no contract size for the verifier to insist on.
    """
    terminal = GoldTerminal()
    terminal.info = SimpleNamespace(**{**terminal.info.__dict__, "currency": "EUR"})
    snapshot = collect(terminal, monkeypatch)
    assert snapshot["account_currency"] == "EUR"
    assert snapshot["risk_cash_per_price_unit"] == 10.0
    assert snapshot["risk_contract_size"] is None
    assert risk_snapshot_matches_order(gold_request(), snapshot) == []
