-- Store the validated response media type beside the exact captured source bytes.
ALTER TABLE source_evidence
  ADD COLUMN IF NOT EXISTS response_content_type TEXT NOT NULL DEFAULT 'text/plain';

ALTER TABLE source_evidence
  DROP CONSTRAINT IF EXISTS source_evidence_content_type_check;
ALTER TABLE source_evidence
  ADD CONSTRAINT source_evidence_content_type_check
  CHECK (
    response_content_type LIKE 'text/%'
    OR response_content_type IN (
      'application/json','application/xml','application/rss+xml','application/atom+xml'
    )
  );
