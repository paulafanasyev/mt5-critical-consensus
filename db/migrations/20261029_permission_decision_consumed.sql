-- Upgrade existing permission-state constraint so one-time consumed decisions can be closed.
-- Safe to re-run; existing unsupported rows cause this migration to fail closed.
ALTER TABLE permissions DROP CONSTRAINT IF EXISTS permissions_status_check;
ALTER TABLE permissions
  ADD CONSTRAINT permissions_status_check
  CHECK (status IN ('DENY','REQUIRE_HUMAN','ALLOW_ONCE','EXPIRED','DECISION_CONSUMED'));
