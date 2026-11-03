"""Fleet configuration: hundreds of MT5 accounts across many brokers, described in code.

The terminal is disposable — one dedicated portable copy per account, provisioned and thrown
away by the fleet. Everything that decides what to trade lives here and in the sibling modules,
so adding a broker means adding a record, not clicking through MT5.
"""
from __future__ import annotations

import dataclasses
import json
import pathlib
from typing import Any

DEFAULT_TERMINAL_SOURCE = pathlib.Path(r"C:\Program Files\MetaTrader 5")
FLEET_ROOT = pathlib.Path(r"C:\mt5-fleet")
CONFIG_PATH = pathlib.Path(__file__).resolve().parents[1] / "config" / "accounts.yaml"


@dataclasses.dataclass(slots=True)
class RiskProfile:
    """Per-account money rules. Percentages are of that account's balance."""

    risk_per_trade_pct: float = 0.25
    daily_stop_pct: float = 1.0
    max_open_positions: int = 3
    max_lots: float = 0.10
    min_lots: float = 0.01
    max_spread_points: int = 60
    stop_atr: float = 1.6
    take_atr: float = 2.6
    trail_arm_atr: float = 0.8
    giveback_fraction: float = 0.35
    max_hold_minutes: int = 0  # 0 — без ограничения по времени


@dataclasses.dataclass(slots=True)
class StrategyProfile:
    """Which signals the account may act on."""

    kinds: tuple[str, ...] = ("trend",)
    timeframes: tuple[str, ...] = ("H1", "H4")
    min_atr_pct: float = 0.0004
    max_positions_per_symbol: int = 1
    cooldown_minutes: int = 45


@dataclasses.dataclass(slots=True)
class Account:
    """One trading account at one broker, driven by its own terminal copy."""

    name: str
    login: int
    server: str
    terminal_source: pathlib.Path = DEFAULT_TERMINAL_SOURCE
    mode: str = "demo"  # demo | real
    enabled: bool = True
    network: str = "direct"  # direct | vpn
    vpn_profile: str = ""
    symbols: tuple[str, ...] = ("EURUSD", "GBPUSD", "XAUUSD", "USDJPY")
    risk: RiskProfile = dataclasses.field(default_factory=RiskProfile)
    strategy: StrategyProfile = dataclasses.field(default_factory=StrategyProfile)
    magic: int = 7000000
    shard: int = 0
    password_env: str = ""
    seed_config: str = ""  # data dir whose config/*.dat carry a saved session for this account
    keep_running: bool = False  # leave the terminal up between cycles (still frees the API port)

    @property
    def data_dir(self) -> pathlib.Path:
        return FLEET_ROOT / "terminals" / f"{self.login}-{_slug(self.server)}"

    @property
    def terminal_exe(self) -> pathlib.Path:
        return self.data_dir / "terminal64.exe"

    @property
    def is_real(self) -> bool:
        return self.mode == "real"


@dataclasses.dataclass(slots=True)
class PortfolioLimits:
    """Rules that treat the whole fleet as one book."""

    max_total_risk_pct: float = 2.0
    max_gold_lots: float = 0.30
    max_usd_lots: float = 1.00
    max_open_positions: int = 60
    daily_stop_pct: float = 2.0
    max_worker_processes: int = 4


@dataclasses.dataclass(slots=True)
class FleetConfig:
    accounts: list[Account] = dataclasses.field(default_factory=list)
    portfolio: PortfolioLimits = dataclasses.field(default_factory=PortfolioLimits)

    def enabled(self) -> list[Account]:
        return [a for a in self.accounts if a.enabled]

    def by_name(self, name: str) -> Account | None:
        return next((a for a in self.accounts if a.name == name), None)

    def by_shard(self, shard: int) -> list[Account]:
        return [a for a in self.enabled() if a.shard == shard]

    def shards(self) -> list[int]:
        return sorted({a.shard for a in self.enabled()})


def _slug(text: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in text).strip("-").lower()


# --- loading --------------------------------------------------------------------------------

def _risk(data: dict[str, Any]) -> RiskProfile:
    allowed = {f.name for f in dataclasses.fields(RiskProfile)}
    return RiskProfile(**{k: v for k, v in data.items() if k in allowed})


def _strategy(data: dict[str, Any]) -> StrategyProfile:
    allowed = {f.name for f in dataclasses.fields(StrategyProfile)}
    cleaned = {k: v for k, v in data.items() if k in allowed}
    for key in ("kinds", "timeframes"):
        if key in cleaned:
            cleaned[key] = tuple(cleaned[key])
    return StrategyProfile(**cleaned)


def account_from_dict(data: dict[str, Any]) -> Account:
    known = {f.name for f in dataclasses.fields(Account)}
    payload = {k: v for k, v in data.items() if k in known and k not in {"risk", "strategy"}}
    if "symbols" in payload:
        payload["symbols"] = tuple(payload["symbols"])
    account = Account(**payload)
    account.risk = _risk(data.get("risk", {}))
    account.strategy = _strategy(data.get("strategy", {}))
    return account


def load(path: pathlib.Path | None = None) -> FleetConfig:
    """Read the YAML registry; JSON is accepted too so the file stays dependency-free."""
    target = pathlib.Path(path) if path else CONFIG_PATH
    if not target.exists():
        return FleetConfig()
    raw = target.read_text(encoding="utf-8")
    if target.suffix.lower() in {".yaml", ".yml"}:
        data = _parse_yaml(raw)
    else:
        data = json.loads(raw)
    accounts = [account_from_dict(item) for item in data.get("accounts", [])]
    portfolio_data = data.get("portfolio", {})
    allowed = {f.name for f in dataclasses.fields(PortfolioLimits)}
    portfolio = PortfolioLimits(**{k: v for k, v in portfolio_data.items() if k in allowed})
    return FleetConfig(accounts=accounts, portfolio=portfolio)


def _parse_yaml(text: str) -> dict[str, Any]:
    """Parse the small, predictable subset of YAML the registry uses.

    Pulling PyYAML into the fleet is avoidable: the file is a list of flat records with nested
    risk/strategy blocks, so a two-level indentation parser covers it without a dependency.
    """
    try:  # a real parser is better when the environment happens to have one
        import yaml  # type: ignore

        return yaml.safe_load(text) or {}
    except Exception:  # noqa: BLE001 - fall back to the built-in reader
        pass

    root: dict[str, Any] = {}
    current: dict[str, Any] | None = None
    nested: dict[str, Any] | None = None
    section: str | None = None
    for raw_line in text.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip())
        line = raw_line.strip()
        if indent == 0 and line.endswith(":"):
            section = line[:-1]
            root[section] = [] if section == "accounts" else {}
            current = None
            nested = None
            continue
        if line.startswith("- "):
            item: dict[str, Any] = {}
            if section == "accounts":
                root[section].append(item)
                current = item
                nested = None
                line = line[2:].strip()
            else:
                root[section].append(_scalar(line[2:]))
                continue
        if section == "accounts" and current is not None and indent >= 2:
            parts = line.split(":", 1)
            if len(parts) != 2:
                continue
            key, value = parts[0].strip(), parts[1].strip()
            if not value:
                nested = {}
                current[key] = nested
                continue
            target = nested if (nested is not None and indent >= 4) else current
            target[key] = _scalar(value)
        elif section and section != "accounts" and indent >= 2:
            parts = line.split(":", 1)
            if len(parts) == 2:
                root[section][parts[0].strip()] = _scalar(parts[1].strip())
    return root


def _scalar(value: str) -> Any:
    text = value.strip()
    # A trailing "# ..." is an inline comment, not part of the value (docs encourage them).
    if " #" in text and not text.startswith(("'", '"')):
        text = text.split(" #", 1)[0].rstrip()
    if text.startswith("[") and text.endswith("]"):
        return [_scalar(part) for part in text[1:-1].split(",") if part.strip()]
    if (text.startswith('"') and text.endswith('"')) or (text.startswith("'") and text.endswith("'")):
        return text[1:-1]
    if text.lower() in {"true", "false"}:
        return text.lower() == "true"
    for caster in (int, float):
        try:
            return caster(text)
        except ValueError:
            continue
    return text
