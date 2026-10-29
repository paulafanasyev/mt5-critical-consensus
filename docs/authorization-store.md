# PostgreSQL decision registry

This is a storage primitive for server-side authorization work; it is not connected to the public API.

- The registry accepts only a server-side VerifiedAuthReceipt object. The authentication service must create it only after real identity/session verification, and the receipt includes the canonical hash of the exact decision payload. A receipt for altered content is rejected and each verifier receipt reference is unique in PostgreSQL. For live mode, it must also attest to second-factor and signature verification. The dataclass itself is not cryptographic proof; only trusted server code may call this API.
- Registration is bound to an existing permission row in REQUIRE_HUMAN state and to its stored expiry/mode. The same decision ID cannot be inserted twice, and a unique database index permits only one decision row per permission; a new review requires a new permission record.
- Consumption locks the authoritative permission row, validates pending status and expiry, then atomically consumes the decision and moves the permission to `DECISION_CONSUMED`. Competing consumers cannot both consume the same row; revocation/expiry serializes with consumption, and the payload must match the canonical hash.
- Storage errors must be treated as DENY. The adapter returns trade_allowed=false even when it successfully consumes an APPROVE decision.
- This does not implement identity proof, a TOTP/WebAuthn/signature provider, permission issuance, the authorization endpoint, a broker adapter, or order dispatch. Do not enable the HTTP 423 endpoint based on this store alone.
- Production deployment must apply db/postgres_schema.sql, use a managed durable PostgreSQL service, configure DATABASE_URL server-side, and enforce least-privilege DB credentials. This CI test uses a disposable PostgreSQL service only.
