"""Prepare the installed MetaTrader 5 terminal for the loopback demo bridge.

Turns the reviewed EA into a demo-enabled build, installs it into the terminal's
``MQL5/Experts`` folder, and enables automated trading plus WebRequest for the bridge
in ``Config/common.ini``. The terminal must be closed: MT5 rewrites its configuration
on exit, so a patch applied while it runs would be overwritten.

Dry run by default; ``--apply`` writes. Rewrites are atomic, backed up once, and keep
the file's UTF-16 encoding, BOM and CRLF line endings. An existing WebRequest URL list
is extended rather than replaced; ``--force-url`` replaces it. Every decision here is
about the host terminal, never about an order: execution still needs the runner, the
bridge, the EA's compile-time demo gate and the operator's approval.
"""
from __future__ import annotations

import argparse
import codecs
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import build_demo_ea  # noqa: E402
from critical_consensus.mt5_agent import i18n  # noqa: E402
from critical_consensus.mt5_agent.i18n import text  # noqa: E402

INSTALL_DIR = Path(r"C:\Program Files\MetaTrader 5")
PROCESS_NAMES = ("terminal64.exe", "metatester64.exe")
SECTION = "Experts"
KEYS = ("Enabled", "WebRequest", "WebRequestUrl")
BRIDGE_URL = "http://127.0.0.1:8000"
LANGUAGE_ENV_VAR = "MT5_AGENT_LANGUAGE"
_BOMS = ((codecs.BOM_UTF16_LE, "utf-16-le"), (codecs.BOM_UTF16_BE, "utf-16-be"))
_TRAILING = re.compile(r"(?:\r\n|\n|\r)$")


class SetupError(RuntimeError):
    """A refusal that leaves every file untouched."""

    exit_code = 2


class TerminalRunning(SetupError):
    """The terminal holds its configuration in memory and would overwrite the patch."""

    exit_code = 3


def read_ini(path: Path, language: str = i18n.DEFAULT_LANGUAGE) -> tuple[bytes, str, str]:
    """Return the raw bytes, the exact UTF-16 flavour and the decoded text."""
    if not path.is_file():
        raise SetupError(text("setup.ini_missing", language, path=path))
    raw = path.read_bytes()
    for bom, encoding in _BOMS:
        if raw.startswith(bom):
            break
    else:
        raise SetupError(text("setup.ini_encoding", language, path=path))
    try:
        decoded = raw[len(bom):].decode(encoding)
    except UnicodeError as exc:
        raise SetupError(text("setup.ini_encoding", language, path=path)) from exc
    if "\x00" in decoded:
        raise SetupError(text("setup.ini_encoding", language, path=path))
    return raw, encoding, decoded


def encode_ini(decoded: str, encoding: str) -> bytes:
    if encoding == "utf-16-be":
        return codecs.BOM_UTF16_BE + decoded.encode("utf-16-be")
    return codecs.BOM_UTF16_LE + decoded.encode("utf-16-le")


def normalize_bridge_url(value: str, language: str = i18n.DEFAULT_LANGUAGE) -> str:
    """Only the loopback bridge this repo ships may enter the terminal allowlist."""
    parsed = urlsplit(str(value).strip())
    host = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError:
        port = None
    if (parsed.scheme.lower() != "http" or host not in ("127.0.0.1", "localhost")
            or port is None or not 1 <= port <= 65535 or parsed.path not in ("", "/")
            or parsed.query or parsed.fragment or parsed.username or parsed.password):
        raise SetupError(text("setup.url_not_loopback", language, url=value))
    return f"http://127.0.0.1:{port}"


def _line_ending(line: str) -> str:
    match = _TRAILING.search(line)
    return match.group(0) if match else ""


def _value(line: str) -> str:
    return line.strip().split("=", 1)[1].strip()


def _url_entries(value: str) -> list[str]:
    return [entry.strip() for entry in value.split(";") if entry.strip()]


def patch_experts_text(ini: str, url: str, *, force_url: bool = False,
                       language: str = i18n.DEFAULT_LANGUAGE) -> tuple[str, dict[str, Any]]:
    """Enable automated trading and WebRequest in the one ``[Experts]`` section."""
    url = normalize_bridge_url(url, language)
    lines = ini.splitlines(keepends=True)
    section = None
    section_hits = 0
    positions: dict[str, int] = {}
    duplicates: set[str] = set()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1].strip()
            if section.casefold() == SECTION.casefold():
                section_hits += 1
            continue
        if section is None or section.casefold() != SECTION.casefold():
            continue
        if "=" not in stripped or stripped.startswith(";") or stripped.startswith("#"):
            continue
        key = stripped.split("=", 1)[0].strip()
        if key not in KEYS:
            continue
        if key in positions:
            duplicates.add(key)
        else:
            positions[key] = index
    if section_hits != 1:
        raise SetupError(text("setup.ini_section", language, count=section_hits))
    for key in KEYS:
        if key in duplicates:
            raise SetupError(text("setup.ini_duplicate_key", language, name=key))
        if key not in positions:
            raise SetupError(text("setup.ini_key_missing", language, name=key))
    current = {key: _value(lines[positions[key]]) for key in KEYS}
    wanted = {"Enabled": "1", "WebRequest": "1"}
    listed = [entry.casefold() for entry in _url_entries(current["WebRequestUrl"])]
    if url.casefold() in listed:
        wanted["WebRequestUrl"] = current["WebRequestUrl"]
    elif not current["WebRequestUrl"] or force_url:
        wanted["WebRequestUrl"] = url
    else:
        wanted["WebRequestUrl"] = f"{current['WebRequestUrl']};{url}"
    changes = {key: {"before": current[key], "after": wanted[key]}
               for key in KEYS if current[key] != wanted[key]}
    patched = list(lines)
    for key, value in wanted.items():
        index = positions[key]
        line = patched[index]
        prefix = line[:len(line) - len(line.lstrip())]
        patched[index] = f"{prefix}{key}={value}{_line_ending(line)}"
    result = "".join(patched)
    if len(result.splitlines()) != len(lines):
        raise SetupError("patching changed the line count; refusing to write")
    owned = set(positions.values())
    if any(before != after and index not in owned
           for index, (before, after) in enumerate(zip(lines, patched))):
        raise SetupError("patching touched a line it does not own; refusing to write")
    return result, {"changes": changes, "values": wanted, "section": SECTION}


def read_ini_value(ini: str, section: str, key: str) -> str | None:
    current = None
    for line in ini.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            current = stripped[1:-1].strip()
            continue
        if current is None or current.casefold() != section.casefold():
            continue
        if "=" in stripped:
            name, _, value = stripped.partition("=")
            if name.strip() == key:
                return value.strip()
    return None


def running_terminals(language: str = i18n.DEFAULT_LANGUAGE,
                      names: tuple[str, ...] = PROCESS_NAMES) -> list[str]:
    """Names of running MT5 processes; an unreadable process list refuses the setup."""
    if os.name != "nt":
        raise SetupError(text("setup.not_windows", language))
    found: list[str] = []
    for name in names:
        try:
            completed = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {name}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=30, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise SetupError(text("setup.process_check_failed", language,
                                  error=type(exc).__name__)) from exc
        if completed.returncode != 0:
            raise SetupError(text("setup.process_check_failed", language,
                                  error=f"tasklist exit {completed.returncode}"))
        if re.search(rf'(?im)^"{re.escape(name)}"', completed.stdout):
            found.append(name)
    return found


def default_data_dir(install_dir: Path = INSTALL_DIR, *,
                     language: str = i18n.DEFAULT_LANGUAGE,
                     environ: dict[str, str] | None = None) -> Path:
    """Portable installs keep their data beside the binaries; otherwise APPDATA wins.

    A terminal started with ``/portable`` has no marker file but writes its live
    ``Config/common.ini`` into the install directory; that marker is just as decisive.
    """
    if ((install_dir / "portable.txt").is_file()
            or (install_dir / "Config" / "common.ini").is_file()):
        return install_dir
    appdata = (os.environ if environ is None else environ).get("APPDATA", "")
    base = Path(appdata) / "MetaQuotes" / "Terminal"
    candidates = (sorted(path for path in base.iterdir()
                         if (path / "MQL5").is_dir()
                         and (path / "Config" / "common.ini").is_file())
                  if base.is_dir() else [])
    if not candidates:
        raise SetupError(text("setup.data_dir_missing", language, path=base))
    if len(candidates) > 1:
        raise SetupError(text("setup.data_dir_ambiguous", language, count=len(candidates)))
    return candidates[0]


def backup_ini(path: Path) -> Path:
    """One timestamped byte-for-byte copy, never overwriting an older backup."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.bak-{stamp}")
    counter = 1
    while backup.exists():
        backup = path.with_name(f"{path.name}.bak-{stamp}-{counter}")
        counter += 1
    shutil.copy2(path, backup)
    return backup


def write_ini(path: Path, data: bytes, language: str = i18n.DEFAULT_LANGUAGE) -> None:
    temp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    try:
        temp.write_bytes(data)
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()
    if path.read_bytes() != data:
        raise SetupError(text("setup.verify_failed", language, path=path))


def build_or_check(*, data_dir: Path, do_build: bool, build_out: Path,
                   metaeditor: Path | None, include_dir: Path,
                   language: str = i18n.DEFAULT_LANGUAGE) -> dict[str, Any]:
    experts = data_dir / "MQL5" / "Experts"
    binary = experts / f"{build_demo_ea.DEMO_NAME}.ex5"
    if not do_build:
        if not binary.is_file():
            raise SetupError(text("setup.ea_missing", language, path=binary))
        return {"compiled": False, "ex5": str(binary), "experts": str(experts)}
    try:
        result = build_demo_ea.build(out_dir=build_out, do_compile=True,
                                     metaeditor=metaeditor, include_dir=include_dir,
                                     install_dir=experts)
    except build_demo_ea.BuildError as exc:
        raise SetupError(str(exc)) from exc
    if not binary.is_file():
        raise SetupError(text("setup.ea_missing", language, path=binary))
    return {"compiled": True, "errors": result["errors"], "warnings": result["warnings"],
            "ex5": str(binary), "installed": result.get("installed", []),
            "experts": str(experts)}


def setup_terminal(*, data_dir: Path, bridge_url: str = BRIDGE_URL, apply: bool = False,
                   do_build: bool = True, build_out: Path | None = None,
                   metaeditor: Path | None = None, include_dir: Path | None = None,
                   force_url: bool = False, language: str = i18n.DEFAULT_LANGUAGE,
                   out: Any = None) -> dict[str, Any]:
    out = sys.stdout if out is None else out
    running = running_terminals(language)
    if running:
        raise TerminalRunning(text("setup.terminal_running", language,
                                   processes=", ".join(running)))
    ini_path = data_dir / "Config" / "common.ini"
    _, encoding, ini_text = read_ini(ini_path, language)
    patched, patch = patch_experts_text(ini_text, bridge_url,
                                        force_url=force_url, language=language)
    experts = data_dir / "MQL5" / "Experts"
    report: dict[str, Any] = {
        "data_dir": str(data_dir), "ini": str(ini_path), "experts": str(experts),
        "bridge_url": normalize_bridge_url(bridge_url, language),
        "ea": {"compiled": False, "ex5": str(experts / f"{build_demo_ea.DEMO_NAME}.ex5"),
               "present": (experts / f"{build_demo_ea.DEMO_NAME}.ex5").is_file()},
        "ini_changed": bool(patch["changes"]), "changes": patch["changes"],
        "applied": False, "backup": None}
    if not apply:
        print(text("setup.dry_run", language), file=out)
        return report
    report["ea"] = build_or_check(
        data_dir=data_dir, do_build=do_build,
        build_out=Path(build_out) if build_out is not None else build_demo_ea.DEFAULT_OUT,
        metaeditor=metaeditor,
        include_dir=include_dir if include_dir is not None else build_demo_ea.DEFAULT_INCLUDE,
        language=language)
    if report["ea"]["compiled"]:
        print(text("setup.ea_built", language, errors=report["ea"]["errors"],
                   warnings=report["ea"]["warnings"], experts=experts), file=out)
    else:
        print(text("setup.ea_present", language, path=report["ea"]["ex5"]), file=out)
    if patch["changes"]:
        backup = backup_ini(ini_path)
        write_ini(ini_path, encode_ini(patched, encoding), language)
        report.update({"backup": str(backup), "ini_applied": True})
        print(text("setup.ini_changed", language, backup=backup), file=out)
    else:
        report["ini_applied"] = False
        print(text("setup.ini_unchanged", language, url=report["bridge_url"]), file=out)
    report["applied"] = True
    login = read_ini_value(patched, "Common", "Login") or "?"
    print(text("setup.ready", language, login=login, ea=build_demo_ea.DEMO_NAME,
               url=report["bridge_url"]), file=out)
    print(text("setup.next", language), file=out)
    return report


def _language(value: str | None) -> str:
    for candidate in (value, os.environ.get(LANGUAGE_ENV_VAR)):
        if candidate:
            return i18n.normalize_language(candidate)
    return i18n.DEFAULT_LANGUAGE


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true",
                        help="write changes (default: dry run)")
    parser.add_argument("--data-dir", type=Path, default=None,
                        help="terminal data folder (default: portable or APPDATA)")
    parser.add_argument("--bridge-url", default=BRIDGE_URL,
                        help="loopback URL the EA may call (default: %(default)s)")
    parser.add_argument("--force-url", action="store_true",
                        help="replace an existing WebRequestUrl list instead of extending it")
    parser.add_argument("--skip-build", action="store_true",
                        help="use the already installed demo .ex5 instead of compiling")
    parser.add_argument("--out", type=Path, default=None,
                        help="directory for the generated demo source")
    parser.add_argument("--metaeditor", type=Path, default=None)
    parser.add_argument("--include-dir", type=Path, default=build_demo_ea.DEFAULT_INCLUDE)
    parser.add_argument("--language", default=None, help="ru/en/vi")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    # Every message this script prints is localized, including the failure line, so a cp1252
    # pipe must not be allowed to raise UnicodeEncodeError on the way to a real exit code.
    i18n.configure_console_output(sys.stdout, sys.stderr)
    args = _build_parser().parse_args(argv)
    try:
        language = _language(args.language)
        data_dir = args.data_dir or default_data_dir(language=language)
        report = setup_terminal(
            data_dir=data_dir, bridge_url=args.bridge_url, apply=args.apply,
            do_build=not args.skip_build, build_out=args.out,
            metaeditor=args.metaeditor, include_dir=args.include_dir,
            force_url=args.force_url, language=language,
            out=sys.stderr if args.json else sys.stdout)
    except (SetupError, build_demo_ea.BuildError, OSError, subprocess.SubprocessError,
            i18n.UnsupportedLanguage) as exc:
        print(f"setup failed: {exc}", file=sys.stderr)
        return getattr(exc, "exit_code", 2)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
