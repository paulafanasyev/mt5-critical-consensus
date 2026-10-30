# Safe rollback and emergency-stop procedure

## Current branch / deployment state

Hardening work lives on the local branch `harden/10of10` and has not been pushed or merged into `main`. It has not changed any deployed production code path, and no hosting deployment is attached to this repository. Do not merge or push until the full local suite, the MQL5 static checker and a fresh CI run on the pushed head are green and the changes have been independently reviewed. Hosted CI results from earlier commits do not carry over to this branch.

## Stop new commands

The persisted kill switch defaults to active. To activate it from the Windows bridge host, configure MT5_BRIDGE_TOKEN in the process environment and run:

    ./scripts/activate_kill_switch.ps1 -Reason "Stop new MT5 demo commands for investigation"

The script requires a successful persisted confirmation. Verify the current server state via authenticated GET /v1/risk/kill-switch. If the API/DB is unavailable, the EA fails closed and will not poll for commands.

The EA itself checks the persisted kill-switch endpoint on every timer tick and again immediately before `OrderSend`; if the state is unavailable or active it aborts. EnableDemoExecution must remain false during compilation and smoke tests, and AllowLiveExecution must remain false. Remove the EA from the chart / disable Algo Trading while investigating. The endpoint and EA stop new command polling; **they do not cancel an already submitted broker order or close an open position**. A human must inspect MT5 Journal, Orders, Deals and Positions and reconcile any uncertain OrderSend result before taking further action.

## Revert software

- **Before merge:** delete the local `harden/10of10` branch or add a corrective commit to it. No production rollback is required because the work is isolated from `main` and has not been pushed.
- **After a future reviewed merge:** use GitHub's Revert action for the merge commit or create a dedicated revert PR. Do not force-push main or rewrite the audit history.
- **After a future deployment:** keep the kill switch active, roll the deployed API/worker/EA package back to the exact previously verified release, compare its commit hash, and rerun health/contract tests before considering reactivation. No hosting/preview deployment is configured for this repository, so only a recorded local or CI run for the exact commit counts as release evidence.
- **Database changes:** migrations are forward/idempotent and market, risk and resolved-forecast evidence is intentionally immutable. Do not delete audit evidence to roll back code. If a migration requires correction, add a new forward migration reviewed against a backup and test database.

## Reactivation criteria

The switch may only be disabled via the API using both MT5_BRIDGE_TOKEN and a separate MT5_KILL_SWITCH_ADMIN_TOKEN of at least 32 characters. Do not reactivate while any risk snapshot is missing/stale, market/news calendar evidence is unknown, provider roles or calibrations are unproven, there is an unresolved broker result, or the build/test state is red. In this scaffold, unknown news status deliberately keeps risk-controlled permission denied.
