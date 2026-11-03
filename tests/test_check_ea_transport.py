"""The advisor's log is the only proof that a queued command could reach the broker.

Two failures this suite pins come from the live run on 2026-11-02: `system_status.ps1` read a
log file whose date was hardcoded, so it printed yesterday's lines while today's polls were all
`error=4014`, and the chain looked alive because the runner and its bridge really were alive.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import re
from pathlib import Path

import pytest


def _root() -> Path:
    return Path(__file__).resolve().parents[1]


def _checker():
    path = _root() / "scripts" / "check_ea_transport.py"
    spec = importlib.util.spec_from_file_location("check_ea_transport", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CHECKER = _checker()

# Exactly as MetaTrader writes it: two-char tag, level, local clock, chart, message.
BLOCKED_4014 = ("RK\t0\t11:09:51.325\tCriticalConsensusEA_Demo (XAUUSD,H1)\t"
                "BLOCKED: kill-switch state unavailable; HTTP -1, error=4014\n")
ARMED = ("AB\t0\t07:06:35.100\tCriticalConsensusEA_Demo (XAUUSD,H1)\t"
         "BLOCKED: kill switch is active or state is invalid.\n")
SENT = ("FD\t0\t07:00:10.000\tCriticalConsensusEA_Demo (XAUUSD,H1)\t"
        "Order accepted; reconcile actual fill/position from the terminal.\n")


def _write_log(dir_path: Path, name: str, text: str, *, mtime_offset: int = 0) -> Path:
    file = dir_path / name
    file.write_bytes(b"\xff\xfe" + text.encode("utf-16-le") if text else b"")
    stamp = dt.datetime.now().timestamp() - mtime_offset
    os.utime(file, (stamp, stamp))
    return file


@pytest.fixture()
def log_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "logs"
    directory.mkdir()
    return directory


def test_the_newest_file_is_read_not_the_one_a_script_named(log_dir: Path) -> None:
    """A date baked into a status script means tomorrow's failure is invisible to it."""
    _write_log(log_dir, "20261101.log", ARMED, mtime_offset=86_400)
    _write_log(log_dir, "20261102.log", BLOCKED_4014)
    verdict = CHECKER.read_and_classify(log_dir, now=dt.datetime(2026, 11, 2, 11, 10, 30))
    assert verdict["state"] == "request_not_allowed"
    assert verdict["log"].endswith("20261102.log")
    assert verdict["last_line_age_seconds"] == 39.0


def test_a_missing_directory_is_not_reported_as_a_healthy_chain(log_dir: Path) -> None:
    assert CHECKER.read_and_classify(log_dir / "absent")["state"] == "no_log"
    empty = log_dir / "empty"
    empty.mkdir()
    _write_log(empty, "20261102.log", "")
    assert CHECKER.read_and_classify(empty)["state"] == "no_log"
def test_a_log_without_advisor_lines_says_the_advisor_is_not_running(log_dir: Path) -> None:
    _write_log(log_dir, "20261102.log",
               "KK\t0\t10:14:22.926\tNetwork\t'114929502': connection lost\n")
    assert CHECKER.read_and_classify(log_dir)["state"] == "never_polled"


def test_a_quiet_advisor_is_a_different_failure_from_a_refusing_one(log_dir: Path) -> None:
    """No log line at all is worse than a BLOCKED line: nothing is even attempting to poll."""
    _write_log(log_dir, "20261102.log", BLOCKED_4014 + BLOCKED_4014)
    verdict = CHECKER.read_and_classify(log_dir, now=dt.datetime(2026, 11, 2, 11, 20),
                                        stale_after_seconds=120)
    assert verdict["state"] == "ea_silent"
    # The reason it had reached before going quiet survives, or the operator gets a guess.
    assert verdict["stale_from"] == "request_not_allowed"
    assert verdict["blocked_streak"] == 2


def test_an_armed_stop_is_not_reported_as_a_broken_transport(log_dir: Path) -> None:
    """The stop is the operator's own act; the transport under it demonstrably works."""
    _write_log(log_dir, "20261102.log", ARMED)
    verdict = CHECKER.read_and_classify(log_dir, now=dt.datetime(2026, 11, 2, 7, 7))
    assert verdict["state"] == "kill_switch_armed"
    assert verdict["healthy"] is True
    stale = CHECKER.read_and_classify(log_dir, now=dt.datetime(2026, 11, 2, 9, 7))
    assert stale["state"] == "kill_switch_armed"


def test_a_poll_that_reached_the_bridge_and_sent_an_order_is_healthy(log_dir: Path) -> None:
    _write_log(log_dir, "20261102.log", BLOCKED_4014 + SENT)
    verdict = CHECKER.read_and_classify(log_dir, now=dt.datetime(2026, 11, 2, 7, 1))
    assert (verdict["state"], verdict["healthy"]) == ("order_accepted", True)


@pytest.mark.parametrize("message,expected", [
    ("BLOCKED: kill-switch state unavailable; HTTP -1, error=4014", "request_not_allowed"),
    ("BLOCKED: kill-switch state unavailable; HTTP 1001, error=5203", "bridge_unreachable"),
    ("BLOCKED: kill-switch state unavailable; HTTP 401", "token_rejected"),
    ("BLOCKED: BridgeToken is empty or not 24..256 characters of [A-Za-z0-9._~-]; "
     "refusing to initialize.", "token_missing"),
    ("BLOCKED: bridge token or kill-switch URL is invalid.", "token_missing"),
    ("BLOCKED: BridgeUrl rejected (host is not loopback)", "token_missing"),
    ("BLOCKED: kill switch is active or state is invalid.", "kill_switch_armed"),
    ("BLOCKED: kill-switch response is not a flat JSON object: trailing bytes",
     "bridge_response_invalid"),
    ("BLOCKED: command expired.", "command_expired"),
    ("BLOCKED: unsupported direction: 7", "malformed_command"),
    ("BLOCKED: invalid BUY SL/TP geometry.", "malformed_command"),
    ("BLOCKED: account is not demo. This EA does not execute live trades.", "not_demo"),
    ("BLOCKED: demo mode and explicit user approval are required.", "not_demo"),
    ("BLOCKED: MagicNumber must be a positive integer", "chart_input_invalid"),
    ("BLOCKED: cannot persist idempotency tombstones.", "tombstone_unavailable"),
    ("BLOCKED: SL/TP inside broker stops level (20 points).", "broker_gate_refused"),
    ("BLOCKED: no current tick.", "broker_gate_refused"),
    ("DUPLICATE BLOCKED: command or order already attempted.", "duplicate_refused"),
    ("CriticalConsensusEA initialized. Demo execution compiled in.", "idle"),
])
def test_each_advisor_verdict_is_named(message: str, expected: str) -> None:
    lines = CHECKER.ea_lines(f"QQ\t0\t01:02:03.004\tCriticalConsensusEA_Demo (XAUUSD,H1)"
                             f"\t{message}\n", date=dt.date(2026, 11, 2))
    assert CHECKER.classify(lines, now=None)["state"] == expected


def test_every_blocked_line_the_source_can_print_is_named() -> None:
    """The EA's strings and this checker drift apart silently otherwise: both sides are read.

    A guard added to the advisor without a rule here would be reported as a healthy poll, which
    is the exact way this chain died twice on 2026-11-02 without saying so.
    """
    source = (_root() / "mql5" / "CriticalConsensusEA.mq5").read_text(encoding="utf-8")
    printed = re.findall(r'Print\("(BLOCKED[^;]*|DUPLICATE BLOCKED[^;]*)', source)
    assert len(printed) > 30
    unnamed = []
    for fragment in printed:
        message = fragment.replace('", "', " ")
        lines = CHECKER.ea_lines(f"QQ\t0\t01:02:03.004\tCriticalConsensusEA_Demo (XAUUSD,H1)"
                                 f"\t{message}\n", date=dt.date(2026, 11, 2))
        state = CHECKER.classify(lines, now=None)["state"]
        if state in ("polling", "unclassified_blocked"):
            unnamed.append((message, state))
    assert unnamed == []


def test_the_operator_scripts_do_not_name_a_day_in_code() -> None:
    """Both fixes this round came from the same habit: a date written into a launcher.

    `system_status.ps1` read `20261101.log` and so printed yesterday's clean tail over today's
    refusals; `system_start.ps1` named `demo_live_20261101.db` and so appended every later run to
    the previous day's journal, where that day's risk baseline and served ids read as history.
    Comments may name the day they mean; executable lines may not.
    """
    offenders = []
    for name in ("system_start.ps1", "system_status.ps1", "system_stop.ps1"):
        script = _root() / "scripts" / name
        for number, line in enumerate(script.read_text(encoding="utf-8").splitlines(), start=1):
            if line.strip().startswith("#"):
                continue
            if re.search(r"20\d{6}", line):
                offenders.append(f"{name}:{number}: {line.strip()[:60]}")
    assert offenders == []


def test_command_line_exit_codes_separate_dead_from_absent(tmp_path: Path, capsys) -> None:
    """The command line is what an operator or a scheduler runs, so its code is the contract.

    The real clock is used here, so the staleness window is opened instead of a timestamp
    fixture: a log written at 11:09 must still read as the refusal it records at 11:45.
    """
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    assert CHECKER.main(["--log-dir", str(tmp_path / "absent")]) == 2
    assert "no_log" in capsys.readouterr().out

    _write_log(log_dir, "20261102.log", BLOCKED_4014)
    assert CHECKER.main(["--log-dir", str(log_dir), "--stale-seconds", "86400"]) == 1
    assert "request_not_allowed" in capsys.readouterr().out

    _write_log(log_dir, "20261102.log", ARMED)
    assert CHECKER.main(["--log-dir", str(log_dir), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "kill_switch_armed"


def test_a_silence_without_a_witness_exits_two_not_one(tmp_path: Path, capsys) -> None:
    """Exit 1 means "the pipe is dead"; an unaskable bridge may not claim that.

    The real bridge is stood in for by an ephemeral server that answers 404, which is what the
    runner on `:8000` does until the operator restarts it with this checker's build.
    """
    import http.server
    import threading

    class OnlyFourOhFour(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_error(404)

        def log_message(self, *a: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), OnlyFourOhFour)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    _write_log(log_dir, "20261102.log", SENT)
    try:
        code = CHECKER.main(["--log-dir", str(log_dir),
                             "--bridge-url", f"http://127.0.0.1:{server.server_port}/v1/health"])
        printed = capsys.readouterr().out
    finally:
        server.shutdown()
    assert code == 2
    assert "unwitnessed_silence (UNKNOWN)" in printed


# The advisor prints a line only when it is refused or when something happens, so on 2026-11-02
# a chain that had just filled a broker order read as BROKEN: the log's newest line was the
# "Order accepted" stamp, and after that it went quiet on purpose. These cases pin that the
# bridge's own record of an authenticated poll settles a silence the log cannot settle.
LIVE_POLL = {"ok": True, "ea_polls": 4128, "last_ea_poll_at": "2026-11-02T11:19:56+00:00",
             "last_ea_poll_path": "/v1/risk/kill-switch", "ea_poll_age_seconds": 4.0,
             "served": 1, "queued": 1, "expired": 0, "refused": 0}


def test_a_quiet_log_with_a_fresh_bridge_poll_is_a_live_transport(log_dir: Path) -> None:
    _write_log(log_dir, "20261102.log", BLOCKED_4014 + SENT)
    verdict = CHECKER.read_and_classify(
        log_dir, now=dt.datetime(2026, 11, 2, 11, 20), stale_after_seconds=120,
        bridge=lambda: dict(LIVE_POLL))
    assert (verdict["state"], verdict["healthy"]) == ("polling", True)
    assert verdict["stale_from"] == "order_accepted"
    assert verdict["bridge_poll_age_seconds"] == 4.0
    assert verdict["bridge_polls"] == 4128
    assert "only when it is refused" in verdict["detail"]


def test_a_stale_bridge_poll_confirms_the_silence_rather_than_erasing_it(log_dir: Path) -> None:
    _write_log(log_dir, "20261102.log", SENT)
    stale = dict(LIVE_POLL, ea_poll_age_seconds=900.0)
    verdict = CHECKER.read_and_classify(
        log_dir, now=dt.datetime(2026, 11, 2, 11, 20), bridge=lambda: stale)
    assert (verdict["state"], verdict["healthy"]) == ("ea_silent", False)
    assert "900.0 s ago" in verdict["bridge_detail"]


def test_a_silence_is_never_overruled_by_a_bridge_that_cannot_answer(log_dir: Path) -> None:
    """A mute bridge excuses nothing: the log's own last refusal stays the verdict."""
    _write_log(log_dir, "20261102.log", BLOCKED_4014)
    for error, expected in (
            ("unreachable", "nothing answered"),
            ("http_401", "not its token"),
            ("http_404", "older build"),
            ("not_json", "not with the bridge")):
        verdict = CHECKER.read_and_classify(
            log_dir, now=dt.datetime(2026, 11, 2, 11, 20),
            bridge=lambda error=error: {"error": error})
        assert (verdict["state"], verdict["healthy"]) == ("ea_silent", False)
        assert expected in verdict["bridge_detail"]


def test_a_silence_after_a_proven_send_is_not_reported_as_a_dead_pipe(log_dir: Path) -> None:
    """Measured on the live chain at 06:43 UTC on 2026-11-02, 63 min after a filled order.

    The advisor prints nothing while the queue is empty, the bridge answering `:8000` is the
    runner build from before `/v1/health`, and the status line therefore called a working chain
    BROKEN and promised a queued command would expire unheard. An empty port (`unreachable`) is
    still a proven break; a listener that will not answer this question is not.
    """
    _write_log(log_dir, "20261102.log", SENT)
    for error in ("http_404", "http_401", "not_json"):
        verdict = CHECKER.read_and_classify(
            log_dir, now=dt.datetime(2026, 11, 2, 11, 20),
            bridge=lambda error=error: {"error": error})
        assert verdict["state"] == "unwitnessed_silence"
        assert verdict["stale_from"] == "order_accepted"
        assert verdict["healthy"] is False
        assert "not a broken pipe" in verdict["detail"]
    unreachable = CHECKER.read_and_classify(
        log_dir, now=dt.datetime(2026, 11, 2, 11, 20),
        bridge=lambda: {"error": "unreachable"})
    assert unreachable["state"] == "ea_silent"


def test_a_named_refusal_in_the_log_is_not_replaced_by_a_counter(log_dir: Path) -> None:
    """The bridge is asked only where the log has nothing to say: a real block stays named."""
    _write_log(log_dir, "20261102.log", BLOCKED_4014)
    asked = []

    def probe() -> dict[str, object]:
        asked.append(1)
        return dict(LIVE_POLL)

    verdict = CHECKER.read_and_classify(log_dir, now=dt.datetime(2026, 11, 2, 11, 10, 30),
                                        bridge=probe)
    assert (verdict["state"], verdict["healthy"]) == ("request_not_allowed", False)
    assert asked == []


def test_a_log_without_advisor_lines_is_rescued_the_same_way(log_dir: Path) -> None:
    _write_log(log_dir, "20261102.log",
               "KK\t0\t10:14:22.926\tNetwork\t'114929502': connection lost\n")
    verdict = CHECKER.read_and_classify(log_dir, bridge=lambda: dict(LIVE_POLL))
    assert (verdict["state"], verdict["healthy"]) == ("polling", True)


def test_the_health_probe_never_raises(tmp_path: Path) -> None:
    """A transport check that dies on a socket error is worse than one that says nothing."""
    assert CHECKER.probe_bridge("http://127.0.0.1:1/v1/health", timeout=0.5) == {
        "error": "unreachable"}
    answered = tmp_path / "not-a-url"
    assert CHECKER.probe_bridge("http://127.0.0.1:1/v1/health",
                                token="", timeout=0.1)["error"] == "unreachable"
    assert CHECKER.probe_bridge("")["error"] == "unreachable"
    assert CHECKER.probe_bridge(str(answered))["error"] == "unreachable"


def test_the_token_comes_from_the_environment_or_the_ignored_file(tmp_path: Path) -> None:
    """The token is never read from an argument: command lines are visible to the whole session."""
    token_file = tmp_path / "bridge_token.txt"
    token_file.write_text("c" * 40 + "\n", encoding="utf-8")
    assert CHECKER.bridge_token(env={}, token_file=token_file) == "c" * 40
    assert CHECKER.bridge_token(env={"MT5_BRIDGE_TOKEN": "d" * 40},
                               token_file=token_file) == "d" * 40
    assert CHECKER.bridge_token(env={}, token_file=tmp_path / "absent.txt") == ""
