CREATE TABLE IF NOT EXISTS memory_view_queue (
 kind text NOT NULL, entity_id text NOT NULL, revision bigint NOT NULL DEFAULT 1,
 changed_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(kind,entity_id)
);
CREATE TABLE IF NOT EXISTS memory_view_exports (
 kind text NOT NULL, entity_id text NOT NULL, title text NOT NULL, path text NOT NULL UNIQUE,
 sources text[] NOT NULL DEFAULT '{}', fingerprint text NOT NULL,
 updated_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(kind,entity_id)
);
CREATE INDEX IF NOT EXISTS memory_view_due ON memory_view_queue(kind,changed_at);
CREATE INDEX IF NOT EXISTS memory_view_sources ON memory_view_exports USING gin(sources);
CREATE OR REPLACE FUNCTION queue_memory_view(category text, identifier text) RETURNS void LANGUAGE plpgsql AS $$
BEGIN
 IF identifier IS NULL OR identifier='' THEN RETURN; END IF;
 INSERT INTO memory_view_queue(kind,entity_id) VALUES(category,identifier)
 ON CONFLICT(kind,entity_id) DO UPDATE SET revision=memory_view_queue.revision+1,changed_at=now();
END;
$$;
CREATE OR REPLACE FUNCTION invalidate_memory_views(reference text) RETURNS void LANGUAGE plpgsql AS $$
DECLARE item record;
BEGIN
 FOR item IN SELECT kind,entity_id FROM memory_view_exports WHERE sources @> ARRAY[reference] LOOP
  PERFORM queue_memory_view(item.kind,item.entity_id);
 END LOOP;
END;
$$;
CREATE OR REPLACE FUNCTION memory_views_changed() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE item jsonb; prior jsonb; identifier text; category text;
BEGIN
 item:=CASE WHEN TG_OP='DELETE' THEN to_jsonb(OLD) ELSE to_jsonb(NEW) END;
 prior:=CASE WHEN TG_OP='INSERT' THEN '{}'::jsonb ELSE to_jsonb(OLD) END;
 IF TG_OP='UPDATE' AND (prior-'updated_at'-'last_interaction')=(item-'updated_at'-'last_interaction') THEN RETURN NULL; END IF;
 IF TG_TABLE_NAME='records' THEN
  identifier:=item->>'id';
  PERFORM invalidate_memory_views(identifier);
  IF item->>'kind'='event' THEN PERFORM queue_memory_view('meetings',identifier); END IF;
 ELSIF TG_TABLE_NAME='people' THEN
  PERFORM queue_memory_view('people',item->>'id');
  PERFORM queue_memory_view('organizations',lower(trim(item->>'organization')));
  PERFORM queue_memory_view('organizations',lower(trim(prior->>'organization')));
  PERFORM invalidate_memory_views('person:'||(item->>'id'));
 ELSIF TG_TABLE_NAME='relationship_profiles' THEN
  PERFORM queue_memory_view('people',item->>'person_id');
  PERFORM queue_memory_view('organizations',(SELECT lower(trim(organization)) FROM people WHERE id=item->>'person_id'));
 ELSIF TG_TABLE_NAME='project_entities' THEN
  PERFORM queue_memory_view('projects',item->>'id');
 ELSIF TG_TABLE_NAME='commitments' THEN
  PERFORM queue_memory_view('commitments',item->>'id');
  PERFORM invalidate_memory_views('commitment:'||(item->>'id'));
 ELSIF TG_TABLE_NAME='memories' OR TG_TABLE_NAME='memory_lineage' THEN
  identifier:=coalesce(item->>'memory_id',item->>'id');
  PERFORM queue_memory_view('facts',identifier);
  PERFORM invalidate_memory_views('memory:'||identifier);
 ELSIF TG_TABLE_NAME='state' AND left(item->>'key',10)='extracted.' THEN
  PERFORM queue_memory_view('decisions',substring(item->>'key' from 11));
 ELSE RETURN NULL;
 END IF;
 PERFORM queue_memory_view('daily',to_char(now() AT TIME ZONE 'America/Denver','YYYY-MM-DD'));
 RETURN NULL;
END;
$$;
DO $$ DECLARE name text; BEGIN
 FOREACH name IN ARRAY ARRAY['records','people','relationship_profiles','project_entities','commitments','memories','memory_lineage','state'] LOOP
  IF NOT EXISTS(SELECT 1 FROM pg_trigger WHERE tgname='memory_views_changed' AND tgrelid=name::regclass) THEN
   EXECUTE format('CREATE TRIGGER memory_views_changed AFTER INSERT OR UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION memory_views_changed()',name);
  END IF;
 END LOOP;
 IF NOT EXISTS(SELECT 1 FROM schema_version WHERE version=7) THEN
  INSERT INTO memory_view_queue(kind,entity_id) SELECT 'people',p.id FROM people p LEFT JOIN relationship_profiles r ON r.person_id=p.id WHERE p.vip OR r.eligible ORDER BY p.vip DESC LIMIT 100 ON CONFLICT DO NOTHING;
  INSERT INTO memory_view_queue(kind,entity_id) SELECT 'projects',id FROM project_entities WHERE status<>'archived' LIMIT 100 ON CONFLICT DO NOTHING;
  INSERT INTO memory_view_queue(kind,entity_id) SELECT 'organizations',lower(trim(organization)) FROM people WHERE vip AND trim(organization)<>'' GROUP BY lower(trim(organization)) LIMIT 50 ON CONFLICT DO NOTHING;
  INSERT INTO memory_view_queue(kind,entity_id) SELECT 'meetings',id FROM records WHERE kind='event' AND NOT deleted AND occurred_at>now()-interval '1 day' AND occurred_at<now()+interval '14 days' ORDER BY occurred_at LIMIT 100 ON CONFLICT DO NOTHING;
  INSERT INTO memory_view_queue(kind,entity_id) SELECT 'commitments',id::text FROM commitments WHERE status IN ('open','done') ORDER BY updated_at DESC LIMIT 100 ON CONFLICT DO NOTHING;
  INSERT INTO memory_view_queue(kind,entity_id) SELECT 'facts',id::text FROM retrievable_memories ORDER BY created_at DESC LIMIT 100 ON CONFLICT DO NOTHING;
  INSERT INTO memory_view_queue(kind,entity_id) SELECT 'decisions',substring(key from 11) FROM state WHERE left(key,10)='extracted.' ORDER BY updated_at DESC LIMIT 100 ON CONFLICT DO NOTHING;
  PERFORM queue_memory_view('daily',to_char(now() AT TIME ZONE 'America/Denver','YYYY-MM-DD'));
 END IF;
END $$;
INSERT INTO schema_version(version) VALUES(7) ON CONFLICT DO NOTHING;
