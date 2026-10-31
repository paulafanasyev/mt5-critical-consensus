-- Preserve the "first persisted future bar" target after resolution.
-- Existing identical replays remain idempotent; new late historical inserts are rejected.
CREATE OR REPLACE FUNCTION reject_late_market_bar_insert()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM market_bars
    WHERE symbol = NEW.symbol
      AND timeframe = NEW.timeframe
      AND bar_time = NEW.bar_time
  ) THEN
    RETURN NEW;
  END IF;

  IF EXISTS (
    SELECT 1 FROM market_bars
    WHERE symbol = NEW.symbol
      AND timeframe = NEW.timeframe
      AND bar_time > NEW.bar_time
  ) THEN
    RAISE EXCEPTION 'late historical market-bar insert is blocked; backfill requires a separately audited revision path';
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS market_bars_forward_only_insert ON market_bars;
CREATE TRIGGER market_bars_forward_only_insert
  BEFORE INSERT ON market_bars
  FOR EACH ROW EXECUTE FUNCTION reject_late_market_bar_insert();
