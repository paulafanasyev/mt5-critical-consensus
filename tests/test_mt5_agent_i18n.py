"""The locale table and the auto/confirm switch are contracts, so they are tested as such."""
from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from critical_consensus.mt5_agent import codes, i18n
from test_mt5_agent import make_bars

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

POLICY_TEMPLATE = """\
version: 1
mode: paper
symbol: EURUSD
timeframe: H1
history_bars: 120
server_utc_offset_seconds: 0
{extra}kill_switch: {{active: false, reason: locale test}}
schedule: {{blocked_weekdays_utc: [], window_start_minute_utc: 0, window_end_minute_utc: 1440}}
symbol_spec: {{symbol: EURUSD, point: 0.00001, digits: 5, tick_size: 0.00001,
  tick_value: 1.0, volume_min: 0.01, volume_max: 100.0, volume_step: 0.01,
  spread_points: 10, stops_level_points: 0}}
"""


def declared_codes() -> list[str]:
    names = (set(codes.CYCLE_CAP_CODES) | set(codes.STRATEGY_CODES)
             | set(codes.EXIT_REASON_CODES) | set(codes.POLICY_CODES)
             | {codes.STATUS_BLOCKED, codes.STATUS_NO_SIGNAL, codes.STATUS_OBSERVED,
                codes.STATUS_PAPER_OPENED})
    return sorted(names)


# ------------------------------------------------------------------ catalogue
def test_every_journaled_code_has_a_non_empty_label_in_all_three_languages():
    missing = [code for code in declared_codes() if code not in i18n.CODE_LABELS]
    assert missing == []
    for code in declared_codes():
        labels = i18n.CODE_LABELS[code]
        assert len(labels) == len(i18n.LANGUAGES), code
        assert all(isinstance(label, str) and label.strip() for label in labels), code
        # A label that repeats the token teaches the operator nothing.
        assert code not in labels, code


def test_every_uppercase_code_constant_is_reachable_through_describe():
    constants = {getattr(codes, name) for name in codes.__all__
                 if isinstance(getattr(codes, name), str)}
    assert constants == set(declared_codes())


def test_numbered_blackout_codes_are_localized_and_unknown_codes_fall_back():
    for language in i18n.LANGUAGES:
        assert "2" in i18n.describe("BLACKOUT_WINDOW_2", language)
        assert i18n.describe("BLACKOUT_WINDOW_2", language) != "BLACKOUT_WINDOW_2"
    assert i18n.describe("SOMETHING_NEW", "ru") == "SOMETHING_NEW"
    assert i18n.describe_many([], "vi") == "-"


def test_every_message_key_has_three_variants_with_identical_placeholders():
    for key, variants in i18n.TEXT.items():
        assert len(variants) == len(i18n.LANGUAGES), key
        assert all(isinstance(v, str) and v.strip() for v in variants), key
        placeholder_sets = {i18n.placeholders(variant) for variant in variants}
        assert len(placeholder_sets) == 1, f"{key} placeholders differ between locales"


def test_every_message_renders_with_its_own_placeholders():
    for key, variants in i18n.TEXT.items():
        for position, variant in enumerate(variants):
            language = i18n.LANGUAGES[position]
            values = {name: f"<{name}>" for name in i18n.placeholders(variant)}
            rendered = i18n.text(key, language, **values)
            assert rendered and "{" not in rendered, key


def test_an_unknown_message_key_is_an_error_not_a_guess():
    with pytest.raises(KeyError):
        i18n.text("confirm.nonexistent", "en")


def test_language_aliases_normalize_and_junk_is_refused():
    assert i18n.normalize_language("RU") == "ru"
    assert i18n.normalize_language("ru-RU") == "ru"
    assert i18n.normalize_language("en_US") == "en"
    assert i18n.normalize_language(" Vietnamese ") == "vi"
    assert i18n.normalize_language("vn") == "vi"
    # The same name with a hyphen or an underscore is still Vietnamese.
    assert i18n.normalize_language("tieng-viet") == "vi"
    assert i18n.normalize_language("tieng_viet") == "vi"
    assert i18n.normalize_language("tiếng_việt") == "vi"
    assert i18n.normalize_language("vi-VN") == "vi"
    for bad in ("de", "", "   ", None, 5, ["ru"]):
        with pytest.raises(ValueError):
            i18n.normalize_language(bad)


def test_the_three_languages_are_each_somebody_s_real_language():
    assert i18n.LANGUAGES == ("ru", "en", "vi")
    assert set(i18n.LANGUAGE_NAMES) == set(i18n.LANGUAGES)
    # Translating the statuses is not enough: the blockers are what the operator reads.
    assert i18n.describe(codes.QUOTE_STALE, "ru") != i18n.describe(codes.QUOTE_STALE, "en")
    assert i18n.describe(codes.QUOTE_STALE, "en") != i18n.describe(codes.QUOTE_STALE, "vi")


# ----------------------------------------------------------------------- CLI
def write_fixture(directory: Path, *, extra_policy: str = "") -> tuple[Path, Path]:
    csv_path = directory / "eurusd_h1.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Date", "Time", "Open", "High", "Low", "Close", "TickVol", "Volume",
                         "Spread"])
        for bar in make_bars(150):
            writer.writerow([bar.time.strftime("%Y.%m.%d"), bar.time.strftime("%H:%M"),
                             f"{bar.open:.5f}", f"{bar.high:.5f}", f"{bar.low:.5f}",
                             f"{bar.close:.5f}", bar.tick_volume, 0, bar.spread_points])
    policy_path = directory / "policy.yaml"
    policy_path.write_text(POLICY_TEMPLATE.format(extra=extra_policy), encoding="utf-8")
    return csv_path, policy_path


def run_cli(tmp_path: Path, csv_path: Path, policy_path: Path, *arguments: str,
            stdin_text: str | None = None, env: dict[str, str] | None = None,
            ) -> subprocess.CompletedProcess:
    command = [sys.executable, str(ROOT / "scripts" / "run_mt5_agent.py"),
               "--db", str(tmp_path / "journal.db"), "run", "--policy", str(policy_path),
               "--csv", str(csv_path), "--source-timezone", "UTC", "--confirm", *arguments]
    # Hermetic by default: a language exported by the developer's shell must not decide what
    # these tests observe. A test that wants the environment sets it explicitly.
    process_env = {key: value for key, value in os.environ.items()
                   if key != "MT5_AGENT_LANGUAGE"}
    if env:
        process_env.update(env)
    return subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", check=False, input=stdin_text, env=process_env)


def summary_of(result: subprocess.CompletedProcess) -> dict:
    # Prompts and the localized summary are on stderr; stdout is exactly the JSON object.
    return json.loads(result.stdout)


def test_vietnamese_output_is_selected_by_the_flag(tmp_path):
    csv_path, policy_path = write_fixture(tmp_path)
    result = run_cli(tmp_path, csv_path, policy_path, "--language", "vi", "--execution", "auto",
                     "--limit-cycles", "10")
    assert result.returncode == 0, result.stderr
    summary = summary_of(result)
    assert summary["language"] == "vi"
    assert summary["execution_mode"] == "auto"
    # Human prose is on stderr; stdout is exactly the JSON summary.
    assert "Tóm tắt" in result.stderr
    assert "tới broker" in result.stderr
    assert json.loads(result.stdout)["language"] == "vi"


@pytest.mark.parametrize("language", ["ru", "en", "vi"])
def test_exit_reasons_in_the_summary_are_codes_with_localized_labels(tmp_path, language):
    csv_path, policy_path = write_fixture(tmp_path)
    result = run_cli(tmp_path, csv_path, policy_path, "--language", language, "--execution",
                     "auto", "--limit-cycles", "150")
    assert result.returncode == 0, result.stderr
    summary = summary_of(result)
    assert summary["paper_trades_closed"] >= 1
    # The runner closes the book at the end, so at least one trade carries that reason.
    assert summary["exit_reason_histogram"].get("END_OF_RUN", 0) >= 1
    assert set(summary["exit_reason_labels"]) == set(summary["exit_reason_histogram"])
    for reason, label in summary["exit_reason_labels"].items():
        assert reason in codes.EXIT_REASON_CODES
        assert label and label != reason


def test_english_output_is_selected_by_the_environment(tmp_path):
    csv_path, policy_path = write_fixture(tmp_path)
    result = run_cli(tmp_path, csv_path, policy_path, "--execution", "auto",
                     "--limit-cycles", "10", env={"MT5_AGENT_LANGUAGE": "en"})
    assert result.returncode == 0, result.stderr
    assert summary_of(result)["language"] == "en"
    assert "Summary" in result.stderr


def test_the_language_flag_beats_the_environment(tmp_path):
    csv_path, policy_path = write_fixture(tmp_path)
    result = run_cli(tmp_path, csv_path, policy_path, "--language", "vi", "--execution", "auto",
                     "--limit-cycles", "2", env={"MT5_AGENT_LANGUAGE": "en"})
    assert result.returncode == 0, result.stderr
    assert summary_of(result)["language"] == "vi"


def test_the_policy_language_is_the_fallback_when_no_flag_or_env_is_set(tmp_path):
    csv_path, policy_path = write_fixture(tmp_path, extra_policy="ui_language: vi\n")
    result = run_cli(tmp_path, csv_path, policy_path, "--execution", "auto",
                     "--limit-cycles", "2")
    assert result.returncode == 0, result.stderr
    assert summary_of(result)["language"] == "vi"
    assert "Tóm tắt" in result.stderr


def test_confirm_mode_is_the_default_and_declines_an_unanswered_prompt(tmp_path):
    csv_path, policy_path = write_fixture(tmp_path)
    result = run_cli(tmp_path, csv_path, policy_path, "--limit-cycles", "10",
                     stdin_text="")
    assert result.returncode == 0, result.stderr
    summary = summary_of(result)
    assert summary["execution_mode"] == "confirm"
    assert summary["paper_positions_opened"] == 0
    assert summary["blocker_histogram"].get("OPERATOR_DECLINED", 0) >= 1
    assert "Ввод с клавиатуры недоступен" in result.stderr


def test_confirm_mode_opens_only_after_an_explicit_answer(tmp_path):
    csv_path, policy_path = write_fixture(tmp_path)
    result = run_cli(tmp_path, csv_path, policy_path, "--limit-cycles", "10",
                     stdin_text="a\n")
    assert result.returncode == 0, result.stderr
    summary = summary_of(result)
    assert summary["paper_positions_opened"] >= 1
    assert "Агент предлагает позицию" in result.stderr
    # The operator sees the account equity the sizing decision was made against.
    assert "Средства (equity)" in result.stderr


def test_a_single_letter_never_means_approve_every_later_position(tmp_path):
    """The old word lists split on whitespace, so Russian "в" and Vietnamese "tất" said yes to
    everything. Whole phrases only: "в" is not an answer, "tất cả" is."""
    csv_path, policy_path = write_fixture(tmp_path)
    russian = run_cli(tmp_path / "ru", csv_path, policy_path, "--language", "ru",
                      "--limit-cycles", "10", stdin_text="в\nk\nд\nн\nn\n")
    assert russian.returncode == 0, russian.stderr
    assert summary_of(russian)["paper_positions_opened"] == 0
    assert "Агент предлагает позицию" in russian.stderr

    partial = run_cli(tmp_path / "vi", csv_path, policy_path, "--language", "vi",
                      "--limit-cycles", "10", stdin_text="tất\nn\n")
    assert partial.returncode == 0, partial.stderr
    assert summary_of(partial)["paper_positions_opened"] == 0

    accepted = run_cli(tmp_path / "vi-all", csv_path, policy_path, "--language", "vi",
                       "--limit-cycles", "10", stdin_text="tất cả\n")
    assert accepted.returncode == 0, accepted.stderr
    summary = summary_of(accepted)
    assert summary["paper_positions_opened"] >= 1, (summary, accepted.stderr)
    assert "Agent đề xuất" in accepted.stderr


def test_a_cp1251_da_answer_is_understood(tmp_path):
    """A Russian console can send cp1251 bytes; they are recovered, not silently declined."""
    csv_path, policy_path = write_fixture(tmp_path)
    command = [sys.executable, str(ROOT / "scripts" / "run_mt5_agent.py"),
               "--db", str(tmp_path / "journal.db"), "run", "--policy", str(policy_path),
               "--csv", str(csv_path), "--source-timezone", "UTC", "--confirm",
               "--limit-cycles", "10"]
    process_env = {key: value for key, value in os.environ.items()
                   if key != "MT5_AGENT_LANGUAGE"}
    result = subprocess.run(command, cwd=ROOT, capture_output=True, check=False,
                            input="да\n".encode("cp1251"), env=process_env)
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    summary = json.loads(result.stdout.decode("utf-8"))
    assert summary["paper_positions_opened"] >= 1


def test_a_junk_environment_language_is_a_clean_localized_error(tmp_path):
    csv_path, policy_path = write_fixture(tmp_path)
    command = [sys.executable, str(ROOT / "scripts" / "run_mt5_agent.py"),
               "--db", str(tmp_path / "journal.db"), "run", "--policy", str(policy_path),
               "--csv", str(csv_path), "--source-timezone", "UTC", "--confirm",
               "--limit-cycles", "2"]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                            errors="replace", check=False,
                            env={**os.environ, "MT5_AGENT_LANGUAGE": "de"})
    assert result.returncode == 2
    assert "Traceback" not in result.stderr
    assert "de" in result.stderr
    assert not (tmp_path / "journal.db").exists()


def test_the_language_flag_is_accepted_before_the_subcommand(tmp_path):
    csv_path, policy_path = write_fixture(tmp_path)
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "run_mt5_agent.py"), "--language", "vi",
         "--db", str(tmp_path / "journal.db"), "run", "--policy", str(policy_path),
         "--csv", str(csv_path), "--source-timezone", "UTC", "--confirm",
         "--execution", "auto", "--limit-cycles", "2"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=False,
        env={key: value for key, value in os.environ.items()
             if key != "MT5_AGENT_LANGUAGE"})
    assert result.returncode == 0, result.stderr
    assert summary_of(result)["language"] == "vi"


def _run_report(tmp_path: Path, *arguments: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "run_mt5_agent.py"),
         "--db", str(tmp_path / "journal.db"), "report", *arguments],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=False,
        env={key: value for key, value in os.environ.items()
             if key != "MT5_AGENT_LANGUAGE"})


def test_the_journal_records_the_effective_language_and_each_report_speaks_it(tmp_path):
    """The flag, not the policy default, is what the journal must remember."""
    csv_path, policy_path = write_fixture(tmp_path)
    first = run_cli(tmp_path, csv_path, policy_path, "--language", "en", "--execution", "auto",
                    "--limit-cycles", "2", "--run-id", "run-en")
    assert first.returncode == 0, first.stderr
    second = run_cli(tmp_path, csv_path, policy_path, "--language", "vi", "--execution", "auto",
                     "--limit-cycles", "2", "--run-id", "run-vi")
    assert second.returncode == 0, second.stderr

    # A report on an old run must not be translated into whichever run is newest.
    english = _run_report(tmp_path, "--run-id", "run-en")
    assert english.returncode == 0, english.stderr
    assert json.loads(english.stdout)["language"] == "en"
    vietnamese = _run_report(tmp_path, "--run-id", "run-vi")
    assert vietnamese.returncode == 0, vietnamese.stderr
    assert json.loads(vietnamese.stdout)["language"] == "vi"

    # reset carries no run id, so it speaks the language of the most recent run.
    cleared = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "run_mt5_agent.py"),
         "--db", str(tmp_path / "journal.db"), "reset", "--confirm"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=False,
        env={key: value for key, value in os.environ.items()
             if key != "MT5_AGENT_LANGUAGE"})
    assert cleared.returncode == 0, cleared.stderr
    assert "Đã xóa nhật ký." in cleared.stdout


def test_report_without_a_balance_uses_the_first_recorded_equity_point(tmp_path):
    csv_path, policy_path = write_fixture(tmp_path)
    run = run_cli(tmp_path, csv_path, policy_path, "--execution", "auto", "--balance", "5000",
                  "--limit-cycles", "3", "--run-id", "run-5k")
    assert run.returncode == 0, run.stderr
    report = _run_report(tmp_path, "--run-id", "run-5k")
    assert report.returncode == 0, report.stderr
    payload = json.loads(report.stdout)
    assert payload["initial_equity"] == pytest.approx(5_000.0)
    assert payload["broker_order_sent"] is False
    assert payload["verified_chain"]["anchor_source"] == "first_event_previous_hash"
    # The report's prose is localized too, not a fixed English string in the journal module.
    assert "бумажная симуляция" in payload["note"]


def test_an_unsupported_language_flag_is_a_clean_localized_error(tmp_path):
    csv_path, policy_path = write_fixture(tmp_path)
    clean = {key: value for key, value in os.environ.items() if key != "MT5_AGENT_LANGUAGE"}
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "run_mt5_agent.py"), "--language", "de",
         "--db", str(tmp_path / "journal.db"), "run", "--policy", str(policy_path),
         "--csv", str(csv_path), "--source-timezone", "UTC", "--confirm"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=False, env=clean)
    assert result.returncode == 2
    assert "Traceback" not in result.stderr
    assert "de" in result.stderr
    assert not (tmp_path / "journal.db").exists()


def test_a_junk_environment_language_never_creates_the_journal(tmp_path):
    """A language mistake must fail before a command opens, and therefore creates, the file."""
    junk = {**os.environ, "MT5_AGENT_LANGUAGE": "de"}
    for arguments in (("report",), ("reset", "--confirm")):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "run_mt5_agent.py"),
             "--db", str(tmp_path / "journal.db"), *arguments],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
            check=False, env=junk)
        assert result.returncode == 2, arguments
        assert "Traceback" not in result.stderr
        assert "de" in result.stderr
        assert not (tmp_path / "journal.db").exists(), arguments


def test_help_and_its_section_titles_are_localized(tmp_path):
    clean = {key: value for key, value in os.environ.items() if key != "MT5_AGENT_LANGUAGE"}
    cases = (
        ("ru", "использование:", "показать эту справку", "позиционные аргументы:"),
        ("en", "usage:", "show this help message and exit", "positional arguments:"),
        ("vi", "cách dùng:", "hiển thị trợ giúp này", "đối số vị trí:"),
    )
    for language, usage, help_line, positional in cases:
        for arguments in (("--help",), ("run", "--help")):
            result = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "run_mt5_agent.py"),
                 "--language", language, *arguments],
                cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
                check=False, env=clean)
            assert result.returncode == 0, (language, arguments, result.stderr)
            assert usage in result.stdout, (language, arguments)
            assert help_line in result.stdout, (language, arguments)
            if language != "en":
                for english in ("usage:", "options:", "positional arguments:",
                                "show this help message and exit"):
                    assert english not in result.stdout, (language, arguments, english)
        root = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "run_mt5_agent.py"),
             "--language", language, "--help"],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
            check=False, env=clean)
        assert positional in root.stdout, (language, "positional section title")

    # argparse keeps the last repeated flag, so localized help must use the last one too.
    repeated = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "run_mt5_agent.py"),
         "--language", "ru", "--language", "vi", "--help"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=False, env=clean)
    assert repeated.returncode == 0, repeated.stderr
    assert "cách dùng:" in repeated.stdout


def test_a_junk_policy_language_is_reported_in_the_operator_s_language(tmp_path):
    csv_path, policy_path = write_fixture(tmp_path, extra_policy="ui_language: de\n")
    clean = {key: value for key, value in os.environ.items() if key != "MT5_AGENT_LANGUAGE"}
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "run_mt5_agent.py"), "--language", "ru",
         "--db", str(tmp_path / "journal.db"), "run", "--policy", str(policy_path),
         "--csv", str(csv_path), "--source-timezone", "UTC", "--confirm"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=False, env=clean)
    assert result.returncode == 2
    assert "Traceback" not in result.stderr
    assert "Ошибка:" in result.stderr
    assert "не поддерживается" in result.stderr
    assert "unsupported language" not in result.stderr
    assert not (tmp_path / "journal.db").exists()


def test_a_refused_reset_still_speaks_the_journal_language(tmp_path):
    csv_path, policy_path = write_fixture(tmp_path)
    run = run_cli(tmp_path, csv_path, policy_path, "--language", "vi", "--execution", "auto",
                  "--limit-cycles", "2")
    assert run.returncode == 0, run.stderr

    clean = {key: value for key, value in os.environ.items() if key != "MT5_AGENT_LANGUAGE"}
    refused = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "run_mt5_agent.py"),
         "--db", str(tmp_path / "journal.db"), "reset"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=False, env=clean)
    assert refused.returncode == 2
    assert "Thêm --confirm" in refused.stderr
    assert "--confirm" in refused.stderr
    # A refused reset must leave the journal untouched.
    with sqlite3.connect(tmp_path / "journal.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] > 0


def test_argument_errors_are_localized_and_abbreviations_are_refused(tmp_path):
    command = [sys.executable, str(ROOT / "scripts" / "run_mt5_agent.py"), "--language", "ru",
               "--db", str(tmp_path / "journal.db")]
    clean = {key: value for key, value in os.environ.items() if key != "MT5_AGENT_LANGUAGE"}
    missing_value = subprocess.run([*command, "run", "--policy"], cwd=ROOT, capture_output=True,
                                   text=True, encoding="utf-8", errors="replace", check=False,
                                   env=clean)
    assert missing_value.returncode == 2
    assert "Ошибка:" in missing_value.stderr
    assert "usage:" not in missing_value.stderr
    assert "использование:" in missing_value.stderr

    # allow_abbrev is off, so a shortened flag is an error rather than a silent guess.
    abbreviated = subprocess.run([*command, "report", "--verify"], cwd=ROOT,
                                 capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", check=False, env=clean)
    assert abbreviated.returncode == 2
    assert "Ошибка:" in abbreviated.stderr
    assert not (tmp_path / "journal.db").exists()


def test_stopping_the_run_on_q_keeps_the_report_and_the_journal_consistent(tmp_path):
    csv_path, policy_path = write_fixture(tmp_path)
    result = run_cli(tmp_path, csv_path, policy_path, "--limit-cycles", "10", stdin_text="q\n")
    assert result.returncode == 0, result.stderr
    summary = summary_of(result)
    assert summary["operator_stopped"] is True
    assert summary["cycles"] == 1
    assert summary["audit_chain_valid"] is True
    assert summary["paper_trades_journaled"] == summary["paper_trades_closed"]


def test_auto_execution_cannot_be_switched_on_for_a_non_demo_policy(tmp_path):
    # The policy itself stays valid (confirm is the default); only --execution auto is refused,
    # because replace() re-runs the policy validation.
    csv_path, policy_path = write_fixture(
        tmp_path, extra_policy="account: {require_demo: false, expected_login: null,"
                               " expected_server: null}\n")
    assert "execution_mode" not in policy_path.read_text(encoding="utf-8")
    result = run_cli(tmp_path, csv_path, policy_path, "--execution", "auto")
    assert result.returncode == 2
    assert "require_demo" in result.stderr
    assert not (tmp_path / "journal.db").exists()


def test_the_journal_stores_codes_and_never_translations(tmp_path):
    csv_path, policy_path = write_fixture(tmp_path)
    result = run_cli(tmp_path, csv_path, policy_path, "--language", "vi", "--limit-cycles", "10",
                     stdin_text="n\n")
    assert result.returncode == 0, result.stderr
    known = set(declared_codes())
    with sqlite3.connect(tmp_path / "journal.db") as connection:
        payloads = [row[0] for row in connection.execute(
            "SELECT payload FROM audit_events WHERE event_type = 'agent_cycle'")]
    assert payloads
    for payload in payloads:
        for blocker in json.loads(payload)["blockers"]:
            assert blocker in known or blocker.startswith("BLACKOUT_WINDOW_"), blocker
        assert "отверг" not in payload and "từ chối" not in payload


# --------------------------------------------------------------------------- console encoding
# A cp1252 console or pipe is the normal state on this host's Windows shell. Cyrillic and
# Vietnamese must never be able to raise UnicodeEncodeError on the way out of a CLI.
def test_configure_console_output_makes_a_legacy_stream_writable():
    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding="cp1252", errors="strict", line_buffering=True)
    with pytest.raises(UnicodeEncodeError):
        stream.write("подтвердить")

    i18n.configure_console_output(stream)

    stream.write("подтвердить — xác nhận")
    stream.flush()
    assert buffer.getvalue().decode("utf-8") == "подтвердить — xác nhận"


def test_configure_console_output_skips_streams_it_cannot_reconfigure():
    class NoReconfigure:
        def __init__(self) -> None:
            self.written: list[str] = []

        def write(self, value: str) -> None:
            self.written.append(value)

    class FailingReconfigure:
        def reconfigure(self, **_kwargs):
            raise OSError("closed")

    quiet = NoReconfigure()
    i18n.configure_console_output(quiet, FailingReconfigure(), None)
    assert quiet.written == []


@pytest.mark.parametrize("script,arguments", [
    ("run_mt5_agent.py", ["--help"]),
    ("run_mt5_demo_trading.py", ["--policy", "x.yaml", "--db", "x.db", "--help"]),
])
def test_help_survives_a_cp1252_pipe(tmp_path, script, arguments):
    process_env = {key: value for key, value in os.environ.items()
                   if key != "MT5_AGENT_LANGUAGE"}
    process_env.update(MT5_AGENT_LANGUAGE="ru", PYTHONIOENCODING="cp1252")
    result = subprocess.run([sys.executable, str(SCRIPTS / script), *arguments],
                            cwd=tmp_path, capture_output=True, check=False, env=process_env)
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    combined = result.stdout.decode("utf-8") + result.stderr.decode("utf-8")
    assert "Traceback" not in combined
    # The Russian help is only reachable if the write itself did not raise.
    assert "язык" in combined


def test_a_setup_refusal_reaches_a_legacy_stderr_as_localized_text(monkeypatch):
    import setup_demo_terminal

    monkeypatch.setattr(setup_demo_terminal, "running_terminals",
                        lambda *args, **kwargs: ["terminal64.exe"])
    captured = io.BytesIO()
    monkeypatch.setattr(sys, "stderr", io.TextIOWrapper(captured, encoding="cp1252",
                                                        errors="strict", line_buffering=True))
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(io.BytesIO(), encoding="cp1252",
                                                        errors="strict", line_buffering=True))

    # Without the console guard, main() raises UnicodeEncodeError while reporting the refusal.
    assert setup_demo_terminal.main(["--language", "ru"]) == 3
    sys.stderr.flush()
    message = captured.getvalue().decode("utf-8")
    assert "terminal64.exe" in message
    assert "запущен" in message
