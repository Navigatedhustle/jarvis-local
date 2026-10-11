from uuid import uuid4

import httpx


class Memory:
    def __init__(self, db, inference):
        self.db, self.inference = db, inference

    async def remember(
        self,
        text,
        kind="semantic",
        source_id="demo:direct",
        permanent=False,
        confidence=1.0,
        identifier=None,
    ):
        if kind not in {
            "working",
            "episodic",
            "semantic",
            "relationship",
            "project",
            "commitment",
            "document",
            "preference",
        }:
            raise ValueError("Unknown memory class")
        if not text.strip() or len(text) > 16000:
            raise ValueError("Memory must contain 1 to 16000 characters")
        identifier = identifier or uuid4()
        try:
            vector = await self.inference.embed(text)
        except (httpx.HTTPError, ValueError, KeyError):
            vector = None  # SQL/full-text memory remains available when embeddings are down.
        with self.db.transaction() as conn:
            changed = conn.execute(
                """INSERT INTO memories(id,kind,text,source_id,confidence,permanent,embedding)
                            VALUES (%s,%s,%s,%s,%s,%s,%s::vector) ON CONFLICT(id) DO NOTHING""",
                (identifier, kind, text, source_id, confidence, permanent, vector),
            ).rowcount
            if changed:
                self.db.audit("memory.created", identifier, {"source_id": source_id, "kind": kind}, conn=conn)
        return {
            "id": str(identifier),
            "text": text,
            "source_id": source_id,
            "indexed": bool(changed and vector is not None),
            "duplicate": not changed,
        }

    async def search(self, query, limit=8):
        rows = self.db.query(
            """SELECT id,kind,text,source_id,confidence,
                              ts_rank(search,websearch_to_tsquery('english',%s)) AS score
                              FROM retrievable_memories WHERE forgotten_at IS NULL
                              AND search @@ websearch_to_tsquery('english',%s)
                              ORDER BY score DESC LIMIT 24""",
            (query, query),
        )
        combined = {str(r["id"]): r for r in rows}
        try:
            vector = await self.inference.embed(
                "Instruct: Retrieve relevant executive assistant memories\nQuery: " + query
            )
            semantic = self.db.query(
                """SELECT id,kind,text,source_id,confidence,1-(embedding <=> %s::vector) AS score
                                       FROM retrievable_memories WHERE forgotten_at IS NULL AND embedding IS NOT NULL
                                       ORDER BY embedding <=> %s::vector LIMIT 24""",
                (vector, vector),
            )
            combined.update({str(r["id"]): r for r in semantic})
        except (httpx.HTTPError, ValueError, KeyError):
            pass
        candidates = list(combined.values())
        if candidates:
            try:
                ranked = await self.inference.rerank(query, [r["text"] for r in candidates])
                return self.current([candidates[r["index"]] for r in ranked[:limit]])
            except (httpx.HTTPError, ValueError, KeyError, IndexError):
                pass
        return self.current(candidates[:limit])

    def current(self, candidates):
        # Source edits/approved forgetting can occur while embedding or reranking awaits.
        if not candidates:
            return []
        ids = {
            str(row["id"])
            for row in self.db.query(
                "SELECT id FROM retrievable_memories WHERE id=ANY(%s::uuid[])",
                ([str(row["id"]) for row in candidates],),
            )
        }
        return [row for row in candidates if str(row["id"]) in ids]

    async def forget_approved(self, payload, action_id):
        # Content is cleared as well as deindexed, so forgotten facts do not leak via other retrieval paths.
        with self.db.transaction() as conn:
            lineage = conn.execute(
                "SELECT source_id FROM memory_lineage WHERE memory_id=%s", (payload["memory_id"],)
            ).fetchone()
            if lineage:
                # Serialize against consolidation's source lock before locking any memory row.
                conn.execute("SELECT id FROM records WHERE id=%s FOR UPDATE", (lineage["source_id"],))
                conn.execute(
                    "UPDATE memory_lineage SET active=false,updated_at=now() WHERE source_id=%s",
                    (lineage["source_id"],),
                )
            prior = conn.execute(
                "SELECT text FROM memories WHERE id=%s AND forgotten_at IS NULL FOR UPDATE",
                (payload["memory_id"],),
            ).fetchone()
            if prior:
                # Prevent recently remembered content being reintroduced by conversation-history retrieval.
                conn.execute(
                    "UPDATE messages SET content='[Message redacted after approved memory deletion]' WHERE position(%s in content)>0",
                    (prior["text"],),
                )
            changed = conn.execute(
                "UPDATE memories SET text='[forgotten]',embedding=NULL,forgotten_at=now() WHERE id=%s AND forgotten_at IS NULL",
                (payload["memory_id"],),
            ).rowcount
            self.db.audit("memory.forgotten", payload["memory_id"], {"approval_id": action_id}, conn=conn)
        from .memory_views import remove_forgotten_copies

        remove_forgotten_copies(self.db, payload["memory_id"])
        return {"forgotten": bool(changed)}
