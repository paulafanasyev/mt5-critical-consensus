-- Emergency stop must be active by default. It can only be deactivated via the
-- separately authenticated operator path; no trade execution is implemented here.
CREATE TABLE IF NOT EXISTS risk_control_state (
  singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton IS TRUE),
  kill_switch_active BOOLEAN NOT NULL DEFAULT TRUE,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_by TEXT NOT NULL,
  reason TEXT NOT NULL
);

INSERT INTO risk_control_state(singleton, kill_switch_active, updated_by, reason)
VALUES (TRUE, TRUE, 'system-bootstrap', 'Fail-closed default: operator has not explicitly disabled emergency stop')
ON CONFLICT (singleton) DO NOTHING;

CREATE TABLE IF NOT EXISTS risk_control_events (
  event_seq BIGSERIAL PRIMARY KEY,
  event_id UUID NOT NULL UNIQUE DEFAULT gen_random_uuid(),
  kill_switch_active BOOLEAN NOT NULL,
  actor_id TEXT NOT NULL,
  reason TEXT NOT NULL,
  occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  previous_hash TEXT,
  event_hash TEXT NOT NULL UNIQUE,
  event JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_risk_control_events_time ON risk_control_events(occurred_at DESC);

CREATE OR REPLACE FUNCTION reject_risk_control_event_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'risk_control_events are append-only';
END;
$$;
DROP TRIGGER IF EXISTS risk_control_events_no_update ON risk_control_events;
CREATE TRIGGER risk_control_events_no_update BEFORE UPDATE ON risk_control_events
  FOR EACH ROW EXECUTE FUNCTION reject_risk_control_event_mutation();
DROP TRIGGER IF EXISTS risk_control_events_no_delete ON risk_control_events;
CREATE TRIGGER risk_control_events_no_delete BEFORE DELETE ON risk_control_events
  FOR EACH ROW EXECUTE FUNCTION reject_risk_control_event_mutation();

CREATE TABLE IF NOT EXISTS mt5_risk_snapshots (
  snapshot_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  observed_at TIMESTAMPTZ NOT NULL,
  terminal_server TEXT,
  account_mode TEXT NOT NULL CHECK (account_mode IN ('demo','contest','real','unknown')),
  symbol TEXT NOT NULL,
  snapshot_sha256 TEXT NOT NULL CHECK (snapshot_sha256 ~ '^sha256:[0-9a-f]{64}$'),
  snapshot JSONB NOT NULL,
  inserted_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_mt5_risk_snapshots_time ON mt5_risk_snapshots(symbol, observed_at DESC);

CREATE OR REPLACE FUNCTION reject_mt5_risk_snapshot_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'MT5 risk snapshots are append-only';
END;
$$;
DROP TRIGGER IF EXISTS mt5_risk_snapshots_no_update ON mt5_risk_snapshots;
CREATE TRIGGER mt5_risk_snapshots_no_update BEFORE UPDATE ON mt5_risk_snapshots
  FOR EACH ROW EXECUTE FUNCTION reject_mt5_risk_snapshot_mutation();
DROP TRIGGER IF EXISTS mt5_risk_snapshots_no_delete ON mt5_risk_snapshots;
CREATE TRIGGER mt5_risk_snapshots_no_delete BEFORE DELETE ON mt5_risk_snapshots
  FOR EACH ROW EXECUTE FUNCTION reject_mt5_risk_snapshot_mutation();
