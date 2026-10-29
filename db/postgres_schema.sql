CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS audit_events (
  id BIGSERIAL PRIMARY KEY,
  event_id UUID NOT NULL UNIQUE DEFAULT gen_random_uuid(),
  event_type TEXT NOT NULL,
  schema_version INTEGER NOT NULL,
  occurred_at TIMESTAMPTZ NOT NULL,
  previous_hash TEXT,
  event_hash TEXT NOT NULL,
  payload JSONB NOT NULL,
  inserted_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_audit_events_type_time ON audit_events(event_type, occurred_at DESC);

CREATE TABLE IF NOT EXISTS agent_verdicts (
  verdict_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  signal_id UUID NOT NULL,
  agent_id TEXT NOT NULL,
  verdict TEXT NOT NULL CHECK (verdict IN ('APPROVE','ABSTAIN','REJECT','VETO','HOLD')),
  confidence DOUBLE PRECISION NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
  calibration_meta JSONB NOT NULL,
  evidence_refs JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  payload_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_verdicts_signal ON agent_verdicts(signal_id, created_at);

CREATE TABLE IF NOT EXISTS challenges (
  challenge_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  signal_id UUID NOT NULL,
  target_verdict_id UUID REFERENCES agent_verdicts(verdict_id),
  status TEXT NOT NULL CHECK (status IN ('OPEN','RESOLVED','ESCALATED')),
  challenge_type TEXT NOT NULL,
  evidence_refs JSONB NOT NULL,
  resolution_history JSONB NOT NULL DEFAULT '[]'::jsonb,
  created_at TIMESTAMPTZ NOT NULL,
  resolved_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS permissions (
  permission_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  signal_id UUID NOT NULL,
  mode TEXT NOT NULL CHECK (mode IN ('demo','live')),
  status TEXT NOT NULL CHECK (status IN ('DENY','REQUIRE_HUMAN','ALLOW_ONCE','EXPIRED','DECISION_CONSUMED')),
  request JSONB NOT NULL,
  decision JSONB,
  expires_at TIMESTAMPTZ NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Idempotent migration for databases created before the decision registry was added.
ALTER TABLE permissions DROP CONSTRAINT IF EXISTS permissions_status_check;
ALTER TABLE permissions
  ADD CONSTRAINT permissions_status_check
  CHECK (status IN ('DENY','REQUIRE_HUMAN','ALLOW_ONCE','EXPIRED','DECISION_CONSUMED'));

CREATE TABLE IF NOT EXISTS order_commands (
  command_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  client_order_id TEXT NOT NULL UNIQUE,
  permission_id UUID NOT NULL REFERENCES permissions(permission_id),
  status TEXT NOT NULL CHECK (status IN ('PREPARED','APPROVED','SENT','FILLED','REJECTED','UNKNOWN','RECONCILED')),
  command JSONB NOT NULL,
  broker_result JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Enforce append-only audit at deployment: application role should have INSERT/SELECT only.
-- Use WORM/object-lock storage for exported audit batches; a hash chain alone is not immutability.

-- Durable idempotency inbox for at-least-once Redis Streams consumers.
-- Consumer side effects and this insert must be committed in the same transaction.
CREATE TABLE IF NOT EXISTS processed_events (
  consumer_name TEXT NOT NULL,
  event_id TEXT NOT NULL,
  processed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  result_hash TEXT,
  PRIMARY KEY (consumer_name, event_id)
);

-- Prevent accidental mutation of audit rows. Application roles should still receive
-- only SELECT/INSERT on this table; table owners/superusers can override database policy.
CREATE OR REPLACE FUNCTION reject_audit_events_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  RAISE EXCEPTION 'audit_events is append-only; UPDATE/DELETE is prohibited';
END;
$$;

DROP TRIGGER IF EXISTS audit_events_no_update ON audit_events;
CREATE TRIGGER audit_events_no_update
BEFORE UPDATE ON audit_events
FOR EACH ROW EXECUTE FUNCTION reject_audit_events_mutation();

DROP TRIGGER IF EXISTS audit_events_no_delete ON audit_events;
CREATE TRIGGER audit_events_no_delete
BEFORE DELETE ON audit_events
FOR EACH ROW EXECUTE FUNCTION reject_audit_events_mutation();

CREATE UNIQUE INDEX IF NOT EXISTS uq_audit_events_hash ON audit_events(event_hash);
REVOKE UPDATE, DELETE, TRUNCATE ON audit_events FROM PUBLIC;


-- One-time human decision registry. Decisions are inserted by trusted server-side
-- authentication code only after verified identity/MFA/signature checks.
-- Permission IDs are stored as text to match the external payload; queries bind them
-- back to the authoritative permissions table through permission_id::text.
CREATE TABLE IF NOT EXISTS user_decisions (
  decision_id TEXT PRIMARY KEY CHECK (btrim(decision_id) <> ''),
  permission_id TEXT NOT NULL CHECK (btrim(permission_id) <> ''),
  user_id TEXT NOT NULL CHECK (btrim(user_id) <> ''),
  decision TEXT NOT NULL CHECK (decision IN ('APPROVE','REJECT')),
  mode TEXT NOT NULL CHECK (mode IN ('demo','live')),
  decision_timestamp TIMESTAMPTZ NOT NULL,
  expires_at TIMESTAMPTZ NOT NULL,
  payload_hash TEXT NOT NULL,
  verifier_id TEXT NOT NULL,
  verification_method TEXT NOT NULL,
  verification_ref TEXT NOT NULL,
  verified_at TIMESTAMPTZ NOT NULL,
  consumed_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (expires_at > decision_timestamp)
);
-- A permission can receive only one registered human decision; a new review needs a new permission.
CREATE UNIQUE INDEX IF NOT EXISTS uq_user_decisions_permission
  ON user_decisions (permission_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_user_decisions_auth_receipt
  ON user_decisions (verifier_id, verification_ref);
