# Vercel Preview Deployment (Policy API Only)

## Scope

This config exposes the existing FastAPI policy API as a Vercel Python Function. It is a Preview/development deployment scaffold only. The current application does not ingest market prices, host a dashboard, connect to an MT5 terminal, or submit orders. Live execution remains disabled.

Vercel Functions have bounded duration and ephemeral instances. Use Vercel for the web/API control plane, not for a persistent MT5 terminal, an always-on broker bridge, or a durable worker. MT5 stays on a separate Windows demo host in a later phase.

## Connect the repository

1. In Vercel, import `paulafanasyev/mt5-critical-consensus` from GitHub.
2. Set the project root to the repository root. Keep the Python framework detection enabled.
3. The FastAPI framework app is explicitly resolved from `src.critical_consensus.api:app` in `pyproject.toml`. The `functions` rule in `vercel.json` targets that resolved entrypoint, sets a 30-second maximum duration, and includes the policy YAML files required at runtime.
4. For the first deploy, use a Preview environment only. Enable Vercel Deployment Protection for Preview URLs if available on the account.
5. Add `MT5_BRIDGE_TOKEN` as an encrypted server-side environment variable in Vercel. Generate a random value at least 32 bytes long. Never commit it, put it in a `NEXT_PUBLIC_*` variable, or expose it to browser JavaScript. Protected API routes require at least 24 characters.
6. Do not add broker credentials. Do not configure a live account. The current authorization and order-submission endpoints are deliberately disabled.
7. Deploy a Preview and verify `/` and `/health` report `execution_enabled: false` and `live_enabled: false`. Verify protected policy routes reject missing/invalid `X-Bridge-Key`. Send a fresh normalized snapshot to `/v1/market/snapshot/validate` and confirm it returns `trade_allowed: false`; submit a stale or crossed quote and confirm it is rejected.

## Important caveats

- The public `/health` endpoint is a liveness check and contains no secret. Protect the Preview deployment at the Vercel project level; application key checks remain necessary for policy routes.
- A successful health response does not prove policy configuration is available. The function explicitly bundles `config/**/*.yaml`; test an authenticated policy route and stop if it responds with configuration unavailable. Do not weaken authentication or hard-code policy values.
- Do not put `MT5_BRIDGE_TOKEN` in a client-side variable. A future dashboard should call the API from server-side code or use a separate scoped user-authentication design.
- No market-data provider, quote display, or paper ledger is included. The protected snapshot endpoint is stateless validation only. The `MT5ReadOnlyAdapter` module is intended for a separate Windows host and is not run inside Vercel; a successful snapshot validation is not proof of feed authenticity or broker connectivity.
- Preview deploy success is not evidence of trading correctness or profitability. A Vercel `Ready` status is not an HTTP smoke test.

## References

- [Vercel Python runtime and entrypoints](https://vercel.com/docs/functions/runtimes/python)
- [Deploy FastAPI on Vercel](https://vercel.com/docs/frameworks/backend/fastapi)
- [Vercel function duration](https://vercel.com/docs/functions/configuring-functions/duration)
- [Vercel project configuration](https://vercel.com/docs/project-configuration/vercel-json)
