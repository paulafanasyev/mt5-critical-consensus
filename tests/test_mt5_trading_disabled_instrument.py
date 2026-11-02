"""An instrument the broker will not let us open cannot be signalled into an order.

Measured on MetaQuotes-Demo on 2026-11-02: US500 quotes normally and its ``symbol_info`` reports
``trade_mode=0``, while EURUSD, XAUUSD and XAGUSD report ``4``. The agent sized a US500 leg
anyway, the demo EA refused it before ``OrderSend`` with "BLOCKED: symbol does not allow full
trading (mode=0)", and the runner was left holding a served command with no broker deal behind
it - the exact state that arms the kill switch and stops the whole run, taking the six legs that
*were* fillable down with it. The EA's rule is mirrored here, so the cycle ends as a journaled
blocker instead of an unconfirmed send.
"""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_mt5_agent import SPEC, FakeBackend, agent_for, make_policy
from critical_consensus.mt5_agent import codes
from critical_consensus.mt5_agent.backend import SymbolSpec
from critical_consensus.mt5_agent.i18n import CODE_LABELS

SYMBOL = "EURUSD"


def spec_with(mode: int | None) -> SymbolSpec:
    return replace(SPEC, symbol=SYMBOL, trade_mode=mode)


def info(**overrides) -> SimpleNamespace:
    """A terminal ``SymbolInfo`` for a US500-like instrument, overridable field by field."""
    fields = dict(name="US500", point=0.01, digits=2, trade_tick_size=0.01,
                  trade_tick_value_loss=1.0, trade_tick_value=1.0, volume_min=0.01,
                  volume_max=100.0, volume_step=0.01, spread=50, trade_stops_level=0,
                  currency_profit="USD", trade_contract_size=1.0, trade_mode=0)
    fields.update(overrides)
    return SimpleNamespace(**fields)


def cycle(mode: int | None):
    backend = FakeBackend(symbol_specs={SYMBOL: spec_with(mode)})
    return agent_for(backend, make_policy(symbol=SYMBOL)).run_cycle()


# --------------------------------------------------------------------- the gate
def test_a_disabled_instrument_ends_the_cycle_before_it_could_queue_an_order():
    result = cycle(0)
    assert result.status == codes.STATUS_BLOCKED
    assert result.blockers == (codes.SYMBOL_TRADING_DISABLED,)
    # Nothing was proposed, so nothing was sized and nothing reached the broker: the run keeps
    # its legs on the instruments that are tradeable instead of dying on this one.
    assert result.proposal is None
    assert result.ticket is None


@pytest.mark.parametrize("mode", [1, 2, 3], ids=["long_only", "short_only", "close_only"])
def test_every_mode_below_full_is_refused(mode: int):
    assert cycle(mode).blockers == (codes.SYMBOL_TRADING_DISABLED,)


def test_an_instrument_in_full_mode_is_traded():
    result = cycle(4)
    # The gate let the cycle through: the signal was proposed and a lot was sized, which is the
    # most this fake backend can prove - the paper fill itself is exercised in test_mt5_agent.py.
    assert codes.SYMBOL_TRADING_DISABLED not in result.blockers
    assert result.proposal is not None and result.sizing is not None


def test_a_reader_that_gave_no_mode_is_not_read_as_a_prohibition():
    # Absence of the field is not evidence that the broker forbids the instrument; the EA checks
    # the live value again before it sends, so the cycle is allowed to reach that point.
    result = cycle(None)
    assert codes.SYMBOL_TRADING_DISABLED not in result.blockers
    assert result.proposal is not None and result.sizing is not None


# --------------------------------------------------------------------- the reading
def test_the_mode_comes_off_the_terminal_with_the_rest_of_the_spec():
    spec = SymbolSpec.from_terminal(info(), account_currency="USD")
    assert spec.trade_mode == 0
    assert spec.can_open is False
    assert spec.trade_mode_name == "disabled"


def test_a_tradeable_instrument_is_measured_the_same_way():
    assert SymbolSpec.from_terminal(info(trade_mode=4), account_currency="USD").can_open is True


def test_an_info_object_without_the_field_reads_as_unknown():
    spec = SymbolSpec.from_terminal(
        SimpleNamespace(**{k: v for k, v in vars(info()).items() if k != "trade_mode"}),
        account_currency="USD")
    assert spec.trade_mode is None
    assert spec.can_open is True
    assert spec.trade_mode_name == "unknown"


@pytest.mark.parametrize("mode", [0, 1, 2, 3, 4])
def test_the_mode_names_cover_the_terminal_enum(mode: int):
    assert SymbolSpec(symbol="X", point=1.0, digits=2, tick_size=1.0, tick_value=1.0,
                      volume_min=0.01, volume_max=1.0, volume_step=0.01, spread_points=1,
                      stops_level_points=0, trade_mode=mode).trade_mode_name != f"mode_{mode}"


@pytest.mark.parametrize("mode", [5, -1, True, "4", 4.0],
                         ids=["above_enum", "negative", "bool", "string", "float"])
def test_a_mode_that_is_not_an_enum_value_is_rejected(mode):
    with pytest.raises(ValueError, match=r"trade_mode must be an integer in \[0, 4\] or None"):
        replace(SPEC, trade_mode=mode)


# --------------------------------------------------------------------- the reporting
def test_the_code_is_a_cycle_blocker_and_not_a_data_complaint():
    # A feed that cannot be read is damage; an instrument the broker closed for opening is a
    # property of the contract, and only the first of those should read as a broken terminal.
    assert codes.SYMBOL_TRADING_DISABLED in codes.CYCLE_CAP_CODES
    assert codes.SYMBOL_TRADING_DISABLED not in codes.DATA_INTEGRITY_CODES


def test_the_blocker_is_named_in_every_supported_language():
    labels = CODE_LABELS[codes.SYMBOL_TRADING_DISABLED]
    assert len(labels) == 3
    assert all(label.strip() for label in labels)
