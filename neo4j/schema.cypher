// Constraints and indexes for the knowledge graph (Neo4j 5.x)
CREATE CONSTRAINT source_id IF NOT EXISTS FOR (n:Source) REQUIRE n.source_id IS UNIQUE;
CREATE CONSTRAINT event_id IF NOT EXISTS FOR (n:Event) REQUIRE n.event_id IS UNIQUE;
CREATE CONSTRAINT instrument_symbol IF NOT EXISTS FOR (n:Instrument) REQUIRE n.symbol IS UNIQUE;
CREATE CONSTRAINT rumor_id IF NOT EXISTS FOR (n:Rumor) REQUIRE n.rumor_id IS UNIQUE;
CREATE CONSTRAINT agent_id IF NOT EXISTS FOR (n:Agent) REQUIRE n.agent_id IS UNIQUE;
CREATE CONSTRAINT verdict_id IF NOT EXISTS FOR (n:Verdict) REQUIRE n.verdict_id IS UNIQUE;
CREATE CONSTRAINT challenge_id IF NOT EXISTS FOR (n:Challenge) REQUIRE n.challenge_id IS UNIQUE;
CREATE INDEX source_credibility IF NOT EXISTS FOR (n:Source) ON (n.credibility);
CREATE INDEX event_timestamp IF NOT EXISTS FOR (n:Event) ON (n.timestamp);
CREATE INDEX challenge_status IF NOT EXISTS FOR (n:Challenge) ON (n.status);

// Suggested labels/relationships:
// (:Source)-[:MENTIONED {seen_at, confidence}]->(:Event)
// (:Source)-[:CONFIRMED|REFUTED {evidence_ref, seen_at}]->(:Event)
// (:Event)-[:AFFECTS]->(:Instrument)
// (:Agent)-[:ISSUED]->(:Verdict)
// (:Verdict)-[:CHALLENGED_BY]->(:Challenge)
// (:Challenge)-[:RESOLVED_BY {round, evidence_refs}]->(:Verdict)
// (:Rumor)-[:ABOUT]->(:Instrument)
// Always version time-bound facts; do not overwrite provenance or prior verdicts.
