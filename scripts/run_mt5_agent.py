"""Run the MetaTrader 5 agent in a chosen language over real bars, on a demo account only.

Data comes from an installed MT5 terminal (order-free history) or an exported CSV.
``--execution auto`` lets the agent open simulated positions by itself;
``--execution confirm`` asks the operator about every one of them. Both fill locally:
this script never sends an order to a broker, and ``auto`` is refused by the policy
unless the account is marked demo.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import math
import os
import sqlite3
import sys
import uuid
from pathlib import Path
from typing import IO, Any, Callable, Mapping

from critical_consensus.market_data import MarketBar, read_mt5_csv
from critical_consensus.mt5_agent import (AgentPolicy, DecisionJournal, MT5Agent, PaperBroker,
                                          codes, i18n, load_policy, trade_event_payload)
from critical_consensus.mt5_agent.i18n import describe, describe_many, normalize_language, text
from operator_answers import ANSWER_VERDICTS, ApprovalState, verdict as _verdict

LANGUAGE_ENV_VAR = "MT5_AGENT_LANGUAGE"
EXECUTION_CHOICES = ("auto", "confirm")


# --------------------------------------------------------------------- language
def _language_from_argv(argv: list[str], env: Mapping[str, str] = None) -> str:
    """Language used to build the parser, before the policy file is known."""
    env = os.environ if env is None else env
    candidate = None
    # argparse keeps the last occurrence of a repeated flag, so help/error text must use it too.
    for index, item in enumerate(argv):
        if item.startswith("--language="):
            candidate = item.split("=", 1)[1]
        elif item == "--language" and index + 1 < len(argv):
            candidate = argv[index + 1]
    if candidate is not None:
        try:
            return normalize_language(candidate)
        except ValueError:
            pass  # let the real parser report the rejected value
    try:
        return normalize_language(env.get(LANGUAGE_ENV_VAR) or i18n.DEFAULT_LANGUAGE)
    except ValueError:
        return i18n.DEFAULT_LANGUAGE


def _journal_language(journal: DecisionJournal, run_id: str | None = None) -> str | None:
    """Language the recorded run used, for a report issued without a flag.

    Scoped to ``run_id`` when one is given: a report on an old run must not speak the
    language of whichever later run happens to be newest in the shared journal.
    """
    for event in reversed(journal.events(limit=500, event_type="agent_cycle", run_id=run_id)):
        value = event["payload"].get("ui_language")
        if value:
            try:
                return normalize_language(value)
            except ValueError:
                return None
    return None


def _recorded_language(db_path: str, run_id: str | None = None) -> str | None:
    """Language of the newest recorded cycle, read without creating or modifying the file."""
    path = Path(db_path)
    if not path.is_file():
        return None
    try:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        query = ("SELECT payload FROM audit_events WHERE event_type = 'agent_cycle'"
                 + (" AND run_id = ?" if run_id is not None else "")
                 + " ORDER BY seq DESC LIMIT 1")
        row = connection.execute(query,
                                 ((run_id,) if run_id is not None else ())).fetchone()
    except sqlite3.Error:
        return None
    finally:
        connection.close()
    if row is None:
        return None
    try:
        payload = json.loads(row[0])
    except (TypeError, ValueError):
        return None
    value = payload.get("ui_language") if isinstance(payload, Mapping) else None
    if not value:
        return None
    try:
        return normalize_language(value)
    except ValueError:
        return None


def _language_for(args: argparse.Namespace, policy: AgentPolicy | None = None, *,
                  journal: DecisionJournal | None = None,
                  journal_language: str | None = None) -> str:
    """Flag beats the environment, which beats the policy or the journal, then the default."""
    fallback = getattr(args, "_fallback_language", i18n.DEFAULT_LANGUAGE)
    candidates = [getattr(args, "language", None), os.environ.get(LANGUAGE_ENV_VAR)]
    if policy is not None:
        candidates.append(getattr(policy, "ui_language", None))
    elif journal is not None:
        candidates.append(_journal_language(journal, getattr(args, "run_id", None)))
    elif journal_language is not None:
        candidates.append(journal_language)
    for candidate in candidates:
        if candidate:
            try:
                return normalize_language(candidate)
            except ValueError:
                raise ValueError(text("err.language", fallback, value=candidate)) from None
    return fallback


def _language_type(language: str) -> Callable[[str], str]:
    def parse(value: str) -> str:
        try:
            return normalize_language(value)
        except ValueError:
            raise argparse.ArgumentTypeError(
                text("err.language", language, value=value)) from None
    return parse


# --------------------------------------------------------------------- arguments
def _positive_finite(language: str) -> Callable[[str], float]:
    def parse(value: str) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise argparse.ArgumentTypeError(text("err.number", language, value=value)) from None
        if not math.isfinite(number) or number <= 0:
            raise argparse.ArgumentTypeError(
                text("err.positive_number", language, value=value))
        return number
    return parse


def _positive_int(language: str) -> Callable[[str], int]:
    def parse(value: str) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            raise argparse.ArgumentTypeError(text("err.integer", language, value=value)) from None
        if number < 1:
            raise argparse.ArgumentTypeError(text("err.at_least_one", language, value=value))
        return number
    return parse


def _run_id(language: str) -> Callable[[str], str]:
    def parse(value: str) -> str:
        text_value = str(value).strip()
        if not text_value:
            raise argparse.ArgumentTypeError(text("err.empty_run_id", language))
        # The journal applies the same rule; rejecting here keeps the error localized and
        # fails before any run starts instead of at DecisionJournal construction.
        if len(text_value) > 64 or any(char in text_value for char in ("\x00", "\n", "\r")):
            raise argparse.ArgumentTypeError(text("err.run_id_invalid", language))
        return text_value
    return parse


# ------------------------------------------------------------------- confirmation
def _print_proposal(proposal: Mapping[str, Any], sizing: Mapping[str, Any], *, language: str,
                    digits: int, stream: IO[str]) -> None:
    def price(value: Any) -> str:
        return f"{float(value):.{digits}f}"

    reward = float(sizing["risk_value"]) * float(proposal["risk_reward"])
    rows = [("confirm.direction", str(proposal["direction"])),
            ("confirm.volume", f"{float(sizing['volume']):.2f}"),
            ("confirm.entry", price(proposal["entry_price"])),
            ("confirm.stop", price(proposal["stop_loss"])),
            ("confirm.take_profit", price(proposal["take_profit"])),
            ("confirm.risk", f"{float(sizing['risk_value']):.2f}"),
            ("confirm.reward", f"{reward:.2f}")]
    if "equity" in sizing:
        rows.append(("confirm.equity", f"{float(sizing['equity']):.2f}"))
    stream.write(text("confirm.title", language, symbol=proposal["symbol"]) + "\n")
    for key, value in rows:
        stream.write(f"  {text(key, language)}: {value}\n")


def _make_approver(*, language: str, state: ApprovalState, digits: int, out: IO[str],
                   source: IO[str]) -> Callable[[Mapping[str, Any], Mapping[str, Any]], bool]:
    """The confirm-mode operator: only an explicit yes lets a position through."""

    def approve(proposal: Mapping[str, Any], sizing: Mapping[str, Any]) -> bool:
        if state.accept_all:
            return True
        if state.decline_all:
            return False
        _print_proposal(proposal, sizing, language=language, digits=digits, stream=out)
        while True:
            out.write(text("confirm.question", language))
            out.flush()
            line = source.readline()
            if not line:  # no keyboard: an unconfirmed proposal is never an approved one
                state.decline_all = True
                out.write("\n" + text("out.non_interactive", language) + "\n")
                out.flush()
                return False
            verdict = _verdict(line)
            if verdict == "y":
                out.write(text("confirm.accepted", language) + "\n")
                return True
            if verdict == "n":
                out.write(text("confirm.declined", language) + "\n")
                return False
            if verdict == "a":
                state.accept_all = True
                out.write(text("confirm.auto_accepted", language) + "\n")
                return True
            if verdict == "q":
                state.stop_requested = True
                out.write(text("confirm.stopped", language) + "\n")
                return False
    return approve


# -------------------------------------------------------------------------- bars
def _load_bars(args, policy: AgentPolicy,
               language: str) -> tuple[list[MarketBar], tuple[int, str] | None]:
    """Bars plus the account identity they came from.

    A CSV export carries no account; a live read returns the terminal identity so the paper
    execution layer can present it to the agent's login/server pins instead of the fixed
    local identity, which would make every pinned live run fail as a mismatch.
    """
    if args.source == "csv":
        if not args.csv:
            raise RuntimeError(text("out.need_csv", language))
        return read_mt5_csv(args.csv, symbol=policy.symbol, timeframe=policy.timeframe,
                            source_timezone=args.source_timezone), None
    # The adapter, not market_data.fetch_mt5_bars: MT5 stamps bars with the broker wall
    # clock, so the policy offset has to be subtracted before any freshness or window check.
    from critical_consensus.mt5_agent.backend import LiveMT5Backend

    backend = LiveMT5Backend(terminal_path=args.terminal_path,
                             server_utc_offset_seconds=policy.server_utc_offset_seconds)
    try:
        account = backend.account()
        # The terminal is only being read for history, but a session on a real account is a
        # signal that the wrong terminal is attached to this research box. Refuse the source.
        if policy.require_demo and not account.is_demo:
            raise RuntimeError(text("out.live_real_account", language))
        if policy.expected_login is not None and account.login != policy.expected_login:
            raise RuntimeError(text("out.live_login_mismatch", language, actual=account.login,
                                    expected=policy.expected_login))
        if policy.expected_server is not None and account.server != policy.expected_server:
            raise RuntimeError(text("out.live_server_mismatch", language, actual=account.server,
                                    expected=policy.expected_server))
        return (backend.bars(policy.symbol, policy.timeframe, args.live_bars),
                (account.login, account.server))
    finally:
        backend.shutdown()


def _drain_trades(broker: PaperBroker, journal: DecisionJournal, already: int, *,
                  execution_mode: str) -> int:
    """Journal fills that the broker closed while the clock jumped forward."""
    for trade in broker.trades[already:]:
        journal.record("paper_trade_closed",
                       trade_event_payload(trade, execution_mode=execution_mode))
    return len(broker.trades)


# ----------------------------------------------------------------------- commands
def cmd_run(args) -> int:
    language = _language_for(args)
    try:
        policy = load_policy(args.policy)
    except i18n.UnsupportedLanguage as exc:
        # The third precedence level is the policy's own ui_language; if that value is junk,
        # report it in the flag/environment/default language rather than in English.
        raise ValueError(text("err.language", language, value=exc.value)) from None
    language = _language_for(args, policy)
    # The error envelope in main() must speak the language the command actually resolved,
    # not the one guessed from argv before the policy was read.
    args._resolved_language = language
    # The journal records the language the run actually spoke, so a later report can speak it
    # too; the policy's ui_language is only a default, not the effective choice.
    policy = dataclasses.replace(policy, ui_language=language)
    if args.fresh and not args.confirm:
        # Clearing the journal destroys the audit history of every earlier run.
        print(text("out.fresh_needs_confirm", language), file=sys.stderr)
        return 2
    if args.execution is not None:
        # replace() re-runs AgentPolicy validation, so --execution auto cannot smuggle an
        # unattended run onto a policy that does not require a demo account.
        policy = dataclasses.replace(policy, execution_mode=args.execution)
    bars, identity = _load_bars(args, policy, language)
    if len(bars) <= policy.strategy.min_bars:
        print(text("out.not_enough_bars", language, bars=len(bars),
                   minimum=policy.strategy.min_bars + 1), file=sys.stderr)
        return 2
    spec = policy.symbol_specification()
    login, server = identity if identity is not None else (1_000_000_000, "PAPER-LOCAL")
    broker = PaperBroker(bars={policy.symbol: bars}, spec=spec, login=login, server=server,
                         starting_balance=args.balance, currency=args.currency,
                         server_utc_offset_seconds=policy.server_utc_offset_seconds)
    run_id = args.run_id or f"run-{uuid.uuid4().hex[:12]}"
    journal = DecisionJournal(args.db, run_id=run_id)
    if args.fresh:
        journal.reset()
    state = ApprovalState()
    approver = None
    if policy.confirmation_required and policy.mode == "paper":
        approver = _make_approver(language=language, state=state, digits=spec.digits,
                                  out=sys.stderr, source=sys.stdin)
    agent = MT5Agent(backend=broker, policy=policy, journal=journal, clock=broker.now,
                     approver=approver)
    counts = {"PAPER_OPENED": 0, "BLOCKED": 0, "NO_SIGNAL": 0, "OBSERVED": 0}
    journaled_trades = 0
    # The clock stops at the last cycle the operator actually saw: seeking further would book
    # fills from bars that no decision covered, and the report would not match the run.
    stopped_at = None
    operator_stopped = False
    for bar in bars[policy.strategy.min_bars:][:args.limit_cycles]:
        broker.seek(bar.time)
        stopped_at = bar.time
        result = agent.run_cycle()
        counts[result.status] = counts.get(result.status, 0) + 1
        journaled_trades = _drain_trades(broker, journal, journaled_trades,
                                         execution_mode=policy.execution_mode)
        if args.verbose:
            print(f"{bar.time.isoformat()} {describe(result.status, language)}: "
                  f"{describe_many(result.blockers, language)}", file=sys.stderr)
        if state.stop_requested:
            operator_stopped = True
            break
    # Close the book, then drain again: a stop can fill in the same seek that ends the run, and
    # a trade missing from the journal makes the report disagree with itself.
    if stopped_at is not None:
        broker.seek(stopped_at)
    journaled_trades = _drain_trades(broker, journal, journaled_trades,
                                     execution_mode=policy.execution_mode)
    agent.close_positions(codes.END_OF_RUN)
    report = journal.report(initial_equity=args.balance, run_id=run_id)
    exit_reasons: dict[str, int] = {}
    for trade in broker.trades:
        exit_reasons[trade.exit_reason] = exit_reasons.get(trade.exit_reason, 0) + 1
    wins = sum(1 for trade in broker.trades if trade.pnl > 0)
    cycles = sum(counts.values())
    print(json.dumps({"run_id": run_id, "language": language, "mode": policy.mode,
                      "execution_mode": policy.execution_mode, "symbol": policy.symbol,
                      "operator_stopped": operator_stopped,
                      "cycles": cycles,
                      "paper_positions_opened": counts.get("PAPER_OPENED", 0),
                      "blocked": counts.get("BLOCKED", 0),
                      "no_signal": counts.get("NO_SIGNAL", 0),
                      "observed": counts.get("OBSERVED", 0),
                      "paper_trades_closed": len(broker.trades),
                      "paper_trades_journaled": report["event_counts"].get("paper_trade_closed", 0),
                      "paper_wins": wins,
                      "exit_reason_histogram": exit_reasons,
                      "exit_reason_labels": {reason: describe(reason, language)
                                             for reason in exit_reasons},
                      "closing_equity_recorded": agent.closing_equity_recorded,
                      "closing_equity_error": agent.closing_equity_error,
                      "realized_pnl": round(broker.realized_pnl, 4),
                      "final_equity": round(broker.equity(), 4),
                      "return_fraction": round(broker.equity() / args.balance - 1.0, 6),
                      "max_drawdown": round(report["max_drawdown_account_currency"], 4),
                      "blocker_histogram": report["blocker_histogram"],
                      "blocker_labels": {code: describe(code, language)
                                         for code in report["blocker_histogram"]},
                      "audit_chain_valid": report["verified_chain"]["valid"],
                      "anchor_hash": report["anchor_hash"],
                      "last_hash": report["verified_chain"]["last_hash"],
                      "broker_order_sent": False}, ensure_ascii=False, indent=2))
    # Human-readable lines go to stderr, so stdout is exactly one parseable JSON object.
    print(text("out.mode_line", language, mode=describe(policy.execution_mode, language),
               symbol=policy.symbol, source=args.source), file=sys.stderr)
    if operator_stopped:
        print(text("out.operator_stopped", language), file=sys.stderr)
    print(text("out.summary", language, run_id=run_id, cycles=cycles,
               opened=counts.get("PAPER_OPENED", 0), trades=len(broker.trades),
               pnl=f"{broker.realized_pnl:.2f}", equity=f"{broker.equity():.2f}",
               chain=text("out.yes" if report["verified_chain"]["valid"] else "out.no",
                          language)), file=sys.stderr)
    print(text("out.disclaimer", language), file=sys.stderr)
    journal.close()
    return 0


def cmd_report(args) -> int:
    # Resolve before opening the journal: a junk flag or environment value must fail without
    # creating the database, the same contract `reset` keeps.
    language = _language_for(args)
    args._resolved_language = language
    journal = DecisionJournal(args.db, run_id=args.run_id)
    report = journal.report(initial_equity=args.balance)
    # Without a flag or environment override, a report speaks the language its run used.
    language = _language_for(args, journal=journal)
    args._resolved_language = language
    report["language"] = language
    report["note"] = text("report.note", language)
    report["blocker_labels"] = {code: describe(code, language)
                               for code in report["blocker_histogram"]}
    if args.verify_anchor:
        status = journal.chain_status(run_id=args.run_id, anchor=args.verify_anchor)
        report["requested_anchor"] = args.verify_anchor
        report["anchor_check"] = status
        if not status["valid"]:
            # stdout still carries exactly one JSON object (the command's contract); the
            # failed check is visible in anchor_check and exit 3, with a human line on stderr.
            print(json.dumps(report, ensure_ascii=False, indent=2))
            print(text("out.anchor_mismatch", language), file=sys.stderr)
            journal.close()
            return 3
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["verified_chain"]["valid"]:
        # The JSON carries the machine verdict; without a human line an operator watching the
        # terminal would miss that an earlier row was edited.
        print(text("out.chain_invalid", language), file=sys.stderr)
    journal.close()
    return 0


def cmd_reset(args) -> int:
    # A refused reset must not create or touch the file, but an existing journal may still be
    # read so the message speaks the language of the run it would have destroyed.
    recorded = _recorded_language(args.db, getattr(args, "run_id", None))
    language = _language_for(args, journal_language=recorded)
    args._resolved_language = language
    if not args.confirm:
        print(text("out.reset_needs_confirm", language), file=sys.stderr)
        return 2
    journal = DecisionJournal(args.db)
    try:
        language = _language_for(args, journal=journal)
        args._resolved_language = language
        journal.reset()
    finally:
        journal.close()
    print(text("out.journal_cleared", language))
    return 0


def _localized_parser(language: str) -> Callable[..., argparse.ArgumentParser]:
    """An ArgumentParser whose usage prefix and error line speak ``language``."""

    class LocalizedParser(argparse.ArgumentParser):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs.setdefault("allow_abbrev", False)
            # argparse's built-in help action and section titles are English; supply our own.
            kwargs.setdefault("add_help", False)
            super().__init__(*args, **kwargs)
            self.add_argument("-h", "--help", action="help", help=text("cli.help", language))

        def format_usage(self) -> str:
            # argparse hard-codes "usage:"; the rest of the usage line is flag names.
            return super().format_usage().replace("usage:", text("cli.usage", language), 1)

        def format_help(self) -> str:
            rendered = super().format_help()
            rendered = rendered.replace("usage:", text("cli.usage", language), 1)
            rendered = rendered.replace("positional arguments:", text("cli.positional", language))
            rendered = rendered.replace("options:", text("cli.options", language))
            return rendered

        def error(self, message: str) -> None:
            self.print_usage(sys.stderr)
            self.exit(2, text("out.error", language, reason=message) + "\n")

    return LocalizedParser


def build_parser(language: str) -> argparse.ArgumentParser:
    parser_class = _localized_parser(language)
    parser = parser_class(description=text("cli.description", language))
    # --db and --language are accepted before or after the subcommand. SUPPRESS on the
    # subparsers stops an omitted flag from overwriting the value the root parser parsed.
    parser.add_argument("--language", type=_language_type(language), default=argparse.SUPPRESS,
                        metavar="ru|en|vi", help=text("cli.language", language))
    parser.add_argument("--db", default="agent_data/mt5_agent.db", help=text("cli.db", language))
    sub = parser.add_subparsers(dest="command", required=True, parser_class=parser_class)

    def add_common(target: argparse.ArgumentParser) -> None:
        target.add_argument("--language", type=_language_type(language), default=argparse.SUPPRESS,
                            metavar="ru|en|vi", help=text("cli.language", language))
        target.add_argument("--db", default=argparse.SUPPRESS, help=text("cli.db", language))

    run = sub.add_parser("run", help=text("cli.run", language))
    add_common(run)
    run.add_argument("--policy", required=True, type=Path, help=text("cli.policy", language))
    run.add_argument("--source", choices=["csv", "live"], default="csv",
                     help=text("cli.source", language))
    run.add_argument("--csv", type=Path, help=text("cli.csv", language))
    run.add_argument("--source-timezone", default=None, help=text("cli.source_timezone", language))
    run.add_argument("--terminal-path", default=None, help=text("cli.terminal_path", language))
    run.add_argument("--live-bars", type=_positive_int(language), default=2_000,
                     help=text("cli.live_bars", language))
    run.add_argument("--balance", type=_positive_finite(language), default=10_000.0,
                     help=text("cli.balance", language))
    run.add_argument("--currency", default="USD", help=text("cli.currency", language))
    run.add_argument("--limit-cycles", type=_positive_int(language), default=100_000,
                     help=text("cli.limit_cycles", language))
    run.add_argument("--run-id", type=_run_id(language), default=None,
                     help=text("cli.run_id", language))
    run.add_argument("--execution", choices=EXECUTION_CHOICES, default=None,
                     help=text("cli.execution", language))
    run.add_argument("--fresh", action="store_true", help=text("cli.fresh", language))
    run.add_argument("--confirm", action="store_true", help=text("cli.confirm", language))
    run.add_argument("--verbose", action="store_true", help=text("cli.verbose", language))
    run.set_defaults(func=cmd_run)

    report = sub.add_parser("report", help=text("cli.report", language))
    add_common(report)
    report.add_argument("--balance", type=_positive_finite(language), default=None,
                        help=text("cli.balance_report", language))
    report.add_argument("--run-id", type=_run_id(language), default=None,
                        help=text("cli.run_id", language))
    report.add_argument("--verify-anchor", default=None, help=text("cli.verify_anchor", language))
    report.set_defaults(func=cmd_report)

    reset = sub.add_parser("reset", help=text("cli.reset", language))
    add_common(reset)
    reset.add_argument("--confirm", action="store_true", help=text("cli.confirm", language))
    reset.set_defaults(func=cmd_reset)
    return parser


def main(argv: list[str] | None = None) -> int:
    # Operator answers are not ASCII only ("да", "tất cả"). stdin keeps undecodable bytes as
    # surrogates so a cp1251 console can be recovered; output never carries them and must not
    # crash the run, so it replaces instead.
    i18n.configure_console_output(sys.stdin, errors="surrogateescape")
    i18n.configure_console_output(sys.stdout, sys.stderr)
    argv = list(sys.argv[1:]) if argv is None else list(argv)
    fallback_language = _language_from_argv(argv)
    args = None
    try:
        args = build_parser(fallback_language).parse_args(argv)
        args._fallback_language = fallback_language
        # Resolve early so a junk MT5_AGENT_LANGUAGE is a clean localized error, not a traceback.
        _language_for(args)
        return int(args.func(args))
    except (RuntimeError, ValueError, FileNotFoundError, sqlite3.Error) as exc:
        # A missing terminal, an unreadable export, a rejected policy or an unusable journal
        # must exit cleanly with the reason instead of a traceback. Prefer the language the
        # command itself resolved; argv/env is only a fallback.
        language = getattr(args, "_resolved_language", None) or fallback_language
        print(text("out.error", language, reason=exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
