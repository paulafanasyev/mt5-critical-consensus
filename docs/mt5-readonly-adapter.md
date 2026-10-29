# Read-only MetaTrader 5 quote adapter

The Python adapter reads only symbol ticks from an already installed MetaTrader 5 Python package and turns them into normalized market snapshots.

- It calls initialize, symbol_info_tick, and shutdown only. It does not call order_send, access trading endpoints, submit commands, or make trading decisions.
- Symbols are allow-listed by the caller. Snapshot freshness and tolerated future skew are bounded and explicitly configured.
- Invalid timestamps, stale ticks, crossed quotes, missing ticks, non-finite prices, non-allow-listed symbols, and terminal read errors fail closed.
- The source_id is a caller-supplied label, not cryptographic proof of the quote origin. A valid read-only snapshot is not proof of feed authenticity, licensing, correct account, or profitability.
- CI uses a fake terminal client. It does not prove that MetaTrader 5 is installed, that a Windows terminal is running, or that a broker connection works.
- For a real read-only smoke test, run the bridge on the separate Windows demo host with a demo terminal and an explicitly approved symbol list. Keep the API bound to localhost/firewall allowlist. Do not configure live credentials. The Vercel API is not an MT5 terminal host.
