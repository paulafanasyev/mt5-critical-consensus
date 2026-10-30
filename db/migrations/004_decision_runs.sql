-- Durable decision runs and their exact in-memory audit events.
-- Rows are append-only. A policy result is evidence; it is never an order command.
CREATE TABLE IF NOT EXISTS decision_runs (
  decision_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  signal_id TEXT NOT NULL,
  signal_input_sha256 TEXT NOT NULL CHECK (signal_input_sha256 ~ '^sha256:[0-9a-f]{64}$'),
  status TEXT NOT NULL CHECK (status IN (
    'GATHER_MORE','ESCALATE','HOLD','REJECT','DENY','REQUIRE_HUMAN','AUTHORIZED_ONCE'
  )),
  blockers JSONB NOT NULL,
  result_sha256 TEXT NOT NULL CHECK (result_sha256 ~ '^sha256:[0-9a-f]{64}$'),
  last_audit_event_hash TEXT,
  audit_event_count INTEGER NOT NULL CHECK (audit_event_count >= 1),
  result JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_decision_runs_signal_time
  ON decision_runs(signal_id, created_at DESC);

CREATE OR REPLACE FUNCTION reject_decision_run_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'decision_runs are append-only';
END;
$$;
DROP TRIGGER IF EXISTS decision_runs_no_update ON decision_runs;
CREATE TRIGGER decision_runs_no_update BEFORE UPDATE ON decision_runs
  FOR EACH ROW EXECUTE FUNCTION reject_decision_run_mutation();
DROP TRIGGER IF EXISTS decision_runs_no_delete ON decision_runs;
CREATE TRIGGER decision_runs_no_delete BEFORE DELETE ON decision_runs
  FOR EACH ROW EXECUTE FUNCTION reject_decision_run_mutation();

REVOKE UPDATE, DELETE, TRUNCATE ON decision_runs FROM PUBLIC;
