"""Runtime validation for versioned JSON contracts in this repository.

Schemas are repository-level assets. Missing/invalid schema files always fail closed.
"""
from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

_SCHEMA_DIR = Path(__file__).resolve().parents[2] / "schemas"


@lru_cache(maxsize=None)
def _validator(name: str) -> Draft202012Validator:
    if Path(name).name != name or not name.endswith(".schema.json"):
        raise ValueError("invalid schema name")
    schema_path = _SCHEMA_DIR / name
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def validate_contract(name: str, instance: Any) -> tuple[bool, tuple[str, ...]]:
    """Return schema validity and stable blocker codes, never leaking values."""
    try:
        errors = sorted(_validator(name).iter_errors(instance), key=lambda error: tuple(map(str, error.absolute_path)))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False, ("CONTRACT_SCHEMA_UNAVAILABLE_OR_INVALID",)
    if not errors:
        return True, ()
    return False, tuple(f"CONTRACT_SCHEMA_INVALID:{name}:{'.'.join(map(str, error.absolute_path)) or '$'}" for error in errors)
