-- Read-through relational graph: existing tables remain the only source of truth.
-- No copied facts, full-graph refresh, model or background service.
CREATE OR REPLACE VIEW memory_graph_records AS
SELECT r.* FROM records r WHERE NOT r.deleted
 AND r.kind IN ('email','event','document','transcript')
 AND (NOT (r.metadata ? 'source_id') OR EXISTS(
   SELECT 1 FROM records parent WHERE parent.id=r.metadata->>'source_id' AND NOT parent.deleted));

CREATE OR REPLACE VIEW memory_graph_nodes AS
SELECT 'record:'||r.id id, r.kind type, r.title name, left(r.body,400) summary,
 jsonb_build_object('coverage',r.metadata->>'coverage','cancelled',r.metadata->'isCancelled','source_id',r.id) metadata,
 NULL::real confidence, 'source record'::text status, r.occurred_at activity_at, r.updated_at
FROM memory_graph_records r
UNION ALL
SELECT 'person:'||p.id,'person',p.name,'',jsonb_build_object('source_id',p.id,'vip',p.vip),
 NULL::real,'saved profile',p.last_interaction,NULL::timestamptz FROM people p
UNION ALL
SELECT 'project:'||p.id,'project',p.name,'',jsonb_build_object('source_id',p.id,'origin',p.origin),
 NULL::real,p.status,p.updated_at,p.updated_at FROM project_entities p WHERE status<>'archived'
UNION ALL
SELECT 'commitment:'||c.id::text,'commitment',left(c.text,160),left(c.text,400),
 jsonb_build_object('source_id',c.source_id,'owner',c.owner,'beneficiary',c.beneficiary,'due_at',c.due_at),
 c.confidence,c.status,c.created_at,c.updated_at FROM commitments c
WHERE c.status<>'dismissed' AND (c.source_id='demo:direct' OR EXISTS(SELECT 1 FROM memory_graph_records r WHERE r.id=c.source_id))
UNION ALL
SELECT 'memory:'||m.id::text,m.kind,left(m.text,160),left(m.text,400),
 jsonb_build_object('source_id',m.source_id,'permanent',m.permanent),m.confidence,
 CASE WHEN m.source_id='demo:direct' THEN 'Demo supplied' ELSE 'extracted memory' END,
 m.created_at,m.created_at FROM retrievable_memories m WHERE m.forgotten_at IS NULL
 AND (m.source_id='demo:direct' OR EXISTS(SELECT 1 FROM memory_graph_records r WHERE r.id=m.source_id))
UNION ALL
SELECT 'organization:'||lower(trim(organization)),'organization',min(trim(organization)),'',
 '{}'::jsonb,NULL::real,'profile label',NULL::timestamptz,NULL::timestamptz
FROM people WHERE trim(organization)<>'' GROUP BY lower(trim(organization));

CREATE OR REPLACE VIEW memory_graph_edges AS
SELECT 'record:'||rp.record_id source,'person:'||rp.person_id target,
 CASE rp.role WHEN 'from' THEN 'SENDER' WHEN 'attendees' THEN 'INVITED_ATTENDEE' ELSE 'RECIPIENT' END relationship,
 1.0::real confidence,70::real base_weight,r.occurred_at activity_at,
 rp.record_id provenance,'recorded participant'::text status
FROM record_people rp JOIN memory_graph_records r ON r.id=rp.record_id JOIN people p ON p.id=rp.person_id
UNION ALL
SELECT 'project:'||ps.project_id,'record:'||ps.record_id,'RELATED_SOURCE',0.65::real,65::real,
 r.occurred_at,ps.record_id,'candidate association: '||ps.reason
FROM project_sources ps JOIN project_entities p ON p.id=ps.project_id AND p.status<>'archived'
 JOIN memory_graph_records r ON r.id=ps.record_id
WHERE NOT EXISTS(SELECT 1 FROM project_source_dirty d WHERE d.record_id=ps.record_id)
 AND NOT EXISTS(SELECT 1 FROM project_source_dirty d WHERE d.record_id=r.metadata->>'source_id')
UNION ALL
SELECT 'record:'||r.id,'record:'||(r.metadata->>'source_id'),'LINKED_SOURCE',1.0::real,80::real,
 r.occurred_at,r.id,'explicit source association' FROM memory_graph_records r
 JOIN memory_graph_records parent ON parent.id=r.metadata->>'source_id'
UNION ALL
SELECT 'commitment:'||c.id::text,'record:'||c.source_id,'SUPPORTED_BY',c.confidence,75::real,
 c.created_at,c.source_id,c.status FROM commitments c JOIN memory_graph_records r ON r.id=c.source_id WHERE c.status<>'dismissed'
UNION ALL
SELECT 'memory:'||m.id::text,'record:'||m.source_id,'SUPPORTED_BY',m.confidence,55::real,
 m.created_at,m.source_id,'extracted memory' FROM retrievable_memories m JOIN memory_graph_records r ON r.id=m.source_id WHERE m.forgotten_at IS NULL
UNION ALL
SELECT 'memory:'||m.id::text,'actor:demo','REMEMBERED_BY',m.confidence,80::real,
 m.created_at,'demo:direct','Demo supplied' FROM retrievable_memories m WHERE m.forgotten_at IS NULL AND m.source_id='demo:direct'
UNION ALL
SELECT 'person:'||p.id,'organization:'||lower(trim(p.organization)),'PROFILE_ORGANIZATION',NULL::real,40::real,
 p.last_interaction,'person:'||p.id,'reported in saved profile' FROM people p WHERE trim(p.organization)<>''
UNION ALL
SELECT 'actor:demo','person:'||p.id,'PRIORITY_CONTACT',1.0::real,100::real,
 p.last_interaction,'person:'||p.id,'configured VIP' FROM people p WHERE p.vip;

CREATE INDEX IF NOT EXISTS commitments_source_id ON commitments(source_id);
CREATE INDEX IF NOT EXISTS memories_source_id ON memories(source_id) WHERE forgotten_at IS NULL;
INSERT INTO schema_version(version) VALUES(5) ON CONFLICT DO NOTHING;
