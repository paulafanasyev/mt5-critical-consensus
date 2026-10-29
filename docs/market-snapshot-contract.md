# Read-only market snapshot contract

`schemas/market_snapshot_v1.schema.json` is the repository-level structural contract for a normalized quote observation. It is intentionally not a complete integrity gate: JSON Schema checks the shape, required fields, primitive types, and timestamp syntax, while the runtime validator applies semantic rules such as crossed quotes, finite values, event/receipt ordering, future skew, and explicit freshness limits.

Consumers must run `critical_consensus.market_data.validate_market_snapshot` after structural validation and treat every blocker as a hard reject. A structurally valid snapshot is not proof that a feed is authentic, licensed, complete, or suitable for trading. This contract does not fetch or persist data, produce signals, connect to MT5, or authorize orders.

The schema currently lives at the repository root and is not declared as installed package data. Consumers that need it outside a source checkout should fetch/version it explicitly until a package-distribution decision is made.


## HTTP validation surface

The protected `POST /v1/market/snapshot/validate` endpoint first checks `market_snapshot_v1.schema.json`, then applies the server-owned `config/market_snapshot_policy.yaml` freshness policy and runtime quote checks. It requires `X-Bridge-Key`, returns `trade_allowed: false` for both accepted and rejected snapshots, and does not persist observations, authenticate the feed source, create a signal, or authorize an order.
