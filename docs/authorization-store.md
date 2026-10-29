# PostgreSQL decision registry

This is a storage primitive for server-side authorization work; it is not connected to the public API.

- The registry accepts only a server-side VerifiedAuthReceipt object. The authentication service must create it only after real identity/session verification. For live mode, it must also attest to second-factor and signature verification.
- Registration is bound to an existing permission row in REQUIRE_HUMAN state and to its stored expiry/mode. The same decision ID cannot be inserted twice, and a unique database index permits only one decision row per permission; a new review requires a new permission record.
- Consumption uses one conditional PostgreSQL UPDATE. Competing consumers cannot both consume the same row; the row must still be pending, unexpired, and byte-for-byte bound by the canonical decision payload hash.
- Storage errors must be treated as DENY. The adapter returns trade_allowed=false even when it successfully consumes an APPROVE decision.
- This does not implement identity proof, a TOTP/WebAuthn/signature provider, permission issuance, the authorization endpoint, a broker adapter, or order dispatch. Do not enable the HTTP 423 endpoint based on this store alone.
- Production deployment must apply db/postgres_schema.sql, use a managed durable PostgreSQL service, configure DATABASE_URL server-side, and enforce least-privilege DB credentials. This CI test uses a disposable PostgreSQL service only.
