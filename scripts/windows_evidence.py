#!/usr/bin/env python3
"""Мерки окон из config/windows.yaml на записанных барах: ничего не переносится из прошлого.

Проектная доктрина: любое число в документе — свежее измерение, а не унаследованное
утверждение. YAML держит гипотезы (expected/criterion); этот скрипт считает для каждого окна
медиану абсолютной относительной ширины бара (high-low)/mid внутри окна против базы, по всем
записанным инструментам его классов, и говорит, держится ли предзарегистрированный критерий
на ЭТОЙ выборке. Движок срабатываний общий с scripts/windows_schedule.py — включая
разрешение скобки по DST на каждой дате и фильтр биржевых закрытий, — поэтому «окно» в обоих
скриптах означает одно и то же: окно нельзя измерять по одной скобке, а торговать по другой.

ЧТО ЗДЕСЬ СЧИТАЕТСЯ ДОКАЗАТЕЛЬСТВОМ (и что нет):

  1. ВЕРДИКТ — это СТАТИСТИКА, и только она. Лестница такая и только такая:
       n_event_days <= THIN_SAMPLE_EVENT_DAYS (5)  -> HYPOTHESIS, права на слово
           «доказательство» нет, сколько бы отношение ни понравилось;
       иначе                                       -> PASS / FAIL по предзарегистрированному
           критерию этого окна на этой выборке.
     Всё прочее (неизмеренный сезон, шумовой коридор, закрытая или тонкая книга, спред)
     записывается в verdict_reasons как ОГОВОРКА к напечатанному вердикту и живёт на СВОЕЙ
     оси — `action`. Гасить из-за них PASS/FAIL раньше означало то, что и наблюдали:
     91-дневное окно с отношением 1.982 имело вердикт HYPOTHESIS, то есть лестница
     перестала различать измерение и анекдот.
     Ни одно окно не отчитывается при n_event_days < MIN_EVENT_DAYS (2) — см. collect().
  2. ACTION — ось исполнимости: enter | avoid | neutral. Она и отвечает на вопрос «можно ли
     это торговать», потому что «статистически заметно» и «торгуемо» — разные утверждения.
     Тихая книга (rollover, пятничное закрытие) и часы, известные расписанием расширения
     спреда и брокерских остановок (открытие недели после выходных, среда тройного свепа,
     первый бар понедельника), — это avoid, а не сигнал. Правило, которое здесь главное:
     PASS по КРИТЕРИЮ ТИШИНЫ (`<=`) никогда не рендерится как enter.
  3. база должна быть правильным сравнением. Для окна, живущего только в определённые дни
     недели или даты (weekly/monthly), «всё остальное в сутках» — неверный контроль: всплеск
     пятничного послеполудня, обогнавший общесуточный фон, чаще доказывает, что послеполудне
     оживлённее ночи, то есть перекрытие Лондон–Нью-Йорк, а не пятницу. Такие окна
     сравниваются с ТЕМИ ЖЕ часами в другие дни (hour-matched), а смешанная цифра печатается
     рядом с пометкой «искажена».
  4. тихая книга — не мёртвый рынок. Часть «низкой волатильности внутри окна» — это наложение
     окна на брокерский перерыв, где записи просто нет (измерено на этой записи: у XAUUSD 0
     из 91 буднего слота в 21:00 UTC и 4 из 91 в 22:00). Такое окно мерится coverage'ом
     собственных слотов; PASS, стоящий на закрытой книге, печатается с этой причиной и
     получает avoid — но не перестаёт быть измерением.
  5. спред — ВТОРАЯ КРИТЕРИАЛЬНАЯ ЛИНИЙКА каждого окна, а не справочная строка. Считается
     медиана записанного лентой спреда внутри окна против ТОЙ ЖЕ базы, что и у диапазона
     (hour-matched там, где она hour-matched), с теми же счётками выборки, и проверяется по
     собственному предзарегистрированному критерию окна (`spread_criterion`). Поле `spread`
     сначала аудируется само: покрытие по символу, минимум баров, и только потом вера.
     Измерение на этой ленте (см. spread_audit_note): у серебра спреда честная суточная
     структура (02:00 UTC ~34 пункта против ~18 в лондонско-нью-йоркский день), у золота
     колонка почти вырождена (медиана 8.0 в 21 часе из 23 — по ней окно не отличить),
     у валют покрытие ~2 % и одно модовое значение — там спред НЕ ИЗМЕРЁН, а не ноль.

Политика данных: только бары, только файлы с явными UTC-метками (agent_data/forward_bars/
*.json). Тик-стоимость у брокера не запрашивается: на этой ленте поля тика металлов известны
как ошибочные примерно в 10 раз, а относительная ширина по OHLC от тиковых соглашений не
зависит. Сети, ордеров, обращений к терминалу здесь нет: ничего нельзя открыть, изменить или
закрыть. --apply меняет только текст config/windows.yaml.

Использование:
    python scripts/windows_evidence.py                  # таблица по всем окнам
    python scripts/windows_evidence.py --window nfp_employment_release
    python scripts/windows_evidence.py --json           # машинный вывод мерок
    python scripts/windows_evidence.py --blocks         # блоки measured: для YAML
    python scripts/windows_evidence.py --apply          # вписать measured: в windows.yaml
"""
from __future__ import annotations

import argparse
import json
import random
import re
import statistics
import sys
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BARS_DIR = ROOT / "agent_data" / "forward_bars"
CONFIG = ROOT / "config" / "windows.yaml"

sys.path.insert(0, str(ROOT / "scripts"))
from windows_schedule import (  # noqa: E402
    ACTION_VALUES, DECIDED_BY_EVIDENCE_NOTE, MIN_BAR_COVERAGE, MIN_EVENT_DAYS,
    REQUIRED_EXECUTABILITY_FIELDS, REQUIRED_MEASURED_FIELDS, SPREAD_COST_KILL,
    SPREAD_THIN_MULTIPLE, THIN_SAMPLE_EVENT_DAYS, decide_action, holiday_names, is_dst,
    load_windows, resolve_bracket, unrecorded_symbols, window_occurrences,
)

RANGE_METRIC = "median_abs_rel_range_ratio_inside_vs_outside"
MATCHED_METRIC = "median_abs_rel_range_ratio_inside_vs_hour_matched_outside"
#: вторая критериальная линейка: медиана записанного спреда внутри окна против той же базы
SPREAD_RATIO_METRIC = "median_spread_points_ratio_inside_vs_outside"
SPREAD_MATCHED_METRIC = "median_spread_points_ratio_inside_vs_hour_matched_outside"
MIN_POWERED_EVENT_DAYS = 10  # меньше стольких срабатываний — оговорка «предварительно»
CAVEAT_EVENT_DAYS = 10       # ниже — оговорка «предварительно» печатается вслух
BOOTSTRAP_DRAWS = 1000       # шумовой коридор вокруг отношения
BOOTSTRAP_SEED = 20261105    # закреплён: коридор должен воспроизводиться, а не подбрасываться
MIN_SPREAD_COVERAGE = 0.5    # ниже этого spread-поле — пропуск, а не измерение
MIN_SPREAD_INSIDE_BARS = 20  # и с таким числом записанных спредов отношение не считают
SPREAD_HOUR_VARIATION = 1.15  # часовые медианы спреда должны различаться хотя бы в столько раз
WIDE_COVERAGE = 0.9          # покрытие ниже этого, но выше MIN_BAR_COVERAGE — частичная книга

#: масштабы, чья база обязана быть hour-matched, потому что они живут не каждый день
MATCHED_SCALES = ("weekly", "monthly")

#: обязательные поля measured: — ОДИН список на артефакт и на валидатор YAML, чтобы
#: напечатанное и проверенное не разъезжались
REQUIRED_MEASURED_KEYS = REQUIRED_MEASURED_FIELDS
REQUIRED_EXEC_KEYS = REQUIRED_EXECUTABILITY_FIELDS


@dataclass
class BarSet:
    """Один записанный инструмент: метки открытия, ширины баров, записанные спреды.

    `spreads` хранит None там, где лента не записала спред, поэтому пропуск поля не попадает в
    медиану как измерение. `point_size` берётся из digits, объявленных реестром символов в
    windows.yaml (лента пишет спред в точках), и только если реестр молчит — выводится из
    числа знаков, которые печатает сам файл. Нужна ровно для одного: перевести точки спреда в
    ценовые единицы бара.

    `spread_audit` описывает саму колонку `spread` на всём файле (покрытие, число различных
    значений, доля модового). Нужно для честного ответа на вопрос «а годится ли эта колонка
    вообще»: отношение внутри/снаружи у символа, где 60 % баров стоят ровно на одном моде,
    не различает окна, и врать об этом молча нельзя.
    """
    stamps: list[datetime] = field(default_factory=list)
    ranges: list[float] = field(default_factory=list)
    abs_ranges: list[float] = field(default_factory=list)
    spreads: list[float | None] = field(default_factory=list)
    point_size: float | None = None
    point_size_source: str = "нет"
    spread_audit: dict[str, object] = field(default_factory=dict)

    @property
    def span(self) -> tuple[datetime, datetime] | None:
        return (min(self.stamps), max(self.stamps)) if self.stamps else None

    @property
    def usable_spread_column(self) -> bool:
        """Один ответ на вопрос «годится ли колонка»: его даёт аудит, а не догадка свойств.

        Покрытие и число значений сами по себе обманывают: у золота на этой ленте 22
        различных значения при 92 % покрытия, но часовая медиана стоит на 8.0 в 21 часу из
        22 — отношение «внутри/снаружи» по такой колонке всегда 1.0 и не различает окна.
        """
        return bool(self.spread_audit.get("usable"))


@dataclass
class Bracket:
    """Сработавшее окно: абсолютные UTC-моменты плюс сезон, который их породил."""
    day: date
    start: datetime
    end: datetime
    season: str


def median(values: list[float]) -> float:
    return statistics.median(values)


def spread_column_audit(values: list[float | None],
                        stamps: list[datetime] | None = None) -> dict[str, object]:
    """Аудит самой колонки `spread` на всём файле: годится ли она для сравнения окон.

    Печатается рядом с каждым отношением, потому что отношение, построенное на моде
    «всегда 8», — это не измерение. Три проверки: покрытие поля, число различных значений и
    РАЗЛИЧИЕ МЕДИАН ПО ЧАСАМ СУТОК. Последнее важнее первых двух: у золота на этой ленте 22
    различных значения, но часовая медиана 8.0 стоит в 21 часу из 22, то есть колонка не
    различает ни одно окно; у серебра часовые медианы ходят от 18 до 34 пунктов, и сравнение
    внутри/снаружи по нему осмысленно. Модовая доля и часовой размах считаются только по
    часам, где есть >= MIN_SPREAD_INSIDE_BARS записанных спредов, чтобы один редкий час не
    выдавал себя за структуру.
    """
    recorded = [float(value) for value in values if value is not None]
    total = len(values)
    if not recorded:
        return {"coverage": 0.0, "distinct_values": 0, "modal_value": None, "modal_share": 0.0,
                "median_points": None, "hour_median_spread": None, "usable": False,
                "note": "колонка пуст: спред не записан ни в одном баре"}
    counts: dict[float, int] = {}
    for value in recorded:
        counts[value] = counts.get(value, 0) + 1
    modal, hits = max(counts.items(), key=lambda item: (item[1], item[0]))
    share = hits / len(recorded)
    by_hour: dict[int, list[float]] = {}
    if stamps:
        for stamp, value in zip(stamps, values):
            if value is not None:
                by_hour.setdefault(stamp.hour, []).append(float(value))
    hour_medians = [median(pool) for pool in by_hour.values()
                    if len(pool) >= MIN_SPREAD_INSIDE_BARS]
    hour_ratio = (round(max(hour_medians) / min(hour_medians), 3)
                  if len(hour_medians) >= 2 and min(hour_medians) > 0 else None)
    usable = (len(recorded) / total >= MIN_SPREAD_COVERAGE and len(counts) >= 5
              and share < 0.75 and hour_ratio is not None and hour_ratio >= SPREAD_HOUR_VARIATION)
    reasons = []
    if len(recorded) / total < MIN_SPREAD_COVERAGE:
        reasons.append(f"покрытие {len(recorded) / total:.1%} < {MIN_SPREAD_COVERAGE:.0%}")
    if len(counts) < 5:
        reasons.append(f"лишь {len(counts)} различных значений")
    if share >= 0.75:
        reasons.append(f"мод {modal} занимает {share:.0%} баров")
    if hour_ratio is None:
        reasons.append("часовых медиан меньше двух — суточной структуры не видно")
    elif hour_ratio < SPREAD_HOUR_VARIATION:
        reasons.append(f"часовые медианы различаются всего в {hour_ratio}x — по колонке окна "
                       "не различимы")
    note = ("колонка пригодна для сравнения окон" if usable
            else "колонка не годится для сравнения окон: " + "; ".join(reasons))
    return {"coverage": round(len(recorded) / total, 4), "distinct_values": len(counts),
            "modal_value": modal, "modal_share": round(share, 4),
            "median_points": round(median(recorded), 2),
            "hour_median_spread": round(hour_ratio, 3) if hour_ratio else None,
            "usable": usable, "note": note}


def infer_point_size(rows: list[dict]) -> tuple[float | None, str]:
    """Запасной вариант, когда реестр не объявил digits: модальное число знаков.

    Модал, а не максимум: JSON выбрасывает ведомые нули, поэтому пятиразрядный знак может
    напечататься как 1.16, а один артефакт float не должен сдвигать точку на десять
    порядков.
    """
    counts: dict[int, int] = {}
    for row in rows:
        for value in (row["h"], row["l"]):
            text = repr(float(value))
            if "." in text and "e" not in text.lower():
                decimals = len(text.split(".")[1])
                if decimals <= 8:
                    counts[decimals] = counts.get(decimals, 0) + 1
    if not counts:
        return None, "не выведено"
    return 10.0 ** -max(counts, key=lambda d: (counts[d], d)), "выведено из разрядов файла"


def load_bars(path: Path, digits: int | None = None) -> BarSet:
    rows = json.loads(path.read_text(encoding="utf-8"))
    stamps, ranges, abs_ranges, spreads = [], [], [], []
    if digits is not None:
        point_size, source = 10.0 ** -digits, f"digits={digits} из реестра символов"
    else:
        point_size, source = infer_point_size(rows)
    for row in rows:
        moment = datetime.fromisoformat(row["time"])
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        high, low = float(row["h"]), float(row["l"])
        mid = (high + low) / 2.0
        if mid <= 0:
            continue
        raw_spread = row.get("spread")
        stamps.append(moment.astimezone(UTC))
        ranges.append((high - low) / mid)
        abs_ranges.append(high - low)
        spreads.append(None if raw_spread is None or float(raw_spread) <= 0
                       else float(raw_spread))
    audit = spread_column_audit(spreads, stamps)
    return BarSet(stamps, ranges, abs_ranges, spreads, point_size, source, audit)


def resolved_brackets(window: dict, first: date, last: date,
                      holidays: dict) -> dict[date, Bracket]:
    """день -> скобка, которую это окно означает в этот день, с сезоном и переносом через UTC.

    Считаётся через общий движок срабатываний, поэтому праздник или перевод часов сдвигают
    измерение ровно так же, как сдвигают напечатанное расписание.
    """
    out: dict[date, Bracket] = {}
    for row in window_occurrences(window, first, last, set(holidays)):
        midnight = datetime(row.start.year, row.start.month, row.start.day, tzinfo=UTC)
        out[row.start] = Bracket(row.start, midnight + timedelta(minutes=row.start_minute),
                                 midnight + timedelta(minutes=row.end_minute), row.season)
    return out


def classify(window: dict, bars: BarSet, holidays: dict) -> tuple[list[bool], list[bool]]:
    """(внутри, hour-matched контроль) по барам одного символа.

    Бар помечен временем СВОЕГО открытия и сопоставляется полуинтервалом [start, end), поэтому
    якорем служат дата самого бара и дата накануне (для скобок через полночь). Из-за этой
    метки старая зимняя скобка 12:30–14:00 UTC ловила пред-релизный бар: свеча, открытая в
    13:00 UTC, и есть та, внутри которой случился релиз 13:30 UTC.
    """
    if not bars.stamps:
        return [], []
    first, last = bars.span[0].date(), bars.span[-1].date()
    brackets = resolved_brackets(window, first, last, holidays)
    inside = [False] * len(bars.stamps)
    control = [False] * len(bars.stamps)
    for index, stamp in enumerate(bars.stamps):
        hit = False
        for anchor in (stamp.date(), stamp.date() - timedelta(days=1)):
            bracket = brackets.get(anchor)
            if bracket and bracket.start <= stamp < bracket.end:
                hit = True
                break
        if hit:
            inside[index] = True
            continue
        # контроль — та же скобка в день, когда окно НЕ срабатывает: те же часы обычного
        # торгового дня; праздники исключены из обеих половин сравнения.
        day = stamp.date()
        if day in brackets or day in holidays:
            continue
        begin, finish, _ = resolve_bracket(window, day)
        minute = stamp.hour * 60 + stamp.minute
        if begin <= minute < finish:
            control[index] = True
        elif finish > 1440 and minute < begin and minute + 1440 < finish:
            control[index] = True
    return inside, control


def bar_slots(window: dict, day: date) -> list[tuple[date, int]]:
    """Целиком покрытые окном часы H1, которые скобка этого окна требует в `day`.

    Бар с меткой 13:00 накрывает 13:00–14:00, поэтому считается только целый час внутри
    скобки; скобка через полночь осознанно даёт слоты следующего дня — так лента их и хранит.
    """
    begin, finish, _ = resolve_bracket(window, day)
    slots: list[tuple[date, int]] = []
    hour = begin // 60
    while hour * 60 < finish:
        slots.append((day + timedelta(days=hour // 24), hour % 24))
        hour += 1
    return slots


def spread_block(bars: dict[str, BarSet], symbols: list[str],
                 masks: dict[str, tuple[list[bool], list[bool]]],
                 use_matched: bool) -> dict:
    """Поле `spread` записанной ленты: сначала проверка покрытия, потом вера.

    Проверяется ПО СИМВОЛАМ и в ценовых единицах самого символа: складывать сырые точки
    двухразрядного металла и пятиразрядной валюты — не измерение, а усреднение
    отсутствующего спреда как нуля молча выдало бы бесплатную книгу там, где лента просто
    молчит. Поэтому у символа с плохим покрытием стоит null и его покрытие, а не число.

    База сравнения — ТА ЖЕ, что и у диапазона: hour-matched («те же часы в другие дни») для
    окон weekly/monthly и общий фон суток для дневных. Сравнивать спред пятничного закрытия
    с «ночным фоном» — значит измерить время суток, а не окно. Дополнительно к покрытию
    требуется минимум записанных спредов с обеих сторон и непривырожденная колонка (см.
    spread_column_audit): отношение, посчитанное по четырём барам валюты, не печатается
    измерением.
    """
    by_symbol: dict[str, dict] = {}
    measurable: dict[str, float] = {}
    costs: dict[str, float] = {}
    for symbol in symbols:
        bar_set = bars[symbol]
        flags, control = masks[symbol]
        own_inside: list[float] = []
        own_background: list[float] = []
        own_abs_ranges: list[float] = []
        for index, value in enumerate(bar_set.spreads):
            if flags[index]:
                if value is not None:
                    own_inside.append(value)
                own_abs_ranges.append(bar_set.abs_ranges[index])
            elif use_matched:
                if control[index] and value is not None:
                    own_background.append(value)
            elif value is not None:
                own_background.append(value)
        n_inside = sum(flags)
        n_base = sum(control) if use_matched else len(bar_set.stamps) - n_inside
        coverage = len(own_inside) / n_inside if n_inside else 0.0
        base_coverage = len(own_background) / n_base if n_base else 0.0
        row = {
            "n_inside_bars": n_inside,
            "n_background_bars": n_base,
            "spread_recorded": len(own_inside),
            "spread_recorded_background": len(own_background),
            "spread_coverage_inside": round(coverage, 4),
            "spread_coverage_background": round(base_coverage, 4),
            "median_points_inside": round(median(own_inside), 2) if own_inside else None,
            "median_points_background": (round(median(own_background), 2) if own_background
                                         else None),
            "point_size": bar_set.point_size,
            "ratio_inside_vs_background": None,
            "crossing_cost_fraction_of_hour_range": None,
            "spread_column": bar_set.spread_audit,
            "usable_column": bar_set.usable_spread_column,
        }
        enough = (len(own_inside) >= MIN_SPREAD_INSIDE_BARS
                  and len(own_background) >= MIN_SPREAD_INSIDE_BARS)
        if (coverage >= MIN_SPREAD_COVERAGE and enough and row["usable_column"]
                and own_inside and own_background):
            row["ratio_inside_vs_background"] = round(
                median(own_inside) / median(own_background), 3)
            measurable[symbol] = row["ratio_inside_vs_background"]
        elif coverage >= MIN_SPREAD_COVERAGE and row["usable_column"] and not enough:
            row["note"] = (f"записанных спредов {len(own_inside)} внутри / "
                           f"{len(own_background)} в базе: нужно >= "
                           f"{MIN_SPREAD_INSIDE_BARS} с обеих сторон — отношение не считается")
        elif coverage >= MIN_SPREAD_COVERAGE and not row["usable_column"]:
            row["note"] = ("колонка spread у этого символа вырождена ("
                           + str(bar_set.spread_audit["note"]) + ") — отношением пользоваться "
                           "нельзя, печатается только аудит колонки")
        else:
            row["note"] = (f"покрытие поля {coverage:.1%} < {MIN_SPREAD_COVERAGE:.0%} — "
                           "спред НЕ ИЗМЕРЕН, пропуски в медиану не подставлены")
        if (own_inside and own_abs_ranges and bar_set.point_size and row["usable_column"]
                and len(own_inside) >= MIN_SPREAD_INSIDE_BARS):
            costs[symbol] = round(median(own_inside) * bar_set.point_size
                                  / max(median(own_abs_ranges), 1e-12), 5)
            row["crossing_cost_fraction_of_hour_range"] = costs[symbol]
        by_symbol[symbol] = row
    block = {
        "unit_note": ("spread лента пишет в точках; точка — из digits реестра символов ("
                      + "; ".join(f"{s}: {bars[s].point_size_source}" for s in symbols)
                      + "); перевод в цены нужен только тем, чтобы сравнить спред с шириной "
                        "бара"),
        "baseline": ("hour_matched_other_days" if use_matched else "pooled_day_background"),
        "by_symbol": dict(sorted(by_symbol.items())),
        "measurable_symbols": sorted(measurable),
        "unmeasurable_symbols": sorted(set(symbols) - set(measurable)),
        # берётся максимум по измеримым символам: дороже всего проход там, где он дороже, а
        # не «в среднем по книге», где металл и валюта утопили бы сигнал
        "ratio_inside_vs_background": (max(measurable.values()) if measurable else None),
        "ratio_driver": (max(measurable, key=lambda s: measurable[s]) if measurable else None),
        "crossing_cost_fraction_of_hour_range": (max(costs.values()) if costs else None),
        "crossing_cost_driver": (max(costs, key=lambda s: costs[s]) if costs else None),
        "coverage_threshold": MIN_SPREAD_COVERAGE,
        "min_spread_bars": MIN_SPREAD_INSIDE_BARS,
    }
    if not measurable:
        block["note"] = (
            f"ни по одному символу окна spread не записан хотя бы на "
            f"{MIN_SPREAD_COVERAGE:.0%} его баров ({MIN_SPREAD_INSIDE_BARS}+ штук, колонка "
            "не вырождена) — это пропуск поля, а не нулевой спред; спред внутри окна "
            "НЕ ИЗМЕРЕН, пропуски в медиану не подставлены")
    return block


def executability(window: dict, bars: dict[str, BarSet], symbols: list[str],
                  holidays: dict, masks: dict[str, tuple[list[bool], list[bool]]],
                  use_matched: bool) -> dict:
    """Окно — это рынок или дыра в ленте, похожая на рынок?

    Знаменатель: слоты тех дней, когда окно действительно срабатывает (его срабатывания, а не
    каждый день календаря) И только по тем таким дням, в которые инструмент торговал вообще. В
    субботу баров нет потому, что нет книги, и считать её в знаменателе — значит объявить
    тонким каждое дневное окно. Дни без единого бара при этом не прячутся: они печатаются
    отдельной строкой, потому что «окно срабатывает в пустой ленте» — это и есть находка про
    исполнимость.
    """
    expected = present = empty_days = 0
    per_slot: dict[int, dict[str, list[int]]] = {}
    for symbol in symbols:
        bar_set = bars[symbol]
        if not bar_set.stamps:
            continue
        first, last = bar_set.span[0].date(), bar_set.span[-1].date()
        brackets = resolved_brackets(window, first, last, holidays)
        filled = {(stamp.date(), stamp.hour) for stamp in bar_set.stamps}
        alive = {day for day, _ in filled}
        for day in sorted(brackets):
            if day not in alive:
                empty_days += 1
                continue
            for slot_day, hour in bar_slots(window, day):
                expected += 1
                got = (slot_day, hour) in filled
                present += 1 if got else 0
                bucket = per_slot.setdefault(hour, {}).setdefault(symbol, [0, 0])
                bucket[1] += 1
                bucket[0] += 1 if got else 0
    dead = [hour for hour, by_symbol in sorted(per_slot.items())
            if all(counts[0] == 0 for counts in by_symbol.values())]
    sparse = [hour for hour, by_symbol in sorted(per_slot.items())
              if any(counts[0] * 4 < counts[1] for counts in by_symbol.values())]
    coverage = present / expected if expected else 0.0
    spread = spread_block(bars, symbols, masks, use_matched)

    verdict = "EXECUTABLE"
    if not expected:
        verdict = "NO_SLOTS"
    elif coverage < MIN_BAR_COVERAGE:
        verdict = "CLOSED_BOOK"
    elif coverage < WIDE_COVERAGE:
        verdict = "THIN_BOOK"
    ratio = spread["ratio_inside_vs_background"]
    if verdict == "EXECUTABLE" and ratio is not None and ratio >= SPREAD_THIN_MULTIPLE:
        verdict = "THIN_BOOK"
    return {"verdict": verdict,
            "bar_coverage_inside": round(coverage, 4),
            "n_expected_slots": expected,
            "n_present_bars": present,
            "n_occurrence_days_without_any_bars": empty_days,
            "coverage_note": ("доля слотов часа, которые окно требует, имеющих запись; в "
                              "знаменателе только те дни срабатывания окна, по которым у "
                              "символа есть хоть один бар (выходные и closures не размывают "
                              "показатель, но учтены строкой "
                              "n_occurrence_days_without_any_bars)"),
            "dead_hour_slots_utc": [f"{hour:02d}:00" for hour in dead],
            "sparse_hour_slots_utc": [f"{hour:02d}:00" for hour in sparse],
            "per_slot_fill": {f"{hour:02d}:00": {symbol: f"{counts[0]}/{counts[1]}"
                                                 for symbol, counts in per_slot[hour].items()}
                              for hour in sorted(per_slot)},
            "spread": spread}


def bootstrapped_band(inside: list[float], baseline: list[float],
                      draws: int = BOOTSTRAP_DRAWS) -> tuple[float, float] | None:
    """Коридор 10–90 % для отношения медиан, передискретизированный до меньшей выборки.

    Детерминирован (закреплённое зерно), чтобы одна и та же запись давала один и тот же
    коридор. База передискретизируется до объёма «внутри»: коридор описывает, насколько могла
    уехать ДЕФИЦИТНАЯ половина сравнения, а не вознаграждает базу за тысячи баров.
    """
    if len(inside) < 2 or len(baseline) < 2:
        return None
    rng = random.Random(BOOTSTRAP_SEED)
    size = len(inside)
    ratios = []
    for _ in range(draws):
        low = median(rng.choices(inside, k=size))
        high = median(rng.choices(baseline, k=min(size, len(baseline))))
        if high > 0:
            ratios.append(low / high)
    if len(ratios) < 20:
        return None
    ratios.sort()
    return (round(ratios[int(0.10 * len(ratios))], 3),
            round(ratios[min(int(0.90 * len(ratios)), len(ratios) - 1)], 3))


def holds(ratio: float, criterion: dict) -> bool:
    if criterion["op"] == ">=":
        return ratio >= criterion["value"]
    if criterion["op"] == "<=":
        return ratio <= criterion["value"]
    return ratio == criterion["value"]


def spread_summary(window: dict, exec_block: dict) -> dict:
    """Вторая критериальная линия окна: СПРЕД, вынесенный из executability на верх measured.

    Имя «spread_ratio» и сырые медианы должны быть видны без раскручивания блока: у окна,
    которое выглядит интересным по диапазону, но убивает 0.01 лота металла spreads'ом, ответ
    живёт здесь. Отношение — максимум по измеримым символам (дороже всего проход там, где
    дороже); водителем числится именно тот символ, по которому взяты медианы и счётки, чтобы
    «медиана внутри/снаружи» не была средним по разным шкалам.
    """
    spread = exec_block["spread"]
    driver_name = spread["ratio_driver"]
    driver = spread["by_symbol"].get(driver_name) if driver_name else None
    matched = spread["baseline"] == "hour_matched_other_days"
    out: dict[str, Any] = {
        "spread_metric": SPREAD_MATCHED_METRIC if matched else SPREAD_RATIO_METRIC,
        "spread_baseline": spread["baseline"],
        "spread_ratio": spread["ratio_inside_vs_background"],
        "spread_ratio_driver": driver_name,
        "median_spread_points_inside": (driver["median_points_inside"] if driver else None),
        "median_spread_points_outside": (driver["median_points_background"] if driver
                                         else None),
        "n_spread_bars_inside": driver["spread_recorded"] if driver else 0,
        "n_spread_bars_outside": (driver["spread_recorded_background"] if driver else 0),
        "n_window_bars_inside": driver["n_inside_bars"] if driver else 0,
        "spread_coverage_inside": driver["spread_coverage_inside"] if driver else None,
        "spread_point_size": driver["point_size"] if driver else None,
        "spread_measurable_symbols": spread["measurable_symbols"],
        "spread_unmeasurable_symbols": spread["unmeasurable_symbols"],
        "spread_column_audit": {symbol: {key: row["spread_column"][key] for key in
                                         ("coverage", "distinct_values", "modal_share",
                                          "hour_median_spread", "usable")}
                                for symbol, row in spread["by_symbol"].items()},
        "crossing_cost_fraction_of_hour_range": (
            spread["crossing_cost_fraction_of_hour_range"]),
        "crossing_cost_driver": spread["crossing_cost_driver"],
    }
    crit = window.get("spread_criterion")
    if not crit:
        out["spread_criterion"] = "не предзарегистрирован"
        out["spread_criterion_holds"] = "NOT_MEASURED"
        out["spread_criterion_note"] = ("у окна нет второй критериальной линии по спреду — "
                                        "добавьте spread_criterion в windows.yaml")
        return out
    threshold = float(crit["value"])
    out["spread_criterion"] = f"{crit['op']} {threshold}"
    ratio = out["spread_ratio"]
    if ratio is None:
        out["spread_criterion_holds"] = "NOT_MEASURED"
        out["spread_criterion_note"] = spread.get(
            "note", "спред внутри окна не измерен: отношение не проверяется")
    elif holds(ratio, {"op": crit["op"], "value": threshold}):
        out["spread_criterion_holds"] = "PASS"
    else:
        out["spread_criterion_holds"] = "FAIL"
        out["spread_criterion_note"] = (
            f"спред внутри {ratio}x фона при пределе {threshold}x по символу "
            f"{out['spread_ratio_driver']} — книга в этом окне дороже, чем обещает гипотеза")
    return out


def measure(window: dict, bars: dict[str, BarSet], classes: dict,
            holidays: dict | None = None) -> dict | None:
    """Объединённые медианы «внутри/база» по применимым символам, у которых есть бары."""
    holidays = holidays or {}
    symbols = [symbol for cls in window["applies_to"]
               for symbol in classes[cls]["symbols"]
               if symbol in bars and bars[symbol].stamps]
    if not symbols:
        return None
    inside_values: list[float] = []
    pooled_outside: list[float] = []
    matched_values: list[float] = []
    event_days: set[str] = set()
    days_by_season: dict[str, set[str]] = {"dst": set(), "std": set(), "fixed": set()}
    masks: dict[str, tuple[list[bool], list[bool]]] = {}
    for symbol in symbols:
        bar_set = bars[symbol]
        flags, control = classify(window, bar_set, holidays)
        masks[symbol] = (flags, control)
        first, last = bar_set.span[0].date(), bar_set.span[-1].date()
        brackets = resolved_brackets(window, first, last, holidays)
        for flag, control_flag, value, stamp in zip(flags, control, bar_set.ranges,
                                                   bar_set.stamps):
            if flag:
                inside_values.append(value)
                bracket = (brackets.get(stamp.date())
                           or brackets.get(stamp.date() - timedelta(days=1)))
                if bracket is not None:
                    event_days.add(bracket.day.isoformat())
                    days_by_season[bracket.season].add(bracket.day.isoformat())
            else:
                pooled_outside.append(value)
                if control_flag:
                    matched_values.append(value)
    if not inside_values or not pooled_outside:
        return None
    if window.get("utc_seasons"):
        by_season = {"dst": len(days_by_season["dst"]), "std": len(days_by_season["std"])}
    else:
        by_season = {"fixed": len(days_by_season["fixed"])}

    criterion = window["criterion"]
    matched = window.get("scale") in MATCHED_SCALES or criterion["metric"] == MATCHED_METRIC
    use_matched = matched and len(matched_values) >= MIN_EVENT_DAYS
    baseline = matched_values if use_matched else pooled_outside
    baseline_kind = "hour_matched_other_days" if use_matched else "pooled_day_background"
    med_in = median(inside_values)
    med_base = median(baseline)
    ratio = med_in / med_base
    band = bootstrapped_band(inside_values, baseline)
    criterion_value = float(criterion["value"])
    in_noise_band = band is not None and band[0] <= criterion_value <= band[1]
    passed = holds(ratio, criterion)

    exec_block = executability(window, bars, symbols, holidays, masks, use_matched)
    caveats: list[str] = []
    if len(event_days) < MIN_POWERED_EVENT_DAYS:
        caveats.append(f"оговорка (вердикта не снижает): выборка {len(event_days)} "
                       f"срабатываний < {MIN_POWERED_EVENT_DAYS} — число срабатываний "
                       "напечатано рядом")
    today = datetime.now(UTC).date()
    if not window.get("utc_seasons"):
        season_now = None
    else:
        season_now = "dst" if is_dst(today, window["season_region"]) else "std"
        if by_season[season_now] < MIN_POWERED_EVENT_DAYS:
            label = "летняя (dst)" if season_now == "dst" else "зимняя (std)"
            measured_here = by_season[season_now]
            tail = ("не измерена ни разу" if measured_here == 0 else
                    f"измерена всего {measured_here} раз(а) — тонкая выборка")
            caveats.append(
                f"оговорка (вердикта не снижает): окно привязано к чужим часам, сегодня "
                f"действует {label} половина скобки, а её геометрия "
                f"(dst={by_season['dst']}, std={by_season['std']}) — это {tail}: расписание "
                "на эти недели торгует расчётным сдвигом, а не измеренным")
    if in_noise_band:
        caveats.append(f"оговорка (вердикта не снижает): порог {criterion_value} лежит внутри "
                       f"шумового коридора [{band[0]}, {band[1]}] — вердикт зависит от порога, "
                       "а не опирается на него")
    if exec_block["verdict"] != "EXECUTABLE":
        driver: list[str] = []
        if exec_block["bar_coverage_inside"] < WIDE_COVERAGE:
            driver.append(f"покрытие баров {exec_block['bar_coverage_inside']:.3f}, "
                          f"мёртвые часы {exec_block['dead_hour_slots_utc']}")
        thin_ratio = exec_block["spread"]["ratio_inside_vs_background"]
        if thin_ratio is not None and thin_ratio >= SPREAD_THIN_MULTIPLE:
            driver.append(f"спред внутри окна в {thin_ratio} раз(а) шире фона "
                          f"(порог {SPREAD_THIN_MULTIPLE})")
        caveats.append(f"оговорка (вердикта не снижает): книга в окне "
                       f"{exec_block['verdict']}: "
                       + "; ".join(driver or ["нет измеренного покрытия"]))

    # Лестница вердиктов: HYPOTHESIS — ТОЛЬКО тонкая выборка. Всё остальное — PASS/FAIL по
    # предзарегистрированному критерию этого окна; прочее живёт в оговорках и на оси action.
    thin_sample = len(event_days) <= THIN_SAMPLE_EVENT_DAYS
    verdict = "HYPOTHESIS" if thin_sample else ("PASS" if passed else "FAIL")
    reasons: list[str] = []
    if thin_sample:
        reasons.append(
            f"вердикт HYPOTHESIS: {len(event_days)} срабатываний <= "
            f"{THIN_SAMPLE_EVENT_DAYS} — при такой выборке арифметика "
            f"({criterion['op']} {criterion_value} против {round(ratio, 3)}) не даёт права "
            "называть это доказательством, это анекдот про эти дни")
    reasons.extend(caveats)
    caveat = None
    if len(event_days) < CAVEAT_EVENT_DAYS:
        detail = (f"летом {len(days_by_season['dst'])}, зимой {len(days_by_season['std'])}"
                  if window.get("utc_seasons")
                  else f"несезонное окно: {len(days_by_season['fixed'])}")
        caveat = f"предварительно: в записи лишь {len(event_days)} срабатываний ({detail})"
    unmeasured = sorted({symbol for cls in window["applies_to"]
                         for symbol in classes[cls]["symbols"]
                         if symbol not in bars or not bars[symbol].stamps})
    per_symbol: dict[str, dict] = {}
    for symbol in symbols:
        bar_set = bars[symbol]
        flags, control = masks[symbol]
        ins = [v for v, flag in zip(bar_set.ranges, flags) if flag]
        base = ([v for v, c, flag in zip(bar_set.ranges, control, flags) if c and not flag]
                if use_matched else
                [v for v, flag in zip(bar_set.ranges, flags) if not flag])
        if len(ins) >= 2 and len(base) >= 2:
            per_symbol[symbol] = {"n_inside_bars": len(ins), "n_base_bars": len(base),
                                  "ratio": round(median(ins) / median(base), 3)}

    result = {
        "metric": MATCHED_METRIC if use_matched else RANGE_METRIC,
        "baseline": baseline_kind,
        "symbols_measured": symbols,
        "symbols_without_bars": unmeasured,
        "timeframe": "H1",
        "source": [f"agent_data/forward_bars/{symbol}.json" for symbol in symbols],
        "generated_at": datetime.now(UTC).date().isoformat(),
        "n_inside_bars": len(inside_values),
        "n_baseline_bars": len(baseline),
        "n_event_days": len(event_days),
        "n_event_days_by_season": by_season,
        "season_in_effect_at_measurement": season_now or "fixed",
        "median_rel_range_inside": round(med_in, 6),
        "median_rel_range_baseline": round(med_base, 6),
        "ratio": round(ratio, 3),
        "ratio_noise_band_p10_p90": list(band) if band else None,
        "criterion": f"{criterion['op']} {criterion_value}",
        "criterion_holds": "PASS" if passed else "FAIL",
        "verdict": verdict,
        "verdict_reasons": reasons,
        "executability": exec_block,
        "caveat": caveat,
        "per_symbol": per_symbol,
    }
    result.update(spread_summary(window, exec_block))
    result["action"], result["action_reasons"] = decide_action(window, result)
    if matched and len(matched_values) >= 2:
        result["ratio_vs_pooled_day_background"] = round(med_in / median(pooled_outside), 3)
    if len(per_symbol) > 1:
        # объединённая медиана смешивает инструменты, чьи часовые ширины различаются в разы
        # (металлы и валюта), поэтому может лежать между двумя кластерами и не описывать ни
        # один. Вердикт остаётся на предзарегистрированном объединённом числе — порог не
        # двигался; эта сопутствующая цифра напечатана, чтобы видно было, согласны ли
        # кластеры.
        ratios = sorted(values["ratio"] for values in per_symbol.values())
        result["ratio_within_symbol_median"] = round(median(ratios), 3)
        result["ratio_within_symbol_span"] = [ratios[0], ratios[-1]]
    return result


def collect(document: dict, bars: dict[str, BarSet]) -> list[tuple[dict, dict | None, str]]:
    """Мерки по всем окнам: список (окно, measured, причина «не измерено»)."""
    classes = document["instrument_classes"]
    holidays = holiday_names(document)
    rows: list[tuple[dict, dict | None, str]] = []
    for window in document["windows"]:
        rule = window.get("rule") or {}
        if rule.get("kind") == "dates" and not rule.get("dates"):
            reason = " ".join((window.get("inactive_reason") or "").split())
            rows.append((window, None,
                         f"окно инертно: якорных дат в правиле нет. {reason[:220]}"))
            continue
        if window["criterion"]["metric"] not in (RANGE_METRIC, MATCHED_METRIC):
            rows.append((window, None, "метрика не измерима по барам H1"))
            continue
        result = measure(window, bars, classes, holidays)
        note = ""
        if result and result["n_event_days"] < MIN_EVENT_DAYS:
            note = (f"событий в записи: {result['n_event_days']} — это анекдот, не выборка")
            result = None
        if result is None and not note:
            note = ("нет записанных баров ни по одному применимому символу окна"
                    if not any(sym in bars and bars[sym].stamps
                               for cls in window["applies_to"]
                               for sym in classes[cls]["symbols"])
                    else "окно не попало в запись: внутри него нет ни одного бара")
        rows.append((window, result, note))
    return rows


def flow(value: object) -> str:
    """Компактная однострочная YAML-запись словаря/списка (валидный flow-стиль)."""
    if isinstance(value, dict):
        return "{" + ", ".join(f"{key}: {flow(item)}" for key, item in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ", ".join(flow(item) for item in value) + "]"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if value is None:
        return "null"
    return json.dumps(value)


def render_measured(result: dict) -> str:
    """Блок `measured:` для YAML: те же числа, что напечатаны таблицей, без потерь."""
    for key in REQUIRED_MEASURED_KEYS:
        if key not in result:
            raise SystemExit(f"measured потерял обязательное поле {key!r}")
    for key in REQUIRED_EXEC_KEYS:
        if key not in result["executability"]:
            raise SystemExit(f"measured.executability потерял поле {key!r}")
    exec_block = result["executability"]
    spread = exec_block["spread"]
    lines = ["    measured:",
             f"      source: {flow(result['source'])}",
             f"      generated_at: {flow(result['generated_at'])}",
             f"      timeframe: {result['timeframe']}",
             f"      metric: {result['metric']}",
             f"      baseline: {result['baseline']}",
             f"      symbols_measured: {flow(result['symbols_measured'])}",
             f"      symbols_without_bars: {flow(result['symbols_without_bars'])}",
             f"      n_inside_bars: {result['n_inside_bars']}",
             f"      n_baseline_bars: {result['n_baseline_bars']}",
             f"      n_event_days: {result['n_event_days']}",
             f"      n_event_days_by_season: {flow(result['n_event_days_by_season'])}",
             "      season_in_effect_at_measurement: "
             f"{result['season_in_effect_at_measurement']}",
             f"      median_rel_range_inside: {result['median_rel_range_inside']}",
             f"      median_rel_range_baseline: {result['median_rel_range_baseline']}",
             f"      ratio: {result['ratio']}",
             f"      ratio_noise_band_p10_p90: {flow(result['ratio_noise_band_p10_p90'])}"]
    if "ratio_within_symbol_median" in result:
        lines.append(f"      ratio_within_symbol_median: {result['ratio_within_symbol_median']}")
        lines.append("      ratio_within_symbol_note: \"объединённая медиана смешивает "
                     "металлы и валюту; по-символьные отношения — вот здесь, критерий при "
                     "этом остаётся на предзарегистрированном объединённом числе\"")
    if "ratio_within_symbol_span" in result:
        lines.append("      ratio_within_symbol_span: "
                     f"{flow(result['ratio_within_symbol_span'])}")
    if "ratio_vs_pooled_day_background" in result:
        lines.append("      ratio_vs_pooled_day_background: "
                     f"{result['ratio_vs_pooled_day_background']}")
        lines.append("      ratio_vs_pooled_day_background_note: \"искажённая база: общий "
                     "фон суток смешивает день недели с часом суток; вердикт по ней не "
                     "выносится\"")
    lines.append(f"      criterion: {flow(result['criterion'])}")
    lines.append(f"      criterion_holds: {result['criterion_holds']}")
    lines.append("      # вторая критериальная линия: СПРЕД, а не только диапазон")
    lines.append(f"      spread_metric: {result['spread_metric']}")
    lines.append(f"      spread_baseline: {result['spread_baseline']}")
    lines.append(f"      spread_criterion: {flow(result['spread_criterion'])}")
    lines.append(f"      spread_ratio: {flow(result['spread_ratio'])}")
    lines.append(f"      spread_ratio_driver: {flow(result['spread_ratio_driver'])}")
    lines.append("      median_spread_points_inside: "
                 f"{flow(result['median_spread_points_inside'])}")
    lines.append("      median_spread_points_outside: "
                 f"{flow(result['median_spread_points_outside'])}")
    lines.append(f"      n_spread_bars_inside: {result['n_spread_bars_inside']}")
    lines.append(f"      n_spread_bars_outside: {result['n_spread_bars_outside']}")
    lines.append(f"      spread_coverage_inside: {flow(result['spread_coverage_inside'])}")
    lines.append("      spread_measurable_symbols: "
                 f"{flow(result['spread_measurable_symbols'])}")
    lines.append("      spread_unmeasurable_symbols: "
                 f"{flow(result['spread_unmeasurable_symbols'])}")
    lines.append("      spread_column_audit: " f"{flow(result['spread_column_audit'])}")
    lines.append("      spread_column_audit_note: \"coverage — доля баров файла с записанным "
                 "спредом; distinct/modal_share — вырожденность колонки; hour_median_spread — "
                 "во сколько раз различаются часовые медианы (без этого колонка не различает "
                 "окна, даже если значений много); usable — годится ли ею пользоваться\"")
    lines.append("      spread_criterion_holds: "
                 f"{result['spread_criterion_holds']}")
    if "spread_criterion_note" in result:
        lines.append(f"      spread_criterion_note: {flow(result['spread_criterion_note'])}")
    lines.append("      crossing_cost_fraction_of_hour_range: "
                 f"{flow(result['crossing_cost_fraction_of_hour_range'])}")
    lines.append(f"      crossing_cost_driver: {flow(result['crossing_cost_driver'])}")
    lines.append(f"      verdict: {result['verdict']}")
    lines.append("      verdict_reasons:")
    for reason in result["verdict_reasons"] or [
            "нет оговорок: выборка не тонкая, критерий диапазона выполнен, книга, сезон и "
            "шумовой коридор напечатаны"]:
        lines.append(f"        - {flow(reason)}")
    lines.append(f"      action: {result['action']}")
    lines.append("      action_reasons:")
    for reason in result["action_reasons"]:
        lines.append(f"        - {flow(reason)}")
    lines.append(f"      action_note: {flow(DECIDED_BY_EVIDENCE_NOTE)}")
    if result["caveat"]:
        lines.append(f"      caveat: {flow(result['caveat'])}")
    lines.append(f"      per_symbol: {flow(result['per_symbol'])}")
    lines.append("      executability:")
    lines.append(f"        verdict: {exec_block['verdict']}")
    lines.append(f"        bar_coverage_inside: {exec_block['bar_coverage_inside']}")
    lines.append(f"        n_expected_slots: {exec_block['n_expected_slots']}")
    lines.append(f"        n_present_bars: {exec_block['n_present_bars']}")
    lines.append("        n_occurrence_days_without_any_bars: "
                 f"{exec_block['n_occurrence_days_without_any_bars']}")
    lines.append(f"        coverage_note: {flow(exec_block['coverage_note'])}")
    lines.append("        dead_hour_slots_utc: "
                 f"{flow(exec_block['dead_hour_slots_utc'])}")
    lines.append("        sparse_hour_slots_utc: "
                 f"{flow(exec_block['sparse_hour_slots_utc'])}")
    lines.append(f"        per_slot_fill: {flow(exec_block['per_slot_fill'])}")
    lines.append("        spread:")
    lines.append(f"          unit_note: {flow(spread['unit_note'])}")
    lines.append(f"          baseline: {spread['baseline']}")
    lines.append(f"          coverage_threshold: {spread['coverage_threshold']}")
    lines.append(f"          min_spread_bars: {spread['min_spread_bars']}")
    lines.append(f"          by_symbol: {flow(spread['by_symbol'])}")
    lines.append(f"          measurable_symbols: {flow(spread['measurable_symbols'])}")
    lines.append(f"          unmeasurable_symbols: {flow(spread['unmeasurable_symbols'])}")
    lines.append("          ratio_inside_vs_background: "
                 f"{flow(spread['ratio_inside_vs_background'])}")
    lines.append(f"          ratio_driver: {flow(spread['ratio_driver'])}")
    lines.append("          crossing_cost_fraction_of_hour_range: "
                 f"{flow(spread['crossing_cost_fraction_of_hour_range'])}")
    lines.append(f"          crossing_cost_driver: {flow(spread['crossing_cost_driver'])}")
    if "note" in spread:
        lines.append(f"          note: {flow(spread['note'])}")
    return "\n".join(lines)


def measured_note(result: dict) -> str:
    return (f"перемерено scripts/windows_evidence.py на записи {result['generated_at']}; "
            f"вердикт {result['verdict']} (статистика) при n={result['n_event_days']} "
            f"срабатываний {result['n_event_days_by_season']}; исполнимость "
            f"{result['executability']['verdict']}, спред "
            f"{result['spread_criterion_holds']} (ratio={result['spread_ratio']}, база "
            f"{result['spread_baseline']}) -> action {result['action']}")


def sync_action_lines(lines: list[str], derived: dict[str, str]) -> tuple[int, set[str]]:
    """Вписать выведенное измерением `action:` на уровне окна (4 пробела).

    Ось исполнимости не может быть рукописной там, где есть измерение: тогда «avoid» был бы
    мнением, а не следствием закрытой книги или измеренного спреда. Окна без измерения
    (measured: null) сохраняют авторское значение — для них выводы делаются из календаря
    (`hazard`), и их проверяет валидатор, а не замер. Строка `action:` внутри блока measured
    (6 пробелов) под замену не попадает: она и есть то самое измерение.
    """
    seen: set[str] = set()
    current = None
    replaced = 0
    for index, line in enumerate(lines):
        if line.startswith("  - id: "):
            current = line[len("  - id: "):].strip()
        if (current in derived and line.startswith("    action: ")
                and not line.startswith("      ")):
            value = derived[current]
            if line[len("    action: "):].strip() != value:
                lines[index] = f"    action: {value}"
                replaced += 1
            seen.add(current)
    return replaced, seen


def apply_to_yaml(path: Path, rows: list[tuple[dict, dict | None, str]]) -> int:
    """Вписать свежие measured: и выведенные action: в config/windows.yaml, окно за окном.

    Правится ТЕКСТ артефакта, а не пересериализованный документ: проза, порядок ключей и
    комментарии остаются нетронутыми, а числам неоткуда взяться из памяти о прошлом раунде.
    Границы блока ищутся по строкам (окно = `  - id:`, блок = `    measured:` +
    `    measured_note:`), поэтому соседние ключи и пустые строки между окнами не съезжают.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    wanted = {window["id"]: (result, note) for window, result, note in rows}
    derived_actions = {window["id"]: result["action"]
                       for window, result, _ in rows if result is not None}
    replaced = 0
    index = 0
    current = None
    while index < len(lines):
        line = lines[index]
        if line.startswith("  - id: "):
            current = line[len("  - id: "):].strip()
        if current in wanted and line.startswith("    measured:"):
            result, note = wanted.pop(current)
            end = index + 1
            while end < len(lines):
                nxt = lines[end]
                if (not nxt.strip() or nxt.startswith("  - ")
                        or re.match(r"^[a-z_#]", nxt)):
                    break
                if nxt.startswith("    measured_note:"):
                    end += 1
                    continue
                if re.match(r"^    [a-z_]+: ", nxt):
                    break
                end += 1
            if result is None:
                block = ["    measured: null",
                         "    measured_note: " + json.dumps(
                             note or "окно не измерено на записи", ensure_ascii=False)]
            else:
                block = (render_measured(result).splitlines()
                         + ["    measured_note: " + json.dumps(
                             measured_note(result), ensure_ascii=False)])
            lines[index:end] = block
            replaced += 1
            current = None
            index += len(block)
            continue
        index += 1
    if wanted:
        raise SystemExit(f"в {path.name} не найдены блоки measured у окон: "
                         f"{', '.join(sorted(wanted))}")
    synced, missing = sync_action_lines(lines, derived_actions)
    unhandled = set(derived_actions) - missing
    if unhandled:
        raise SystemExit(f"в {path.name} у окон с измерением нет строки action: уровня окна: "
                         f"{', '.join(sorted(unhandled))} — ось исполнимости обязательна")
    text = "\n".join(lines) + "\n"
    n_measured = len(re.findall(r"^    measured:", text, re.M))
    n_notes = len(re.findall(r"^    measured_note:", text, re.M))
    if n_measured != n_notes:
        raise SystemExit(f"после правки measured: {n_measured}, а measured_note: {n_notes} — "
                         "блоки съехали, файл не записан")
    import yaml

    from windows_schedule import validate
    try:
        validate(yaml.safe_load(text))  # проверяем ДО записи: битый артефакт не должен
    except Exception as error:          # оставаться на диске даже на полшаге миграции
        raise SystemExit(f"windows.yaml после правки НЕВАЛИДЕН, файл не записан: {error}")
    path.write_text(text, encoding="utf-8")
    load_windows(path)  # документ обязан остаться валидным для движка и тестов
    return replaced


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, default=CONFIG)
    parser.add_argument("--bars-dir", type=Path, default=BARS_DIR)
    parser.add_argument("--window", help="измерить только это окно (id)")
    parser.add_argument("--json", action="store_true", help="машинный вывод мерок")
    parser.add_argument("--blocks", action="store_true",
                        help="напечатать блоки measured: для windows.yaml")
    parser.add_argument("--apply", action="store_true",
                        help="вписать свежие measured: в config/windows.yaml")
    args = parser.parse_args(argv)

    if args.apply:
        # Переезд схемы: артефакт RIGHT NOW не имеет новых полей measured:, которые
        # валидатор уже требует, — load_windows() упал бы до того, как есть чем их заполнить.
        # Поэтому черновик читается без валидации, а готовый файл apply_to_yaml проверяет
        # сам и не пишет его, если схема не сошлась.
        import yaml

        document = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    else:
        document = load_windows(args.config)
    registry = document.get("symbols") or {}
    bars = {}
    for path in sorted(args.bars_dir.glob("*.json")):
        meta = registry.get(path.stem) or {}
        bars[path.stem] = load_bars(path, digits=meta.get("digits"))

    rows = collect(document, bars)
    if args.window:
        rows = [row for row in rows if row[0]["id"] == args.window]
        if not rows:
            raise SystemExit(f"окно {args.window!r} не найдено в {args.config.name}")

    if args.apply:
        count = apply_to_yaml(args.config, rows)
        print(f"windows.yaml: обновлено блоков measured: {count} из {len(rows)}")
        return 0
    if args.json:
        print(json.dumps([{"id": window["id"], "measured": result,
                           "not_measured_reason": note or None}
                          for window, result, note in rows],
                         ensure_ascii=False, indent=1, default=str))
        return 0
    if args.blocks:
        for window, result, note in rows:
            print(f"# {window['id']}")
            if result is None:
                print("    measured: null")
                print(f"    measured_note: {json.dumps(note, ensure_ascii=False)}")
            else:
                print(render_measured(result))
                print(f"    measured_note: "
                      f"{json.dumps(measured_note(result), ensure_ascii=False)}")
        return 0

    dark = unrecorded_symbols(document)
    all_symbols = sorted({symbol for cls in document["instrument_classes"].values()
                          for symbol in cls["symbols"]})
    missing = sorted({symbol for symbol in all_symbols
                      if symbol not in bars or symbol in dark})

    print("=" * 128)
    print(f"МЕРКИ ОКОН НА ЗАПИСАННЫХ БАРАХ · {datetime.now(UTC):%Y-%m-%d %H:%M UTC} · "
          f"H1, относительная (high-low)/mid + записанный спред")
    print(f"файлы: {', '.join(bars) or 'нет'}"
          + (f" | без баров: {', '.join(missing)}" if missing else ""))
    def hour_text(spread_audit: dict) -> str:
        variation = spread_audit.get("hour_median_spread")
        return f"{variation}x" if variation else "нет суточной структуры"

    print("аудит колонки spread (сначала она, потом вера): " + "; ".join(
        f"{symbol}: покрытие {audit['coverage']:.0%}, {audit['distinct_values']} значений, "
        f"мод {audit['modal_share']:.0%}, часовые медианы {hour_text(audit)}, "
        f"{'ГОДИТСЯ' if audit['usable'] else 'не годится'}"
        for symbol, audit in sorted((symbol, bars[symbol].spread_audit)
                                    for symbol in all_symbols if symbol in bars)))
    print("=" * 128)
    header = (f"{'окно':<28}{'внутри':>19}{'база':>19}{'отношение':>30}{'критерий':>13}"
              f"{'вердикт':>12}{'спред':>16}{'действие':>11}")
    print(header)
    print("-" * len(header))
    for window, result, note in rows:
        name = window["id"]
        authored = window.get("action", "?")
        if result is None:
            print(f"{name:<28}{'—':>19}{'—':>19}{'—':>30}{'':>13}{'НЕ ИЗМЕРЕНО':>12}"
                  f"{'—':>16}{authored:>11}  ({note})")
            continue
        inside = f"{result['median_rel_range_inside']:.5f} (n={result['n_inside_bars']})"
        base = f"{result['median_rel_range_baseline']:.5f} (n={result['n_baseline_bars']})"
        band = result["ratio_noise_band_p10_p90"]
        seasons = "/".join(f"{key}={value}"
                           for key, value in result["n_event_days_by_season"].items())
        ratio = (f"{result['ratio']:.3f} ({result['n_event_days']} соб. {seasons}"
                 + (f", [{band[0]}..{band[1]}]" if band else "") + ")")
        spread_text = (f"{result['spread_ratio']}x {result['spread_criterion_holds']}"
                       if result["spread_ratio"] is not None
                       else f"— {result['spread_criterion_holds']}")
        print(f"{name:<28}{inside:>19}{base:>19}{ratio:>30}"
              f"{result['criterion']:>13}{result['verdict']:>12}{spread_text:>16}"
              f"{result['action']:>11}"
              + (f"  [{result['caveat']}]" if result["caveat"] else ""))
        if result["action"] != authored:
            print(f"    !! windows.yaml говорит action: {authored}, измерение выводит "
                  f"{result['action']} — артефакт рассинхронизирован, нужен --apply")
        exec_block = result["executability"]
        spread = exec_block["spread"]
        med = spread["by_symbol"]
        printed = ", ".join(
            f"{symbol} {row['median_points_inside']}xфон"
            f"/{row['median_points_background']} (покрытие {row['spread_coverage_inside']:.0%}"
            + (", колонка не годится" if not row["usable_column"] else "") + ")"
            for symbol, row in med.items()
            if row["median_points_inside"] is not None) or "ни по одному символу"
        print(f"    исполнимость: {exec_block['verdict']} "
              f"покрытие={exec_block['bar_coverage_inside']:.3f} "
              f"(слотов {exec_block['n_present_bars']}/{exec_block['n_expected_slots']}, "
              f"дней окна без единого бара "
              f"{exec_block['n_occurrence_days_without_any_bars']}) "
              f"мёртвые={exec_block['dead_hour_slots_utc']} "
              f"редкие={exec_block['sparse_hour_slots_utc']}")
        costs = ", ".join(
            f"{symbol} {row['crossing_cost_fraction_of_hour_range']:.4f}"
            for symbol, row in med.items()
            if row["crossing_cost_fraction_of_hour_range"] is not None) or "не вычислена"
        print(f"    спред (2-я линия, {result['spread_criterion']}, база "
              f"{result['spread_baseline']}): {spread_text} по {result['spread_ratio_driver']}"
              f" | медианы в точках: {printed}"
              f" | издержка прохода, доля часового диапазона: {costs}")
        for reason in result["verdict_reasons"]:
            print(f"      · {reason}")
        for reason in result["action_reasons"]:
            print(f"      {result['action']}: {reason}")
        if result["per_symbol"]:
            print("    по символам: " + ", ".join(
                f"{symbol} {values['ratio']:.2f} (n={values['n_inside_bars']} против "
                f"{values['n_base_bars']})"
                for symbol, values in result["per_symbol"].items()))
            if result.get("ratio_within_symbol_median") is not None:
                print(f"    медиана по-символьных отношений "
                      f"{result['ratio_within_symbol_median']} (разброс "
                      f"{result['ratio_within_symbol_span']}); предзарегистрированный "
                      f"критерий считается по объединённой выборке, порог не двигался")
        if result.get("ratio_vs_pooled_day_background") is not None:
            print("    справочно, искажённая база (общий фон суток): "
                  f"{result['ratio_vs_pooled_day_background']}")
        if result["symbols_without_bars"]:
            print("    без баров (не измерено): " + ", ".join(result["symbols_without_bars"]))
    print("-" * len(header))
    print("ОТНОШЕНИЕ = медиана |h-l|/mid внутри / медиана базы. База для weekly/monthly —")
    print("ТЕ ЖЕ часы в ДРУГИЕ дни (hour-matched): иначе «пятничный всплеск» — это просто")
    print("перекрытие Лондон–Нью-Йорк. ЛЕСТНИЦА ВЕРДИКТОВ: HYPOTHESIS — только при")
    print(f"n_event_days <= {THIN_SAMPLE_EVENT_DAYS}; выше — PASS/FAIL по предзарегистрированному")
    print("критерию этого окна, а шум, сезон и состояние книги печатаются оговорками рядом.")
    print("ДЕЙСТВИЕ (ось исполнимости, независимая от вердикта): enter только когда PASS по")
    print("активности держит и диапазон, и спред, и открытая книга; тихая или закрытая")
    print("книга, растянутый спред и календарный hazard — avoid; без доказательства — neutral.")
    print("Это не обещание доходности, а проверка механизма.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
