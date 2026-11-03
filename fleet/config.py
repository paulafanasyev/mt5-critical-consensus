"""Fleet configuration: hundreds of MT5 accounts across many brokers, described in code.

Every account gets its own records; nothing about trading is configured by clicking in a terminal.
Unknown keys are an error, not a silent default: a typo in a risk limit must stop the fleet, not
quietly trade with a different one.
"""
from __future__ import annotations

import dataclasses
import json
import pathlib
from typing import Any

DEFAULT_TERMINAL_SOURCE = pathlib.Path(r"C:\Program Files\MetaTrader 5")
FLEET_ROOT = pathlib.Path(r"C:\mt5-fleet")
DEFAULT_STORE = FLEET_ROOT / "fleet.sqlite"
CONFIG_PATH = pathlib.Path(__file__).resolve().parents[1] / "config" / "accounts.yaml"


class ConfigError(ValueError):
    """The registry cannot be trusted: refuse to trade rather than trade the wrong numbers."""


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
    max_hold_minutes: int = 0
    # real money takes profit earlier and protects a winner harder than the laboratory
    take_atr_real: float = 1.8
    trail_arm_atr_real: float = 0.5
    giveback_fraction_real: float = 0.25
    break_even_atr: float = 0.4


@dataclasses.dataclass(slots=True)
class StrategyProfile:
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
    mode: str = "real"  # demo | real; fail-closed: an unlabelled account is treated as real
    enabled: bool = True
    network: str = "direct"  # direct | vpn
    vpn_profile: str = ""
    symbols: tuple[str, ...] = ("EURUSD", "GBPUSD", "XAUUSD", "USDJPY")
    risk: RiskProfile = dataclasses.field(default_factory=RiskProfile)
    strategy: StrategyProfile = dataclasses.field(default_factory=StrategyProfile)
    magic: int = 7000000
    shard: int = 0
    host: str = ""  # empty: this account may run on any worker; set to pin it to one machine
    password_env: str = ""
    seed_config: str = ""
    currency_scale: float = 1.0  # 0.01 for cent accounts: 100 "cents" are one dollar
    keep_running: bool = False

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
    max_total_risk_pct: float = 2.0
    max_gold_lots: float = 0.30
    max_usd_lots: float = 1.00
    max_open_positions: int = 60
    daily_stop_pct: float = 2.0
    max_worker_processes: int = 1  # one terminal owns the API port per Windows session


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
    return "".join(c if c.isalnum() else "-" for c in str(text)).strip("-").lower()


# --- loading and validation -----------------------------------------------------------------

_ACCOUNT_KEYS = {f.name for f in dataclasses.fields(Account)} - {"risk", "strategy"}
_RISK_KEYS = {f.name for f in dataclasses.fields(RiskProfile)}
_STRATEGY_KEYS = {f.name for f in dataclasses.fields(StrategyProfile)}
_PORTFOLIO_KEYS = {f.name for f in dataclasses.fields(PortfolioLimits)}


def _check_keys(where: str, data: dict[str, Any], allowed: set[str]) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ConfigError(f"{where}: неизвестные ключи {unknown}; "
                          f"допустимые: {sorted(allowed)}")


def _risk(data: dict[str, Any], where: str) -> RiskProfile:
    _check_keys(where, data, _RISK_KEYS)
    return RiskProfile(**data)


def _strategy(data: dict[str, Any], where: str) -> StrategyProfile:
    _check_keys(where, data, _STRATEGY_KEYS)
    cleaned = dict(data)
    for key in ("kinds", "timeframes"):
        if key in cleaned:
            cleaned[key] = tuple(cleaned[key])
    return StrategyProfile(**cleaned)


def account_from_dict(data: dict[str, Any]) -> Account:
    where = f"accounts[{data.get('name', '?')}]"
    _check_keys(where, data, _ACCOUNT_KEYS | {"risk", "strategy"})
    payload = {k: v for k, v in data.items() if k in _ACCOUNT_KEYS}
    if "symbols" in payload:
        symbols = payload["symbols"]
        if isinstance(symbols, str) or not all(isinstance(s, str) for s in symbols):
            raise ConfigError(f"{where}: symbols должен быть списком строк")
        payload["symbols"] = tuple(symbols)
    if "login" in payload and not isinstance(payload["login"], int):
        raise ConfigError(f"{where}: login должен быть числом")
    if "enabled" in payload and not isinstance(payload["enabled"], bool):
        raise ConfigError(f"{where}: enabled должен быть true/false")
    if "terminal_source" in payload:
        payload["terminal_source"] = pathlib.Path(str(payload["terminal_source"]))
    try:
        account = Account(**payload)
    except TypeError as exc:
        raise ConfigError(f"{where}: {exc}") from exc
    if account.mode not in ("demo", "real"):
        raise ConfigError(f"{where}: mode={account.mode!r}, допустимо demo|real")
    if account.network not in ("direct", "vpn"):
        raise ConfigError(f"{where}: network={account.network!r}, допустимо direct|vpn")
    if account.network == "vpn":
        if not account.vpn_profile:
            raise ConfigError(f"{where}: network=vpn требует vpn_profile (путь к .ovpn)")
        # An enabled account must have a real profile; a disabled placeholder may not exist yet.
        if account.enabled and not pathlib.Path(account.vpn_profile).exists():
            raise ConfigError(f"{where}: нет файла профиля {account.vpn_profile}")
    account.risk = _risk(data.get("risk", {}), f"{where}.risk")
    account.strategy = _strategy(data.get("strategy", {}), f"{where}.strategy")
    from .risk import validate_risk_profile

    problems = validate_risk_profile(account)
    if problems:
        raise ConfigError("; ".join(problems))
    return account


def load(path: pathlib.Path | None = None) -> FleetConfig:
    """Read and validate the registry; raise ConfigError instead of trading bad numbers."""
    target = pathlib.Path(path) if path else CONFIG_PATH
    if not target.exists():
        raise ConfigError(f"нет реестра счетов: {target}")
    raw = target.read_text(encoding="utf-8")
    data = _parse_yaml(raw) if target.suffix.lower() in {".yaml", ".yml"} else json.loads(raw)
    _check_keys("реестр", data, {"accounts", "portfolio"})
    items = data.get("accounts", [])
    if not isinstance(items, list):
        raise ConfigError("accounts должен быть списком")
    accounts = [account_from_dict(item) for item in items]
    portfolio_data = data.get("portfolio", {})
    _check_keys("portfolio", portfolio_data, _PORTFOLIO_KEYS)
    portfolio = PortfolioLimits(**portfolio_data)
    config = FleetConfig(accounts=accounts, portfolio=portfolio)
    _validate_uniqueness(config)
    return config


def _validate_uniqueness(config: FleetConfig) -> None:
    names: dict[str, int] = {}
    logins: dict[tuple[int, str], str] = {}
    dirs: dict[pathlib.Path, str] = {}
    for account in config.accounts:
        if account.name in names:
            raise ConfigError(f"дубликат имени счёта: {account.name}")
        names[account.name] = 1
        key = (account.login, account.server)
        if key in logins:
            raise ConfigError(f"дубликат счёта {account.login}/{account.server}: "
                              f"{logins[key]} и {account.name}")
        logins[key] = account.name
        directory = account.data_dir
        if directory in dirs:
            raise ConfigError(f"счета {dirs[directory]} и {account.name} делят папку {directory}")
        dirs[directory] = account.name


def _parse_yaml(text: str) -> dict[str, Any]:
    """Parse the registry with PyYAML. A hand-rolled fallback silently misread valid YAML."""
    try:
        import yaml  # type: ignore
    except ImportError as exc:  # pragma: no cover - the dependency is declared
        raise ConfigError("нужен PyYAML: pip install PyYAML") from exc
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ConfigError("реестр должен быть отображением верхнего уровня")
    return data
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
