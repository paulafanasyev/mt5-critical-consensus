"""The terminal setup must be a dry run by default, refuse a running terminal, and
rewrite ``common.ini`` without disturbing anything it did not explicitly change.

Every test here works on a temporary data directory; the real terminal is only touched
by an explicit ``--apply`` run, which the tests never perform.
"""
from __future__ import annotations

import codecs
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import setup_demo_terminal as setup  # noqa: E402

INI = (
    "\r\n[Common]\r\nLogin=10013404855\r\nServer=MetaQuotes-Demo\r\n"
    "[Experts]\r\nAllowDllImport=0\r\nEnabled=0\r\nAccount=1\r\nWebRequest=0\r\n"
    "WebRequestUrl=\r\n[Charts]\r\nMaxBars=100000\r\n")


def _data_dir(tmp_path: Path, ini: str = INI) -> Path:
    data = tmp_path / "terminal"
    (data / "Config").mkdir(parents=True)
    (data / "MQL5" / "Experts").mkdir(parents=True)
    (data / "Config" / "common.ini").write_bytes(setup.encode_ini(ini, "utf-16-le"))
    return data


def _text(data: Path) -> str:
    return setup.read_ini(data / "Config" / "common.ini")[2]


def test_patch_enables_experts_and_changes_nothing_else():
    patched, report = setup.patch_experts_text(INI, "http://127.0.0.1:8000")
    assert patched == (INI.replace("Enabled=0", "Enabled=1")
                       .replace("WebRequest=0", "WebRequest=1")
                       .replace("WebRequestUrl=\r\n",
                                "WebRequestUrl=http://127.0.0.1:8000\r\n"))
    assert patched.count("\r\n") == INI.count("\r\n")
    assert set(report["changes"]) == {"Enabled", "WebRequest", "WebRequestUrl"}
    assert report["changes"]["Enabled"] == {"before": "0", "after": "1"}
    # The login and every unrelated byte stay where they were.
    assert "Login=10013404855\r\n" in patched
    assert "[Charts]\r\nMaxBars=100000\r\n" in patched


def test_patch_is_idempotent_after_the_first_apply():
    once, _ = setup.patch_experts_text(INI, "http://127.0.0.1:8000")
    twice, report = setup.patch_experts_text(once, "http://127.0.0.1:8000")
    assert twice == once
    assert report["changes"] == {}


def test_patch_extends_an_existing_webrequest_list_instead_of_replacing_it():
    ini = INI.replace("WebRequestUrl=\r\n", "WebRequestUrl=https://www.mql5.com\r\n")
    patched, _ = setup.patch_experts_text(ini, "http://127.0.0.1:8000")
    assert ("WebRequestUrl=https://www.mql5.com;http://127.0.0.1:8000\r\n" in patched)


def test_force_url_replaces_an_existing_list():
    ini = INI.replace("WebRequestUrl=\r\n", "WebRequestUrl=https://www.mql5.com\r\n")
    patched, _ = setup.patch_experts_text(ini, "http://127.0.0.1:8000", force_url=True)
    assert "WebRequestUrl=http://127.0.0.1:8000\r\n" in patched
    assert "mql5.com" not in patched.split("[Experts]")[1]


def test_a_url_that_is_already_allowlisted_is_not_duplicated():
    ini = INI.replace("WebRequestUrl=\r\n",
                      "WebRequestUrl=https://www.mql5.com;http://127.0.0.1:8000\r\n")
    patched, report = setup.patch_experts_text(ini, "http://127.0.0.1:8000")
    assert patched.count("http://127.0.0.1:8000") == 1
    assert "WebRequestUrl" not in report["changes"]


@pytest.mark.parametrize("url", [
    "https://127.0.0.1:8000", "http://example.com:8000", "http://127.0.0.1",
    "http://127.0.0.1:8000/path", "http://user:pw@127.0.0.1:8000",
    "http://127.0.0.1:0", "http://::1:8000", "",
])
def test_only_an_http_loopback_url_with_a_port_is_accepted(url):
    with pytest.raises(setup.SetupError):
        setup.normalize_bridge_url(url)


def test_localhost_is_normalized_to_the_address_the_bridge_binds():
    assert setup.normalize_bridge_url("http://localhost:8000") == "http://127.0.0.1:8000"
    assert setup.normalize_bridge_url("http://127.0.0.1:8123/") == "http://127.0.0.1:8123"


@pytest.mark.parametrize("mutation", ["no_section", "duplicate_section", "missing_key",
                                     "duplicate_key"])
def test_a_common_ini_whose_experts_shape_is_wrong_is_refused(mutation):
    ini = INI
    if mutation == "no_section":
        ini = INI.replace("[Experts]\r\n", "[Other]\r\n")
    elif mutation == "duplicate_section":
        ini = INI + "[Experts]\r\nEnabled=0\r\nWebRequest=0\r\nWebRequestUrl=\r\n"
    elif mutation == "missing_key":
        ini = INI.replace("WebRequest=0\r\n", "")
    else:
        ini = INI.replace("Enabled=0\r\n", "Enabled=0\r\nEnabled=0\r\n")
    with pytest.raises(setup.SetupError):
        setup.patch_experts_text(ini, "http://127.0.0.1:8000")


def test_read_and_write_preserve_utf16_bom_and_crlf(tmp_path):
    data = _data_dir(tmp_path)
    path = data / "Config" / "common.ini"
    raw, encoding, decoded = setup.read_ini(path)
    assert encoding == "utf-16-le"
    assert raw.startswith(codecs.BOM_UTF16_LE)
    patched, _ = setup.patch_experts_text(decoded, "http://127.0.0.1:8000")
    data_out = setup.encode_ini(patched, encoding)
    assert data_out.startswith(codecs.BOM_UTF16_LE)
    assert data_out.endswith(b"\r\x00\n\x00")
    assert setup.read_ini(path)[2].count("\r\n") == decoded.count("\r\n")


def test_a_utf8_common_ini_is_not_rewritten(tmp_path):
    data = _data_dir(tmp_path)
    (data / "Config" / "common.ini").write_text(INI, encoding="utf-8")
    with pytest.raises(setup.SetupError):
        setup.read_ini(data / "Config" / "common.ini")


def test_dry_run_reports_but_writes_nothing(tmp_path, monkeypatch, capsys):
    data = _data_dir(tmp_path)
    before = (data / "Config" / "common.ini").read_bytes()
    monkeypatch.setattr(setup, "running_terminals", lambda language="ru": [])
    report = setup.setup_terminal(data_dir=data, language="en")
    assert report["applied"] is False
    assert report["ini_changed"] is True
    assert (data / "Config" / "common.ini").read_bytes() == before
    assert "Dry run" in capsys.readouterr().out


def test_a_running_terminal_is_refused_before_anything_is_touched(tmp_path, monkeypatch):
    data = _data_dir(tmp_path)
    before = (data / "Config" / "common.ini").read_bytes()
    monkeypatch.setattr(setup, "running_terminals", lambda language="ru": ["terminal64.exe"])
    with pytest.raises(setup.TerminalRunning) as caught:
        setup.setup_terminal(data_dir=data, apply=True, language="en")
    assert caught.value.exit_code == 3
    assert (data / "Config" / "common.ini").read_bytes() == before


def test_apply_builds_installs_patches_and_keeps_one_byte_exact_backup(tmp_path, monkeypatch):
    data = _data_dir(tmp_path)
    original = (data / "Config" / "common.ini").read_bytes()
    calls = {}

    def fake_build(*, out_dir, do_compile, metaeditor, include_dir, install_dir):
        calls.update({"out_dir": out_dir, "do_compile": do_compile,
                      "install_dir": install_dir})
        install_dir.mkdir(parents=True, exist_ok=True)
        (install_dir / f"{setup.build_demo_ea.DEMO_NAME}.ex5").write_bytes(b"fake")
        return {"source": "x", "compiled": True, "errors": 0, "warnings": 0,
                "ex5": str(install_dir / f"{setup.build_demo_ea.DEMO_NAME}.ex5"),
                "installed": ["x"]}

    monkeypatch.setattr(setup, "running_terminals", lambda language="ru": [])
    monkeypatch.setattr(setup.build_demo_ea, "build", fake_build)
    report = setup.setup_terminal(data_dir=data, apply=True, language="en")
    assert report["applied"] is True and report["ea"]["compiled"] is True
    assert calls["do_compile"] is True
    assert calls["install_dir"] == data / "MQL5" / "Experts"
    text = _text(data)
    assert "Enabled=1" in text and "WebRequest=1" in text
    assert "WebRequestUrl=http://127.0.0.1:8000" in text
    backup = Path(report["backup"])
    assert backup.read_bytes() == original
    assert backup.name.startswith("common.ini.bak-")


def test_a_failed_build_leaves_the_ini_untouched(tmp_path, monkeypatch):
    data = _data_dir(tmp_path)
    original = (data / "Config" / "common.ini").read_bytes()

    def failing_build(**kwargs):
        raise setup.build_demo_ea.BuildError("MetaEditor reported 2 error(s)")

    monkeypatch.setattr(setup, "running_terminals", lambda language="ru": [])
    monkeypatch.setattr(setup.build_demo_ea, "build", failing_build)
    with pytest.raises(setup.SetupError):
        setup.setup_terminal(data_dir=data, apply=True, language="en")
    assert (data / "Config" / "common.ini").read_bytes() == original
    assert not list((data / "Config").glob("common.ini.bak-*"))


def test_skip_build_still_requires_an_installed_binary(tmp_path, monkeypatch):
    data = _data_dir(tmp_path)
    monkeypatch.setattr(setup, "running_terminals", lambda language="ru": [])
    with pytest.raises(setup.SetupError):
        setup.setup_terminal(data_dir=data, apply=True, do_build=False, language="en")
    ex5 = data / "MQL5" / "Experts" / f"{setup.build_demo_ea.DEMO_NAME}.ex5"
    ex5.write_bytes(b"fake")
    report = setup.setup_terminal(data_dir=data, apply=True, do_build=False, language="en")
    assert report["ea"] == {"compiled": False, "ex5": str(ex5),
                            "experts": str(data / "MQL5" / "Experts")}


def test_a_portable_terminal_uses_its_install_directory_for_data(tmp_path):
    install = tmp_path / "mt5"
    (install / "MQL5").mkdir(parents=True)
    (install / "portable.txt").write_text("", encoding="utf-8")
    assert setup.default_data_dir(install) == install


def test_an_install_directory_with_live_config_is_the_data_directory(tmp_path):
    install = tmp_path / "mt5"
    (install / "Config").mkdir(parents=True)
    (install / "Config" / "common.ini").write_text("", encoding="utf-8")
    assert setup.default_data_dir(install) == install


def test_appdata_lookup_requires_exactly_one_terminal(tmp_path):
    appdata = tmp_path / "roaming"
    base = appdata / "MetaQuotes" / "Terminal"
    one = base / "ABCD1234"
    (one / "MQL5").mkdir(parents=True)
    (one / "Config").mkdir()
    (one / "Config" / "common.ini").write_text("", encoding="utf-8")
    assert setup.default_data_dir(tmp_path / "no-portable",
                                  environ={"APPDATA": str(appdata)}) == one
    second = base / "EFGH5678"
    (second / "MQL5").mkdir(parents=True)
    (second / "Config").mkdir()
    (second / "Config" / "common.ini").write_text("", encoding="utf-8")
    with pytest.raises(setup.SetupError):
        setup.default_data_dir(tmp_path / "no-portable", environ={"APPDATA": str(appdata)})


def test_cli_json_exit_codes_and_language(tmp_path, monkeypatch, capsys):
    data = _data_dir(tmp_path)
    monkeypatch.setattr(setup, "running_terminals", lambda language="ru": [])
    assert setup.main(["--data-dir", str(data), "--language", "vi", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ini_changed"] is True and payload["applied"] is False
    monkeypatch.setattr(setup, "running_terminals", lambda language="ru": ["terminal64.exe"])
    assert setup.main(["--data-dir", str(data), "--apply", "--json"]) == 3
    monkeypatch.setattr(setup, "running_terminals", lambda language="ru": [])
    assert setup.main(["--data-dir", str(data), "--bridge-url", "http://evil.example:80"]) == 2
