"""Vercel Python Function entrypoint for the policy-only FastAPI app.

This exposes only the existing policy API. It does not connect to MT5 or dispatch orders.
"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from critical_consensus.api import app  # noqa: E402
