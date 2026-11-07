"""Tests for the windows schedule: occurrence math, YAML schema, CLI rendering.

Pinned here, deliberately:
  * the calendar math of every rule kind, including month/week/year boundaries;
  * the hybrid time policy — pure UTC minute-of-day for bands of the day (utc_fixed), one
    local clock with a derived DST season for anchored events (local_anchored). Both halves
    are calendar arithmetic, no DST library, so the same window anchors predictably in July
    and in December;
  * schema completeness of config/windows.yaml (every window carries every field, including
    the spread criterion line and the action axis);
  * the first occurrence each documented rule actually produces (the BLS/FOMC anchor
    dates and the nth-weekday rules);
  * both CLIs render across a month boundary and a year boundary without crashing.
No test asserts a performance number; measured verdicts live in the YAML's measured
blocks and are refreshed by scripts/windows_evidence.py, never frozen here.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses resolve their module namespace from here
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def schedule():
    return _load("windows_schedule")


@pytest.fixture(scope="module")
def evidence():
    return _load("windows_evidence")


@pytest.fixture(scope="module")
def document(schedule):
    return schedule.load_windows(ROOT / "config" / "windows.yaml")


# ---------------------------------------------------------------- schema

REQUIRED = ("id", "local_name", "scale", "applies_to", "utc", "rule", "why",
            "expected", "criterion", "spread_criterion", "action", "measured")


def test_every_window_in_yaml_has_all_required_fields(document, schedule):
    schedule.validate(document)
    for window in document["windows"]:
        for field in REQUIRED:
            assert field in window, f"{window.get('id')}: missing {field}"
        assert window["scale"] in ("daily", "weekly", "monthly")
        assert window["criterion"]["op"] in (">=", "<=", "==")
        # гибрид политики: локально привязанное окно ОБЯЗАНО нести обе скобки, а полоса
        # суток — не иметь ни одной, иначе «·dst»/«·std» в рендере врёт
        assert window["timezone_kind"] == ("local_anchored" if window.get("utc_seasons")
                                           else "utc_fixed")
        assert (bool(window.get("season_region")) == bool(window.get("utc_seasons")))
        assert 0 <= window["utc"]["start_minute"] < 1440
        assert 0 <= window["utc"]["end_minute"] < 1440
        # measured is either null (honest hypothesis) or a full record with provenance
        measured = window["measured"]
        if measured is not None:
            assert measured["source"], window["id"]
            assert date.fromisoformat(measured["generated_at"])
            assert isinstance(measured["ratio"], float)
            assert measured["n_inside_bars"] > 0


def test_validate_rejects_missing_field(schedule):
    """Every field the schema demands must be there — `measured` included.

    The fixture carries the whole current window contract EXCEPT `measured`, so this test
    really pins the field it names (and not whatever happens to be checked first); the
    spread line and the action axis are part of the contract now, which is exactly why an
    "obviously minimal" window no longer reaches the measured check.
    """
    doc = json.loads(json.dumps({  # keep it plain-serializable
        "version": 1, "generated": "x", "instrument_classes": {"gold": {"label": "g",
                                                                        "symbols": ["XAUUSD"]}},
        "windows": [{"id": "x", "local_name": "н", "scale": "daily",
                     "applies_to": ["gold"], "utc": {"start_minute": 0, "end_minute": 60},
                     "rule": {"kind": "daily"}, "why": "w", "expected": "e",
                     "criterion": {"metric": "m", "op": ">=", "value": 1, "text": "t"},
                     "spread_criterion": {"metric": "median_spread_points_ratio_inside_vs_"
                                                "outside", "op": "<=", "value": 1.5,
                                          "text": "t"},
                     "action": "neutral"}]}))
    with pytest.raises(schedule.ScheduleError, match="measured"):
        schedule.validate(doc)


def test_validate_rejects_window_without_a_spread_line(schedule):
    """Вторая критериальная линия не факультативна: без неё окно не знает, чем торговать."""
    doc = json.loads(json.dumps({
        "version": 1, "generated": "x", "instrument_classes": {"gold": {"label": "g",
                                                                        "symbols": ["XAUUSD"]}},
        "windows": [{k: v for k, v in {
            "id": "x", "local_name": "н", "scale": "daily", "applies_to": ["gold"],
            "utc": {"start_minute": 0, "end_minute": 60}, "rule": {"kind": "daily"},
            "why": "w", "expected": "e",
            "criterion": {"metric": "m", "op": ">=", "value": 1, "text": "t"},
            "spread_criterion": {"metric": "m", "op": "<=", "value": 1, "text": "t"},
            "action": "neutral", "measured": None, "measured_note": "n"}.items()
            if k != "spread_criterion"}]}))
    with pytest.raises(schedule.ScheduleError, match="spread_criterion"):
        schedule.validate(doc)


def test_validate_rejects_handwritten_action_axis(schedule):
    """`action` — вывод из измерения, а не мнение: расхождение с derive ловится валидатором."""
    document = schedule.load_windows(ROOT / "config" / "windows.yaml")
    for window in document["windows"]:
        derived, _ = schedule.action_for_window(window)
        assert window["action"] == derived, window["id"]
    tampered = json.loads(json.dumps(document, default=str))
    # берём измеренное окно без календарного hazard: тогда reject идёт именно от вывода
    # оси исполнимости, а не от более ранней проверки hazard/неизмеренного окна
    target = next(w for w in tampered["windows"]
                  if w["measured"] is not None and not w.get("hazard")
                  and w["action"] != "enter")
    target["action"] = "enter"
    with pytest.raises(schedule.ScheduleError, match="измерение выводит"):
        schedule.validate(tampered)


def test_unknown_rule_kind_rejected(schedule):
    with pytest.raises(schedule.ScheduleError):
        schedule.occurrences_for_rule({"kind": "quarterly_solstice"},
                                      date(2026, 1, 1), date(2026, 1, 31))


# ---------------------------------------------------------------- occurrence math

def test_daily_rule_spans_week_and_month_boundaries(schedule):
    fired = schedule.occurrences_for_rule({"kind": "daily"},
                                          date(2026, 10, 30), date(2026, 11, 2))
    # Fri 30 Oct fires; weekend 31 Oct / 1 Nov skipped; Mon 2 Nov fires.
    assert fired == [date(2026, 10, 30), date(2026, 11, 2)]


def test_weekly_friday_across_month_boundary(schedule):
    fired = schedule.occurrences_for_rule({"kind": "weekly", "weekday": 4},
                                          date(2026, 10, 26), date(2026, 11, 8))
    assert fired == [date(2026, 10, 30), date(2026, 11, 6)]


def test_nth_weekday_first_friday_of_month(schedule):
    assert schedule.nth_weekday_of_month(2026, 8, 4, 1) == date(2026, 8, 7)
    assert schedule.nth_weekday_of_month(2026, 9, 4, 1) == date(2026, 9, 4)
    # 3rd Friday of March 2026 (quarterly expiry anchor)
    assert schedule.nth_weekday_of_month(2026, 3, 4, 3) == date(2026, 3, 20)
    # a 5th weekday that does not exist returns None, not next month's overflow
    assert schedule.nth_weekday_of_month(2026, 2, 4, 5) is None


def test_trading_days_from_end_of_month(schedule):
    fired = schedule.occurrences_for_rule(
        {"kind": "monthly_nth_trading_day_from_end", "nth_from_end": 2},
        date(2026, 12, 1), date(2026, 12, 31))
    assert fired == [date(2026, 12, 30)]  # Thu 31 and Wed 30 are the last two weekdays
    fired = schedule.occurrences_for_rule(
        {"kind": "monthly_nth_trading_day_from_end", "nth_from_end": 1, "months": [2]},
        date(2026, 1, 1), date(2026, 12, 31))
    assert fired == [date(2026, 2, 27)]  # Feb 28 2026 is a Saturday


def test_monthly_day_with_skip_weekend(schedule):
    fired = schedule.occurrences_for_rule(
        {"kind": "monthly_day", "day": 1, "skip_weekend": True},
        date(2026, 11, 1), date(2026, 11, 5))
    assert fired == [date(2026, 11, 2)]  # 1 Nov 2026 is a Sunday -> Monday


# ---------------------------------------------------------------- documented first occurrences

def _first_firing(document, schedule, window_id):
    window = next(w for w in document["windows"] if w["id"] == window_id)
    fired = schedule.occurrences_for_rule(window["rule"], date(2026, 1, 1), date(2026, 12, 31))
    return window, fired[0]


def test_nfp_anchor_dates_are_bls_not_first_friday_guesswork(document, schedule):
    window, first = _first_firing(document, schedule, "nfp_employment_release")
    # BLS 2026 Employment Situation schedule (the rule_note documents the exceptions):
    # January is Friday the 9th, February is Wednesday the 11th, July is Thursday the 2nd.
    assert first == date(2026, 1, 9)
    assert "2026-02-11" in window["rule"]["dates"]
    assert "2026-07-02" in window["rule"]["dates"]
    assert "2026-10-02" in window["rule"]["dates"]  # October IS the first Friday


def test_fomc_and_cpi_first_2026_dates(document, schedule):
    _, cpi_first = _first_firing(document, schedule, "us_cpi_release")
    assert cpi_first == date(2026, 1, 13)      # BLS CPI release schedule 2026
    _, fomc_first = _first_firing(document, schedule, "fomc_decision")
    assert fomc_first == date(2026, 1, 28)     # Fed 2026 meeting calendar, decision day


def test_witching_rule_first_occurrence(document, schedule):
    _, first = _first_firing(document, schedule, "quarterly_expiry_witching")
    assert first == date(2026, 3, 20)  # 3rd Friday of March, first quarterly month


# ------------------------------------------- гибрид времени: полосы UTC и локальные якоря

def test_utc_fixed_windows_are_dst_free_minute_wise(document, schedule):
    """A band of the UTC day is the same band in July and in December — no DST library."""
    window = next(w for w in document["windows"] if w["timezone_kind"] == "utc_fixed")
    summer = schedule.window_occurrences(window, date(2026, 7, 6), date(2026, 7, 6))
    winter = schedule.window_occurrences(window, date(2026, 12, 7), date(2026, 12, 7))
    assert summer[0].start_minute == winter[0].start_minute
    assert summer[0].duration_minutes == winter[0].duration_minutes
    assert summer[0].season == winter[0].season == "fixed"


def test_local_anchored_windows_move_with_their_own_clock(document, schedule):
    """Окно, привязанное к чужим часам, держит ЛОКАЛЬНУЮ геометрию и сдвигается на час.

    Это ровно то, ради чего существование `utc_seasons` пришлось доказывать: после
    01.11.2026 лондонский 08:00 — это уже не 07:00 UTC, и летняя скобка, приложенная к
    зиме, начинает тратить час на ожидание события.
    """
    window = next(w for w in document["windows"] if w["id"] == "london_open")
    summer = schedule.window_occurrences(window, date(2026, 7, 6), date(2026, 7, 6))[0]
    winter = schedule.window_occurrences(window, date(2026, 12, 7), date(2026, 12, 7))[0]
    assert (summer.start_minute, summer.end_minute, summer.season) == (420, 540, "dst")
    assert (winter.start_minute, winter.end_minute, winter.season) == (480, 600, "std")
    assert summer.duration_minutes == winter.duration_minutes == 120
    # та же самая локальная привязка у американских окон: 09:30 ET — и летом, и зимой
    ny = next(w for w in document["windows"] if w["id"] == "new_york_open")
    ny_summer = schedule.window_occurrences(ny, date(2026, 7, 6), date(2026, 7, 6))[0]
    ny_winter = schedule.window_occurrences(ny, date(2026, 12, 7), date(2026, 12, 7))[0]
    assert (ny_summer.start_minute, ny_winter.start_minute) == (810, 870)


def test_season_switches_are_derived_calendar_dates(document, schedule):
    """Даты переводов выведены арифметикой, а не списком: 2-е Вс марта / 1-е Вс ноября
    для США и последние Вс марта/октября для Европы."""
    assert schedule._nth_weekday(2026, 3, 6, 2) == date(2026, 3, 8)   # US spring forward
    assert schedule._nth_weekday(2026, 11, 6, 1) == date(2026, 11, 1)  # US falls back
    assert schedule._last_weekday(2026, 3, 6) == date(2026, 3, 29)    # EU spring forward
    assert schedule._last_weekday(2026, 10, 6) == date(2026, 10, 25)  # EU falls back
    # день перехода разрешается по полудню UTC — честно про целые сутки: в 07:00 UTC
    # американские часы уже шагнули назад, поэтому 1 ноября — уже STD
    assert schedule.is_dst(date(2026, 3, 7), "us_eastern") is False
    assert schedule.is_dst(date(2026, 3, 8), "us_eastern") is True
    assert schedule.is_dst(date(2026, 10, 24), "europe_london") is True
    assert schedule.is_dst(date(2026, 10, 25), "europe_london") is False
    assert schedule.is_dst(date(2026, 10, 31), "us_eastern") is True
    assert schedule.is_dst(date(2026, 11, 1), "us_eastern") is False
    with pytest.raises(schedule.ScheduleError):
        schedule.is_dst(date(2026, 7, 6), "asia_tokyo")


def test_wrapping_window_spans_midnight(schedule):
    window = {"id": "wrap", "utc": {"start_minute": 1380, "end_minute": 60},
              "rule": {"kind": "daily"}}
    fired = schedule.window_occurrences(window, date(2026, 11, 2), date(2026, 11, 2))
    row = fired[0]
    assert row.start_minute == 1380 and row.end_minute == 1500
    assert row.duration_minutes == 120
    assert "01:00 UTC (+1д)" in schedule.fmt_clock(row.end_minute)


# ---------------------------------------------------------------- CLI rendering

def test_cli_renders_month_boundary_range(schedule, capsys):
    assert schedule.main(["--from", "2026-10-26", "--to", "2026-11-05"]) == 0
    out = capsys.readouterr().out
    assert "2026-10-30" in out and "2026-11-02" in out
    # Fri 30 Oct is October's last trading day -> month-end flows must be rendered there
    month_end_block = out.split("2026-10-30")[1].split("2026-10-31")[0]
    assert "Конец месяца" in month_end_block


def test_cli_renders_year_boundary_week(schedule, capsys):
    assert schedule.main(["--week", "--today", "2026-12-30"]) == 0
    out = capsys.readouterr().out
    assert "2026-12-28" in out and "2027-01-03" in out


def test_cli_month_view_of_february_2026(schedule, capsys):
    assert schedule.main(["--month", "2026-02"]) == 0
    out = capsys.readouterr().out
    assert "2026-02-27" in out  # last trading day: month_end + MSCI effective


def test_cli_next_and_json(schedule, capsys):
    assert schedule.main(["--next", "3", "--today", "2026-11-04"]) == 0
    lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith("2026")]
    assert len(lines) == 3
    assert schedule.main(["--from", "2026-12-28", "--to", "2027-01-04", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["range"]["start"] == "2026-12-28"
    assert "2027-01-01" in payload["days"]  # keys exist even where days are quiet
    assert payload["days"]["2026-12-31"], "New Year's Eve must carry the month-end windows"


# ---------------------------------------------------------------- evidence engine

def test_measure_synthetic_bars(evidence, tmp_path):
    """Hand-built hourly bars pin the evidence math AND the verdict ladder.

    Только бар, начавшийся внутри полуоткрытого окна, считается внутри; ratio — медиана
    внутри / медиана вне. Это арифметика, она не зависит от размера выборки. А вот то,
    КАК громко она может быть сказана, решает лестница: один случай — анекдот (HYPOTHESIS
    при criterion_holds PASS), шесть срабатываний — уже измерение (PASS).
    """
    import datetime as dt

    def window(dates):
        return {
            "id": "synthetic", "scale": "daily", "applies_to": ["gold"],
            "utc": {"start_minute": 750, "end_minute": 840},   # 12:30–14:00 UTC
            "rule": {"kind": "dates", "dates": dates},
            "criterion": {"metric": evidence.RANGE_METRIC, "op": ">=", "value": 1.5,
                          "text": "t"},
        }

    event_days = [dt.date(2026, 8, 7) + dt.timedelta(days=i) for i in range(6)]
    base = dt.datetime(2026, 8, 6, tzinfo=dt.timezone.utc)
    bars = []
    for hour in range(24 * 8):  # восемь полных суток почасовых баров
        moment = base + dt.timedelta(hours=hour)
        in_window = moment.date() in event_days and moment.hour == 13
        rel = 0.0045 if in_window else 0.0015
        bars.append({"time": moment.isoformat(), "o": 1.0,
                     "h": 1.0 + rel / 2, "l": 1.0 - rel / 2, "c": 1.0})
    path = tmp_path / "XAUUSD.json"
    path.write_text(json.dumps(bars), encoding="utf-8")
    loaded = {"XAUUSD": evidence.load_bars(path)}
    classes = {"gold": {"label": "g", "symbols": ["XAUUSD"]}}

    thin = evidence.measure(window(["2026-08-07"]), loaded, classes)
    assert thin["n_inside_bars"] == 1        # the 13:00 bar; 14:00 is half-open outside
    assert thin["n_event_days"] == 1
    assert thin["ratio"] == 3.0              # 0.0045 / 0.0015
    assert thin["criterion_holds"] == "PASS"  # арифметика на стороне гипотезы
    assert thin["verdict"] == "HYPOTHESIS"   # и всё равно один случай — не доказательство

    full = evidence.measure(window([day.isoformat() for day in event_days]), loaded, classes)
    assert full["n_inside_bars"] == 6 and full["n_event_days"] == 6
    assert full["ratio"] == 3.0
    assert full["verdict"] == "PASS"          # выборка вышла за порог тонкости
    assert full["per_symbol"]["XAUUSD"]["ratio"] == 3.0
    assert full["executability"]["verdict"] == "EXECUTABLE"
    # спред в ленте не записан ни в одном баре — колонка называется неизмеренной, а не нулём
    assert full["spread_ratio"] is None
    assert full["spread_criterion_holds"] == "NOT_MEASURED"
    assert full["spread_unmeasurable_symbols"] == ["XAUUSD"]
