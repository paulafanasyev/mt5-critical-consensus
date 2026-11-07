"""Tests for the evidence renderer: journal maths, chart escaping, and the walk-forward."""
from __future__ import annotations

import functools
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest


def _root() -> Path:
    return Path(__file__).resolve().parents[1]


@functools.lru_cache(maxsize=1)
def _render():
    path = _root() / "scripts" / "render_demo_evidence.py"
    spec = importlib.util.spec_from_file_location("render_demo_evidence", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["render_demo_evidence"] = module
    spec.loader.exec_module(module)
    return module


render = _render()


def _write_journal(path: Path, rows: list[tuple[str, dict]]) -> None:
    con = sqlite3.connect(path)
    con.execute("create table audit_events (seq integer primary key autoincrement,"
                " event_type text, payload text)")
    for index, (event_type, payload) in enumerate(rows):
        con.execute("insert into audit_events (seq, event_type, payload) values (?,?,?)",
                    (index, event_type, json.dumps(payload)))
    con.commit()
    con.close()


def test_a_database_without_a_journal_table_is_refused(tmp_path: Path) -> None:
    other = tmp_path / "not_a_journal.db"
    con = sqlite3.connect(other)
    con.execute("create table something_else (id integer)")
    con.commit()
    con.close()
    with pytest.raises(ValueError, match="audit_events"):
        render.load_journal_events(other)


def test_equity_series_skips_broken_points_and_sorts(tmp_path: Path) -> None:
    db = tmp_path / "journal.db"
    _write_journal(db, [
        ("equity_point", {"at": "2026-10-02T00:00:00+00:00", "equity": 99.0}),
        ("equity_point", {"at": "2026-10-01T00:00:00+00:00", "equity": 100.0}),
        ("equity_point", {"at": "not-a-time", "equity": 101.0}),
        ("equity_point", {"at": "2026-10-03T00:00:00+00:00", "equity": "102.0"}),
        ("agent_cycle", {"at": "2026-10-04T00:00:00+00:00", "equity": 103.0}),
    ])
    points = render.equity_series(render.load_journal_events(db))
    assert [(at.strftime("%Y-%m-%d"), value) for at, value in points] == [
        ("2026-10-01", 100.0), ("2026-10-02", 99.0)]


def test_r_multiple_divides_profit_by_the_sized_risk() -> None:
    trade = {"event_type": "paper_trade_closed", "opened_at": "2026-10-01T00:00:00+00:00",
             "entry_price": 100.0, "stop_loss": 98.0, "volume": 1.0, "pnl": 4.0}
    # stop distance 2.0 x 1.0 of cash per price unit x 1.0 lot = 2.0 of risk
    assert render.planned_risk_currency(trade, cash_per_price_unit=1.0) == 2.0
    assert render.r_multiples([trade], cash_per_price_unit=1.0) == [("2026-10-01T00:00", 2.0)]


@pytest.mark.parametrize("field,value", [("volume", None), ("pnl", "1"), ("entry_price", float("nan"))])
def test_an_unusable_trade_produces_no_bar(field: str, value: object) -> None:
    trade = {"event_type": "paper_trade_closed", "opened_at": "2026-10-01T00:00:00+00:00",
             "entry_price": 100.0, "stop_loss": 98.0, "volume": 1.0, "pnl": 4.0}
    trade[field] = value
    assert render.r_multiples([trade], cash_per_price_unit=1.0) == []


def test_autocorrelation_distinguishes_reversion_from_trend() -> None:
    reverting = [1.0, -1.0, 1.0, -1.0, 1.0, -1.0, 1.0, -1.0]
    trending = [1.0, 1.0, 1.0, 1.0, -1.0, -1.0, -1.0, -1.0]
    assert render.lag1_autocorrelation(reverting) < -0.5
    assert render.lag1_autocorrelation(trending) > 0.5
    assert render.lag1_autocorrelation([2.0, 2.0, 2.0]) == 0.0


def _bars(closes: list[float]) -> list[dict[str, float]]:
    return [{"time": 1_700_000_000 + index * 3_600, "open": price, "high": price,
             "low": price, "close": price} for index, price in enumerate(closes)]


def test_bollinger_fades_extremes_and_costs_only_ever_hurt() -> None:
    oscillating = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 94.0] * 12
    cheap = render.bollinger_walk_forward(_bars(oscillating), costs_bps_per_side=0.0)
    costly = render.bollinger_walk_forward(_bars(oscillating), costs_bps_per_side=10.0)
    assert cheap["trades"] > 0
    assert cheap["chained_return_pct"] > costly["chained_return_pct"]
    assert abs((cheap["mean_net_bps"] or 0.0) - (costly["mean_net_bps"] or 0.0)) == pytest.approx(20.0)


def test_flat_history_trades_nothing() -> None:
    result = render.sma_trend_walk_forward(_bars([100.0] * 200))
    assert result == {"trades": 0, "win_rate": None, "mean_net_bps": None,
                      "chained_return_pct": 0.0, "max_drawdown_pct": 0.0, "bars": 200}


def test_trend_following_a_rise_then_a_fall_closes_one_positive_trade() -> None:
    # A monotone rise only flips the side once, so nothing ever closes; up then down gives the
    # long leg a real exit, which is what the chained return has to be measured on.
    v_shaped = _bars([100.0 + index * 0.5 for index in range(60)]
                     + [129.5 - index * 0.5 for index in range(60)])
    result = render.sma_trend_walk_forward(v_shaped, costs_bps_per_side=0.0)
    assert result["trades"] >= 1
    assert result["chained_return_pct"] > 0.0


def test_hourly_profile_reports_sign_and_needs_a_sample() -> None:
    bars = _bars([100.0, 101.0, 100.0, 101.0] * 20)
    profile = render.hourly_t_statistics(bars)
    assert all(len(str(hour)) <= 2 for hour, _, _ in profile)
    assert all(count >= 20 for _, count, _ in profile)


def test_svg_text_is_escaped_so_a_label_cannot_inject_markup(tmp_path: Path) -> None:
    svg = render.line_chart(title='a "title"', subtitle="<script>",
                            series=[('L<&gt;', [(0.0, 1.0), (1.0, 2.0)])])
    assert "<script>" not in svg
    assert "&lt;script&gt;" in svg
    assert "&amp;gt;" in svg
    assert svg.startswith("<svg xmlns=") and svg.endswith("</svg>")


def test_an_empty_chart_is_a_loud_error() -> None:
    with pytest.raises(ValueError, match="at least one"):
        render.line_chart(title="t", subtitle="s", series=[("empty", [])])
    with pytest.raises(ValueError, match="at least one"):
        render.bar_chart(title="t", subtitle="s", groups=[("empty", [])], category_labels=["a"])


def test_journal_argument_shape_is_enforced() -> None:
    with pytest.raises(ValueError, match="LABEL=POLICY=DB"):
        render.parse_journal_argument("XAUUSD=only-one")
    label, policy, db = render.parse_journal_argument("XAU=a.yaml=b.db")
    assert (label, policy.name, db.name) == ("XAU", "a.yaml", "b.db")


def test_rendering_two_journals_writes_svg_and_records_the_numbers(tmp_path: Path) -> None:
    policy = tmp_path / "policy.yaml"
    policy.write_text("symbol_spec:\n  tick_size: 0.01\n  tick_value: 0.1\n", encoding="utf-8")
    db = tmp_path / "journal.db"
    _write_journal(db, [
        ("agent_cycle", {"event_type": "agent_cycle", "broker_order_sent": False}),
        ("equity_point", {"at": "2026-10-01T00:00:00+00:00", "equity": 100_000.0,
                          "broker_order_sent": False}),
        ("equity_point", {"at": "2026-10-02T00:00:00+00:00", "equity": 99_900.0,
                          "broker_order_sent": False}),
        ("paper_trade_closed", {"opened_at": "2026-10-01T01:00:00+00:00", "entry_price": 4000.0,
                                "stop_loss": 3980.0, "volume": 0.05, "pnl": -10.0,
                                "broker_order_sent": False}),
    ])
    evidence: dict = {}
    out_dir = tmp_path / "charts"
    written = render.render_demo_journals([("XAUUSD", policy, db)], out_dir, evidence)
    assert len(written) == 2
    assert all(Path(path).read_text(encoding="utf-8").lstrip().startswith("<svg") for path in written)
    summary = evidence["demos"]["XAUUSD"]
    assert summary["closed_trades"] == 1 and summary["winning_trades"] == 0
    assert summary["realized_pnl_account_currency"] == -10.0
    assert summary["final_equity"] == 99_900.0
    # -10 over a 20-point stop = 2000 ticks x 0.1 x 0.05 = 10 of risk, so exactly -1R
    assert summary["planned_risk_currency_per_trade"] == [10.0]
    assert summary["r_multiples"] == [-1.0]


def test_the_report_divides_by_the_basis_the_journal_itself_booked(tmp_path: Path) -> None:
    """A pre-fix journal priced its P/L and its risk at the broker tick field; both must move
    together, or re-rendering last week's evidence redraws -1R trades as -0.1R ones.

    The numbers are the real XAUUSD paper journal: one lot at a 40.784-point stop booked
    407.84 $ of risk per lot, i.e. the 10 $ per price unit MetaQuotes-Demo's tick pair claims,
    while the contract the policy now declares says 100 $.
    """
    policy = tmp_path / "policy.yaml"
    policy.write_text("symbol_spec:\n  tick_size: 0.01\n  tick_value: 0.1\n"
                      "  contract_size: 100.0\n", encoding="utf-8")
    db = tmp_path / "journal.db"
    _write_journal(db, [
        ("agent_cycle", {"status": "PAPER_OPENED",
                         "proposal": {"risk_distance": 40.7842857142853},
                         "sizing": {"risk_per_lot": 407.842857142853, "volume": 0.04},
                         "broker_order_sent": False}),
        ("equity_point", {"at": "2026-10-01T00:00:00+00:00", "equity": 100_000.0,
                          "broker_order_sent": False}),
        ("equity_point", {"at": "2026-10-02T00:00:00+00:00", "equity": 99_983.69,
                          "broker_order_sent": False}),
        ("paper_trade_closed", {"opened_at": "2026-10-01T01:00:00+00:00", "entry_price": 4024.68,
                                "stop_loss": 4065.4642857142853, "volume": 0.04,
                                "pnl": -16.313714285714195, "broker_order_sent": False}),
    ])
    evidence: dict = {}
    render.render_demo_journals([("XAUUSD", policy, db)], tmp_path / "charts", evidence)
    summary = evidence["demos"]["XAUUSD"]
    assert summary["risk_basis"] == "journal_sizing"
    assert summary["risk_cash_per_price_unit"] == pytest.approx(10.0)
    assert summary["planned_risk_currency_per_trade"] == [16.3137]
    assert summary["r_multiples"] == [-1.0]


def test_a_journal_without_sizing_rows_uses_the_contract_the_policy_declares(tmp_path: Path) -> None:
    """No booked basis to read: fall back to the declared contract, not to a tick field that is
    ten times too small for these metals."""
    policy = tmp_path / "policy.yaml"
    policy.write_text("symbol_spec:\n  tick_size: 0.01\n  tick_value: 0.1\n"
                      "  contract_size: 100.0\n", encoding="utf-8")
    db = tmp_path / "journal.db"
    _write_journal(db, [
        ("equity_point", {"at": "2026-10-01T00:00:00+00:00", "equity": 100_000.0,
                          "broker_order_sent": False}),
        ("equity_point", {"at": "2026-10-02T00:00:00+00:00", "equity": 99_990.0,
                          "broker_order_sent": False}),
        ("paper_trade_closed", {"opened_at": "2026-10-01T01:00:00+00:00", "entry_price": 4000.0,
                                "stop_loss": 3980.0, "volume": 0.05, "pnl": -10.0,
                                "broker_order_sent": False}),
    ])
    evidence: dict = {}
    render.render_demo_journals([("XAUUSD", policy, db)], tmp_path / "charts", evidence)
    summary = evidence["demos"]["XAUUSD"]
    # 20 points x 100 $ per price unit per lot x 0.05 lots = 100 of risk.
    assert summary["risk_basis"] == "policy_contract_size"
    assert summary["planned_risk_currency_per_trade"] == [100.0]
    assert summary["r_multiples"] == [-0.1]
    assert summary["contract_size"] == 100.0


def test_a_policy_without_a_contract_size_falls_back_to_the_tick_ratio(tmp_path: Path) -> None:
    policy = tmp_path / "policy.yaml"
    policy.write_text("symbol_spec:\n  tick_size: 0.01\n  tick_value: 0.1\n", encoding="utf-8")
    assert render.read_symbol_spec(policy) == (0.01, 0.1, None)
    assert render.policy_cash_per_price_unit(0.01, 0.1, None) == 10.0
    assert render.policy_cash_per_price_unit(0.01, 0.1, 100.0) == 100.0
    trade = {"event_type": "paper_trade_closed", "entry_price": 4000.0,
             "stop_loss": 3980.0, "volume": 0.05, "pnl": -10.0}
    assert render.planned_risk_currency(trade, cash_per_price_unit=10.0) == 10.0
    assert render.planned_risk_currency(trade, cash_per_price_unit=100.0) == 100.0


def test_a_cycle_without_a_usable_sizing_pair_provides_no_basis() -> None:
    assert render.recorded_cash_per_price_unit([
        {"event_type": "agent_cycle", "sizing": {"risk_per_lot": 407.84},
         "proposal": {"risk_distance": 0}}]) is None
    assert render.recorded_cash_per_price_unit([{"event_type": "agent_cycle"}]) is None


def test_a_journal_with_no_equity_is_refused_before_anything_is_written(tmp_path: Path) -> None:
    policy = tmp_path / "policy.yaml"
    policy.write_text("symbol_spec:\n  tick_size: 0.01\n  tick_value: 0.1\n", encoding="utf-8")
    db = tmp_path / "journal.db"
    _write_journal(db, [("agent_cycle", {"event_type": "agent_cycle"})])
    with pytest.raises(ValueError, match="no equity_point"):
        render.render_demo_journals([("XAUUSD", policy, db)], tmp_path / "charts", {})


def test_main_without_inputs_explains_itself(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    assert render.main(["--out-dir", str(tmp_path / "charts")]) == 2
    assert "nothing to render" in capsys.readouterr().out
