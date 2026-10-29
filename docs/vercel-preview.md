# Vercel Preview Deployment (Policy API Only)

## Scope

This config exposes the existing FastAPI policy API as a Vercel Python Function. It is a Preview/development deployment scaffold only. The current application does not ingest market prices, host a dashboard, connect to an MT5 terminal, or submit orders. Live execution remains disabled.

Vercel Functions have bounded duration and ephemeral instances. Use Vercel for the web/API control plane, not for a persistent MT5 terminal, an always-on broker bridge, or a durable worker. MT5 stays on a separate Windows demo host in a later phase.

## Connect the repository

1. In Vercel, import `paulafanasyev/mt5-critical-consensus` from GitHub.
2. Set the project root to the repository root. Keep the Python framework detection enabled.
3. Use the repository's `vercel.json`; the `api/index.py` entrypoint serves the existing FastAPI `app` with a 30-second maximum duration.
4. For the first deploy, use a Preview environment only. Enable Vercel Deployment Protection for Preview URLs if available on the account.
5. Add `MT5_BRIDGE_TOKEN` as an encrypted server-side environment variable in Vercel. Generate a random value at least 32 bytes long. Never commit it, put it in a `NEXT_PUBLIC_*` variable, or expose it to browser JavaScript. Protected API routes require at least 24 characters.
6. Do not add broker credentials. Do not configure a live account. The current authorization and order-submission endpoints are deliberately disabled.
7. Deploy a preview and verify `/health` reports `execution_enabled: false` and `live_enabled: false`. Verify protected policy routes reject requests with missing or invalid `X-Bridge-Key`.

## Important caveats

- The public `/health` endpoint is a liveness check and contains no secret. Protect the Preview deployment at the Vercel project level; application key checks remain necessary for policy routes.
- The API's policy YAML files must be present in the deployment bundle. If the deployed health route works but policy routes report configuration unavailable, stop and fix file tracing before proceeding; do not weaken authentication or hard-code policy values.
- Do not put `MT5_BRIDGE_TOKEN` in a client-side variable. A future dashboard should call the API from server-side code or use a separate scoped user-authentication design.
- No market-data provider, quote display, paper ledger, durable database adapter, or MT5 integration is created by this scaffold.
- Preview deploy success is not evidence of trading correctness or profitability.

## References

- [Vercel Python runtime](https://vercel.com/docs/functions/runtimes/python)
- [Deploy FastAPI on Vercel](https://vercel.com/docs/frameworks/backend/fastapi)
- [Vercel function limits](https://vercel.com/docs/functions/limitations)
