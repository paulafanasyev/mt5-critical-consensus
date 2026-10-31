"""Build the demo-enabled EA from the reviewed source with one verified line change.

The repository source keeps ``ENABLE_DEMO_EXECUTION = false``; the static checker pins
that default. Turning execution on for a controlled demo test is a deliberate build
step, not a chart input, so this script flips exactly that line in a generated copy and
refuses if the source no longer matches the reviewed shape. Compilation is optional and
uses the installed MetaEditor; the log's error count decides success because
MetaEditor's process exit code is not reliable.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "mql5" / "CriticalConsensusEA.mq5"
DEFAULT_OUT = ROOT / "agent_data" / "demo_ea"
DEFAULT_METAEDITOR = Path(r"C:\Program Files\MetaTrader 5\MetaEditor64.exe")
DEFAULT_INCLUDE = Path(r"C:\Program Files\MetaTrader 5\MQL5")
DEMO_NAME = "CriticalConsensusEA_Demo"
GATE_FALSE = "const bool ENABLE_DEMO_EXECUTION = false;"
GATE_TRUE = "const bool ENABLE_DEMO_EXECUTION = true;"
LIVE_FLAG = "input bool AllowLiveExecution = false;"


class BuildError(RuntimeError):
    pass


def demo_source(base: str) -> tuple[str, int]:
    """Return the generated demo source and the 1-based line of the one changed gate."""
    if base.count(GATE_FALSE) != 1:
        raise BuildError(f"expected exactly one {GATE_FALSE!r} line in the source")
    if GATE_TRUE in base:
        raise BuildError("the source already enables demo execution; refusing to build")
    if base.count(LIVE_FLAG) != 1:
        raise BuildError(f"expected exactly one {LIVE_FLAG!r} line in the source")
    if base.count("AllowLiveExecution = true") != 0:
        raise BuildError("AllowLiveExecution must never default to true")
    lines = base.splitlines(keepends=True)
    changed = [index for index, line in enumerate(lines) if line.strip() == GATE_FALSE]
    if len(changed) != 1:
        raise BuildError("the demo gate is not on a line of its own")
    generated = base.replace(GATE_FALSE, GATE_TRUE, 1)
    verify_only_gate_changed(base, generated)
    return generated, changed[0] + 1


def verify_only_gate_changed(base: str, generated: str) -> None:
    """The build must differ by exactly the demo gate line and nothing else."""
    original = base.splitlines()
    built = generated.splitlines()
    if len(original) != len(built):
        raise BuildError("the demo build changed the number of lines")
    differences = [(number, before, after)
                   for number, (before, after) in enumerate(zip(original, built), start=1)
                   if before != after]
    if len(differences) != 1:
        raise BuildError(f"the demo build changed {len(differences)} lines, expected 1")
    _, before, after = differences[0]
    if before.strip() != GATE_FALSE or after.strip() != GATE_TRUE:
        raise BuildError("the only changed line is not the demo gate")


def parse_compile_log(raw: bytes) -> tuple[int, int, str]:
    """MetaEditor writes a UTF-16 log; the trailing Result line carries the error count."""
    for encoding in ("utf-16", "utf-8-sig", "utf-8"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeError:
            continue
    else:
        raise BuildError("MetaEditor log could not be decoded")
    match = re.search(r"Result:\s*(\d+)\s+errors?,\s*(\d+)\s+warnings?", text)
    if match is None:
        raise BuildError("MetaEditor log has no Result line; treat the build as failed")
    return int(match.group(1)), int(match.group(2)), text


def _find_metaeditor(explicit: Path | None) -> Path:
    candidate = explicit or DEFAULT_METAEDITOR
    if candidate.is_file():
        return candidate
    found = shutil.which("MetaEditor64.exe") or shutil.which("metaeditor64.exe")
    if found:
        return Path(found)
    raise BuildError(f"MetaEditor not found at {candidate}; pass --metaeditor")


def compile_source(source: Path, *, metaeditor: Path, include_dir: Path,
                   timeout: int = 300) -> dict:
    log_path = source.with_suffix(".log")
    completed = subprocess.run(
        [str(metaeditor), f"/compile:{source}", f"/log:{log_path}", f"/inc:{include_dir}"],
        capture_output=True, text=True, timeout=timeout, check=False)
    # MetaEditor may not have written the log yet when the process exits; poll briefly.
    errors = warnings = None
    text = ""
    for _ in range(50):
        if log_path.is_file() and log_path.stat().st_size > 0:
            try:
                errors, warnings, text = parse_compile_log(log_path.read_bytes())
            except BuildError:
                errors = None
            if errors is not None:
                break
        import time

        time.sleep(0.1)
    if errors is None:
        raise BuildError(f"no usable MetaEditor result log (exit {completed.returncode})")
    binary = source.with_suffix(".ex5")
    if errors or not binary.is_file():
        tail = "\n".join(line for line in text.splitlines()[-12:])
        raise BuildError(f"MetaEditor reported {errors} error(s); log tail:\n{tail}")
    return {"errors": errors, "warnings": warnings, "ex5": str(binary),
            "log": str(log_path)}


def build(*, out_dir: Path, do_compile: bool = False, metaeditor: Path | None = None,
          include_dir: Path = DEFAULT_INCLUDE, install_dir: Path | None = None) -> dict:
    base = SOURCE.read_text(encoding="utf-8")
    generated, gate_line = demo_source(base)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{DEMO_NAME}.mq5"
    target.write_text(generated, encoding="utf-8", newline="")
    result: dict = {"source": str(target), "gate_line": gate_line,
                    "gate_enabled": True, "compiled": False}
    if do_compile:
        compiled = compile_source(target, metaeditor=_find_metaeditor(metaeditor),
                                  include_dir=include_dir)
        result.update(compiled)
        result["compiled"] = True
        ex5 = Path(compiled["ex5"])
    if install_dir is not None:
        if not do_compile:
            raise BuildError("--install-dir requires --compile so an .ex5 exists")
        install_dir.mkdir(parents=True, exist_ok=True)
        for path in (target, ex5):
            destination = install_dir / path.name
            shutil.copy2(path, destination)
            result.setdefault("installed", []).append(str(destination))
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT,
                        help="directory for the generated demo source")
    parser.add_argument("--compile", action="store_true", help="compile with MetaEditor")
    parser.add_argument("--metaeditor", type=Path, default=None)
    parser.add_argument("--include-dir", type=Path, default=DEFAULT_INCLUDE)
    parser.add_argument("--install-dir", type=Path, default=None,
                        help="copy the .mq5 and .ex5 into this Experts folder")
    args = parser.parse_args(argv)
    try:
        result = build(out_dir=args.out, do_compile=args.compile,
                       metaeditor=args.metaeditor, include_dir=args.include_dir,
                       install_dir=args.install_dir)
    except (BuildError, OSError, subprocess.SubprocessError) as exc:
        print(f"demo EA build failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
