-- Evidence-based market-event forecast target and deterministic one-bar resolution.
-- Historical rows created before this migration remain LEGACY_VERDICT_CONFIDENCE
-- and are deliberately excluded from calibration exports.
ALTER TABLE forecast_predictions
  ADD COLUMN IF NOT EXISTS forecast_kind TEXT NOT NULL DEFAULT 'LEGACY_VERDICT_CONFIDENCE',
  ADD COLUMN IF NOT EXISTS target_policy_version TEXT,
  ADD COLUMN IF NOT EXISTS target_event TEXT,
  ADD COLUMN IF NOT EXISTS symbol TEXT,
  ADD COLUMN IF NOT EXISTS timeframe TEXT,
  ADD COLUMN IF NOT EXISTS reference_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS reference_close DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS resolved_bar_time TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS resolved_bar_close DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS resolution_source TEXT,
  ADD COLUMN IF NOT EXISTS resolution_evidence_sha256 TEXT;

ALTER TABLE forecast_predictions
  DROP CONSTRAINT IF EXISTS forecast_predictions_forecast_kind_check;
ALTER TABLE forecast_predictions
  ADD CONSTRAINT forecast_predictions_forecast_kind_check
  CHECK (forecast_kind IN ('LEGACY_VERDICT_CONFIDENCE', 'NEXT_BAR_MARKET_EVENT'));

ALTER TABLE forecast_predictions
  DROP CONSTRAINT IF EXISTS forecast_predictions_market_target_check;
ALTER TABLE forecast_predictions
  ADD CONSTRAINT forecast_predictions_market_target_check
  CHECK (
    (forecast_kind = 'LEGACY_VERDICT_CONFIDENCE'
      AND target_policy_version IS NULL AND target_event IS NULL
      AND symbol IS NULL AND timeframe IS NULL
      AND reference_at IS NULL AND reference_close IS NULL)
    OR
    (forecast_kind = 'NEXT_BAR_MARKET_EVENT'
      AND target_policy_version = 'next-available-bar-close-above-reference-v1'
      AND target_event = 'NEXT_AVAILABLE_BAR_CLOSE_ABOVE_REFERENCE'
      AND symbol IS NOT NULL AND length(trim(symbol)) > 0
      AND timeframe IS NOT NULL AND timeframe IN ('M1','M5','M15','M30','H1','H4','D1','W1')
      AND reference_at IS NOT NULL AND reference_at < issued_at
      AND reference_close IS NOT NULL AND reference_close > 0)
  );

ALTER TABLE forecast_predictions
  DROP CONSTRAINT IF EXISTS forecast_predictions_market_resolution_check;
ALTER TABLE forecast_predictions
  ADD CONSTRAINT forecast_predictions_market_resolution_check
  CHECK (
    (
      forecast_kind = 'LEGACY_VERDICT_CONFIDENCE'
      AND target_policy_version IS NULL AND target_event IS NULL
      AND symbol IS NULL AND timeframe IS NULL
      AND reference_at IS NULL AND reference_close IS NULL
      AND resolved_bar_time IS NULL AND resolved_bar_close IS NULL
      AND resolution_source IS NULL AND resolution_evidence_sha256 IS NULL
      AND (
        (outcome IS NULL AND resolved_at IS NULL)
        OR (outcome IS NOT NULL AND resolved_at IS NOT NULL)
      )
    )
    OR
    (
      forecast_kind = 'NEXT_BAR_MARKET_EVENT'
      AND (
        (outcome IS NULL AND resolved_at IS NULL
          AND resolved_bar_time IS NULL AND resolved_bar_close IS NULL
          AND resolution_source IS NULL AND resolution_evidence_sha256 IS NULL)
        OR
        (outcome IS NOT NULL AND resolved_at IS NOT NULL
          AND resolved_bar_time IS NOT NULL AND resolved_bar_time > issued_at
          AND resolved_bar_close IS NOT NULL AND resolved_bar_close > 0
          AND resolution_source IS NOT NULL AND length(trim(resolution_source)) > 0
          AND resolution_evidence_sha256 IS NOT NULL
          AND resolution_evidence_sha256 ~ '^sha256:[0-9a-f]{64}$'
          AND outcome = CASE WHEN resolved_bar_close > reference_close THEN 1 ELSE 0 END)
      )
    )
  );

CREATE INDEX IF NOT EXISTS idx_forecast_predictions_pending_market_events
  ON forecast_predictions(symbol, timeframe, issued_at)
  WHERE forecast_kind = 'NEXT_BAR_MARKET_EVENT' AND outcome IS NULL;

CREATE TABLE IF NOT EXISTS forecast_resolution_events (
  resolution_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  model_id TEXT NOT NULL,
  signal_id TEXT NOT NULL,
  target_policy_version TEXT NOT NULL,
  resolved_bar_time TIMESTAMPTZ NOT NULL,
  outcome SMALLINT NOT NULL CHECK (outcome IN (0,1)),
  evidence_sha256 TEXT NOT NULL CHECK (evidence_sha256 ~ '^sha256:[0-9a-f]{64}$'),
  evidence JSONB NOT NULL,
  recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(model_id, signal_id),
  FOREIGN KEY (model_id, signal_id)
    REFERENCES forecast_predictions(model_id, signal_id)
);

CREATE OR REPLACE FUNCTION reject_forecast_resolution_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'forecast_resolution_events is append-only';
END;
$$;

DROP TRIGGER IF EXISTS forecast_resolution_events_no_update ON forecast_resolution_events;
CREATE TRIGGER forecast_resolution_events_no_update
  BEFORE UPDATE ON forecast_resolution_events
  FOR EACH ROW EXECUTE FUNCTION reject_forecast_resolution_mutation();
DROP TRIGGER IF EXISTS forecast_resolution_events_no_delete ON forecast_resolution_events;
CREATE TRIGGER forecast_resolution_events_no_delete
  BEFORE DELETE ON forecast_resolution_events
  FOR EACH ROW EXECUTE FUNCTION reject_forecast_resolution_mutation();
REVOKE UPDATE, DELETE, TRUNCATE ON forecast_resolution_events FROM PUBLIC;

CREATE OR REPLACE FUNCTION protect_forecast_predictions()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.model_id <> OLD.model_id OR NEW.signal_id <> OLD.signal_id
     OR NEW.issued_at <> OLD.issued_at OR NEW.probability <> OLD.probability
     OR NEW.input_sha256 <> OLD.input_sha256
     OR NEW.forecast_kind <> OLD.forecast_kind
     OR NEW.target_policy_version IS DISTINCT FROM OLD.target_policy_version
     OR NEW.target_event IS DISTINCT FROM OLD.target_event
     OR NEW.symbol IS DISTINCT FROM OLD.symbol
     OR NEW.timeframe IS DISTINCT FROM OLD.timeframe
     OR NEW.reference_at IS DISTINCT FROM OLD.reference_at
     OR NEW.reference_close IS DISTINCT FROM OLD.reference_close THEN
    RAISE EXCEPTION 'forecast identity, target contract and emitted probability are immutable';
  END IF;

  IF OLD.resolved_at IS NOT NULL AND
     (NEW.outcome IS DISTINCT FROM OLD.outcome
      OR NEW.resolved_at IS DISTINCT FROM OLD.resolved_at
      OR NEW.resolved_bar_time IS DISTINCT FROM OLD.resolved_bar_time
      OR NEW.resolved_bar_close IS DISTINCT FROM OLD.resolved_bar_close
      OR NEW.resolution_source IS DISTINCT FROM OLD.resolution_source
      OR NEW.resolution_evidence_sha256 IS DISTINCT FROM OLD.resolution_evidence_sha256) THEN
    RAISE EXCEPTION 'resolved market forecast outcome and evidence are immutable';
  END IF;

  IF OLD.outcome IS NULL AND NEW.outcome IS NOT NULL THEN
    IF NEW.forecast_kind <> 'NEXT_BAR_MARKET_EVENT'
       OR NEW.resolved_bar_time IS NULL OR NEW.resolved_bar_time <= OLD.issued_at
       OR NEW.resolved_bar_close IS NULL OR NEW.resolved_bar_close <= 0
       OR NEW.resolution_source IS NULL OR length(trim(NEW.resolution_source)) = 0
       OR NEW.resolution_evidence_sha256 !~ '^sha256:[0-9a-f]{64}$'
       OR (NEW.outcome = 1 AND NEW.resolved_bar_close <= OLD.reference_close)
       OR (NEW.outcome = 0 AND NEW.resolved_bar_close > OLD.reference_close)
       OR NOT EXISTS (
         SELECT 1 FROM forecast_resolution_events e
         WHERE e.model_id = OLD.model_id AND e.signal_id = OLD.signal_id
           AND e.target_policy_version = OLD.target_policy_version
           AND e.resolved_bar_time = NEW.resolved_bar_time
           AND e.outcome = NEW.outcome
           AND e.evidence_sha256 = NEW.resolution_evidence_sha256
       ) THEN
      RAISE EXCEPTION 'forecast resolution requires matching immutable market-bar evidence';
    END IF;
    IF (NEW.timeframe = 'M1' AND NEW.resolved_at < NEW.resolved_bar_time + INTERVAL '1 minute')
       OR (NEW.timeframe = 'M5' AND NEW.resolved_at < NEW.resolved_bar_time + INTERVAL '5 minutes')
       OR (NEW.timeframe = 'M15' AND NEW.resolved_at < NEW.resolved_bar_time + INTERVAL '15 minutes')
       OR (NEW.timeframe = 'M30' AND NEW.resolved_at < NEW.resolved_bar_time + INTERVAL '30 minutes')
       OR (NEW.timeframe = 'H1' AND NEW.resolved_at < NEW.resolved_bar_time + INTERVAL '1 hour')
       OR (NEW.timeframe = 'H4' AND NEW.resolved_at < NEW.resolved_bar_time + INTERVAL '4 hours')
       OR (NEW.timeframe = 'D1' AND NEW.resolved_at < NEW.resolved_bar_time + INTERVAL '1 day')
       OR (NEW.timeframe = 'W1' AND NEW.resolved_at < NEW.resolved_bar_time + INTERVAL '7 days') THEN
      RAISE EXCEPTION 'target market bar is not yet complete';
    END IF;
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS forecast_predictions_protect_update ON forecast_predictions;
CREATE TRIGGER forecast_predictions_protect_update
  BEFORE UPDATE ON forecast_predictions
  FOR EACH ROW EXECUTE FUNCTION protect_forecast_predictions();
