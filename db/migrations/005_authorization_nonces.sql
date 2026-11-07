-- One-time human-authorization nonces. Consumption is a single atomic insert,
-- so concurrent submissions of the same decision cannot both succeed.
--
-- Retention: rows are tombstones and the application never deletes them. The
-- repository rejects an already-expired expires_at, and the (scope, nonce) primary
-- key makes a replay collide atomically. Pruning is a manual operator task allowed
-- only after expires_at plus the agreed authorization-audit retention window; it is
-- never performed inside request handling. There is intentionally no CHECK tying
-- expires_at to consumed_at so historical rows remain valid if clock policy changes.
CREATE TABLE IF NOT EXISTS authorization_nonces (
  scope TEXT NOT NULL,
  nonce TEXT NOT NULL,
  consumed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (scope, nonce)
);
CREATE INDEX IF NOT EXISTS idx_authorization_nonces_expiry
  ON authorization_nonces(expires_at);
