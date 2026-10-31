"""Invariants added after the round-9/10 reviews of the MT5 agent.

These tests exist because the gates are the product: a blocker code that is emitted but
undocumented, a cap that is exclusive at the boundary, or a refusal that surfaces as a
crash all mean the agent is no longer fail-closed.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from critical_consensus.market_data import MarketBar
from critical_consensus.mt5_agent import (AccountInfo, AgentPolicy, DecisionJournal, MT5Agent,
                                          PositionSnapshot, StrategyConfig, SymbolSpec, Tick,
                                          codes, evaluate_signal)
from critical_consensus.mt5_agent.codes import STATUS_BLOCKED
from test_mt5_agent import (SPEC, FakeBackend, agent_for, make_bars, make_policy, paper_broker,
                            policy_raw)

UTC = timezone.utc


def position(symbol="EURUSD", direction="BUY", volume=0.1, entry=1.2, stop=1.19,
             target=1.25):
    return PositionSnapshot(ticket=9, symbol=symbol, direction=direction, volume=volume,
                            entry_price=entry, stop_loss=stop, take_profit=target,
                            open_time=datetime(2026, 1, 5, tzinfo=UTC))


def account(login=42, server="Demo-Server", balance=10_000.0, equity=10_000.0,
            margin_free=10_000.0, leverage=100, mode="demo") -> AccountInfo:
    return AccountInfo(login=login, server=server, currency="USD", balance=balance,
                       equity=equity, margin_free=margin_free, leverage=leverage,
                       trade_mode=mode)


def tick_for(bars, offset=timedelta(0), spread_points=1) -> Tick:
    return Tick(symbol="EURUSD", bid=bars[-1].close,
                ask=bars[-1].close + SPEC.point * spread_points,
                server_time=bars[-1].time + offset, server_utc_offset_seconds=0)


# --------------------------------------------------------------- code registry
def test_codes_module_exports_every_constant_it_defines():
    declared = {name for name in vars(codes) if name.isupper() and not name.startswith("_")}
    assert declared == set(codes.__all__)
    assert len(set(codes.CYCLE_CAP_CODES)) == len(codes.CYCLE_CAP_CODES)
    assert all(code == code.strip() and code.replace("_", "").isupper()
               for code in codes.CYCLE_CAP_CODES)


def test_every_emitted_blocker_is_a_declared_code(tmp_path):
    """The blocker histogram is documentation; an undeclared code means an undocumented gate."""
    known = set(codes.CYCLE_CAP_CODES) | set(codes.STRATEGY_CODES)
    emitted: set[str] = set()

    broken = FakeBackend()
    broken.account = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    emitted |= set(agent_for(broken, make_policy()).run_cycle().blockers)

    unknown = FakeBackend()
    unknown.positions = lambda: None
    emitted |= set(agent_for(unknown, make_policy()).run_cycle().blockers)

    blackout = FakeBackend(clock=datetime(2026, 11, 3, 12, 30, tzinfo=UTC))
    emitted |= set(agent_for(blackout, make_policy(**{
        "schedule__blackout_windows_utc": [["2026-11-03T12:00:00+00:00",
                                            "2026-11-03T13:30:00+00:00"]]}))
        .run_cycle().blockers)

    broker = paper_broker(bars=make_bars())
    journal = DecisionJournal(tmp_path / "journal.db")
    agent = MT5Agent(backend=broker, policy=make_policy(), journal=journal, clock=broker.now)
    bars = make_bars()
    for bar in bars[60:]:
        broker.seek(bar.time)
        emitted |= set(agent.run_cycle().blockers)
    agent.close_positions("END_OF_RUN")

    undeclared = {code for code in emitted if not code.startswith("BLACKOUT_WINDOW_")} - known
    assert not undeclared
    journal.close()


# --------------------------------------------------------------------- policy
@pytest.mark.parametrize("value", [0, "", None, [], "false"])
def test_safety_switches_reject_falsy_non_booleans(value):
    with pytest.raises(ValueError, match="require_demo must be a boolean"):
        make_policy(**{"account__require_demo": value})
    with pytest.raises(ValueError, match="kill_switch.active must be a boolean"):
        make_policy(**{"kill_switch__active": value})


def test_server_offset_must_be_declared_not_defaulted():
    raw = policy_raw()
    raw.pop("server_utc_offset_seconds")
    with pytest.raises(ValueError, match="server_utc_offset_seconds is required"):
        AgentPolicy.from_mapping(raw)
    with pytest.raises(ValueError, match="whole minutes"):
        make_policy(server_utc_offset_seconds=7230)


def test_a_target_below_the_minimum_reward_risk_is_an_impossible_policy():
    with pytest.raises(ValueError, match="take_profit_risk_multiple must be at least"):
        make_policy(**{"strategy__take_profit_risk_multiple": 1.0,
                       "risk__min_risk_reward": 1.6})


def test_blocked_weekdays_must_be_integers():
    # "6" is iterable of characters, so the type of the container is checked first.
    with pytest.raises(ValueError, match="blocked_weekday must be an integer"):
        make_policy(**{"schedule__blocked_weekdays_utc": "6"})
    with pytest.raises(ValueError, match="blocked_weekday must be in \\[0, 6\\]"):
        make_policy(**{"schedule__blocked_weekdays_utc": [7]})
    assert make_policy(**{"schedule__blocked_weekdays_utc": [6, 6, 0]}
                       ).window.blocked_weekdays == (0, 6)


# ----------------------------------------------------------------------- agent
def test_agent_refuses_a_backend_that_cannot_answer_the_contract():
    with pytest.raises(TypeError, match="is not an MT5Backend"):
        MT5Agent(backend=object(), policy=make_policy())


def test_exception_detail_stays_out_of_the_blocker_code():
    backend = FakeBackend()

    def broken_account():
        raise RuntimeError("login:12345 server:private.broker")

    backend.account = broken_account
    result = agent_for(backend, make_policy()).run_cycle()
    assert result.blockers == (codes.ACCOUNT_READ_FAILED,)
    assert "private.broker" not in repr(result.blockers)
    assert "12345" not in repr(result.blockers)


def test_unknown_position_count_is_not_read_as_a_flat_account():
    backend = FakeBackend()
    backend.positions = lambda: None
    result = agent_for(backend, make_policy()).run_cycle()
    assert result.blockers == (codes.POSITION_CAP_COUNT_UNKNOWN,)
    assert result.status == STATUS_BLOCKED


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), None, "10000", True, 0.0, -1.0,
                                1e12])
def test_absurd_or_non_finite_equity_baselines_block(bad):
    backend = FakeBackend(day_start_equity=bad)
    result = agent_for(backend, make_policy()).run_cycle()
    assert codes.EQUITY_BASELINES_UNAVAILABLE in result.blockers


def test_daily_loss_cap_is_inclusive_of_the_line():
    at_the_line = FakeBackend()
    at_the_line.account_info = account(balance=9_800.0, equity=9_800.0, margin_free=9_800.0)
    assert codes.DAILY_LOSS_LIMIT_REACHED in agent_for(at_the_line, make_policy()) \
        .run_cycle().blockers

    inside_the_line = FakeBackend()
    inside_the_line.account_info = account(balance=9_800.01, equity=9_800.01,
                                           margin_free=9_800.01)
    assert codes.DAILY_LOSS_LIMIT_REACHED not in agent_for(inside_the_line, make_policy()) \
        .run_cycle().blockers


def test_realized_daily_loss_locks_the_day_even_after_a_rebound():
    """Floating equity can recover; the loss already banked today cannot be un-spent."""
    spent = FakeBackend(realized_daily_pnl=-260.0)
    assert codes.REALIZED_DAILY_LOSS_LIMIT_REACHED in agent_for(spent, make_policy()) \
        .run_cycle().blockers

    partial = FakeBackend(realized_daily_pnl=-100.0)
    assert codes.REALIZED_DAILY_LOSS_LIMIT_REACHED not in agent_for(partial, make_policy()) \
        .run_cycle().blockers


def test_a_negative_free_margin_blocks_before_any_sizing():
    backend = FakeBackend()
    backend.account_info = account(equity=9_000.0, margin_free=-50.0)
    assert codes.ACCOUNT_MARGIN_INVALID in agent_for(backend, make_policy()).run_cycle().blockers


def test_open_position_without_a_stop_blocks_the_cycle():
    """A position with no stop has no measurable loss, so the day cannot reserve for it.

    The other-symbol fixtures keep SYMBOL_ALREADY_OPEN out of the way: one EURUSD position
    under a two-position cap is already a duplicate-symbol refusal by itself. FakeBackend
    answers every spec request with SPEC, so the risk maths below is the same instrument.
    """
    policy = make_policy(**{"risk__max_open_positions": 2})
    unset = FakeBackend(open_positions=[position(symbol="USDJPY", stop=0.0)])
    assert codes.POSITION_STOP_UNSET in agent_for(unset, policy).run_cycle().blockers


def test_open_book_stop_risk_is_counted_against_the_day():
    # One lot with 160 points of stop risk spends 160 of the 200 daily budget; the proposal
    # below adds about 48 more, which is over the line. 200 points of stop risk is the whole
    # budget already, so the second case never reaches the candidate.
    near_the_line = FakeBackend(open_positions=[
        position(symbol="USDJPY", volume=1.0, stop=1.2 - 0.0016)])
    past_the_line = FakeBackend(open_positions=[
        position(symbol="USDJPY", volume=1.0, stop=1.2 - 0.002)])
    policy = make_policy(**{"risk__max_open_positions": 2})
    assert codes.AGGREGATE_STOP_RISK_LIMIT_REACHED in agent_for(near_the_line, policy) \
        .run_cycle().blockers
    assert codes.LOSS_FLOOR_BREACHED in agent_for(past_the_line, policy).run_cycle().blockers


def test_sizing_denial_keeps_the_proposal_for_the_audit_trail():
    backend = FakeBackend()
    backend.account_info = account(margin_free=0.5)
    result = agent_for(backend, make_policy()).run_cycle()
    assert result.blockers == (codes.MARGIN_MINIMUM_LOT_UNFUNDED,)
    assert result.proposal is not None and result.sizing is None


def test_paper_refusal_is_a_journaled_block_not_a_crash():
    bars = make_bars()
    broker = paper_broker(bars=bars)
    broker.seek(bars[60].time)

    def refuse(**kwargs):
        raise ValueError("paper book refuses this lot")

    broker.open_position = refuse
    agent = MT5Agent(backend=broker, policy=make_policy(), clock=broker.now)
    result = agent.run_cycle()
    assert result.status == STATUS_BLOCKED
    assert result.blockers == (codes.PAPER_OPEN_REJECTED,)


def test_a_quote_that_moved_while_the_operator_decided_blocks_the_fill():
    bars = make_bars()
    broker = paper_broker(bars=bars)
    broker.seek(bars[-1].time)
    fresh = broker.tick
    reads = {"count": 0}

    def stale_after_the_first_read(symbol):
        reads["count"] += 1
        current = fresh(symbol)
        if reads["count"] == 1:
            return current
        return Tick(symbol=current.symbol, bid=current.bid, ask=current.ask,
                    server_time=current.server_time - timedelta(seconds=600),
                    server_utc_offset_seconds=current.server_utc_offset_seconds)

    broker.tick = stale_after_the_first_read
    result = MT5Agent(backend=broker, policy=make_policy(), clock=broker.now).run_cycle()
    assert result.status == STATUS_BLOCKED
    assert result.blockers == (codes.QUOTE_STALE,)
    assert broker.positions() == []


def test_equity_recorded_is_false_when_there_is_no_journal_to_record_into():
    result = agent_for(FakeBackend(), make_policy()).run_cycle()
    assert result.equity_recorded is False


def test_equity_point_failure_is_reported_instead_of_swallowed(tmp_path):
    journal = DecisionJournal(tmp_path / "journal.db")
    journal.record_equity = lambda **kwargs: (_ for _ in ()).throw(RuntimeError("disk full"))
    result = agent_for(FakeBackend(), make_policy(), journal).run_cycle()
    assert result.equity_recorded is False
    event = journal.events(event_type="agent_cycle")[-1]
    assert event["payload"]["status"] == result.status
    journal.close()


def test_broken_bar_series_blocks_instead_of_reading_as_no_signal():
    bars = make_bars()
    duplicate = bars[-1]
    backend = FakeBackend(bar_series=[*bars[:-5], duplicate, duplicate])
    result = agent_for(backend, make_policy()).run_cycle()
    assert result.status == STATUS_BLOCKED
    assert codes.BAR_SERIES_INVALID in result.blockers


# -------------------------------------------------------------------- strategy
def test_strategy_rejects_a_series_that_jumps_back_in_time():
    bars = make_bars()
    shuffled = [*bars[:-2], bars[-1], bars[-2]]
    evaluation = evaluate_signal(shuffled, SPEC, tick_for(bars), make_policy().strategy,
                                 max_spread_points=25, expected_timeframe="H1")
    assert evaluation.blockers == (codes.BAR_SERIES_INVALID,)
    assert evaluation.data_integrity_failure


def test_strategy_rejects_a_bar_stamped_after_the_quote_that_closed_it():
    bars = make_bars()
    evaluation = evaluate_signal(bars, SPEC, tick_for(bars, offset=-timedelta(hours=1)),
                                 make_policy().strategy, max_spread_points=25, expected_timeframe="H1")
    assert evaluation.blockers == (codes.BAR_TIME_IN_FUTURE,)
    assert evaluation.data_integrity_failure


def test_strategy_checks_the_stops_level_for_the_target_too():
    """A broker refuses each level on its own; a short target can fail as the stop clears."""
    config = StrategyConfig(take_profit_risk_multiple=0.6)
    bars = make_bars()
    level_between = SymbolSpec(**{**policy_raw()["symbol_spec"], "stops_level_points": 250})
    evaluation = evaluate_signal(bars, level_between, tick_for(bars), config,
                                 max_spread_points=25, expected_timeframe="H1")
    assert evaluation.blockers == (codes.TARGET_INSIDE_BROKER_STOPS_LEVEL,)


def test_strategy_reports_no_price_field_that_cannot_be_trusted():
    bars = make_bars()
    flat = make_bars(drift=1.0)
    evaluation = evaluate_signal(flat, SPEC, tick_for(flat), make_policy().strategy,
                                 max_spread_points=25, expected_timeframe="H1")
    assert codes.NO_TREND_SIGNAL in evaluation.blockers
    assert not evaluation.data_integrity_failure


# ------------------------------------------------------------- paper broker
def test_paper_sell_stop_is_evaluated_against_the_ask_not_the_bid():
    bars = make_bars(60)
    entry_broker = paper_broker(bars=bars)
    entry_broker.seek(bars[-1].time)
    entry = entry_broker.tick("EURUSD").bid
    spread = SPEC.point * 10
    stop = entry + 0.0005
    bridging = MarketBar(symbol="EURUSD", timeframe="H1",
                         time=bars[-1].time + timedelta(hours=1), open=entry,
                         high=stop - spread / 2, low=entry - 0.0002, close=entry,
                         tick_volume=1, spread_points=10)
    broker = paper_broker(bars=[*bars, bridging])
    broker.seek(bars[-1].time)
    broker.open_position(direction="SELL", volume=0.1, entry_price=entry, stop_loss=stop,
                         take_profit=entry - 0.0005)
    assert broker.trades == []
    broker.seek(bridging.time)
    assert broker.trades[-1].exit_reason == "STOP_LOSS"
    assert broker.trades[-1].pnl < 0


def test_paper_broker_fills_a_gap_at_the_open_not_at_the_level():
    bars = make_bars(60)
    opener = paper_broker(bars=bars)
    opener.seek(bars[-1].time)
    entry = opener.tick("EURUSD").ask
    stop = entry - 0.0005
    gapped = MarketBar(symbol="EURUSD", timeframe="H1", time=bars[-1].time + timedelta(hours=1),
                       open=entry - 0.004, high=entry - 0.0038, low=entry - 0.0042,
                       close=entry - 0.004, tick_volume=1, spread_points=10)
    broker = paper_broker(bars=[*bars, gapped])
    broker.seek(bars[-1].time)
    broker.open_position(direction="BUY", volume=0.1, entry_price=entry, stop_loss=stop,
                         take_profit=entry + 0.002)
    broker.seek(gapped.time)
    trade = broker.trades[-1]
    assert trade.exit_reason == "STOP_LOSS_GAP"
    assert trade.exit_price == pytest.approx(gapped.open)
    # Filling at the level instead of the open would overstate the account by the gap.
    assert trade.pnl < (stop - entry) / SPEC.tick_size * SPEC.tick_value * 0.1 + 1e-9


def test_paper_realized_daily_pnl_only_counts_the_current_server_day():
    bars = make_bars(60)
    opener = paper_broker(bars=bars)
    opener.seek(bars[-1].time)
    entry = opener.tick("EURUSD").ask
    stop = entry - 0.0005
    losing = MarketBar(symbol="EURUSD", timeframe="H1",
                       time=bars[-1].time + timedelta(hours=1), open=stop - 0.001, high=stop,
                       low=stop - 0.002, close=stop - 0.001, tick_volume=1, spread_points=10)
    fresh_day = MarketBar(symbol="EURUSD", timeframe="H1",
                          time=bars[-1].time + timedelta(hours=25), open=entry,
                          high=entry + SPEC.point, low=entry - SPEC.point, close=entry,
                          tick_volume=1, spread_points=10)
    broker = paper_broker(bars=[*bars, losing, fresh_day])
    broker.seek(bars[-1].time)
    broker.open_position(direction="BUY", volume=0.1, entry_price=entry, stop_loss=stop,
                         take_profit=entry + 0.002)
    assert broker.realized_daily_pnl == 0.0
    broker.seek(losing.time)
    assert broker.realized_daily_pnl < 0
    broker.seek(fresh_day.time)
    assert broker.realized_daily_pnl == 0.0
    assert broker.realized_pnl < 0


def test_paper_broker_refuses_a_lot_it_cannot_fund():
    bars = make_bars(60)
    broker = paper_broker(bars=bars, leverage=100)
    broker.seek(bars[-1].time)
    entry = broker.tick("EURUSD").ask
    with pytest.raises(ValueError, match="more margin"):
        broker.open_position(direction="BUY", volume=90.0, entry_price=entry,
                             stop_loss=entry - 0.0005, take_profit=entry + 0.002)


def test_paper_broker_reports_the_server_offset_it_applies():
    assert paper_broker(bars=make_bars(60)).server_utc_offset_seconds == 0


# ------------------------------------------------------------ order surface
ALLOWED_TERMINAL_CALLS = {"initialize", "shutdown", "last_error", "account_info", "symbol_info",
                          "symbol_select", "symbol_info_tick", "copy_rates_from_pos",
                          "positions_get"}
FORBIDDEN_TERMS = ("order_send", "OrderSend", "order_check", "OrderCheck", "OrderCalcMargin",
                   "OrderCalcProfit", "trade_transaction", "TradeTransaction", "order_place",
                   "deal_get", "HistoryDeals")


def test_package_source_contains_no_order_sending_surface():
    """A grep-level promise: the only terminal calls here are the nine order-free ones.

    ``symbol_select`` is the one mutating call — it adds a missing symbol to
    Market Watch so ``symbol_info``/``copy_rates`` have data; it never sends an order.
    """
    source_root = Path(__file__).resolve().parents[1] / "src" / "critical_consensus" / "mt5_agent"
    files = sorted(source_root.glob("*.py"))
    assert files
    text = "\n".join(path.read_text(encoding="utf-8") for path in files)
    for term in FORBIDDEN_TERMS:
        assert term not in text, term
    used = set(re.findall(r"self\._mt5\.([a-z_]+)\(", text))
    assert used <= ALLOWED_TERMINAL_CALLS, used
    assert "import MetaTrader5" not in text
    assert not any(name.startswith("order") or name.startswith("trade")
                   for name in dir(MT5Agent))


# --------------------------------------------------------- execution mode gate
def test_confirm_is_the_default_for_a_policy_that_never_mentions_execution_mode():
    raw = policy_raw()
    raw.pop("execution_mode")
    policy = AgentPolicy.from_mapping(raw)
    assert policy.execution_mode == "confirm"
    assert policy.confirmation_required is True


def test_execution_mode_and_ui_language_are_normalized_and_validated():
    assert AgentPolicy.from_mapping(policy_raw(execution_mode="AUTO")).execution_mode == "auto"
    assert AgentPolicy.from_mapping(policy_raw(ui_language="VI")).ui_language == "vi"
    assert AgentPolicy.from_mapping(policy_raw(ui_language="en-GB")).ui_language == "en"
    with pytest.raises(ValueError, match="execution_mode must be one of"):
        AgentPolicy.from_mapping(policy_raw(execution_mode="always"))
    with pytest.raises(ValueError, match="unsupported language"):
        AgentPolicy.from_mapping(policy_raw(ui_language="de"))


def test_automatic_execution_requires_a_demo_account_even_in_the_library():
    with pytest.raises(ValueError, match="require_demo"):
        AgentPolicy.from_mapping(policy_raw(execution_mode="auto",
                                            account__require_demo=False))
    # The same policy is fine when the agent asks the operator first.
    policy = AgentPolicy.from_mapping(policy_raw(execution_mode="confirm",
                                                 account__require_demo=False))
    assert policy.execution_mode == "confirm"


def test_confirm_mode_without_an_approver_never_opens_a_position(tmp_path):
    broker = paper_broker(bars=make_bars())
    journal = DecisionJournal(tmp_path / "journal.db")
    policy = make_policy(execution_mode="confirm")
    agent = MT5Agent(backend=broker, policy=policy, journal=journal, clock=broker.now)
    broker.seek(make_bars()[60].time)
    result = agent.run_cycle()
    assert result.status == STATUS_BLOCKED
    assert result.blockers == (codes.CONFIRMATION_UNAVAILABLE,)
    assert result.ticket is None and broker.trades == []
    # The proposal the operator never saw is still in the audit trail.
    payload = journal.events(event_type="agent_cycle")[-1]["payload"]
    assert payload["execution_mode"] == "confirm"
    assert payload["proposal"]["direction"] == "BUY"
    journal.close()


def test_only_a_literal_true_from_the_approver_opens_the_position(tmp_path):
    bars = make_bars()
    broker = paper_broker(bars=bars)
    seen: list[dict] = []

    def truthy_but_not_true(proposal, sizing):
        seen.append(proposal)
        return "yes"

    broker.seek(bars[60].time)
    agent = MT5Agent(backend=broker, policy=make_policy(execution_mode="confirm"),
                     clock=broker.now, approver=truthy_but_not_true)
    result = agent.run_cycle()
    assert result.blockers == (codes.OPERATOR_DECLINED,)
    assert result.ticket is None
    assert seen and seen[0]["direction"] == "BUY"


def test_an_approver_that_fails_blocks_rather_than_opens(tmp_path):
    bars = make_bars()
    broker = paper_broker(bars=bars)
    broker.seek(bars[60].time)

    def explode(proposal, sizing):
        raise OSError("operator terminal went away")

    agent = MT5Agent(backend=broker, policy=make_policy(execution_mode="confirm"),
                     clock=broker.now, approver=explode)
    result = agent.run_cycle()
    assert result.blockers == (codes.OPERATOR_CONFIRMATION_FAILED,)
    assert broker.trades == []


def test_an_explicit_yes_opens_and_auto_mode_never_asks(tmp_path):
    bars = make_bars()
    broker = paper_broker(bars=bars)
    broker.seek(bars[60].time)
    asked: list[int] = []
    agent = MT5Agent(backend=broker, policy=make_policy(execution_mode="confirm"),
                     clock=broker.now,
                     approver=lambda proposal, sizing: asked.append(1) or True)
    assert agent.run_cycle().status == "PAPER_OPENED"
    assert len(asked) == 1

    broker = paper_broker(bars=bars)
    broker.seek(bars[60].time)
    agent = MT5Agent(backend=broker, policy=make_policy(execution_mode="auto"),
                     clock=broker.now,
                     approver=lambda proposal, sizing: pytest.fail("auto must not ask"))
    assert agent.run_cycle().status == "PAPER_OPENED"


def test_observe_mode_never_asks_and_never_opens(tmp_path):
    broker = paper_broker(bars=make_bars())
    broker.seek(make_bars()[60].time)
    agent = MT5Agent(backend=broker, policy=make_policy(mode="observe",
                                                        execution_mode="confirm"),
                     clock=broker.now,
                     approver=lambda proposal, sizing: pytest.fail("observe must not ask"))
    assert agent.run_cycle().status == "OBSERVED"


# ------------------------------------------------------- fill after approval
def _paper_with_shifted_requote(shift: float):
    """A paper book whose first quote is the signal quote and whose next one has moved."""
    bars = make_bars()
    broker = paper_broker(bars=bars)
    broker.seek(bars[60].time)
    original = broker.tick
    reads = {"count": 0}

    def shifted(symbol: str) -> Tick:
        tick = original(symbol)
        reads["count"] += 1
        delta = 0.0 if reads["count"] == 1 else shift
        return Tick(symbol=tick.symbol, bid=tick.bid + delta, ask=tick.ask + delta,
                    server_time=tick.server_time,
                    server_utc_offset_seconds=tick.server_utc_offset_seconds)

    broker.tick = shifted
    return broker


def test_a_fill_far_from_the_approved_entry_is_refused(tmp_path):
    """The approval covered one entry; a fill far from it is a trade nobody agreed to."""
    journal = DecisionJournal(tmp_path / "journal.db")
    broker = _paper_with_shifted_requote(0.01)
    agent = MT5Agent(backend=broker, policy=make_policy(), journal=journal, clock=broker.now)
    result = agent.run_cycle()
    assert result.status == STATUS_BLOCKED
    assert result.blockers == (codes.PRICE_MOVED_DURING_CONFIRMATION,)
    assert result.ticket is None
    assert broker.positions() == []
    requote = journal.events(event_type="agent_cycle")[-1]["payload"]["requote"]
    assert requote["slippage"] == pytest.approx(0.01)
    assert requote["slippage"] > requote["max_slippage"]
    journal.close()


def test_a_small_move_is_filled_at_the_fresh_price_and_resized(tmp_path):
    """Inside the bound the trade proceeds, but sized on the fill stop, not the stale one."""
    journal = DecisionJournal(tmp_path / "journal.db")
    broker = _paper_with_shifted_requote(SPEC.point)
    agent = MT5Agent(backend=broker, policy=make_policy(), journal=journal, clock=broker.now)
    result = agent.run_cycle()
    assert result.status == "PAPER_OPENED"
    assert result.sizing is not None
    requote = journal.events(event_type="agent_cycle")[-1]["payload"]["requote"]
    assert requote["fill_price"] == pytest.approx(requote["approved_entry_price"] + SPEC.point)
    assert requote["final_volume"] == pytest.approx(result.sizing["volume"])
    assert requote["final_volume"] <= requote["approved_volume"]
    assert requote["final_risk_value"] == pytest.approx(result.sizing["risk_value"])
    assert requote["risk_reward_at_fill"] >= make_policy().risk.min_risk_reward
    journal.close()


def test_a_peak_below_the_current_or_day_start_equity_is_not_a_baseline():
    below_equity = FakeBackend()
    below_equity.peak_equity = 9_999.0
    assert agent_for(below_equity, make_policy()).run_cycle().blockers == (
        codes.EQUITY_BASELINES_UNAVAILABLE,)

    below_day_start = FakeBackend(day_start_equity=10_100.0, peak_equity=10_000.0)
    below_day_start.account_info = account(balance=9_950.0, equity=9_950.0, margin_free=9_950.0)
    assert agent_for(below_day_start, make_policy()).run_cycle().blockers == (
        codes.EQUITY_BASELINES_UNAVAILABLE,)


def test_a_journal_that_refuses_the_cycle_event_reports_instead_of_raising(tmp_path):
    journal = DecisionJournal(tmp_path / "journal.db")

    def refuse(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    journal.record = refuse
    result = agent_for(FakeBackend(), make_policy(), journal).run_cycle()
    assert result.cycle_recorded is False
    assert result.decision_hash == "journal-unavailable"
    journal.close()


def test_closing_the_book_writes_the_final_chained_equity_point(tmp_path):
    bars = make_bars()
    broker = paper_broker(bars=bars)
    broker.seek(bars[60].time)
    entry = broker.tick("EURUSD").ask
    broker.open_position(direction="BUY", volume=0.1, entry_price=entry,
                         stop_loss=entry - 0.0005, take_profit=entry + 0.002)
    journal = DecisionJournal(tmp_path / "journal.db")
    agent = MT5Agent(backend=broker, policy=make_policy(), journal=journal, clock=broker.now)
    agent.close_positions("END_OF_RUN")
    curve = journal.equity_curve()
    assert curve and curve[-1] == pytest.approx(broker.equity())
    point = journal.events(event_type="equity_point")[-1]["payload"]
    assert point["at"] == broker.now().isoformat()
    assert point["open_positions"] == 0
    journal.close()


def test_the_run_id_is_part_of_the_signed_body(tmp_path):
    """A run id sitting only in a column could be reassigned without breaking the chain."""
    database = tmp_path / "journal.db"
    journal = DecisionJournal(database, run_id="run-a")
    journal.record_equity(equity=10_000.0, balance=10_000.0, open_positions=0,
                          at=datetime(2026, 1, 5, tzinfo=UTC))
    journal.record("agent_cycle", {"status": "OBSERVED"})
    journal.close()

    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE audit_events SET run_id = 'run-b' WHERE seq = 1")
    journal = DecisionJournal(database)
    tampered = journal.chain_status(run_id="run-b")
    assert tampered["valid"] is False
    assert tampered["reason"] == "EVENT_CONTENT_HASH_MISMATCH"
    assert journal.chain_status(run_id="run-a")["valid"] is True
    journal.close()


# ------------------------------------------------------------- one MT5 reader
def test_both_mt5_readers_share_one_offset_helper_and_one_row_normaliser():
    """Two readers with two clock rules is how a bar ends up two hours away from its tick."""
    from critical_consensus import market_data
    from critical_consensus.mt5_agent import backend

    assert backend.server_offset is market_data.server_offset
    assert backend.normalize_rate_rows is market_data.normalize_rate_rows
    assert backend.normalize_rate_rows is not None


class _StructuredArray:
    """What ``copy_rates_*`` actually returns: rows are indexable tuples with named fields."""

    class _Dtype:
        names = ("time", "open", "high", "low", "close", "tick_volume", "spread")

    dtype = _Dtype()

    def __init__(self, rows):
        self._rows = rows

    def __iter__(self):
        for row in self._rows:
            yield tuple(row[name] for name in self.dtype.names)


def _mt5_stub(*, initialized=True, rows=None, last_error=(0, "no error")):
    from types import SimpleNamespace

    return SimpleNamespace(
        TIMEFRAME_H1=2,
        initialize=lambda **kwargs: initialized,
        shutdown=lambda: True,
        last_error=lambda: last_error,
        symbol_select=lambda name, enable: True,
        copy_rates_range=lambda symbol, timeframe, start, end: rows)


def test_fetch_mt5_bars_reads_broker_time_with_the_declared_offset(monkeypatch):
    import sys
    from critical_consensus import market_data

    stamp = 1_767_300_000
    rows = [{"time": stamp, "open": 1.1, "high": 1.2, "low": 1.0, "close": 1.15,
             "tick_volume": 100, "spread": 12},
            {"time": stamp + 3600, "open": 1.15, "high": 1.25, "low": 1.1, "close": 1.2,
             "tick_volume": 90, "spread": 12}]
    monkeypatch.setitem(sys.modules, "MetaTrader5",
                        _mt5_stub(rows=_StructuredArray(rows)))
    start, end = datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC)
    bars = market_data.fetch_mt5_bars("EURUSD", "H1", start, end,
                                      server_utc_offset_seconds=7200)
    assert [bar.time for bar in bars] == [datetime.fromtimestamp(stamp - 7200, tz=UTC),
                                          datetime.fromtimestamp(stamp + 3600 - 7200, tz=UTC)]
    assert all(bar.symbol == "EURUSD" for bar in bars)

    with pytest.raises(TypeError):
        market_data.fetch_mt5_bars("EURUSD", "H1", start, end)  # offset is keyword-required
    with pytest.raises(ValueError, match="whole minutes"):
        market_data.fetch_mt5_bars("EURUSD", "H1", start, end,
                                   server_utc_offset_seconds=7230)


def test_fetch_mt5_bars_reports_an_error_code_without_the_library_text(monkeypatch):
    import sys
    from critical_consensus import market_data

    monkeypatch.setitem(sys.modules, "MetaTrader5",
                        _mt5_stub(initialized=False, last_error=(10018, "broker secret text")))
    with pytest.raises(RuntimeError) as failure:
        market_data.fetch_mt5_bars("EURUSD", "H1", datetime(2026, 1, 1, tzinfo=UTC),
                                   datetime(2026, 1, 2, tzinfo=UTC),
                                   server_utc_offset_seconds=0)
    assert "10018" in str(failure.value)
    assert "secret" not in str(failure.value)
