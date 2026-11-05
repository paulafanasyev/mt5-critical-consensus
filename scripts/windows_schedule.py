#!/usr/bin/env python3
"""Render the windows schedule (config/windows.yaml) for a date range: дни, недели, месяцы.

The operator asked: "предсказывай окна заранее — сделай расписание по дням, неделям и
месяцам". This is the calendar half of that answer. Occurrences are computed from the
rules in the YAML (daily / weekly weekday / nth-weekday-of-month / nth-trading-day-from-
end-of-month / literal anchor dates), never from a hardcoded list of strings, so the same
file that documents *why* a window exists also produces *when* it fires for any range.

Time handling — two different policies, deliberately, because the two kinds of window are
not the same object:

  utc_fixed      — a window that describes a *band of the UTC day* (the Asia session). Its
                   bracket is one pair of minutes forever.
  local_anchored — a window that describes an *event on somebody's clock*: an 08:30 ET
                   release, a 09:30 ET cash open, a 14:00 ET FOMC statement. A fixed UTC
                   bracket for such an event is simply wrong for half the year: after the
                   US leaves DST on 2026-11-01 the 08:30 ET print lands at 13:30 UTC, so a
                   12:30–14:00 UTC bracket spends its first hour before the print and keeps
                   only half the reaction. These windows therefore carry BOTH brackets
                   (`utc_seasons.dst` / `utc_seasons.std`) and the engine picks one per
                   date from the DST rule of the event's region.

The DST rules are calendar arithmetic, not a library and not an estimate: the US switches
on the second Sunday of March and the first Sunday of November, Europe on the last Sunday
of March and of October. They are known in advance for any year this schedule will run,
which is what makes them safe to hard-derive. The bracket pair is validated to be the same
local geometry in both seasons (`std_start == dst_start + 60`, equal duration), so a
window cannot quietly change what it claims about the event while chasing the clock.

Holidays: an occurrence on a day the cash market is shut is not a trading window, it is a
request to predict a closed book. Derived rules therefore drop exchange holidays, and a
`monthly_nth_trading_day_from_end` rule does not even count them as trading days. Literal
anchor dates (BLS/Fed releases) are NOT dropped — those agencies move the release itself —
but a collision is flagged.

Usage:
    python scripts/windows_schedule.py                 # следующие 14 дней (вид по дням)
    python scripts/windows_schedule.py --week          # текущая неделя (Пн–Вс)
    python scripts/windows_schedule.py --month 2026-12 # события по месяцам
    python scripts/windows_schedule.py --next 8        # ближайшие N окон
    python scripts/windows_schedule.py --from 2026-11-01 --to 2026-11-30 --json
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config" / "windows.yaml"

WEEKDAY_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
SCALE_RU = {"daily": "день", "weekly": "неделя", "monthly": "месяц"}

REQUIRED_WINDOW_FIELDS = ("id", "local_name", "scale", "applies_to", "utc", "rule",
                          "why", "expected", "criterion", "spread_criterion", "action",
                          "measured")
REQUIRED_RULE_KINDS = ("daily", "weekly", "dates", "monthly_day", "monthly_nth_weekday",
                       "monthly_nth_trading_day_from_end")
REQUIRED_CRITERION_FIELDS = ("metric", "op", "value", "text")
REQUIRED_SPREAD_CRITERION_FIELDS = ("metric", "op", "value", "text")
SPREAD_CRITERION_METRICS = ("median_spread_points_ratio_inside_vs_outside",
                            "median_spread_points_ratio_inside_vs_hour_matched_outside")

#: Verdicts the evidence engine may hand back. HYPOTHESIS means ONLY "the sample is too thin
#: to license the word evidence" — see THIN_SAMPLE_EVENT_DAYS. A window with 91 firings does
#: not get to hide behind an unmeasured season or a thin book: those are caveats on the
#: verdict and a separate axis (action) below.
VERDICTS = ("PASS", "FAIL", "HYPOTHESIS", "NOT_MEASURED")
#: Refuse to report at all below this many firings (one calendar event is an anecdote).
MIN_EVENT_DAYS = 2
#: At or below this many firings the verdict may only be HYPOTHESIS.
THIN_SAMPLE_EVENT_DAYS = 5
#: The tradability axis. `enter` is a claim about trading, so it is gated by the book and by
#: the measured spread, never by the range statistic alone.
ACTION_VALUES = ("enter", "avoid", "neutral")
#: Spread inside the window vs its own baseline that still counts as tradable, and the share
#: of an hour's range that a spread crossing may eat before the trade is dead.
SPREAD_THIN_MULTIPLE = 1.5
SPREAD_COST_KILL = 0.25
#: одна формулировка разделения осей и для артефакта, и для рендера
DECIDED_BY_EVIDENCE_NOTE = (
    "verdict — статистика (держится ли предзарегистрированный критерий диапазона на этой "
    "выборке); action — исполнимость (можно ли это торговать). PASS по тишине никогда не "
    "бывает enter")
#: Regions whose clocks this engine can resolve. The date rule is shared inside a group.
DST_RULES = {
    "us_eastern": "us", "america_chicago": "us",
    "europe_london": "eu", "europe_frankfurt": "eu",
}
MIN_BAR_COVERAGE = 0.5       # below this the bracket is mostly not-open, not "quiet"
#: Below this many firings a verdict carries the "provisional" caveat. Shared with
#: scripts/windows_evidence.py so the artifact and the measuring stick agree.
MIN_POWERED_EVENT_DAYS = 10
#: Every field a reflowed/edited artifact must keep, or the measured block has silently
#: lost the part the critics asked for (season split, executability, spread, action).
REQUIRED_MEASURED_FIELDS = ("source", "generated_at", "timeframe", "metric", "baseline",
                            "symbols_measured", "symbols_without_bars", "n_inside_bars",
                            "n_baseline_bars", "n_event_days", "n_event_days_by_season",
                            "season_in_effect_at_measurement", "median_rel_range_inside",
                            "median_rel_range_baseline", "ratio", "ratio_noise_band_p10_p90",
                            "criterion", "criterion_holds", "verdict", "verdict_reasons",
                            "executability", "per_symbol",
                            "spread_metric", "spread_baseline", "spread_criterion",
                            "spread_ratio", "spread_ratio_driver",
                            "median_spread_points_inside",
                            "median_spread_points_outside", "n_spread_bars_inside",
                            "n_spread_bars_outside", "spread_coverage_inside",
                            "spread_measurable_symbols", "spread_unmeasurable_symbols",
                            "spread_criterion_holds", "spread_column_audit",
                            "crossing_cost_fraction_of_hour_range", "crossing_cost_driver",
                            "action", "action_reasons")
REQUIRED_EXECUTABILITY_FIELDS = ("verdict", "bar_coverage_inside", "n_expected_slots",
                                 "n_present_bars", "n_occurrence_days_without_any_bars",
                                 "dead_hour_slots_utc", "sparse_hour_slots_utc",
                                 "per_slot_fill", "spread")
REQUIRED_SPREAD_FIELDS = ("unit_note", "baseline", "by_symbol", "measurable_symbols",
                          "unmeasurable_symbols", "ratio_inside_vs_background", "ratio_driver",
                          "crossing_cost_fraction_of_hour_range", "coverage_threshold",
                          "min_spread_bars")


class ScheduleError(ValueError):
    """The YAML does not carry the schema the schedule (and the tests) depend on."""


@dataclass(frozen=True)
class Occurrence:
    """One firing of one window: start date, the UTC minutes resolved FOR THAT DATE, and
    which DST season produced them (end may exceed 1440 when the window wraps past
    midnight)."""
    window_id: str
    start: date
    start_minute: int
    end_minute: int  # absolute: > start_minute always; >= 1440 means it wraps
    season: str = "fixed"  # fixed | dst | std
    on_holiday: str | None = None  # name of the holiday the anchor date collides with

    @property
    def duration_minutes(self) -> int:
        return self.end_minute - self.start_minute


def fmt_clock(absolute_minute: int) -> str:
    day_roll, minute = divmod(absolute_minute, 1440)
    clock = f"{minute // 60:02d}:{minute % 60:02d}"
    return f"{clock} UTC" + (f" (+{day_roll}д)" if day_roll else "")


def _nth_weekday(year: int, month: int, weekday: int, nth: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (nth - 1))


def nth_weekday_of_month(year: int, month: int, weekday: int, nth: int) -> date | None:
    """nth (1-based) given weekday of a calendar month; None if the month has fewer."""
    day = _nth_weekday(year, month, weekday, nth)
    return day if day.month == month else None


def us_dst_started(day: date) -> datetime:
    """US switching instant: 2nd Sunday of March, 07:00 UTC (02:00 ET)."""
    return datetime.combine(_nth_weekday(day.year, 3, 6, 2), datetime.min.time(),
                            UTC) + timedelta(hours=7)


def us_dst_ended(day: date) -> datetime:
    """US switching back: 1st Sunday of November, 07:00 UTC (02:00 ET)."""
    return datetime.combine(_nth_weekday(day.year, 11, 6, 1), datetime.min.time(),
                            UTC) + timedelta(hours=7)


def eu_dst_started(day: date) -> datetime:
    """EU switching: last Sunday of March, 01:00 UTC."""
    return datetime.combine(_last_weekday(day.year, 3, 6), datetime.min.time(),
                            UTC) + timedelta(hours=1)


def eu_dst_ended(day: date) -> datetime:
    """EU switching back: last Sunday of October, 01:00 UTC."""
    return datetime.combine(_last_weekday(day.year, 10, 6), datetime.min.time(),
                            UTC) + timedelta(hours=1)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    if month == 12:
        nxt = date(year + 1, 1, 1)
    else:
        nxt = date(year, month + 1, 1)
    return nxt - timedelta(days=(nxt.weekday() - weekday) % 7 or 7)


def is_dst(day: date, region: str) -> bool:
    """Is `region` on summer time on `day`?

    Both switchings fall on a Sunday small-hours UTC instant, and every locally anchored
    window in this file fires on a weekday, so resolving the day by its 12:00 UTC instant
    is exact for every date this schedule can actually produce; the transition Sunday
    itself is resolved by noon too, which is the honest reading of a whole day.
    """
    if region not in DST_RULES:
        raise ScheduleError(f"unknown season_region: {region!r}")
    noon = datetime.combine(day, datetime.min.time(), UTC) + timedelta(hours=12)
    if DST_RULES[region] == "us":
        return us_dst_started(day) <= noon < us_dst_ended(day)
    return eu_dst_started(day) <= noon < eu_dst_ended(day)


def resolve_bracket(window: dict[str, Any], day: date) -> tuple[int, int, str]:
    """(start_minute, end_minute, season) that this window means ON THIS DATE.

    A window with `utc_seasons` is anchored to a local clock and gets the bracket of the
    season `day` falls in; any other window keeps its single fixed UTC bracket.
    """
    seasons = window.get("utc_seasons")
    if not seasons:
        begin, finish = window["utc"]["start_minute"], window["utc"]["end_minute"]
        if finish <= begin:
            finish += 1440
        return begin, finish, "fixed"
    season = "dst" if is_dst(day, window["season_region"]) else "std"
    spec = seasons[season]
    begin, finish = spec["start_minute"], spec["end_minute"]
    if finish <= begin:
        finish += 1440
    return begin, finish, season


def trading_days_of_month(year: int, month: int, holidays: Any = ()) -> list[date]:
    """Weekdays that the cash market is open: Saturdays/Sundays and exchange holidays."""
    shut = set(holidays or ())
    days, day = [], date(year, month, 1)
    while day.month == month:
        if day.weekday() < 5 and day not in shut:
            days.append(day)
        day += timedelta(days=1)
    return days


def first_trading_day(year: int, month: int, holidays: Any = ()) -> date:
    """First of the month rolled forward past weekends and exchange holidays."""
    shut = set(holidays or ())
    day = date(year, month, 1)
    while day.weekday() >= 5 or day in shut:
        day += timedelta(days=1)
    return day


def month_bounds(anchor: date, horizon_end: date) -> list[tuple[int, int]]:
    months, (y, m) = [], (anchor.year, anchor.month)
    while (y, m) <= (horizon_end.year, horizon_end.month):
        months.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return months


def occurrences_for_rule(rule: dict[str, Any], start: date, end: date,
                         holidays: Any = ()) -> list[date]:
    """Every start date the rule fires on inside [start, end] (inclusive).

    `holidays` is only consulted by the *derived* rules: a rule that predicts a trading
    opportunity must not predict one on a day the market is shut. Literal `dates` anchors
    are returned untouched, because the agencies that publish them reschedule the release
    around a holiday themselves — the collision is flagged at render time instead.
    """
    kind = rule["kind"]
    if kind not in REQUIRED_RULE_KINDS:
        raise ScheduleError(f"unknown rule kind: {kind!r}")
    shut = set(holidays or ())
    fired: list[date] = []
    if kind == "daily":
        wanted = set(rule.get("weekdays", [0, 1, 2, 3, 4]))
        day = start
        while day <= end:
            if day.weekday() in wanted and day not in shut:
                fired.append(day)
            day += timedelta(days=1)
    elif kind == "weekly":
        raw = rule["weekday"]
        wanted = {raw} if isinstance(raw, int) else set(raw)
        day = start
        while day <= end:
            if day.weekday() in wanted and day not in shut:
                fired.append(day)
            day += timedelta(days=1)
    elif kind == "dates":
        for raw in rule["dates"]:
            day = date.fromisoformat(raw)
            if start <= day <= end:
                fired.append(day)
    elif kind == "monthly_day":
        for (year, month) in month_bounds(start, end):
            if rule.get("skip_weekend"):
                # the first *trading* day of the month, holiday included in the roll-forward
                day = first_trading_day(year, month, shut)
            else:
                day = date(year, month, min(rule["day"], 28))
                if day.weekday() >= 5 or day in shut:
                    continue
            if start <= day <= end:
                fired.append(day)
    elif kind == "monthly_nth_weekday":
        for (year, month) in month_bounds(start, end):
            if rule.get("months") and month not in rule["months"]:
                continue
            day = nth_weekday_of_month(year, month, rule["weekday"], rule["nth"])
            if day and day not in shut and start <= day <= end:
                fired.append(day)
    elif kind == "monthly_nth_trading_day_from_end":
        for (year, month) in month_bounds(start, end):
            if rule.get("months") and month not in rule["months"]:
                continue
            trading = trading_days_of_month(year, month, shut)
            index = len(trading) - rule["nth_from_end"]
            if 0 <= index < len(trading):
                day = trading[index]
                if start <= day <= end:
                    fired.append(day)
    return sorted(fired)


def window_occurrences(window: dict[str, Any], start: date, end: date,
                       holidays: Any = ()) -> list[Occurrence]:
    """Every firing of the window in [start, end], with each date's own UTC bracket."""
    holiday_names = holidays if isinstance(holidays, dict) else {}
    plain = set(holiday_names) if holiday_names else set(holidays or ())
    fired = occurrences_for_rule(window["rule"], start, end, plain)
    rows = []
    for day in fired:
        begin, finish, season = resolve_bracket(window, day)
        rows.append(Occurrence(window["id"], day, begin, finish, season,
                               holiday_names.get(day) if day in plain else None))
    return rows


def holiday_dates(document: dict[str, Any]) -> set[date]:
    """Days the market is CLOSED (early-close half-days are not in here on purpose)."""
    return {date.fromisoformat(row["date"]) for row in document.get("market_holidays", [])
            if row.get("kind", "closed") == "closed"}


def holiday_names(document: dict[str, Any]) -> dict[date, str]:
    return {date.fromisoformat(row["date"]): row["name"]
            for row in document.get("market_holidays", [])
            if row.get("kind", "closed") == "closed"}


def early_close_names(document: dict[str, Any]) -> dict[date, str]:
    """Half-days: the window still fires, but part of its bracket has no book."""
    return {date.fromisoformat(row["date"]): row["name"]
            for row in document.get("market_holidays", [])
            if row.get("kind") == "early_close"}


def unrecorded_symbols(document: dict[str, Any]) -> set[str]:
    """Symbols the desk trades but this machine has no bars for — never show them as if
    they had been measured."""
    registry = document.get("symbols") or {}
    return {symbol for symbol, meta in registry.items() if not (meta or {}).get("recorded")}


# --------------------------------------------------------------------------- tradability axis

def decide_action(window: dict[str, Any], measured: dict[str, Any]) -> tuple[str, list[str]]:
    """enter | avoid | neutral из ОДНОГО набора правил — по записанному measured:.

    `verdict` отвечает на «держится ли предзарегистрированный критерий диапазона на этой
    выборке». Это другой вопрос, чем «можно ли здесь торговать», и смешивать их означало то,
    что и наблюдали: окно с 91 срабатыванием и закрытой кникой пряталось за HYPOTHESIS, а
    тихий rollover «пассил» и выглядел как сигнал. Здесь живёт вторая ось, и правила её —
    не мнение, а вывод из тех же полей, что напечатаны в артефакте:

      avoid — хоть один признак, что торговать нечем или дороже, чем обещает гипотеза:
        календарный `hazard` (открытие после выходных, среда тройного свепа, первый бар
        недели), CLOSED_BOOK/NO_SLOTS/THIN_BOOK по покрытию слотов, спред внутри окна
        >= SPREAD_THIN_MULTIPLE x собственной базы, провал второй критериальной линии окна
        (по спреду), спред, съедающий >= SPREAD_COST_KILL часового диапазона, и ГЛАВНОЕ
        ПРАВИЛО: PASS по критерию ТИШИНЫ (`criterion.op == "<="`) никогда не рендерится
        enter — «книга измерена спокойной» это отсутствие движения, а не сигнал входа.
      enter — иначе, и только если PASS по критерию АКТИВНОСТИ (`>=`), книга EXECUTABLE,
        спред измерен и вторую линию держит, выборка не тонкая, а действующая сезонная
        половина скобки встречалась в записи не меньше MIN_EVENT_DAYS раз.
      neutral — всё остальное: FAIL (окно не отличается от фона), HYPOTHESIS (тонкая
        выборка), неизмеренный спред, неизмеренная зимняя геометрия.
    """
    hazards = [str(item) for item in (window.get("hazard") or [])]
    exec_block = measured["executability"]
    exec_verdict = exec_block["verdict"]
    op = window["criterion"]["op"]
    verdict = measured["verdict"]
    ratio = measured["ratio"]
    spread_ratio = measured.get("spread_ratio")
    spread_holds = measured.get("spread_criterion_holds")
    cost = measured.get("crossing_cost_fraction_of_hour_range")

    avoid: list[str] = []
    if hazards:
        avoid.append("час по календарю известен расширением спреда и брокерскими "
                     "остановками (hazard): " + ", ".join(hazards))
    if verdict == "PASS" and op == "<=":
        avoid.append(f"PASS по критерию ТИШИНЫ ({measured['criterion']}, измерено {ratio}): "
                     "окно измерено как спокойное — это не вход, а отсутствие движения; "
                     "правило PASS-on-quiescence-never-enter")
    if exec_verdict in ("CLOSED_BOOK", "NO_SLOTS"):
        avoid.append(f"книга в окне {exec_verdict} (покрытие "
                     f"{exec_block['bar_coverage_inside']:.3f}, мёртвые часы "
                     f"{exec_block['dead_hour_slots_utc']}): торговать не с чем")
    elif exec_verdict == "THIN_BOOK":
        avoid.append(f"книга в окне THIN_BOOK (покрытие "
                     f"{exec_block['bar_coverage_inside']:.3f}, редкие часы "
                     f"{exec_block['sparse_hour_slots_utc']})")
    if spread_ratio is not None and spread_ratio >= SPREAD_THIN_MULTIPLE:
        avoid.append(f"спред внутри окна в {spread_ratio} раз(а) шире фона по символу "
                     f"{measured.get('spread_ratio_driver')} "
                     f"(порог {SPREAD_THIN_MULTIPLE})")
    if spread_holds == "FAIL":
        avoid.append(f"вторая критериальная линия (спред {measured['spread_criterion']}) "
                     f"не выполнена: измерено {spread_ratio}")
    if cost is not None and cost >= SPREAD_COST_KILL:
        avoid.append(f"проход по спреду съедает {cost:.3f} часового диапазона по символу "
                     f"{measured.get('crossing_cost_driver')} (предел {SPREAD_COST_KILL})")
    if avoid:
        return "avoid", avoid

    blocked: list[str] = []
    if verdict == "HYPOTHESIS":
        blocked.append(f"вердикт HYPOTHESIS при n={measured['n_event_days']} срабатываний: "
                       "доказательства нет, входа не обещаем")
    elif verdict == "FAIL":
        blocked.append(f"критерий диапазона {measured['criterion']} не выполнен "
                       f"(измерено {ratio}): окно не отличается от фона")
    elif verdict == "NOT_MEASURED":
        blocked.append("окно не измерено: ось исполнимости не имеет данных")
    if spread_holds == "NOT_MEASURED":
        blocked.append("спред в окне не измерен (поле `spread` в ленте непокрывающее либо "
                       "колонка вырождена), значит цена стояния в окне не подтверждена")
    season = measured["season_in_effect_at_measurement"]
    if season != "fixed":
        in_season = measured["n_event_days_by_season"].get(season, 0)
        if in_season < MIN_EVENT_DAYS:
            blocked.append(f"действующая {season}-половина скобки не имеет ни одного "
                           "измеренного срабатывания на записи")
    if blocked:
        return "neutral", blocked
    return ("enter", [f"вердикт PASS по критерию активности ({measured['criterion']}, "
                      f"измерено {ratio} при n={measured['n_event_days']}), книга EXECUTABLE, "
                      f"спред {measured['spread_criterion']} измерен и выполнен"])


def action_for_window(window: dict[str, Any]) -> tuple[str, list[str]]:
    """Derived action when there is a measurement; the authored one when there is none.

    An unmeasured window has no evidence to derive from, so only the calendar can speak:
    a `hazard` hour is avoid even unread, and nothing unmeasured may claim `enter`.
    """
    measured = window.get("measured")
    if measured is None:
        hazards = [str(item) for item in (window.get("hazard") or [])]
        if hazards:
            return "avoid", ["окно не измерено, но час календарно известный по расширению "
                             "спреда/остановкам (hazard): " + ", ".join(hazards)]
        return "neutral", ["окно не измерено: вход не обещаем, тишину не утверждаем"]
    return decide_action(window, measured)


# --------------------------------------------------------------------------- validation

def validate(document: dict[str, Any]) -> list[dict[str, Any]]:
    """Fail loudly on any window missing the fields the schedule and tests require."""
    for field in ("version", "generated", "instrument_classes", "windows"):
        if field not in document:
            raise ScheduleError(f"windows.yaml: missing top-level field {field!r}")
    windows = document["windows"]
    if not isinstance(windows, list) or not windows:
        raise ScheduleError("windows.yaml: `windows` must be a non-empty list")
    declared = set(document["instrument_classes"])
    seen: set[str] = set()
    for window in windows:
        for field in REQUIRED_WINDOW_FIELDS:
            if field not in window:
                raise ScheduleError(f"window {window.get('id', '?')}: missing {field!r}")
        if window["id"] in seen:
            raise ScheduleError(f"duplicate window id: {window['id']}")
        seen.add(window["id"])
        if window["scale"] not in SCALE_RU:
            raise ScheduleError(f"window {window['id']}: bad scale {window['scale']!r}")
        unknown = set(window["applies_to"]) - declared
        if unknown:
            raise ScheduleError(f"window {window['id']}: applies_to unknown classes {unknown}")
        for field in ("start_minute", "end_minute"):
            minute = window["utc"].get(field)
            if not isinstance(minute, int) or not 0 <= minute < 1440:
                raise ScheduleError(f"window {window['id']}: utc.{field}={minute!r} "
                                    "must be an int minute-of-day in [0, 1440)")
        _validate_seasons(window)
        rule = window["rule"]
        if rule.get("kind") not in REQUIRED_RULE_KINDS:
            raise ScheduleError(f"window {window['id']}: rule.kind {rule.get('kind')!r}")
        if rule["kind"] == "dates":
            for raw in rule.get("dates", []):
                date.fromisoformat(raw)  # raises if an anchor date is malformed
            if not rule.get("dates") and not window.get("inactive_reason"):
                raise ScheduleError(
                    f"window {window['id']}: a dates rule with no dates is inert, so it "
                    "must carry inactive_reason saying which calendar is unverified")
        criterion = window["criterion"]
        for field in REQUIRED_CRITERION_FIELDS:
            if field not in criterion:
                raise ScheduleError(f"window {window['id']}: criterion.{field} missing")
        if criterion["op"] not in (">=", "<=", "=="):
            raise ScheduleError(f"window {window['id']}: bad criterion op")
        spread_criterion = window["spread_criterion"]
        for field in REQUIRED_SPREAD_CRITERION_FIELDS:
            if field not in spread_criterion:
                raise ScheduleError(
                    f"window {window['id']}: spread_criterion.{field} missing — у окна обязана "
                    "быть вторая критериальная линия, по спреду: диапазон сам по себе не "
                    "говорит, чем тут торговать")
        if spread_criterion["metric"] not in SPREAD_CRITERION_METRICS:
            raise ScheduleError(f"window {window['id']}: spread_criterion.metric "
                                f"{spread_criterion['metric']!r} not in "
                                f"{SPREAD_CRITERION_METRICS}")
        if spread_criterion["op"] not in (">=", "<=", "=="):
            raise ScheduleError(f"window {window['id']}: bad spread_criterion op")
        if window["action"] not in ACTION_VALUES:
            raise ScheduleError(f"window {window['id']}: action {window['action']!r} not in "
                                f"{ACTION_VALUES}")
        hazards = [str(item) for item in (window.get("hazard") or [])]
        if window.get("hazard") and not hazards:
            raise ScheduleError(f"window {window['id']}: hazard listed but empty")
        measured = window["measured"]
        if measured is None:
            if not window.get("measured_note"):
                raise ScheduleError(f"window {window['id']}: measured null needs "
                                    "measured_note (hypotheses must say why)")
            # Неизмеренное окно не имеет права обещать вход; не измерено — значит neutral,
            # а календарно опасный час — avoid даже без записи.
            if window["action"] == "enter":
                raise ScheduleError(f"window {window['id']}: enter без измерения — ось "
                                    "исполнимости не может быть мнением, когда measured: null")
            if hazards and window["action"] != "avoid":
                raise ScheduleError(f"window {window['id']}: hazard {hazards} при action "
                                    f"{window['action']!r} — такие часы обязаны быть avoid")
        else:
            missing = [field for field in REQUIRED_MEASURED_FIELDS
                       if field not in measured]
            if missing:
                raise ScheduleError(f"window {window['id']}: measured потерял поля "
                                    f"{missing} — артефакт обязан нести выборку по сезонам, "
                                    "исполнимость, спред-линию и выведенный action")
            if measured["verdict"] not in VERDICTS:
                raise ScheduleError(f"window {window['id']}: measured.verdict "
                                    f"{measured['verdict']!r} not in {VERDICTS}")
            if measured["criterion_holds"] not in ("PASS", "FAIL"):
                raise ScheduleError(f"window {window['id']}: criterion_holds must keep the "
                                    "raw arithmetic fact next to the verdict")
            executability = measured["executability"]
            lost = [field for field in REQUIRED_EXECUTABILITY_FIELDS
                    if field not in executability]
            if lost:
                raise ScheduleError(f"window {window['id']}: executability потерял {lost}")
            spread = executability["spread"]
            lost = [field for field in REQUIRED_SPREAD_FIELDS if field not in spread]
            if lost:
                raise ScheduleError(f"window {window['id']}: executability.spread потерял "
                                    f"{lost} — поле spread либо аудируется по символам, "
                                    "либо называется неизмеренным")
            n_days = measured["n_event_days"]
            # ЛЕСТНИЦА: HYPOTHESIS — только тонкая выборка. Ничего не отчитывается ниже
            # MIN_EVENT_DAYS. Остальное — PASS/FAIL по критерию, и вердикт обязан
            # соответствовать арифметике, а не состоянию книги.
            if n_days < MIN_EVENT_DAYS:
                raise ScheduleError(f"window {window['id']}: measured при "
                                    f"n_event_days={n_days} < {MIN_EVENT_DAYS} — один случай "
                                    "не печатается измерением, ставьте measured: null")
            if n_days <= THIN_SAMPLE_EVENT_DAYS and measured["verdict"] != "HYPOTHESIS":
                raise ScheduleError(f"window {window['id']}: verdict "
                                    f"{measured['verdict']!r} при n={n_days} <= "
                                    f"{THIN_SAMPLE_EVENT_DAYS} — тонкая выборка даёт только "
                                    "HYPOTHESIS")
            if (n_days > THIN_SAMPLE_EVENT_DAYS
                    and measured["verdict"] not in ("PASS", "FAIL")):
                raise ScheduleError(f"window {window['id']}: verdict "
                                    f"{measured['verdict']!r} при n={n_days} — окно с "
                                    "достаточной выборкой не прячется за HYPOTHESIS; сравните "
                                    "измеренное с критерием и напишите PASS или FAIL, а шум, "
                                    "сезон и книгу — оговорками")
            if (measured["verdict"] in ("PASS", "FAIL")
                    and measured["criterion_holds"] != measured["verdict"]):
                raise ScheduleError(f"window {window['id']}: вердикт "
                                    f"{measured['verdict']!r} против criterion_holds "
                                    f"{measured['criterion_holds']!r} — арифметика и вердикт "
                                    "не могут расходиться")
            # Ось исполнимости выводится из тех же полей; рукописный action не проходит.
            derived, _ = decide_action(window, measured)
            if window["action"] != derived:
                raise ScheduleError(f"window {window['id']}: action "
                                    f"{window['action']!r}, измерение выводит {derived!r} — "
                                    "перепишите scripts/windows_evidence.py --apply")
            if measured["action"] != derived or window["action"] != measured["action"]:
                raise ScheduleError(f"window {window['id']}: measured.action "
                                    f"{measured['action']!r} разошёлся с action окна "
                                    f"{window['action']!r}")
            if hazards and derived != "avoid":
                raise ScheduleError(f"window {window['id']}: hazard {hazards} обязан ронять "
                                    "action в avoid")
            if (measured["verdict"] == "PASS" and criterion["op"] == "<="
                    and derived != "avoid"):
                raise ScheduleError(f"window {window['id']}: PASS по критерию тишины "
                                    "не может рендериться чем-то кроме avoid")
            if measured["verdict"] == "PASS" and window["action"] == "enter":
                band = measured.get("ratio_noise_band_p10_p90")
                if band and band[0] <= float(criterion["value"]) <= band[1]:
                    raise ScheduleError(f"window {window['id']}: enter, но порог "
                                        f"{criterion['value']} лежит внутри шумового "
                                        f"коридора {band} — сигнал выбран порогом, а не "
                                        "наблюдением")
            if spread.get("ratio_inside_vs_background") is None and window["action"] == "enter":
                raise ScheduleError(f"window {window['id']}: enter при неизмеренном спреде")
    return windows


def _validate_seasons(window: dict[str, Any]) -> None:
    seasons = window.get("utc_seasons")
    window_id = window.get("id", "?")
    if seasons is None:
        if window.get("season_region"):
            raise ScheduleError(f"window {window_id}: season_region without utc_seasons")
        return
    region = window.get("season_region")
    if region not in DST_RULES:
        raise ScheduleError(f"window {window_id}: utc_seasons needs a known "
                            f"season_region, got {region!r}")
    for name in ("dst", "std"):
        spec = seasons.get(name)
        if not spec:
            raise ScheduleError(f"window {window_id}: utc_seasons.{name} missing")
        for field in ("start_minute", "end_minute"):
            minute = spec.get(field)
            if not isinstance(minute, int) or not 0 <= minute < 1440:
                raise ScheduleError(f"window {window_id}: utc_seasons.{name}.{field}="
                                    f"{minute!r} must be an int minute-of-day in [0, 1440)")
    dst_span = seasons["dst"]["end_minute"] - seasons["dst"]["start_minute"]
    std_span = seasons["std"]["end_minute"] - seasons["std"]["start_minute"]
    if dst_span != std_span:
        raise ScheduleError(f"window {window_id}: the two seasons must cover the same "
                            "local span "
                            f"({seasons['dst']['start_minute']}–{seasons['dst']['end_minute']} "
                            f"vs {seasons['std']['start_minute']}–{seasons['std']['end_minute']})")
    if dst_span < 0:
        dst_span += 1440
    shift = (seasons["std"]["start_minute"] - seasons["dst"]["start_minute"]) % 1440
    if shift != 60:
        raise ScheduleError(f"window {window_id}: the winter bracket must sit exactly one "
                            f"hour after the summer one (got {shift} min)")
    if window["utc"] != seasons["dst"]:
        raise ScheduleError(f"window {window_id}: `utc` must equal utc_seasons.dst so a "
                            "reader who ignores seasons gets the same summer bracket")


def load_windows(path: Path = DEFAULT_CONFIG) -> dict[str, Any]:
    import yaml

    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    validate(document)
    return document


def symbols_for(document: dict[str, Any], window: dict[str, Any]) -> list[str]:
    return sorted({symbol for cls in window["applies_to"]
                   for symbol in document["instrument_classes"][cls]["symbols"]})


def render_symbols(document: dict[str, Any], window: dict[str, Any]) -> str:
    """Instrument list with a dagger on any symbol this machine has never recorded."""
    dark = unrecorded_symbols(document)
    return ", ".join(f"{symbol}†" if symbol in dark else symbol
                     for symbol in symbols_for(document, window))


# --------------------------------------------------------------------------- render

ACTION_RU = {"enter": "вход", "avoid": "обходить", "neutral": "нейтрально"}
#: самая длинная метка оси — `avoid·NOT_MEASURED ⚠` (22 знака); колонка обязана быть шире,
#: иначе `⚠` сливается с названием окна и опасный час читается как обычный
ACTION_COLUMN_WIDTH = 24


def verdict_of(window: dict[str, Any]) -> str:
    """Verdict the evidence engine recorded, or NOT_MEASURED when there is no measurement."""
    measured = window.get("measured") or {}
    return measured.get("verdict") or "NOT_MEASURED"


def action_label(window: dict[str, Any]) -> str:
    """`action` печатается дословно (enter|avoid|neutral) плюс что сказал вердикт.

    Две оси рядом — это и есть смысл разделения: «PASS» из статистики диапазона и «avoid»
    из исполнимости могут законно стоять в одной строке (тихая книга), и ровно поэтому
    вердикт больше не притворяется сигналом.
    """
    action = window.get("action", "neutral")
    hazards = window.get("hazard") or []
    text = f"{action}·{verdict_of(window)}"
    return text + (" ⚠" if hazards else "")


def action_counts(windows: list[dict[str, Any]]) -> str:
    """Сводка по оси исполнимости — сколько окон вообще можно торговать."""
    tally = {action: 0 for action in ACTION_VALUES}
    for window in windows:
        tally[window.get("action", "neutral")] += 1
    verdicts = {verdict: 0 for verdict in VERDICTS}
    for window in windows:
        verdicts[verdict_of(window)] += 1
    return ("по оси действия: "
            + ", ".join(f"{action}={tally[action]} ({ACTION_RU[action]})"
                        for action in ACTION_VALUES)
            + " · по вердикту: "
            + ", ".join(f"{verdict}={verdicts[verdict]}" for verdict in VERDICTS))


def bracket_label(window: dict[str, Any], row: Occurrence) -> str:
    """The UTC span, plus which local clock produced it when the window is anchored."""
    span = f"{fmt_clock(row.start_minute)}–{fmt_clock(row.end_minute)}"
    if row.season == "fixed":
        return span
    local = window.get("anchor_local", "")
    return f"{span} ·{row.season}" + (f" {local}" if local else "")


def render_day(document: dict[str, Any], day: date, rows: list[Occurrence]) -> list[str]:
    by_id = {window["id"]: window for window in document["windows"]}
    names = holiday_names(document)
    half = early_close_names(document)
    title = f"{day.isoformat()} {WEEKDAY_RU[day.weekday()]}"
    if day in names:
        title += f"  · ЗАКРЫТО: {names[day]}"
    elif day in half:
        title += f"  · сокращённая сессия: {half[day]}"
    lines = [title]
    if not rows:
        lines.append("  (окон нет)" if day not in names else
                     "  (окон нет: рынок закрыт, производные правила вычли этот день)")
    for row in sorted(rows, key=lambda item: item.start_minute):
        window = by_id[row.window_id]
        cls = "/".join(document["instrument_classes"][c]["label"]
                       for c in window["applies_to"])
        flag = "  ← якорь на закрытом рынке" if row.on_holiday else ""
        spread = (window.get("measured") or {}).get("spread_ratio")
        spread_text = f" спред={spread}x" if spread is not None else ""
        lines.append(f"  {bracket_label(window, row):<44} "
                     f"{action_label(window):<{ACTION_COLUMN_WIDTH}}"
                     f"{window['local_name']}  [{cls}] "
                     f"{render_symbols(document, window)}{spread_text}{flag}")
    return lines


def season_legend(document: dict[str, Any], start: date, end: date) -> list[str]:
    """Say out loud which local clocks move inside this range, and when."""
    regions = sorted({window["season_region"] for window in document["windows"]
                      if window.get("utc_seasons")})
    lines = []
    for region in regions:
        flips = []
        day = start
        previous = is_dst(day, region)
        while day <= end:
            current = is_dst(day, region)
            if current != previous:
                flips.append(f"{day.isoformat()} → {'DST' if current else 'STD'}")
                previous = current
            day += timedelta(days=1)
        lines.append(f"  {region}: " + ("; ".join(flips) if flips
                                            else "сезон в диапазоне не меняется"))
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--from", dest="start", help="YYYY-MM-DD (default: today UTC)")
    parser.add_argument("--to", dest="end", help="YYYY-MM-DD (default: from + 13 days)")
    parser.add_argument("--week", action="store_true",
                        help="текущая неделя (Пн–Вс) вокруг today")
    parser.add_argument("--month", metavar="YYYY-MM", nargs="?", const="__today__",
                        help="месяц в виде списка окон по дням")
    parser.add_argument("--next", type=int, metavar="N",
                        help="ближайшие N окон (по времени старта, от today)")
    parser.add_argument("--json", action="store_true", help="машинный вывод")
    parser.add_argument("--today", help="YYYY-MM-DD for a deterministic run (tests)")
    args = parser.parse_args(argv)

    document = load_windows(args.config)
    today = date.fromisoformat(args.today) if args.today else datetime.now(UTC).date()

    if args.month is not None:
        anchor = today if args.month == "__today__" else \
            date.fromisoformat(args.month + "-01")
        start = anchor.replace(day=1)
        end = (date(anchor.year + (anchor.month == 12), anchor.month % 12 + 1, 1)
               - timedelta(days=1))
    elif args.week:
        start = today - timedelta(days=today.weekday())
        end = start + timedelta(days=6)
    else:
        start = date.fromisoformat(args.start) if args.start else today
        end = date.fromisoformat(args.end) if args.end else start + timedelta(days=13)
    if end < start:
        raise SystemExit(f"--to {end} is before --from {start}")

    by_id = {window["id"]: window for window in document["windows"]}
    holidays = holiday_names(document)
    every: list[Occurrence] = []
    for window in document["windows"]:
        every.extend(window_occurrences(window, start, end, holidays))

    if args.next is not None:
        upcoming = sorted((row for row in every
                           if (row.start, row.start_minute) >= (today, 0)),
                          key=lambda item: (item.start, item.start_minute))[:args.next]
        if args.json:
            print(json.dumps([{**row.__dict__} | {"start": row.start.isoformat()}
                              for row in upcoming], ensure_ascii=False, indent=1,
                             default=str))
            return 0
        print(f"БЛИЖАЙШИЕ {len(upcoming)} ОКОН · расписание {args.config.name}")
        print(f"  {action_counts([by_id[row.window_id] for row in upcoming])}")
        for row in upcoming:
            window = by_id[row.window_id]
            print(f"{row.start.isoformat()} {WEEKDAY_RU[row.start.weekday()]} "
                  f"{bracket_label(window, row):<44} "
                  f"{action_label(window):<{ACTION_COLUMN_WIDTH}}"
                  f"{window['local_name']}  "
                  f"[{SCALE_RU[window['scale']]}] {render_symbols(document, window)}"
                  + (f"  ← ЗАКРЫТО: {row.on_holiday}" if row.on_holiday else ""))
        if not upcoming:
            print("  (в диапазоне окон нет)")
        return 0

    if args.json:
        payload = {
            "range": {"start": start.isoformat(), "end": end.isoformat()},
            "days": {day.isoformat(): [{
                "window": row.window_id, "name": by_id[row.window_id]["local_name"],
                "scale": by_id[row.window_id]["scale"], "season": row.season,
                "start_utc": fmt_clock(row.start_minute),
                "end_utc": fmt_clock(row.end_minute),
                "holiday": row.on_holiday,
                "action": by_id[row.window_id].get("action"),
                "verdict": verdict_of(by_id[row.window_id]),
                "spread_ratio": (by_id[row.window_id].get("measured") or {}).get(
                    "spread_ratio"),
                "hazard": by_id[row.window_id].get("hazard") or [],
                "symbols": symbols_for(document, by_id[row.window_id]),
            } for row in every if row.start == day]
                for day in [start + timedelta(days=offset)
                            for offset in range((end - start).days + 1)]},
        }
        print(json.dumps(payload, ensure_ascii=False, indent=1))
        return 0

    title = {"month": "МЕСЯЧНЫЙ ВИД", "week": "НЕДЕЛЬНЫЙ ВИД"}.get(
        "month" if args.month else "week" if args.week else "days", "")
    print("=" * 96)
    print(f"РАСПИСАНИЕ ОКОН · {title or 'ВИД ПО ДНЯМ'} · "
          f"{start.isoformat()} → {end.isoformat()} (время UTC)")
    print("=" * 96)
    inert = [window["id"] for window in document["windows"] if window.get("inactive_reason")]
    if inert:
        print("ИНЕРТНЫЕ ОКНА (не сработают, пока список дат не верифицирован на этой "
              "машине): " + ", ".join(inert))
    print(action_counts(document["windows"]))
    for offset in range((end - start).days + 1):
        day = start + timedelta(days=offset)
        rows = [row for row in every if row.start == day]
        if args.month and not rows and day not in holiday_names(document):
            continue
        print()
        for line in render_day(document, day, rows):
            print(line)
    print()
    print("Скобка `·dst`/`·std` — летний/зимний вариант ONE локального якоря; без метки")
    print("окно фиксировано в UTC. Переходы местных часов в диапазоне:")
    for line in season_legend(document, start, end):
        print(line)
    print()
    print("Метка после скобки — ДВЕ ОСИ рядом: действие (enter|avoid|neutral, можно ли")
    print("торговать) и вердикт (PASS|FAIL|HYPOTHESIS|NOT_MEASURED, держится ли критерий")
    print("диапазона на этой выборке). `⚠` = календарно опасный час (расширение спреда,")
    print("брокерская остановка): он avoid и с измерением, и без него. `спред=Nx` —")
    print("медиана записанного спреда внутри окна относительно его же базы; `спред=None`")
    print("не печатается: неизмеренный спред называется словом, а не нулём.")
    print(DECIDED_BY_EVIDENCE_NOTE)
    print()
    print("† — инструмент без записанных баров на этой машине: для него окно остаётся")
    print("гипотезой. Мерки и исполнимость — scripts/windows_evidence.py.")
    states = document.get("risk_states") or []
    if states:
        print()
        print("РИСК-СОСТОЯНИЯ БЕЗ КАЛЕНДАРЯ (не окна, дежурство):")
        for state in states:
            print(f"  · {state['label']} [{', '.join(state['applies_to'])}] — "
                  f"{state['status']}; коридор: {state.get('watch_utc_band', '—')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
