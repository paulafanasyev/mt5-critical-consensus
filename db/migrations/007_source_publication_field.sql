-- Preserve the exact registry-configured source field used for publication timestamps.
ALTER TABLE source_evidence
  ADD COLUMN IF NOT EXISTS publication_time_field TEXT NOT NULL DEFAULT '';
