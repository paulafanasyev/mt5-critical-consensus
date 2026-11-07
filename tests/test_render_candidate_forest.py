"""Tests for the development-run forest plot: the chart copies the evidence, it never recalculates."""
from __future__ import annotations

import functools
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest


def _root() -> Path:
    return Path(__file__).resolve().parents[1]


@functools.lru_cache(maxsize=1)
def _module():
    path = _root() / "scripts" / "render_candidate_forest.py"
    spec = importlib.util.spec_from_file_location("render_candidate_forest", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["render_candidate_forest"] = module
    spec.loader.exec_module(module)
    return module


forest = _module()

EVIDENCE = _root() / "docs" / "evidence" / "candidates_dev_2026_11.json"
CHART = _root() / "docs" / "charts" / "candidates_dev_forest.svg"


@pytest.fixture(scope="module")
def payload() -> dict:
    return json.loads(EVIDENCE.read_text(encoding="utf-8"))


def test_rows_mirror_the_evidence_file(payload):
    rows = {row["name"]: row for row in forest.candidate_rows(payload)}
    assert set(rows) == set(payload["candidates"])
    for name, record in payload["candidates"].items():
        row = rows[name]
        assert row["mean"] == record["mean"]
        assert row["low"] == record["low"]
        assert row["high"] == record["high"]
        assert row["n"] == record["n"]
        assert row["clusters"] == record["clusters"]
        assert row["double_cost"] == record["decision"]["double_cost_mean"]
        assert row["detectable_r"] == record["decision"]["detectable_r"]
        assert row["status"] == record["decision"]["status"]


def test_chart_names_every_candidate_and_prints_its_interval(payload):
    svg = forest.render_svg(payload)
    for record in payload["candidates"].values():
        assert record["name"] in svg
        assert f"{record['mean']:+.4f} R" in svg
        assert f"{record['decision']['double_cost_mean']:+.4f} R" in svg
        assert f"{record['n']} trades / {record['clusters']} clusters" in svg


def test_chart_prints_the_run_provenance(payload):
    svg = forest.render_svg(payload)
    start, end = payload["windows"][payload["window"]]
    assert f"{payload['window']} window {start} .. {end}" in svg
    assert f"{payload['bootstrap_rounds']} bootstrap rounds" in svg
    assert str(payload["seed"]) in svg
    assert payload["cache_manifest_sha256"][:12] in svg


def test_zero_line_is_drawn(payload):
    svg = forest.render_svg(payload)
    assert 'no edge' in svg
    assert '>0.00<' in svg
    assert '-0.00' not in svg


def test_render_is_byte_stable(payload):
    assert forest.render_svg(payload) == forest.render_svg(payload)


def test_committed_chart_equals_a_fresh_render(payload):
    assert CHART.read_text(encoding="utf-8") == forest.render_svg(payload)


def test_hostile_labels_are_escaped():
    fake = {
        "window": "dev",
        "windows": {"dev": ["2004-01-01", "2020-01-01"]},
        "confidence": 0.98,
        "bootstrap_rounds": 10,
        "seed": 1,
        "cache_manifest_sha256": "0123456789abcdef",
        "candidates": {
            "x": {
                "name": '<script>alert("1")</script>',
                "n": 5,
                "clusters": 4,
                "mean": 0.1,
                "low": -0.2,
                "high": 0.4,
                "decision": {"status": "fail", "detectable_r": 0.3, "double_cost_mean": -0.05},
                "cost_sweep": {},
            }
        },
    }
    svg = forest.render_svg(fake)
    assert "<script>alert" not in svg
    assert "&lt;script&gt;" in svg
    assert "&quot;1&quot;" in svg
    assert re.search(r'<text[^>]*>&lt;script&gt;', svg)


def test_empty_candidate_list_is_refused():
    with pytest.raises(ValueError, match="no candidates"):
        forest.render_svg({"candidates": {}})
