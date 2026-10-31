"""The demo EA build must be one reviewed line change, and it must compile.

The repository source keeps demo execution disabled. Turning it on for a controlled
demo test is a build step: this suite pins that the generated source differs from the
reviewed source by exactly the gate line, that tampered sources are refused, and — when
MetaEditor is installed — that the generated source compiles cleanly.
"""
from __future__ import annotations

import importlib.util
import shutil
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest


def _root() -> Path:
    return Path(__file__).resolve().parents[1]


def _builder():
    path = _root() / "scripts" / "build_demo_ea.py"
    spec = importlib.util.spec_from_file_location("build_demo_ea", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source() -> str:
    return (_root() / "mql5" / "CriticalConsensusEA.mq5").read_text(encoding="utf-8")


def test_generated_demo_source_differs_only_in_the_gate_line():
    builder = _builder()
    base = _source()
    generated, line = builder.demo_source(base)
    assert builder.GATE_FALSE in base
    assert builder.GATE_TRUE not in base
    assert builder.GATE_TRUE in generated
    assert builder.GATE_FALSE not in generated
    before = base.splitlines()
    after = generated.splitlines()
    assert len(before) == len(after)
    changed = [number for number, (old, new) in enumerate(zip(before, after), start=1)
               if old != new]
    assert changed == [line]
    assert before[line - 1].strip() == builder.GATE_FALSE
    assert after[line - 1].strip() == builder.GATE_TRUE
    # The live-execution input must stay false in the demo build as well.
    assert generated.count(builder.LIVE_FLAG) == 1
    assert "AllowLiveExecution = true" not in generated


@pytest.mark.parametrize("mutation", [
    "remove", "duplicate", "already_true", "live_true", "semicolon_removed",
])
def test_demo_build_refuses_a_source_whose_gate_shape_changed(mutation):
    builder = _builder()
    base = _source()
    if mutation == "remove":
        tampered = base.replace(builder.GATE_FALSE, "", 1)
    elif mutation == "duplicate":
        tampered = base + "\n" + builder.GATE_FALSE + "\n"
    elif mutation == "already_true":
        tampered = base.replace(builder.GATE_FALSE, builder.GATE_TRUE, 1)
    elif mutation == "live_true":
        tampered = base.replace(builder.LIVE_FLAG,
                                "input bool AllowLiveExecution = true;", 1)
    else:
        tampered = base.replace(builder.GATE_FALSE, builder.GATE_FALSE[:-1], 1)
    with pytest.raises(builder.BuildError):
        builder.demo_source(tampered)


def test_compile_log_parser_reads_the_result_line_and_fails_closed():
    builder = _builder()
    ok = "C:/x.mq5 : information: compiling\nResult: 0 errors, 1 warnings, 1302 ms elapsed\n"
    assert builder.parse_compile_log(ok.encode("utf-16"))[:2] == (0, 1)
    bad = "Result: 2 errors, 0 warnings, 5 ms elapsed\n"
    assert builder.parse_compile_log(bad.encode("utf-16"))[:2] == (2, 0)
    with pytest.raises(builder.BuildError):
        builder.parse_compile_log(b"no result here".decode("utf-8").encode("utf-16"))


def test_build_writes_the_generated_source_without_compiling(tmp_path):
    builder = _builder()
    result = builder.build(out_dir=tmp_path / "demo")
    source = Path(result["source"])
    assert source.is_file()
    assert source.read_text(encoding="utf-8").count(builder.GATE_TRUE) == 1
    assert result["compiled"] is False
    assert result["gate_enabled"] is True


METAEDITOR = Path(r"C:\Program Files\MetaTrader 5\MetaEditor64.exe")
INCLUDE = Path(r"C:\Program Files\MetaTrader 5\MQL5")


@pytest.mark.skipif(not METAEDITOR.is_file(), reason="MetaEditor is not installed")
def test_generated_demo_source_compiles_in_metaeditor(tmp_path):
    builder = _builder()
    if shutil.which("MetaEditor64.exe") is None and not METAEDITOR.is_file():
        pytest.skip("MetaEditor is not installed")
    result = builder.build(out_dir=tmp_path / "demo", do_compile=True,
                           metaeditor=METAEDITOR, include_dir=INCLUDE)
    assert result["compiled"] is True
    assert result["errors"] == 0
    assert Path(result["ex5"]).is_file()
    assert Path(result["ex5"]).stat().st_size > 0

def test_compile_source_uses_documented_metaeditor_include_switch(tmp_path, monkeypatch):
    builder = _builder()
    source = tmp_path / "CriticalConsensusEA_Demo.mq5"
    source.write_text("// test source", encoding="utf-8")
    include_dir = tmp_path / "MQL5"
    include_dir.mkdir()
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        source.with_suffix(".log").write_text(
            "Result: 0 errors, 0 warnings, 1 ms elapsed\n", encoding="utf-16"
        )
        source.with_suffix(".ex5").write_bytes(b"compiled-ex5")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(builder.subprocess, "run", fake_run)
    result = builder.compile_source(
        source, metaeditor=tmp_path / "metaeditor64.exe", include_dir=include_dir
    )

    assert result["errors"] == 0
    assert f"/include:{include_dir}" in captured["args"]
    assert not any(str(arg).startswith("/inc:") for arg in captured["args"])

