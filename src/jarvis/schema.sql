CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS schema_version (version integer PRIMARY KEY, applied_at timestamptz DEFAULT now());
CREATE TABLE IF NOT EXISTS audit (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, at timestamptz NOT NULL DEFAULT now(),
 actor text NOT NULL, source text NOT NULL, action text NOT NULL, entity_id text,
 details jsonb NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS state (key text PRIMARY KEY, value jsonb NOT NULL, updated_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS records (
 id text PRIMARY KEY, kind text NOT NULL, provider text NOT NULL, title text NOT NULL,
 body text NOT NULL DEFAULT '', metadata jsonb NOT NULL DEFAULT '{}',
 occurred_at timestamptz, updated_at timestamptz NOT NULL DEFAULT now(), deleted boolean NOT NULL DEFAULT false,
 search tsvector GENERATED ALWAYS AS (to_tsvector('english', title || ' ' || body)) STORED
);
CREATE INDEX IF NOT EXISTS records_search ON records USING gin(search);
CREATE INDEX IF NOT EXISTS records_kind_time ON records(kind, occurred_at);
CREATE TABLE IF NOT EXISTS memories (
 id uuid PRIMARY KEY, kind text NOT NULL, text text NOT NULL, source_id text NOT NULL,
 confidence real NOT NULL CHECK (confidence BETWEEN 0 AND 1), permanent boolean NOT NULL DEFAULT false,
 embedding vector(1024), created_at timestamptz NOT NULL DEFAULT now(), forgotten_at timestamptz,
 search tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED
);
CREATE INDEX IF NOT EXISTS memory_search ON memories USING gin(search);
CREATE TABLE IF NOT EXISTS people (
 id text PRIMARY KEY, name text NOT NULL, organization text NOT NULL DEFAULT '',
 vip boolean NOT NULL DEFAULT false, profile jsonb NOT NULL DEFAULT '{}', last_interaction timestamptz
);
CREATE TABLE IF NOT EXISTS commitments (
 id uuid PRIMARY KEY, owner text NOT NULL, beneficiary text NOT NULL DEFAULT '', text text NOT NULL,
 due_at timestamptz, confidence real NOT NULL CHECK (confidence BETWEEN 0 AND 1),
 status text NOT NULL DEFAULT 'candidate', source_id text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE (source_id, text)
);
CREATE TABLE IF NOT EXISTS approvals (
 id uuid PRIMARY KEY, kind text NOT NULL, payload jsonb NOT NULL, digest text NOT NULL,
 status text NOT NULL CHECK(status IN ('pending','approved','denied','executing','succeeded','failed','uncertain','expired','superseded')),
 created_at timestamptz NOT NULL DEFAULT now(), expires_at timestamptz NOT NULL,
 actor text NOT NULL, decided_at timestamptz, result jsonb, error text
);
CREATE TABLE IF NOT EXISTS messages (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, conversation text NOT NULL,
 role text NOT NULL CHECK(role IN ('user','assistant')), content text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE messages ADD COLUMN IF NOT EXISTS metadata jsonb NOT NULL DEFAULT '{}';
CREATE TABLE IF NOT EXISTS jobs (
 id uuid PRIMARY KEY, kind text NOT NULL, payload jsonb NOT NULL DEFAULT '{}', priority integer NOT NULL,
 dedupe_key text NOT NULL UNIQUE, status text NOT NULL DEFAULT 'pending',
 due_at timestamptz NOT NULL, locked_at timestamptz, attempts integer NOT NULL DEFAULT 0,
 result jsonb, error text
);
CREATE INDEX IF NOT EXISTS jobs_due ON jobs(status, due_at, priority);
INSERT INTO schema_version(version) VALUES (1) ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS hpos_revisions (name text PRIMARY KEY, revision bigint NOT NULL DEFAULT 0);
INSERT INTO hpos_revisions(name) VALUES ('command') ON CONFLICT DO NOTHING;
CREATE OR REPLACE FUNCTION invalidate_hpos_command() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 UPDATE hpos_revisions SET revision=revision+1 WHERE name='command';
 RETURN NULL;
END;
$$;
DO $$ BEGIN
 IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='records_hpos_changed' AND tgrelid='records'::regclass) THEN
  CREATE TRIGGER records_hpos_changed AFTER INSERT OR UPDATE OR DELETE ON records FOR EACH STATEMENT EXECUTE FUNCTION invalidate_hpos_command();
  CREATE TRIGGER commitments_hpos_changed AFTER INSERT OR UPDATE OR DELETE ON commitments FOR EACH STATEMENT EXECUTE FUNCTION invalidate_hpos_command();
  CREATE TRIGGER people_hpos_changed AFTER INSERT OR UPDATE OR DELETE ON people FOR EACH STATEMENT EXECUTE FUNCTION invalidate_hpos_command();
  CREATE TRIGGER approvals_hpos_changed AFTER INSERT OR UPDATE OR DELETE ON approvals FOR EACH STATEMENT EXECUTE FUNCTION invalidate_hpos_command();
 END IF;
END $$;

INSERT INTO schema_version(version) VALUES (2) ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS relationship_dirty (
 person_id text PRIMARY KEY, revision bigint NOT NULL DEFAULT 1, changed_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS relationship_profiles (
 person_id text PRIMARY KEY REFERENCES people(id) ON DELETE CASCADE,
 snapshot jsonb NOT NULL, source_revision bigint NOT NULL, eligible boolean NOT NULL,
 updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS record_people (
 record_id text NOT NULL REFERENCES records(id) ON DELETE CASCADE,
 person_id text NOT NULL, role text NOT NULL, PRIMARY KEY(record_id,person_id,role)
);
CREATE INDEX IF NOT EXISTS record_people_person ON record_people(person_id,record_id);
CREATE INDEX IF NOT EXISTS records_sender_time ON records ((lower(metadata->'from'->'emailAddress'->>'address')),occurred_at DESC) WHERE kind='email' AND NOT deleted;
CREATE INDEX IF NOT EXISTS records_sent_thread_time ON records ((metadata->>'conversationId'),occurred_at DESC)
 WHERE kind='email' AND NOT deleted AND metadata->>'folder_role'='sentitems';
CREATE INDEX IF NOT EXISTS records_metadata ON records USING gin(metadata jsonb_path_ops);
ALTER TABLE commitments ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();

CREATE OR REPLACE FUNCTION dirty_relationship(identifier text) RETURNS void LANGUAGE plpgsql AS $$
BEGIN
 IF identifier IS NOT NULL AND position('@' in identifier)>1 THEN
  INSERT INTO relationship_dirty(person_id) VALUES (lower(identifier))
  ON CONFLICT(person_id) DO UPDATE SET revision=relationship_dirty.revision+1,changed_at=now();
 END IF;
END $$;
CREATE OR REPLACE FUNCTION index_record_people(identifier text, meta jsonb) RETURNS void LANGUAGE plpgsql AS $$
DECLARE address text; item jsonb; field text;
BEGIN
 address := lower(meta->'from'->'emailAddress'->>'address');
 IF position('@' in address)>1 THEN
  INSERT INTO record_people VALUES(identifier,address,'from') ON CONFLICT DO NOTHING;
 END IF;
 FOREACH field IN ARRAY ARRAY['toRecipients','ccRecipients','attendees'] LOOP
  FOR item IN SELECT value FROM jsonb_array_elements(CASE WHEN jsonb_typeof(meta->field)='array' THEN meta->field ELSE '[]'::jsonb END) LIMIT 100 LOOP
   address := lower(item->'emailAddress'->>'address');
   IF position('@' in address)>1 THEN
    INSERT INTO record_people VALUES(identifier,address,field) ON CONFLICT DO NOTHING;
   END IF;
  END LOOP;
 END LOOP;
END $$;
CREATE OR REPLACE FUNCTION dirty_record_relationships() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE meta jsonb; item jsonb; field text; related record;
BEGIN
 IF TG_OP='UPDATE' AND (OLD.title,OLD.body,OLD.metadata,OLD.deleted,OLD.occurred_at) IS NOT DISTINCT FROM (NEW.title,NEW.body,NEW.metadata,NEW.deleted,NEW.occurred_at) THEN RETURN NULL; END IF;
 FOR meta IN SELECT x FROM unnest(ARRAY[CASE WHEN TG_OP<>'INSERT' THEN OLD.metadata END,CASE WHEN TG_OP<>'DELETE' THEN NEW.metadata END]) AS x WHERE x IS NOT NULL LOOP
  FOR related IN SELECT person_id FROM record_people WHERE record_id=meta->>'source_id' LIMIT 100 LOOP
   PERFORM dirty_relationship(related.person_id);
  END LOOP;
  PERFORM dirty_relationship(meta->'from'->'emailAddress'->>'address');
  FOREACH field IN ARRAY ARRAY['toRecipients','ccRecipients','attendees'] LOOP
   FOR item IN SELECT value FROM jsonb_array_elements(CASE WHEN jsonb_typeof(meta->field)='array' THEN meta->field ELSE '[]'::jsonb END) LIMIT 100 LOOP
    PERFORM dirty_relationship(item->'emailAddress'->>'address');
   END LOOP;
  END LOOP;
 END LOOP;
 IF TG_OP<>'INSERT' THEN DELETE FROM record_people WHERE record_id=OLD.id; END IF;
 IF TG_OP<>'DELETE' AND NOT NEW.deleted THEN PERFORM index_record_people(NEW.id,NEW.metadata); END IF;
 RETURN NULL;
END $$;
CREATE OR REPLACE FUNCTION dirty_person_relationship() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='INSERT' OR (OLD.name,OLD.organization,OLD.vip,OLD.profile) IS DISTINCT FROM (NEW.name,NEW.organization,NEW.vip,NEW.profile) THEN
  PERFORM dirty_relationship(NEW.id);
 END IF;
 RETURN NULL;
END $$;
CREATE OR REPLACE FUNCTION dirty_commitment_relationships() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE person record; names text[];
BEGIN
 names := ARRAY[CASE WHEN TG_OP<>'INSERT' THEN lower(OLD.owner) END,CASE WHEN TG_OP<>'INSERT' THEN lower(OLD.beneficiary) END,CASE WHEN TG_OP<>'DELETE' THEN lower(NEW.owner) END,CASE WHEN TG_OP<>'DELETE' THEN lower(NEW.beneficiary) END];
 FOR person IN SELECT id FROM people WHERE lower(id)=ANY(names) OR lower(name)=ANY(names) OR regexp_split_to_array(lower(name),'[ ,]+') && names LOOP
  PERFORM dirty_relationship(person.id);
 END LOOP;
 RETURN NULL;
END $$;
CREATE OR REPLACE FUNCTION commitment_updated() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at=now(); RETURN NEW; END $$;
DO $$ DECLARE source record; BEGIN
 IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='records_relationship_dirty' AND tgrelid='records'::regclass) THEN
  CREATE TRIGGER records_relationship_dirty AFTER INSERT OR UPDATE OR DELETE ON records FOR EACH ROW EXECUTE FUNCTION dirty_record_relationships();
  CREATE TRIGGER people_relationship_dirty AFTER INSERT OR UPDATE ON people FOR EACH ROW EXECUTE FUNCTION dirty_person_relationship();
  CREATE TRIGGER commitments_relationship_dirty AFTER INSERT OR UPDATE OR DELETE ON commitments FOR EACH ROW EXECUTE FUNCTION dirty_commitment_relationships();
  CREATE TRIGGER commitments_updated BEFORE UPDATE ON commitments FOR EACH ROW EXECUTE FUNCTION commitment_updated();
 END IF;
 IF NOT EXISTS (SELECT 1 FROM schema_version WHERE version=3) THEN
  FOR source IN SELECT id,metadata FROM records WHERE NOT deleted LOOP
   PERFORM index_record_people(source.id,source.metadata);
  END LOOP;
  INSERT INTO relationship_dirty(person_id) SELECT id FROM people ON CONFLICT DO NOTHING;
 END IF;
END $$;
INSERT INTO schema_version(version) VALUES (3) ON CONFLICT DO NOTHING;

CREATE OR REPLACE FUNCTION dirty_relationship_preferences() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF NEW.key='preferences.work_priorities' AND (TG_OP='INSERT' OR OLD.value IS DISTINCT FROM NEW.value) THEN
  INSERT INTO relationship_dirty(person_id) SELECT id FROM people
  ON CONFLICT(person_id) DO UPDATE SET revision=relationship_dirty.revision+1,changed_at=now();
 END IF;
 RETURN NULL;
END $$;
DO $$ BEGIN
 IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='relationship_preferences_dirty' AND tgrelid='state'::regclass) THEN
  CREATE TRIGGER relationship_preferences_dirty AFTER INSERT OR UPDATE ON state FOR EACH ROW EXECUTE FUNCTION dirty_relationship_preferences();
 END IF;
END $$;

CREATE TABLE IF NOT EXISTS project_entities (
 id text PRIMARY KEY, name text NOT NULL, aliases jsonb NOT NULL DEFAULT '[]',
 status text NOT NULL DEFAULT 'candidate' CHECK(status IN ('candidate','active','archived')),
 origin text NOT NULL, revision bigint NOT NULL DEFAULT 1, dirty boolean NOT NULL DEFAULT true,
 snapshot jsonb NOT NULL DEFAULT '{}', created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS project_sources (
 project_id text NOT NULL REFERENCES project_entities(id) ON DELETE CASCADE,
 record_id text NOT NULL REFERENCES records(id) ON DELETE CASCADE,
 reason text NOT NULL, PRIMARY KEY(project_id,record_id)
);
CREATE INDEX IF NOT EXISTS project_sources_record ON project_sources(record_id);
CREATE TABLE IF NOT EXISTS project_source_dirty (
 record_id text PRIMARY KEY, revision bigint NOT NULL DEFAULT 1, changed_at timestamptz NOT NULL DEFAULT now()
);
CREATE OR REPLACE FUNCTION dirty_project_source() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE identifier text;
BEGIN
 IF TG_OP='UPDATE' AND (OLD.title,OLD.body,OLD.metadata,OLD.deleted,OLD.occurred_at) IS NOT DISTINCT FROM (NEW.title,NEW.body,NEW.metadata,NEW.deleted,NEW.occurred_at) THEN RETURN NULL; END IF;
 identifier := CASE WHEN TG_OP='DELETE' THEN OLD.id ELSE NEW.id END;
 UPDATE project_entities SET dirty=true,revision=revision+1 WHERE id IN (SELECT project_id FROM project_sources WHERE record_id=identifier);
 INSERT INTO project_source_dirty(record_id) VALUES(identifier) ON CONFLICT(record_id) DO UPDATE SET revision=project_source_dirty.revision+1,changed_at=now();
 RETURN NULL;
END $$;
CREATE OR REPLACE FUNCTION dirty_project_commitment() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 UPDATE project_entities SET dirty=true,revision=revision+1 WHERE id IN (SELECT project_id FROM project_sources WHERE record_id IN (CASE WHEN TG_OP<>'INSERT' THEN OLD.source_id END,CASE WHEN TG_OP<>'DELETE' THEN NEW.source_id END));
 RETURN NULL;
END $$;
DO $$ BEGIN
 IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='records_project_dirty' AND tgrelid='records'::regclass) THEN
  CREATE TRIGGER records_project_dirty AFTER INSERT OR UPDATE OR DELETE ON records FOR EACH ROW EXECUTE FUNCTION dirty_project_source();
  CREATE TRIGGER commitments_project_dirty AFTER INSERT OR UPDATE OR DELETE ON commitments FOR EACH ROW EXECUTE FUNCTION dirty_project_commitment();
 END IF;
 IF NOT EXISTS(SELECT 1 FROM schema_version WHERE version=4) THEN
  INSERT INTO project_source_dirty(record_id) SELECT id FROM records WHERE NOT deleted AND kind IN ('email','event','document','transcript') AND occurred_at>now()-interval '90 days' ORDER BY occurred_at DESC LIMIT 2000 ON CONFLICT DO NOTHING;
 END IF;
END $$;
INSERT INTO schema_version(version) VALUES(4) ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS content_packets (
 edition date PRIMARY KEY, id uuid NOT NULL UNIQUE, stage text NOT NULL DEFAULT 'research',
 status text NOT NULL DEFAULT 'queued', content jsonb NOT NULL DEFAULT '{}',
 error text, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS packet_deliveries (
 edition date PRIMARY KEY REFERENCES content_packets(edition),
 action_id uuid NOT NULL UNIQUE REFERENCES approvals(id),
 status text NOT NULL, policy_id text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
