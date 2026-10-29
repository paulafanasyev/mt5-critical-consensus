# MT5 Critical Consensus

Safety-first reference scaffold for a multi-agent decision pipeline around MetaTrader 5. This repository is **not production-ready** and does not establish profitable trading performance.

## Current scope

- Fail-closed consensus helper: approval is withheld unless confidence, independent review, contradiction, compliance, and risk gates pass.
- Order permission helper: allow-listed symbols, bounded volume, required stop-loss/take-profit, explicit user approval, and live trading disabled by default.
- YAML policy for mandatory/optional agents, confidence thresholds, and execution limits.
- Docker Compose template for PostgreSQL 16, Redis 7, and Neo4j 5.
- GitHub Actions test workflow.

## Local setup

Requires Python 3.11+.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
pytest -q
```

For local infrastructure, copy `.env.example` to `.env`, set unique local passwords, then run `docker compose up -d`. Never commit secrets.

## Safety boundary

- `EXECUTION_ENABLED=false` and `LIVE_EXECUTION_ENABLED=false` by default.
- The included permission helper is a policy gate, not a broker connector or cryptographic signature verifier.
- No real-money execution should be enabled without a separate security review, broker-side controls, integration tests, and explicit operator approval.
- Test results do not establish future returns or trading edge.

## Verification status

The local scaffold archive available during repository recovery passed **77 pytest tests** in the local Python environment. PostgreSQL/Redis/Neo4j integration, MetaTrader 5 terminal execution, broker calls, and live-trading safety have **not been verified** in this environment. CI results must be checked in the Actions tab before claiming a GitHub CI pass.
