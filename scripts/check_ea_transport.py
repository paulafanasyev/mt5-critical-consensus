"""Classify what the advisor's own log says about the transport to the bridge.

The runner can be perfectly healthy while nothing reaches the broker: a queued command only
becomes an order when the EA polls over `WebRequest`, and a terminal restarted with `/config`
loses the loopback allowlist silently (the EA then logs `error=4014` on every poll). This tool
reads the newest `MQL5/logs/<date>.log`, decodes it, and says which of those states the demo
chain is in, so a dead transport is reported as dead instead of looking like a running system.

The log alone cannot prove the advisor is alive: it writes only when it is refused or when
something happens, so a healthy polling loop is silent by design. When the log has nothing to
say — `ea_silent` or `never_polled` — the tool asks the bridge, which records the last moment an
authenticated request reached an endpoint only the advisor polls, and lets that positive
evidence settle the question. The bridge read is one GET to loopback with the token from the
environment; it never sends, orders or changes anything.
"""
from __future__ import annotations

import argparse
import datetime as dt
import http.client
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOG_DIR = Path(r"C:\Program Files\MetaTrader 5\MQL5\logs")
BRIDGE_HEALTH_URL = "http://127.0.0.1:8000/v1/health"
BRIDGE_TOKEN_ENV = "MT5_BRIDGE_TOKEN"
# The same git-ignored file the launcher reads to set the environment variable. The token is
# never accepted as a command-line argument, because arguments are visible to every process.
BRIDGE_TOKEN_FILE = REPO_ROOT / "agent_data" / "demo_bridge" / "bridge_token.txt"
EA_TAG = "CriticalConsensusEA"
# The terminal writes its log lines in its own local time and stamps only the file name with a
# date, so a line's timestamp is meaningless without the day the file carries.
_STALE_DEFAULT_SECONDS = 120.0

_LINE = re.compile(r"^[A-Za-z]{2}\t(?P<level>\d)\t(?P<time>\d{2}:\d{2}:\d{2}\.\d{3})"
                   r"\t(?P<source>[^\t]*)\t(?P<message>.*)$")

# Ordered: the first rule whose needle appears in the last line wins, and the order is the
# diagnosis. `HTTP -1, error=4014` matches both the allow-list rule and the unreachable rule,
# and only the allow list explains it.
_RULES: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("request_not_allowed", ("error=4014",),
     "WebRequest is not allowed for the bridge URL. The terminal's allow list was cleared "
     "(a /config restart does this). Re-add http://127.0.0.1:8000 in Tools -> Options -> "
     "Expert Advisors, or run scripts/setup_demo_terminal.py --apply with the terminal closed."),
    ("token_missing", ("bridgetoken is empty", "bridge token or kill-switch url is invalid",
                       "bridgeurl rejected", "killswitchurl rejected"),
     "The chart has no usable BridgeToken or URL input, so the EA refuses to poll. Type the "
     "token into the chart's inputs; it is never read from disk by design."),
    ("token_rejected", ("http 401",),
     "The bridge answered 401: the token on the chart is not the token the bridge expects."),
    ("kill_switch_armed", ("kill switch is active", "kill switch changed before send"),
     "Transport is fine and the durable stop is armed. Nothing will be ordered until an "
     "operator releases it through --release-kill-switch."),
    ("bridge_response_invalid", ("kill-switch response is not a flat json object",
                                 "kill-switch response has no"),
     "The bridge answered, but not with the kill-switch object the EA requires. A proxy or an "
     "error page on the port would look like this."),
    ("bridge_unreachable", ("error=5203", "http 1001", "http 1002", "http 1003", "http -1",
                            "kill-switch state unavailable"),
     "The EA could not connect to the bridge at all: nothing is listening on the port, or "
     "WebRequest is blocked. Start the runner, which hosts the bridge."),
    ("tombstone_unavailable", ("cannot persist idempotency tombstones",
                               "idempotency tombstones missing after persist"),
     "The EA could not write its own already-sent record, so it refused to send rather than "
     "risk a duplicate order. Check the terminal's writable paths."),
    ("duplicate_refused", ("already attempted",),
     "Transport is fine: the EA refused a command id it has already tried. Idempotency is "
     "working; the runner journals the same refusal."),
    ("chart_input_invalid", ("must be a positive integer", "must be between 1 and",
                             "must be above 0 and at or below"),
     "A chart input is outside the ceiling the guards measure against, so OnInit failed and "
     "the advisor never started. Fix the inputs on the chart."),
    ("not_demo", ("is not demo", "terminal or account trading is not allowed",
                  "demo execution is disabled", "explicit user approval are required"),
     "The EA refused on account or trading permission. Check the signed-in account and the "
     "algo-trading switch."),
    ("command_expired", ("command expired",),
     "The EA picked up a command after its TTL, so it was never sent. The runner journals it as "
     "COMMAND_EXPIRED."),
    ("broker_gate_refused", ("inside broker stops level", "ordercheck failed",
                             "broker volume constraints", "slippage guard triggered",
                             "no current tick", "symbolselect failed",
                             "does not allow full trading", "neither fok nor ioc"),
     "Transport reached the broker and a broker-side guard stopped the send. The command is "
     "still real; the market or the symbol contract refused it."),
    ("malformed_command", ("command body rejected", "required ordercommand field missing",
                           "unsupported characters", "unsupported direction",
                           "expires_at_epoch is not a positive", "ttl exceeds",
                           "invalid volume/sl/tp", "sl/tp geometry", "invalid symbol point",
                           "non-positive level"),
     "The EA reached the bridge but rejected the command body. The bridge keeps its own record; "
     "read the runner journal for the reason."),
)


def decode_log(raw: bytes) -> str:
    """MT5 writes these logs UTF-16 with a BOM; an ASCII build log decodes as utf-8.

    Encoding must be chosen by evidence, not by trial order: decoding a BOM-less UTF-8 file as
    UTF-16 succeeds silently and turns every line into unreadable pairs, which makes a live
    transport look like it never wrote anything.
    """
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16")
    if b"\x00" in raw[:400]:
        for encoding in ("utf-16-le", "utf-16-be"):
            try:
                return raw.decode(encoding)
            except UnicodeError:
                continue
    for encoding in ("utf-8-sig", "utf-8"):
        try:
            return raw.decode(encoding)
        except UnicodeError:
            continue
    raise ValueError("MT5 log could not be decoded")


def log_date(path: Path) -> dt.date | None:
    stem = path.stem
    if len(stem) == 8 and stem.isdigit():
        try:
            return dt.datetime.strptime(stem, "%Y%m%d").date()
        except ValueError:
            return None
    return None


def newest_log(log_dir: Path) -> Path | None:
    """The file the terminal is writing now, chosen by modification time, not by a date in a
    script: hardcoding yesterday's name is how a dead transport stayed invisible for a day."""
    if not log_dir.is_dir():
        return None
    candidates = [p for p in log_dir.glob("*.log") if p.is_file() and p.stat().st_size > 0]
    return max(candidates, key=lambda p: p.stat().st_mtime) if candidates else None


def ea_lines(text: str, *, date: dt.date | None) -> list[dict[str, object]]:
    """Only lines the advisor itself wrote, with the wall-clock time the file's date implies.

    A log rolled at midnight can hold one day's timestamps under another day's file name, so
    the newest line is taken as the anchor and earlier ones are not re-based.
    """
    out: list[dict[str, object]] = []
    for line in text.splitlines():
        match = _LINE.match(line.strip())
        if match is None or EA_TAG not in match["source"]:
            continue
        entry: dict[str, object] = {
            "time": match["time"],
            "chart": match["source"],
            "message": match["message"].strip(),
            "at": None,
        }
        if date is not None:
            try:
                clock = dt.datetime.strptime(match["time"][:8], "%H:%M:%S")
                entry["at"] = dt.datetime.combine(date, clock.time())
            except ValueError:
                pass
        out.append(entry)
    return out


def classify(lines: list[dict[str, object]], *, now: dt.datetime | None,
             stale_after_seconds: float = _STALE_DEFAULT_SECONDS) -> dict[str, object]:
    if not lines:
        return {"state": "never_polled", "healthy": False, "detail":
                "The log has no CriticalConsensusEA lines: the advisor is not on a chart, or "
                "it has never run against this bridge."}
    last = lines[-1]
    message = str(last["message"])
    lowered = message.lower()
    state = "polling"
    detail = "The advisor is polling the bridge."
    for candidate, needles, explanation in _RULES:
        if any(needle in lowered for needle in needles):
            state, detail = candidate, explanation
            break
    else:
        if message.startswith("Order accepted"):
            state, detail = "order_accepted", "The advisor sent an order to the broker."
        elif message.startswith("BLOCKED"):
            # A refusal this tool cannot name must not read as a healthy poll; tests pin the
            # whole set of strings the EA source can print, so this is a new-guard alarm.
            state, detail = "unclassified_blocked", (
                "The advisor printed a refusal this checker has no rule for. Read the line "
                "above and add a rule; a named block is actionable, an unnamed one is not.")
        else:
            state = "idle"
    healthy = state in ("polling", "idle", "order_accepted", "kill_switch_armed",
                        "duplicate_refused")
    verdict: dict[str, object] = {
        "state": state,
        "healthy": healthy,
        "detail": detail,
        "last_line": f"{last['time']} {last['chart']} {message}"[:220],
        "ea_lines": len(lines),
        "blocked_streak": _trailing_blocked(lines),
    }
    at = last.get("at")
    if isinstance(at, dt.datetime) and now is not None:
        age = (now - at).total_seconds()
        verdict["last_line_age_seconds"] = round(age, 1)
        # A quiet advisor is the worse case: it is not even trying, so no queued command can
        # expire into a visible refusal. The last state is kept because it explains why.
        if age > stale_after_seconds and state not in ("kill_switch_armed",
                                                        "duplicate_refused"):
            verdict["state"] = "ea_silent"
            verdict["healthy"] = False
            verdict["stale_from"] = state
            verdict["detail"] = (f"The advisor wrote nothing for {int(age)} s. Its last line was "
                                 "about the state above. Algo trading is off, or the EA was "
                                 "detached from the chart, or the terminal is not running.")
    return verdict


def _trailing_blocked(lines: list[dict[str, object]]) -> int:
    streak = 0
    for entry in reversed(lines):
        if not str(entry["message"]).startswith("BLOCKED"):
            break
        streak += 1
    return streak


def bridge_token(env: dict[str, str] | None = None,
                 token_file: Path = BRIDGE_TOKEN_FILE) -> str:
    source = env if env is not None else os.environ
    return (source.get(BRIDGE_TOKEN_ENV) or "").strip() or _token_file(token_file)


def _token_file(token_file: Path) -> str:
    try:
        return token_file.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return ""


def probe_bridge(url: str = BRIDGE_HEALTH_URL, *, token: str = "",
                 timeout: float = 3.0) -> dict[str, object]:
    """Ask the bridge what it last saw. Any failure answers with a reason, never an exception.

    A malformed URL, a refused socket and an HTML error page all land here from an operator's
    machine, and a transport check that raises is worth less than one that says what it could
    not learn. http.client is used instead of urllib because urllib treats some server-side
    resets on an error response as "nothing answered", which would hide a 404 behind
    "unreachable" and turn a witnessable silence into a false dead pipe.
    """
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return {"error": "unreachable"}
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    connection: http.client.HTTPConnection | None = None
    try:
        connection_type = (http.client.HTTPSConnection if parsed.scheme == "https"
                           else http.client.HTTPConnection)
        connection = connection_type(parsed.hostname, parsed.port, timeout=timeout)
        headers = {"Accept": "application/json"}
        if token:
            headers["X-Bridge-Key"] = token
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        body = response.read(8192)
        if response.status >= 400:
            return {"error": f"http_{int(response.status)}"}
    except (OSError, http.client.HTTPException, ValueError):
        return {"error": "unreachable"}
    finally:
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {"error": "not_json"}
    if not isinstance(data, dict):
        return {"error": "not_json"}
    return {str(key): value for key, value in data.items()}


def _poll_age(status: dict[str, object]) -> float | None:
    value = status.get("ea_poll_age_seconds")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


# The two verdicts this tool reaches by noticing that the log is quiet. Both can be wrong the
# same way, and the bridge is the only witness: an advisor that polls successfully and has
# nothing to report writes nothing at all.
_SILENCE_STATES = ("ea_silent", "never_polled")


BRIDGE_ERRORS = {
    "unreachable": "nothing answered on the bridge URL; the runner hosts it",
    "http_401": f"the bridge refused the read: {BRIDGE_TOKEN_ENV} is not its token",
    "http_404": "the bridge has no /v1/health: it is running an older build than this checker",
    "not_json": "the address answered, but not with the bridge's health object",
}

# A refusal to answer the health read is not the same evidence as an empty port: 401, 404 and a
# non-health body all mean something is listening that this checker cannot interrogate, while
# `unreachable` means nothing is. Only the second settles a silence.
_MUTED_BRIDGE_ERRORS = ("http_401", "http_404", "not_json")

# Log states that prove the transport carried something. Silence after one of them is the normal
# behaviour of an advisor with an empty queue, so it cannot be reported as a dead pipe.
_TRANSPORT_PROVED = ("order_accepted",)


def apply_bridge_evidence(verdict: dict[str, object], status: dict[str, object],
                          *, stale_after_seconds: float) -> dict[str, object]:
    """Let a poll the bridge witnessed overrule a log that has nothing to say."""
    verdict = dict(verdict)
    verdict["bridge"] = status
    if "error" in status:
        code = str(status["error"])
        verdict["bridge_detail"] = BRIDGE_ERRORS.get(code, code)
        last_witnessed = str(verdict.get("stale_from") or verdict.get("state") or "")
        if (code in _MUTED_BRIDGE_ERRORS and last_witnessed in _TRANSPORT_PROVED):
            verdict["state"] = "unwitnessed_silence"
            verdict["stale_from"] = last_witnessed
            verdict["healthy"] = False
            verdict["detail"] = (
                "The log has been quiet since its last line proved the transport carried an "
                "order to the broker. The bridge cannot confirm or deny that the advisor is "
                f"still polling: {verdict['bridge_detail']}. This is not a broken pipe and not "
                "a proven live one. Start a runner built with this checker so GET /v1/health "
                "answers, and the silence gets a witness.")
        return verdict
    age = _poll_age(status)
    if age is None or not str(status.get("last_ea_poll_at") or ""):
        # An answer with no poll counter is an older bridge, or one that has not been polled
        # since it started. Neither proves anything about the advisor.
        verdict["bridge_detail"] = "the bridge reported no authenticated advisor poll yet"
        return verdict
    verdict["bridge_poll_age_seconds"] = round(age, 1)
    verdict["bridge_polls"] = status.get("ea_polls")
    if verdict.get("state") not in _SILENCE_STATES:
        return verdict
    if age > stale_after_seconds:
        verdict["bridge_detail"] = (f"the bridge last saw the advisor {round(age, 1)} s ago: "
                                    "the silence in the log is real")
        return verdict
    verdict["stale_from"] = verdict.get("stale_from", verdict.get("state"))
    verdict["state"] = "polling"
    verdict["healthy"] = True
    verdict["detail"] = (f"The bridge saw the advisor poll {round(age, 1)} s ago "
                         f"({status.get('ea_polls')} polls, last on "
                         f"{status.get('last_ea_poll_path')}). The advisor writes a log line "
                         "only when it is refused or something happens, so a quiet log with a "
                         "fresh poll is a healthy transport, not a dead one.")
    return verdict


def read_and_classify(log_dir: Path, *, now: dt.datetime | None = None,
                      stale_after_seconds: float = _STALE_DEFAULT_SECONDS,
                      bridge: Callable[[], dict[str, object]] | None = None) -> dict[str, object]:
    path = newest_log(log_dir)
    if path is None:
        return {"state": "no_log", "healthy": False, "log": None,
                "detail": f"No non-empty .log file under {log_dir}."}
    text = decode_log(path.read_bytes())
    verdict = classify(ea_lines(text, date=log_date(path)), now=now,
                       stale_after_seconds=stale_after_seconds)
    verdict["log"] = str(path)
    # The bridge is asked only when the log's verdict rests on silence, so a named block or a
    # refusal in the log is never papered over by a counter from elsewhere.
    if bridge is not None and verdict["state"] in _SILENCE_STATES:
        verdict = apply_bridge_evidence(verdict, bridge(),
                                        stale_after_seconds=stale_after_seconds)
    return verdict


_EXIT = {"no_log": 2, "never_polled": 1, "ea_silent": 1, "request_not_allowed": 1,
         "token_missing": 1, "token_rejected": 1, "bridge_unreachable": 1,
         "malformed_command": 1, "command_expired": 1, "not_demo": 1,
         # Exit 2 is "there is no evidence to reason from", which is exactly what a silence the
         # bridge cannot witness is. Calling it 1 would tell an operator a working chain is dead.
         "unwitnessed_silence": 2}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    parser.add_argument("--stale-seconds", type=float, default=_STALE_DEFAULT_SECONDS)
    parser.add_argument("--bridge-url", default=BRIDGE_HEALTH_URL,
                        help="loopback health endpoint used to confirm an advisor poll")
    parser.add_argument("--token-file", type=Path, default=BRIDGE_TOKEN_FILE,
                        help=f"fallback for {BRIDGE_TOKEN_ENV}; the token is never an argument")
    parser.add_argument("--no-bridge", action="store_true",
                        help="judge the log alone, without asking the bridge")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.stale_seconds <= 0:
        raise ValueError("--stale-seconds must be positive")
    bridge: Callable[[], dict[str, object]] | None = None
    if not args.no_bridge:
        token = bridge_token(token_file=args.token_file)
        bridge = lambda: probe_bridge(args.bridge_url, token=token)  # noqa: E731 - injected hook
    verdict = read_and_classify(args.log_dir, now=dt.datetime.now(),
                                stale_after_seconds=args.stale_seconds, bridge=bridge)
    if args.json:
        print(json.dumps(verdict, ensure_ascii=False, default=str))
    else:
        label = ("healthy" if verdict["healthy"] else
                 "UNKNOWN" if verdict["state"] == "unwitnessed_silence" else "BROKEN")
        print(f"EA transport : {verdict['state']} ({label})")
        print(f"  log        : {verdict.get('log')}")
        print(f"  evidence   : {verdict.get('last_line', '-')}")
        print(f"  since      : {verdict.get('last_line_age_seconds', '?')} s, "
              f"{verdict.get('blocked_streak', 0)} consecutive BLOCKED lines")
        if "bridge_poll_age_seconds" in verdict:
            print(f"  bridge     : advisor polled {verdict['bridge_poll_age_seconds']} s ago, "
                  f"{verdict.get('bridge_polls')} polls total")
        elif "bridge_detail" in verdict:
            print(f"  bridge     : {verdict['bridge_detail']}")
        elif "bridge" in verdict:
            print(f"  bridge     : {verdict['bridge'].get('error', 'answered without counters')}")
        print(f"  meaning    : {verdict['detail']}")
    return 0 if verdict["healthy"] else _EXIT.get(str(verdict["state"]), 1)


if __name__ == "__main__":
    sys.exit(main())
