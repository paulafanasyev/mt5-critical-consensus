-- A permission may be authorized at most once, regardless of how many nonces or
-- operators sign it. The nonce table stops replay of one approval; this table stops
-- a second, freshly-signed approval of the same permission.
--
-- Retention: rows are tombstones and the application never deletes them. Pruning is a
-- manual operator task allowed only after expires_at plus the agreed audit retention
-- window; it is never performed inside request handling.
CREATE TABLE IF NOT EXISTS authorization_permissions (
  permission_id TEXT PRIMARY KEY,
  consumed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_authorization_permissions_expiry
  ON authorization_permissions(expires_at);
