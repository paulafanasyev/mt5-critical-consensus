"""Registry and storage tests: validation, the book aggregate and atomic reservations."""
from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fleet import config  # noqa: E402
from fleet.config import ConfigError, PortfolioLimits  # noqa: E402
from fleet.store import Store  # noqa: E402


def test_registry_parses_accounts(tmp_path: pathlib.Path) -> None:
    target = tmp_path / "accounts.yaml"
    target.write_text((ROOT / "config" / "accounts.example.yaml").read_text(encoding="utf-8"),
                      encoding="utf-8")
    fleet = config.load(target)
    assert len(fleet.accounts) == 3
    exness = fleet.by_name("exness-real")
    assert exness is not None
    assert exness.login == 12345678
    assert exness.server == "BrokerX-Live"
    assert exness.is_real
    assert exness.risk.max_lots == 0.01
    assert exness.risk.daily_stop_pct == 0.5
    assert exness.risk.take_atr_real == 1.6
    assert exness.symbols == ("GBPUSD", "EURUSD", "XAUUSD")
    assert exness.strategy.kinds == ("trend",)
    assert exness.vpn_profile.endswith(".ovpn")
    assert fleet.portfolio.daily_stop_pct == 2.0
    assert [a.name for a in fleet.enabled()] == ["demo-metaquotes"]
    demo = fleet.by_name("demo-metaquotes")
    assert demo is not None and demo.data_dir.name.startswith("114929502-")


def _write(tmp_path: pathlib.Path, body: str) -> pathlib.Path:
    target = tmp_path / "accounts.yaml"
    target.write_text(body, encoding="utf-8")
    return target


def test_unknown_risk_key_is_an_error(tmp_path: pathlib.Path) -> None:
    target = _write(tmp_path, """
accounts:
  - name: a
    login: 1
    server: S
    mode: real
    risk:
      risk_per_trad_pct: 0.2
""")
    with pytest.raises(ConfigError):
        config.load(target)


def test_mode_is_required_and_validated(tmp_path: pathlib.Path) -> None:
    target = _write(tmp_path, """
accounts:
  - name: a
    login: 1
    server: S
    mode: Real
""")
    with pytest.raises(ConfigError):
        config.load(target)


def test_an_account_naming_no_server_is_an_error(tmp_path: pathlib.Path) -> None:
    """An identity is a pair: a login without a server cannot tell this window from any other."""
    for blank in ('server:', 'server: ""', 'server: "  "'):
        target = _write(tmp_path, f"""
accounts:
  - name: a
    login: 1
    {blank}
    mode: real
""")
        with pytest.raises(ConfigError, match="сервер не назван"):
            config.load(target)


def test_vpn_without_profile_is_an_error(tmp_path: pathlib.Path) -> None:
    target = _write(tmp_path, """
accounts:
  - name: a
    login: 1
    server: S
    mode: real
    network: vpn
""")
    with pytest.raises(ConfigError):
        config.load(target)


def test_duplicate_login_and_name_are_errors(tmp_path: pathlib.Path) -> None:
    duplicate_login = _write(tmp_path, """
accounts:
  - name: a
    login: 1
    server: S
    mode: demo
  - name: b
    login: 1
    server: S
    mode: demo
""")
    with pytest.raises(ConfigError):
        config.load(duplicate_login)


def test_inline_comments_are_not_values(tmp_path: pathlib.Path) -> None:
    target = _write(tmp_path, """
accounts:
  - name: a
    login: 1
    server: Demo-Server
    mode: demo
    network: direct   # или vpn
    seed_config: C:\\cfg\\data  # сохранённая сессия
""")
    account = config.load(target).by_name("a")
    assert account is not None
    assert account.network == "direct"
    assert account.seed_config == r"C:\cfg\data"


# --- the book -------------------------------------------------------------------------------

def test_portfolio_sums_every_account_in_usd(tmp_path: pathlib.Path) -> None:
    store = Store(tmp_path / "fleet.sqlite")
    store.equity("usd", 1, 1000.0, 1010.0, 1, 10.0, "USD", 1.0)
    store.equity("eur", 2, 1000.0, 1020.0, 2, 20.0, "EUR", 1.1)
    book = store.portfolio()
    assert book["balance"] == pytest.approx(1000.0 + 1000.0 * 1.1)
    assert book["equity"] == pytest.approx(1010.0 + 1020.0 * 1.1)
    assert book["positions"] == 3.0
    assert book["accounts"] == 2.0
    rows = store.latest_equity()
    assert [row[0] for row in rows] == ["eur", "usd"]


def test_portfolio_uses_latest_row_per_account(tmp_path: pathlib.Path) -> None:
    store = Store(tmp_path / "fleet.sqlite")
    store.equity("a", 1, 1000.0, 1000.0, 0, 0.0, "USD", 1.0)
    store.equity("a", 1, 1000.0, 900.0, 1, -100.0, "USD", 1.0)
    assert store.portfolio()["equity"] == 900.0


# --- atomic reservations ---------------------------------------------------------------------

def test_reservation_blocks_a_second_oversized_entry(tmp_path: pathlib.Path) -> None:
    store = Store(tmp_path / "fleet.sqlite")
    limits = PortfolioLimits(max_gold_lots=0.30)
    ok, _, slot = store.reserve("a1", "XAUUSD", 0.20, limits, 10_000)
    assert ok and slot > 0
    blocked, reason, _ = store.reserve("a2", "XAUUSD", 0.20, limits, 10_000)
    assert not blocked and "золот" in reason.lower()
    store.release(slot)
    again, _, slot2 = store.reserve("a2", "XAUUSD", 0.20, limits, 10_000)
    assert again
    store.release(slot2)


def test_reservation_is_per_symbol_class(tmp_path: pathlib.Path) -> None:
    store = Store(tmp_path / "fleet.sqlite")
    limits = PortfolioLimits(max_gold_lots=0.30, max_usd_lots=1.0)
    ok, _, slot = store.reserve("a", "XAUUSD", 0.20, limits, 10_000)
    assert ok
    ok_fx, _, slot_fx = store.reserve("a", "EURUSD", 0.50, limits, 10_000)
    assert ok_fx
    assert store.reserved_volume("gold") == pytest.approx(0.20)
    store.release(slot)
    store.release(slot_fx)


def test_positions_mirror_has_ttl_and_cleanup(tmp_path: pathlib.Path) -> None:
    store = Store(tmp_path / "fleet.sqlite")

    class P:
        ticket = 11
        symbol = "EURUSD"
        type = 0
        volume = 0.10
        price_open = 1.10
        profit = 1.0
        sl = 1.09
        tp = 1.12
        magic = 7000001

    store.save_positions("a", [P()])
    assert len(store.open_positions()) == 1
    store.close_position_row("a", 11)
    assert store.open_positions() == []
