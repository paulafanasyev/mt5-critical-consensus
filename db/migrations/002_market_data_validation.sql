-- Read-only market history and pre-outcome forecast capture.
-- Bars are immutable: corrections require a separately audited ingestion source/revision.
CREATE TABLE IF NOT EXISTS market_bars (
  symbol TEXT NOT NULL,
  timeframe TEXT NOT NULL,
  bar_time TIMESTAMPTZ NOT NULL,
  open DOUBLE PRECISION NOT NULL,
  high DOUBLE PRECISION NOT NULL,
  low DOUBLE PRECISION NOT NULL,
  close DOUBLE PRECISION NOT NULL,
  tick_volume BIGINT,
  spread_points INTEGER,
  source TEXT NOT NULL,
  inserted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (symbol, timeframe, bar_time),
  CHECK (high >= GREATEST(open, low, close)),
  CHECK (low <= LEAST(open, high, close)),
  CHECK (tick_volume IS NULL OR tick_volume >= 0),
  CHECK (spread_points IS NULL OR spread_points >= 0)
);
CREATE INDEX IF NOT EXISTS idx_market_bars_time
  ON market_bars(symbol, timeframe, bar_time DESC);

CREATE OR REPLACE FUNCTION reject_market_bars_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'market_bars are immutable; corrections require a new audited ingestion path';
END;
$$;
DROP TRIGGER IF EXISTS market_bars_no_update ON market_bars;
CREATE TRIGGER market_bars_no_update BEFORE UPDATE ON market_bars
  FOR EACH ROW EXECUTE FUNCTION reject_market_bars_mutation();
DROP TRIGGER IF EXISTS market_bars_no_delete ON market_bars;
CREATE TRIGGER market_bars_no_delete BEFORE DELETE ON market_bars
  FOR EACH ROW EXECUTE FUNCTION reject_market_bars_mutation();

-- Store forecasts when emitted, before their outcomes are available.
CREATE TABLE IF NOT EXISTS forecast_predictions (
  model_id TEXT NOT NULL,
  signal_id TEXT NOT NULL,
  issued_at TIMESTAMPTZ NOT NULL,
  probability DOUBLE PRECISION NOT NULL CHECK (probability >= 0 AND probability <= 1),
  input_sha256 TEXT NOT NULL CHECK (input_sha256 ~ '^sha256:[0-9a-f]{64}$'),
  outcome SMALLINT CHECK (outcome IN (0, 1)),
  resolved_at TIMESTAMPTZ,
  inserted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (model_id, signal_id),
  CHECK ((outcome IS NULL AND resolved_at IS NULL) OR
         (outcome IS NOT NULL AND resolved_at IS NOT NULL AND resolved_at > issued_at))
);
CREATE INDEX IF NOT EXISTS idx_forecast_predictions_walk_forward
  ON forecast_predictions(model_id, issued_at, resolved_at);

CREATE OR REPLACE FUNCTION protect_forecast_predictions()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.model_id <> OLD.model_id OR NEW.signal_id <> OLD.signal_id
     OR NEW.issued_at <> OLD.issued_at OR NEW.probability <> OLD.probability
     OR NEW.input_sha256 <> OLD.input_sha256 THEN
    RAISE EXCEPTION 'forecast identity and emitted probability are immutable';
  END IF;
  IF OLD.resolved_at IS NOT NULL AND
     (NEW.outcome IS DISTINCT FROM OLD.outcome OR NEW.resolved_at IS DISTINCT FROM OLD.resolved_at) THEN
    RAISE EXCEPTION 'resolved forecast outcome is immutable';
  END IF;
  IF NEW.resolved_at IS NOT NULL AND NEW.resolved_at <= NEW.issued_at THEN
    RAISE EXCEPTION 'resolved_at must be after issued_at';
  END IF;
  RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS forecast_predictions_protect_update ON forecast_predictions;
CREATE TRIGGER forecast_predictions_protect_update BEFORE UPDATE ON forecast_predictions
  FOR EACH ROW EXECUTE FUNCTION protect_forecast_predictions();

CREATE TABLE IF NOT EXISTS calibration_reports (
  report_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  protocol_version TEXT NOT NULL,
  dataset_sha256 TEXT NOT NULL CHECK (dataset_sha256 ~ '^sha256:[0-9a-f]{64}$'),
  report_sha256 TEXT NOT NULL UNIQUE CHECK (report_sha256 ~ '^sha256:[0-9a-f]{64}$'),
  report JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_calibration_reports_dataset
  ON calibration_reports(dataset_sha256, created_at DESC);

CREATE TABLE IF NOT EXISTS system_events (
  event_id TEXT NOT NULL,
  consumer_name TEXT NOT NULL,
  event_type TEXT NOT NULL,
  schema_version INTEGER NOT NULL CHECK (schema_version >= 1),
  occurred_at TIMESTAMPTZ NOT NULL,
  payload_sha256 TEXT NOT NULL CHECK (payload_sha256 ~ '^sha256:[0-9a-f]{64}$'),
  event JSONB NOT NULL,
  inserted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (consumer_name, event_id)
);
CREATE INDEX IF NOT EXISTS idx_system_events_type_time
  ON system_events(event_type, occurred_at DESC);
