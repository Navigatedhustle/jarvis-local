CREATE TABLE IF NOT EXISTS memory_lineage (
 memory_id uuid PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
 source_id text NOT NULL, source_digest text NOT NULL, normalized_hash text NOT NULL,
 active boolean NOT NULL DEFAULT true, updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS memory_lineage_source ON memory_lineage(source_id);
CREATE TABLE IF NOT EXISTS memory_consolidation_dirty (
 source_id text PRIMARY KEY, revision bigint NOT NULL DEFAULT 1, changed_at timestamptz NOT NULL DEFAULT now()
);
CREATE OR REPLACE FUNCTION queue_memory_consolidation(identifier text) RETURNS void LANGUAGE plpgsql AS $$
BEGIN
 INSERT INTO memory_consolidation_dirty(source_id) VALUES(identifier)
 ON CONFLICT(source_id) DO UPDATE SET revision=memory_consolidation_dirty.revision+1,changed_at=now();
END;
$$;
CREATE OR REPLACE FUNCTION invalidate_consolidated_source() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE identifier text;
BEGIN
 IF TG_OP='UPDATE' AND (OLD.title,OLD.body,OLD.occurred_at,OLD.deleted) IS NOT DISTINCT FROM (NEW.title,NEW.body,NEW.occurred_at,NEW.deleted) THEN RETURN NULL; END IF;
 identifier := CASE WHEN TG_OP='DELETE' THEN OLD.id ELSE NEW.id END;
 UPDATE memory_lineage SET active=false,updated_at=now() WHERE source_id=identifier AND active;
 IF EXISTS(SELECT 1 FROM memory_lineage WHERE source_id=identifier) OR EXISTS(SELECT 1 FROM state WHERE key='extracted.'||identifier) THEN
  PERFORM queue_memory_consolidation(identifier);
 END IF;
 RETURN NULL;
END;
$$;
CREATE OR REPLACE FUNCTION extracted_memory_changed() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF left(NEW.key,10)='extracted.' AND (TG_OP='INSERT' OR OLD.value IS DISTINCT FROM NEW.value) THEN
  UPDATE memory_lineage SET active=false,updated_at=now() WHERE source_id=substring(NEW.key from 11) AND active;
  PERFORM queue_memory_consolidation(substring(NEW.key from 11));
 END IF;
 RETURN NULL;
END;
$$;
DO $$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_trigger WHERE tgname='records_memory_consolidation' AND tgrelid='records'::regclass) THEN
  CREATE TRIGGER records_memory_consolidation AFTER INSERT OR UPDATE OR DELETE ON records FOR EACH ROW EXECUTE FUNCTION invalidate_consolidated_source();
  CREATE TRIGGER extracted_memory_consolidation AFTER INSERT OR UPDATE ON state FOR EACH ROW EXECUTE FUNCTION extracted_memory_changed();
 END IF;
 IF NOT EXISTS(SELECT 1 FROM schema_version WHERE version=6) THEN
  INSERT INTO memory_consolidation_dirty(source_id) SELECT substring(key from 11) FROM state WHERE left(key,10)='extracted.' ORDER BY updated_at DESC LIMIT 1000 ON CONFLICT DO NOTHING;
 END IF;
END $$;

-- Direct memories and legacy document excerpts retain their existing behavior.
-- Newly consolidated notes must still have active, undeleted source lineage.
CREATE OR REPLACE VIEW retrievable_memories AS
SELECT m.* FROM memories m LEFT JOIN memory_lineage l ON l.memory_id=m.id
WHERE m.forgotten_at IS NULL AND (l.memory_id IS NULL OR (l.active AND EXISTS(
 SELECT 1 FROM records r WHERE r.id=l.source_id AND NOT r.deleted
 AND (NOT(r.metadata ? 'source_id') OR EXISTS(SELECT 1 FROM records p WHERE p.id=r.metadata->>'source_id' AND NOT p.deleted))
)));
INSERT INTO schema_version(version) VALUES(6) ON CONFLICT DO NOTHING;
