"""Cached loader for football-data.co.uk historical results and 1X2 odds.

Data source: https://www.football-data.co.uk/mmz4281/{season}/{division}.csv
The site offers free historical CSV downloads for personal/research use. The
sandbox caches files locally and never redistributes them. Columns can change
between seasons, so parsing fails closed on missing required headers.
"""
from __future__ import annotations

import csv
import hashlib
import io
import os
import re
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

BASE_URL = "https://www.football-data.co.uk/mmz4281"
USER_AGENT = "critical-consensus-betting-sandbox/0.4 (personal research; contact: local)"
MAX_BYTES = 20 * 1024 * 1024
REQUIRED_COLUMNS = ("Div", "Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG", "FTR")
OPENING_ODDS_SETS = (
    ("B365H", "B365D", "B365A"),
    ("PSH", "PSD", "PSA"),
    ("AvgH", "AvgD", "AvgA"),
    ("BbAvH", "BbAvD", "BbAvA"),
)
CLOSING_ODDS_SETS = (
    ("B365CH", "B365CD", "B365CA"),
    ("PSCH", "PSCD", "PSCA"),
    ("AvgCH", "AvgCD", "AvgCA"),
)
_SEASON = re.compile(r"^\d{4}$")
_DIVISION = re.compile(r"^[A-Z0-9]{2,4}$")


@dataclass(frozen=True)
class Match:
    match_id: str
    date: date
    division: str
    home: str
    away: str
    home_goals: int
    away_goals: int
    result: str
    odds_home: Decimal
    odds_draw: Decimal
    odds_away: Decimal
    closing_home: Decimal | None = None
    closing_draw: Decimal | None = None
    closing_away: Decimal | None = None

    @property
    def odds(self) -> tuple[Decimal, Decimal, Decimal]:
        return self.odds_home, self.odds_draw, self.odds_away

    @property
    def closing_odds(self) -> tuple[Decimal, Decimal, Decimal] | None:
        if None in (self.closing_home, self.closing_draw, self.closing_away):
            return None
        return self.closing_home, self.closing_draw, self.closing_away  # type: ignore[return-value]


@dataclass(frozen=True)
class Dataset:
    matches: list[Match]
    skipped_rows: int
    division: str
    season: str
    sha256: str
    source: str
    from_cache: bool


class DatasetError(ValueError):
    """Raised when the downloaded or cached file cannot be used safely."""


def cache_path(cache_dir: Path, season: str, division: str) -> Path:
    _validate_identifiers(season, division)
    return Path(cache_dir) / f"{season}_{division}.csv"


def _validate_identifiers(season: str, division: str) -> None:
    if not _SEASON.fullmatch(str(season)):
        raise ValueError("season must be four digits, e.g. 2425")
    if not _DIVISION.fullmatch(str(division)):
        raise ValueError("division code must be 2-4 uppercase letters/digits, e.g. E0")


def load_cached_dataset(cache_dir: Path, season: str, division: str) -> Dataset | None:
    path = cache_path(cache_dir, season, division)
    if not path.is_file():
        return None
    raw = path.read_bytes()
    return parse_football_data(raw, season=str(season), division=str(division),
                               source=str(path), from_cache=True)


def download_dataset(cache_dir: Path, season: str, division: str, *,
                     refresh: bool = False, timeout: float = 30.0) -> Dataset:
    """Download (or reuse) one division-season CSV and parse it."""
    _validate_identifiers(season, division)
    path = cache_path(cache_dir, season, division)
    if path.is_file() and not refresh:
        return load_cached_dataset(cache_dir, season, division)  # type: ignore[return-value]

    url = f"{BASE_URL}/{season}/{division}.csv"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                                   "Accept": "text/csv,*/*;q=0.5"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if getattr(response, "status", 200) != 200:
                raise DatasetError(f"download failed with HTTP {response.status}: {url}")
            raw = response.read(MAX_BYTES + 1)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise DatasetError(f"could not download {url}: {exc}") from exc
    if len(raw) > MAX_BYTES:
        raise DatasetError(f"download exceeds {MAX_BYTES} bytes: {url}")
    if not raw.strip():
        raise DatasetError(f"download is empty: {url}")

    dataset = parse_football_data(raw, season=str(season), division=str(division),
                                  source=url, from_cache=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, raw)
    _atomic_write(path.with_suffix(".sha256"), dataset.sha256.encode("ascii"))
    return dataset


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(dir=path.parent, delete=False, suffix=".tmp")
    try:
        with handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, path)
    except BaseException:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


def parse_football_data(raw: bytes | str, *, season: str, division: str,
                        source: str = "<bytes>", from_cache: bool = False) -> Dataset:
    if isinstance(raw, bytes):
        for encoding in ("utf-8-sig", "cp1252"):
            try:
                text = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            raise DatasetError("CSV is neither valid UTF-8 nor cp1252")
        digest = hashlib.sha256(raw).hexdigest()
    else:
        text = raw
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()

    reader = csv.DictReader(io.StringIO(text))
    fields = tuple(name.strip() for name in (reader.fieldnames or ()))
    missing = [column for column in REQUIRED_COLUMNS if column not in fields]
    if missing:
        raise DatasetError(f"missing required columns: {', '.join(missing)}")

    matches: list[Match] = []
    skipped = 0
    seen: dict[tuple[date, str, str], Match] = {}
    for row in reader:
        if not row or not str(row.get("Date", "")).strip():
            skipped += 1
            continue
        parsed = _parse_row(row, division)
        if parsed is None:
            skipped += 1
            continue
        key = (parsed.date, parsed.home, parsed.away)
        existing = seen.get(key)
        if existing is not None:
            if (existing.home_goals, existing.away_goals) != (parsed.home_goals, parsed.away_goals):
                raise DatasetError(f"conflicting duplicate fixture: {key}")
            skipped += 1
            continue
        seen[key] = parsed
        matches.append(parsed)

    if not matches:
        raise DatasetError("no complete matches with 1X2 odds and results were found")
    matches.sort(key=lambda match: (match.date, match.match_id))
    return Dataset(matches, skipped, str(division), str(season), digest, str(source), from_cache)


def _parse_row(row: dict[str, str], division: str) -> Match | None:
    try:
        match_date = _parse_date(row["Date"])
        home = str(row["HomeTeam"]).strip()
        away = str(row["AwayTeam"]).strip()
        home_goals = int(str(row["FTHG"]).strip())
        away_goals = int(str(row["FTAG"]).strip())
        result = str(row["FTR"]).strip().upper()
    except (KeyError, TypeError, ValueError):
        return None
    if not home or not away or result not in ("H", "D", "A"):
        return None
    opening = _odds_from(row, OPENING_ODDS_SETS)
    if opening is None:
        return None
    closing = _odds_from(row, CLOSING_ODDS_SETS)
    match_id = f"{division}:{match_date.isoformat()}:{home}:{away}"
    return Match(match_id, match_date, division, home, away, home_goals, away_goals,
                 result, opening[0], opening[1], opening[2],
                 closing[0] if closing else None,
                 closing[1] if closing else None,
                 closing[2] if closing else None)


def _odds_from(row: dict[str, str], sets: tuple[tuple[str, str, str], ...]) -> tuple[Decimal, Decimal, Decimal] | None:
    for headers in sets:
        try:
            values = tuple(_decimal(row.get(header)) for header in headers)
        except (InvalidOperation, ValueError):
            continue
        if values and all(value is not None for value in values):
            return values  # type: ignore[return-value]
    return None


def _decimal(raw: object) -> Decimal | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    value = Decimal(text)
    if not value.is_finite() or value <= Decimal("1.0"):
        return None
    return value


def _parse_date(raw: str) -> date:
    parts = [part for part in str(raw).strip().split("/") if part]
    if len(parts) != 3:
        raise ValueError("date must be dd/mm/yyyy or dd/mm/yy")
    day, month, year = (int(part) for part in parts)
    if year < 100:
        year += 2000
    return date(year, month, day)
