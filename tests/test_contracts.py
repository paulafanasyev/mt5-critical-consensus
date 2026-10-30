import json
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("schema_path", sorted((ROOT / "schemas").glob("*.json")), ids=lambda p: p.name)
def test_json_schema_is_valid(schema_path):
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)


def test_yaml_configs_parse_to_objects():
    for path in sorted((ROOT / "config").glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(data, dict), path.name


def test_kill_switch_migration_defaults_to_active():
    sql = (ROOT / "db" / "migrations" / "003_risk_controls.sql").read_text(encoding="utf-8")
    # The safety property lives in the schema default, not only in bootstrap code.
    assert "kill_switch_active BOOLEAN NOT NULL DEFAULT TRUE" in sql
    assert "VALUES (TRUE, TRUE, 'system-bootstrap'" in sql


def test_authorization_permission_tombstone_is_enforced_by_the_schema():
    sql = (ROOT / "db" / "migrations" / "006_authorization_permissions.sql").read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS authorization_permissions" in sql
    assert "permission_id TEXT PRIMARY KEY" in sql
    assert "ON CONFLICT" not in sql  # the conflict handling lives in the parameterized query
    repo = (ROOT / "src" / "critical_consensus" / "persistence.py").read_text(encoding="utf-8")
    assert "INSERT INTO authorization_permissions" in repo
    assert "ON CONFLICT (permission_id) DO NOTHING" in repo


def test_agent_verdict_abstain_requires_formal_reason():
    schema = json.loads((ROOT / "schemas" / "agent_verdict_v2.schema.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    sample = {
        "verdict_id": "v-1", "signal_id": "s-1", "agent_id": "TechnicalAnalystAgent", "verdict": "ABSTAIN",
        "confidence": 0.2, "reasoning": "Not enough clean market data", "pros": [], "cons": ["stale data"],
        "evidence_refs": ["md-1"], "ttl_seconds": 60, "input_hash": "sha256:" + "a" * 64,
        "calibration_meta": {"brier_score": 0.1, "sample_size": 50, "method_version": "v1", "calibration_status": "CALIBRATED"},
        "created_at": "2026-10-29T12:00:00+00:00",
    }
    assert list(validator.iter_errors(sample))
    sample["abstain_reason"] = "Input feed has a gap"
    assert not list(validator.iter_errors(sample))


def test_package_version_matches_project_metadata():
    import tomllib
    import critical_consensus

    project = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
    assert critical_consensus.__version__ == project["project"]["version"]
