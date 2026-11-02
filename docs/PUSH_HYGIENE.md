# Push hygiene: what may leave this machine

Measured on 2026-11-02, branch `feat/demo-live-run-2026-11-01`, 46 commits ahead of the same
branch on `origin`.

## The rule

Never push a commit range that contains `f98fd39`. It added `docs/evidence/mt5_terminal.png`, a
screenshot of the Navigator that shows the account holder's personal name and two logins side by
side — the sanctioned demo `114929502` and another account. Deleting the file at the tip does not
unpublish the blob: pushing any range that includes that commit uploads it.

Removing the file from history would mean rewriting a branch that a second live agent is committing
to, so it is not being rewritten. Instead, publish a **snapshot**: one orphan commit built from the
tip tree with screenshots excluded, on its own branch.

    GIT_INDEX_FILE=agent_data/snapshot.index git read-tree 'HEAD^{tree}'
    GIT_INDEX_FILE=agent_data/snapshot.index git update-index --force-remove <each screenshot path>
    GIT_INDEX_FILE=agent_data/snapshot.index git write-tree

Only objects reachable from the pushed ref are uploaded, so an unreferenced blob never leaves.

## What is already public

The login `10013404855` is not new exposure — it already sits in the pushed branch tip at
`CHANGELOG.md`, `README.md`, `README.vi.md`, `docs/MT5_AGENT.{md,en.md,vi.md}`,
`tests/test_mt5_demo_runner.py` and `tests/test_setup_demo_terminal.py` (8 occurrences over 8
files, verified with `git grep -c` against `origin/feat/demo-live-run-2026-11-01`). Those files are
left alone; removing the screenshot only stops the *additional* disclosure of the account holder's
name next to a second, undocumented login.

## What is never pushed regardless of shape

- Anything under `agent_data/` — journals, caches, scratch scripts, and now
  `agent_data/offline_evidence/`, where the terminal screenshot is kept locally.
- `.env`, tokens, MT5 credentials. No credential or token has ever been passed on a command line;
  the bridge token travels only in `MT5_BRIDGE_TOKEN`, the admin token only in
  `MT5_KILL_SWITCH_ADMIN_TOKEN`.
- `docs/evidence/mt5_*.png` — git-ignored so the eight further terminal screenshots in that
  directory cannot be swept up by a broad `git add`.
