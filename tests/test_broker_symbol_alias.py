"""Instrument naming across brokers: XAUUSD here, XAUUSDm there, and never a guess.

A demo run pointed at a second retail broker asks for the same logical instruments and gets
back "symbol not available" for every one of them, because Exness, RoboForex and ITAM list
account-type suffixes rather than bare names. The resolver below is the only place that
mapping happens, so the rule that keeps it honest — exact first, one candidate only, never
on a short name — is testable without a terminal.
"""
from __future__ import annotations

import pytest

from critical_consensus.mt5_agent.backend import resolve_broker_symbol


def test_a_bare_name_the_server_lists_is_used_unchanged():
    assert resolve_broker_symbol("XAUUSD", ["XAUUSD", "XAUUSDm"]) == "XAUUSD"


def test_a_unique_suffixed_variant_resolves():
    assert resolve_broker_symbol("XAUUSD", ["EURUSD", "XAUUSDm", "USDCAD"]) == "XAUUSDm"


def test_suffix_forms_observed_on_retail_servers_all_resolve():
    for name in ("XAUUSDc", "XAUUSD.i", "XAUUSD-2", "XAUUSD10"):
        assert resolve_broker_symbol("XAUUSD", [name]) == name


def test_several_variants_refuse_instead_of_guessing_the_account_type():
    with pytest.raises(RuntimeError) as exc:
        resolve_broker_symbol("XAUUSD", ["XAUUSDm", "XAUUSDc"])
    assert "XAUUSDc" in str(exc.value) and "XAUUSDm" in str(exc.value)


def test_a_short_name_is_never_aliased_to_an_unrelated_ticker():
    # MetaQuotes-Demo lists BTC (a US stock) and BTCL/BTCO/BTCW alongside no BTCUSD at all:
    # resolving "BTC" by prefix would send an order for someone else's instrument.
    for listed in ("BTCO", "BTCL", "BTCW", "BTCUSD"):
        with pytest.raises(RuntimeError):
            resolve_broker_symbol("BTC", [listed])


def test_a_long_suffix_is_a_different_instrument_not_an_account_type():
    with pytest.raises(RuntimeError):
        resolve_broker_symbol("XAUUSD", ["XAUUSDindex"])


def test_names_are_matched_after_the_same_normalization_the_backend_applies():
    assert resolve_broker_symbol("  xauusd ", ["XAUUSDM"]) == "XAUUSDM"


def test_the_servers_own_spelling_survives_a_case_insensitive_lookup():
    # Exness lists the Mini-account gold as XAUUSDm; handing back an upper-cased name would
    # make symbol_select() fail for the one instrument the resolution existed to find.
    assert resolve_broker_symbol("XAUUSD", ["xauusdm", "EURUSD"]) == "xauusdm"
    assert resolve_broker_symbol("xauusd", ["XAUUSDm"]) == "XAUUSDm"


def test_an_instrument_the_server_genuinely_lacks_still_refuses():
    with pytest.raises(RuntimeError) as exc:
        resolve_broker_symbol("BTCUSD", ["XAUUSD", "EURUSD"])
    assert "BTCUSD" in str(exc.value)


def _adapter():
    """A bare adapter: the name map needs no terminal connection to be exercised."""
    from critical_consensus.mt5_agent.backend import LiveMT5Backend
    return LiveMT5Backend.__new__(LiveMT5Backend)


def test_the_adapter_asks_the_terminal_for_the_servers_own_spelling():
    terminal = _adapter()
    terminal.use_broker_names({"XAUUSD": "XAUUSDm"})
    assert terminal._name("XAUUSD") == "XAUUSDm"
    assert terminal._name("EURUSD") == "EURUSD"


def test_two_spellings_cannot_collide_on_one_internal_key():
    terminal = _adapter()
    terminal.use_broker_names({"XAUUSD": "XAUUSDm"})
    with pytest.raises(RuntimeError):
        terminal.use_broker_names({"XAUUSD": "XAUUSDc"})
