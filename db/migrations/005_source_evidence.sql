-- Immutable captures fetched from publisher domains explicitly reviewed in source_registry.yaml.
CREATE TABLE IF NOT EXISTS source_evidence (
  source_id TEXT PRIMARY KEY,
  publisher_id TEXT NOT NULL,
  publisher_domain TEXT NOT NULL,
  source_url TEXT NOT NULL,
  published_at TIMESTAMPTZ NOT NULL,
  retrieved_at TIMESTAMPTZ NOT NULL,
  independence_group TEXT NOT NULL,
  credibility DOUBLE PRECISION NOT NULL CHECK (credibility >= 0 AND credibility <= 1),
  ttl_seconds INTEGER NOT NULL CHECK (ttl_seconds > 0),
  content_sha256 TEXT NOT NULL CHECK (content_sha256 ~ '^sha256:[0-9a-f]{64}$'),
  content_bytes INTEGER NOT NULL CHECK (content_bytes > 0 AND content_bytes <= 512000),
  publication_time_attestation TEXT NOT NULL,
  capture_method TEXT NOT NULL CHECK (capture_method = 'https_fetch'),
  registry_version TEXT NOT NULL,
  inserted_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_source_evidence_publisher_time
  ON source_evidence(publisher_id, published_at DESC);

CREATE OR REPLACE FUNCTION reject_source_evidence_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'source_evidence is append-only';
END;
$$;
DROP TRIGGER IF EXISTS source_evidence_no_update ON source_evidence;
CREATE TRIGGER source_evidence_no_update BEFORE UPDATE ON source_evidence
  FOR EACH ROW EXECUTE FUNCTION reject_source_evidence_mutation();
DROP TRIGGER IF EXISTS source_evidence_no_delete ON source_evidence;
CREATE TRIGGER source_evidence_no_delete BEFORE DELETE ON source_evidence
  FOR EACH ROW EXECUTE FUNCTION reject_source_evidence_mutation();
REVOKE UPDATE, DELETE, TRUNCATE ON source_evidence FROM PUBLIC;
