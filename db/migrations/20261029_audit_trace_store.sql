-- Durable, independently verifiable audit traces. The event timestamp is stored both
-- as TIMESTAMPTZ for querying and as exact text for canonical hash verification.
CREATE TABLE IF NOT EXISTS audit_traces (
  trace_id UUID PRIMARY KEY,
  signal_id TEXT NOT NULL,
  pipeline_status TEXT NOT NULL,
  event_count INTEGER NOT NULL CHECK (event_count > 0),
  chain_head TEXT NOT NULL,
  events_hash TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS audit_trace_events (
  trace_id UUID NOT NULL REFERENCES audit_traces(trace_id) ON DELETE RESTRICT,
  event_index INTEGER NOT NULL CHECK (event_index >= 0),
  event_type TEXT NOT NULL,
  timestamp_text TEXT NOT NULL,
  occurred_at TIMESTAMPTZ NOT NULL,
  previous_hash TEXT,
  event_hash TEXT NOT NULL,
  payload JSONB NOT NULL,
  PRIMARY KEY (trace_id, event_index),
  UNIQUE (trace_id, event_hash)
);
CREATE INDEX IF NOT EXISTS idx_audit_trace_events_hash
  ON audit_trace_events(event_hash);

CREATE OR REPLACE FUNCTION reject_audit_trace_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'audit trace storage is append-only; UPDATE/DELETE is prohibited';
END;
$$;
DROP TRIGGER IF EXISTS audit_traces_no_update_delete ON audit_traces;
CREATE TRIGGER audit_traces_no_update_delete
BEFORE UPDATE OR DELETE ON audit_traces
FOR EACH ROW EXECUTE FUNCTION reject_audit_trace_mutation();
DROP TRIGGER IF EXISTS audit_trace_events_no_update_delete ON audit_trace_events;
CREATE TRIGGER audit_trace_events_no_update_delete
BEFORE UPDATE OR DELETE ON audit_trace_events
FOR EACH ROW EXECUTE FUNCTION reject_audit_trace_mutation();
REVOKE UPDATE, DELETE, TRUNCATE ON audit_traces FROM PUBLIC;
REVOKE UPDATE, DELETE, TRUNCATE ON audit_trace_events FROM PUBLIC;

