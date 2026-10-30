from pathlib import Path
import subprocess
import sys


def test_mql5_static_safety_script_passes_but_does_not_claim_compile():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, str(root / "scripts" / "check_mql5_safety.py")],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "static EA safety invariants verified" in result.stdout
    assert "NOT PROVEN" in result.stdout
