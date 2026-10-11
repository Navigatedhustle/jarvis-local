"""Private generated Markdown snapshots. Never used as conversational authority."""

import asyncio
import hashlib
import html
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID
from zoneinfo import ZoneInfo

from .config import settings
from .consolidation import defer
from .inference import InferenceDeferred
from .meeting_intelligence import importance
from .projects import detail as project_detail
from .relationships import detail as person_detail
from .source_insights import read as source_notes

FLAG = "feature.HPOS_MEMORY_VIEWS"
KINDS = {"people", "projects", "organizations", "meetings", "commitments", "decisions", "facts", "daily"}


def escape(text):
    text = html.escape(str(text or ""), quote=False)
    return re.sub(r"([\\`*_{}\[\]()#!])", r"\\\1", text.replace("\r", ""))


def safe_path(relative, root=None):
    base = Path(root) if root is not None else settings.data_dir / "memory_views"
    if base.is_symlink():
        raise ValueError("Generated view root must not be a symlink")
    parts = Path(relative).parts
    if (
        len(parts) != 2
        or parts[0] not in KINDS
        or not re.fullmatch(r"[a-z0-9-]{1,80}-[a-f0-9]{16}\.md", parts[1])
    ):
        raise ValueError("Invalid generated view path")
    target = base / relative
    if (
        target.parent.is_symlink()
        or target.is_symlink()
        or not target.resolve().is_relative_to(base.resolve())
    ):
        raise ValueError("Generated view path escapes private storage")
    return target


def atomic_write(relative, content, root=None):
    target = safe_path(relative, root)
    base = target.parent.parent
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    base.chmod(0o700)
    target.parent.mkdir(exist_ok=True, mode=0o700)
    target.parent.chmod(0o700)
    fd, name = tempfile.mkstemp(prefix=".view-", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            if os.name != "nt":
                os.fchmod(handle.fileno(), 0o600)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, target)
    finally:
        Path(name).unlink(missing_ok=True)


def remove(relative, root=None):
    safe_path(relative, root).unlink(missing_ok=True)


def render(db, kind, identifier):
    """Return a bounded note and explicit dependency references, or omit ineligible data."""
    title, lines, sources = identifier, [], []
    if kind == "people":
        p = db.one(
            "SELECT p.*,coalesce(r.eligible,false) eligible FROM people p LEFT JOIN relationship_profiles r ON r.person_id=p.id WHERE p.id=%s",
            (identifier,),
        )
        if not p or not (p["vip"] or p["eligible"]):
            return None
        data = person_detail(db, identifier)
        if not data.get("ready") or data.get("stale"):
            raise InferenceDeferred("Person snapshot is being refreshed")
        title = p["name"]
        sources = ["person:" + identifier]
        lines = [
            "Configured priority contact: " + str(p["vip"]),
            "Reported organization: " + (p["organization"] or "Not recorded"),
        ]
        for s in data["sources"][:6]:
            sources.append(s["id"])
            lines.append("Saved interaction: " + s["title"] + " — " + (s.get("at") or "date unavailable"))
        for c in data["commitments"][:8]:
            sources.extend([c["source_id"], "commitment:" + str(c["id"])])
            lines.append(c["status"] + ": " + c["owner"] + " → " + c["beneficiary"] + ": " + c["text"])
    elif kind == "projects":
        try:
            data = project_detail(db, identifier)
        except LookupError:
            return None
        if not data.get("enabled"):
            return None
        if data["stale"]:
            raise InferenceDeferred("Project snapshot is being refreshed")
        title = data["name"]
        lines = ["Project state: " + data["status"], data["status_summary"], *data["next_actions"]]
        for s in data["sources"][:8]:
            sources.append(s["id"])
            lines.append("Evidence: " + s["title"])
        for c in data["commitments"][:8]:
            sources.extend([c["source_id"], "commitment:" + str(c["id"])])
            lines.append(c["status"] + ": " + c["text"])
    elif kind == "organizations":
        people = db.query(
            "SELECT p.id,p.name,p.organization,p.vip FROM people p LEFT JOIN relationship_profiles r ON r.person_id=p.id WHERE lower(trim(p.organization))=%s AND (p.vip OR r.eligible) ORDER BY p.vip DESC,p.name LIMIT 25",
            (identifier,),
        )
        if not people:
            return None
        title = people[0]["organization"]
        lines = ["Organization labels come from saved profiles, not verified employment."]
        for p in people:
            sources.append("person:" + p["id"])
            lines.append(p["name"] + (" (configured VIP)" if p["vip"] else ""))
    elif kind == "meetings":
        row = db.one(
            "SELECT * FROM records WHERE id=%s AND kind='event' AND NOT deleted AND occurred_at>now()-interval '30 days' AND occurred_at<now()+interval '14 days'",
            (identifier,),
        )
        if not row or not importance(db, row):
            return None
        title = row["title"]
        sources = [identifier]
        lines = ["Scheduled: " + str(row["occurred_at"]), "An invitation is not proof of attendance."]
        for person in row["metadata"].get("attendees", [])[:15]:
            address = person.get("emailAddress", {}).get("address", "").lower()
            if address:
                sources.append("person:" + address)
            lines.append("Invited: " + person.get("emailAddress", {}).get("name", "Name unavailable"))
        lines.append("Saved agenda excerpt: " + row["body"][:1200])
    elif kind == "commitments":
        row = db.one(
            "SELECT * FROM commitments WHERE id=%s AND status IN ('open','done')", (UUID(identifier),)
        )
        if not row:
            return None
        title = row["text"][:100]
        sources = [row["source_id"], "commitment:" + identifier]
        lines = [
            row["text"],
            "Status: " + row["status"],
            "Owner: " + row["owner"],
            "Beneficiary: " + row["beneficiary"],
            "Due: " + str(row["due_at"] or "Not confirmed"),
        ]
    elif kind == "facts":
        row = db.one("SELECT * FROM retrievable_memories WHERE id=%s", (UUID(identifier),))
        if not row:
            return None
        title = (
            ("Direct memory" if row["source_id"] == "demo:direct" else "Source memory")
            + " — "
            + row["text"][:65]
        )
        sources = ["memory:" + identifier, row["source_id"]]
        lines = [
            row["text"],
            "Memory class: " + row["kind"],
            "Remembered at: " + str(row["created_at"]),
            "Confidence: " + str(round(row["confidence"], 2)),
            "Permanent: " + str(row["permanent"]),
        ]
    elif kind == "decisions":
        try:
            notes = source_notes(db, identifier)
        except LookupError:
            return None
        if not notes["decisions"]:
            return None
        row = db.one("SELECT title FROM records WHERE id=%s AND NOT deleted", (identifier,))
        if not row:
            return None
        title = row["title"]
        sources = [identifier]
        lines = ["Extracted decision candidates. These are interpretations, not approvals."] + [
            d["text"] for d in notes["decisions"][:8]
        ]
    elif kind == "daily":
        start = datetime.strptime(identifier, "%Y-%m-%d").replace(tzinfo=ZoneInfo("America/Denver"))
        end = start + timedelta(days=1)
        title = "Work memory — " + identifier
        rows = db.query(
            "SELECT id,title FROM records WHERE kind='email' AND NOT deleted AND occurred_at>=%s AND occurred_at<%s AND metadata->'triage'->>'priority'='high' ORDER BY occurred_at DESC LIMIT 10",
            (start, end),
        )
        lines = ["Bounded saved work highlights. Receipt does not prove Demo read or acted on a message."]
        for r in rows:
            sources.append(r["id"])
            lines.append("Important saved message: " + r["title"])
        for c in db.query(
            "SELECT id,text,status,source_id FROM commitments WHERE updated_at>=%s AND updated_at<%s AND status IN ('open','done') ORDER BY updated_at DESC LIMIT 10",
            (start, end),
        ):
            sources.extend([c["source_id"], "commitment:" + str(c["id"])])
            lines.append("Commitment updated (" + c["status"] + "): " + c["text"])
        for m in db.query(
            "SELECT id,text,source_id FROM retrievable_memories WHERE created_at>=%s AND created_at<%s ORDER BY created_at DESC LIMIT 5",
            (start, end),
        ):
            sources.extend(["memory:" + str(m["id"]), m["source_id"]])
            lines.append("Remembered: " + m["text"][:600])
    else:
        raise ValueError("Unknown generated view category")
    if not lines:
        return None
    stamp = datetime.now(timezone.utc).isoformat()
    content = (
        "# "
        + escape(" ".join(title.split()))
        + "\n\nGenerated "
        + stamp
        + ". PostgreSQL is authoritative. This is a private snapshot, not a live status page.\n\n"
    )
    content += "\n\n".join("- " + escape(line) for line in lines) + "\n\n## Source references\n\n"
    content += "\n".join("- " + escape(s) for s in sorted(set(sources))) + "\n"
    return title, content, sorted(set(sources))


async def refresh(db, inference, root=None):
    if not db.get_state(FLAG, False):
        return {"disabled": True}
    written = removed = deferred = 0
    rows = db.query(
        "SELECT pending.* FROM unnest(%s::text[]) category CROSS JOIN LATERAL (SELECT * FROM memory_view_queue WHERE kind=category ORDER BY changed_at,entity_id LIMIT 1) pending",
        (sorted(KINDS),),
    )
    for job in rows:
        defer(inference)
        await asyncio.sleep(0)
        try:
            result = render(db, job["kind"], job["entity_id"])
        except InferenceDeferred:
            deferred += 1
            db.execute(
                "UPDATE memory_view_queue SET changed_at=now() WHERE kind=%s AND entity_id=%s AND revision=%s",
                (job["kind"], job["entity_id"], job["revision"]),
            )
            continue
        defer(inference)
        with db.transaction() as conn:
            pending = conn.execute(
                "SELECT revision FROM memory_view_queue WHERE kind=%s AND entity_id=%s FOR UPDATE",
                (job["kind"], job["entity_id"]),
            ).fetchone()
            if not pending or pending["revision"] != job["revision"]:
                continue
            existing = conn.execute(
                "SELECT path FROM memory_view_exports WHERE kind=%s AND entity_id=%s",
                (job["kind"], job["entity_id"]),
            ).fetchone()
            if result is None:
                if existing:
                    remove(existing["path"], root)
                conn.execute(
                    "DELETE FROM memory_view_exports WHERE kind=%s AND entity_id=%s",
                    (job["kind"], job["entity_id"]),
                )
                removed += 1
            else:
                title, content, sources = result
                slug = re.sub("[^a-z0-9]+", "-", title.lower()).strip("-")[:60] or "note"
                key = hashlib.sha256(job["entity_id"].encode()).hexdigest()[:16]
                path = existing["path"] if existing else job["kind"] + "/" + slug + "-" + key + ".md"
                atomic_write(path, content, root)
                conn.execute(
                    "INSERT INTO memory_view_exports(kind,entity_id,title,path,sources,fingerprint) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(kind,entity_id) DO UPDATE SET title=excluded.title,sources=excluded.sources,fingerprint=excluded.fingerprint,updated_at=now()",
                    (
                        job["kind"],
                        job["entity_id"],
                        title,
                        path,
                        sources,
                        hashlib.sha256(content.encode()).hexdigest(),
                    ),
                )
                written += 1
            conn.execute(
                "DELETE FROM memory_view_queue WHERE kind=%s AND entity_id=%s AND revision=%s",
                (job["kind"], job["entity_id"], job["revision"]),
            )
    db.audit("memory.views_refreshed", details={"written": written, "removed": removed, "deferred": deferred})
    return {"written": written, "removed": removed, "deferred": deferred}


def listing(db):
    enabled = bool(db.get_state(FLAG, False))
    return {
        "enabled": enabled,
        "views": db.query(
            "SELECT e.kind,e.entity_id,e.title,e.updated_at,(q.entity_id IS NOT NULL) stale FROM memory_view_exports e LEFT JOIN memory_view_queue q USING(kind,entity_id) ORDER BY e.kind,e.updated_at DESC LIMIT 200"
        )
        if enabled
        else [],
        "pending": db.one("SELECT count(*) n FROM memory_view_queue")["n"] if enabled else 0,
    }


def read(db, kind, identifier, root=None):
    if not db.get_state(FLAG, False):
        raise LookupError("Generated memory views are not enabled")
    row = db.one(
        "SELECT e.* FROM memory_view_exports e WHERE kind=%s AND entity_id=%s AND NOT EXISTS(SELECT 1 FROM memory_view_queue q WHERE q.kind=e.kind AND q.entity_id=e.entity_id)",
        (kind, identifier),
    )
    if not row:
        raise LookupError("This note is unavailable or awaiting refresh")
    path = safe_path(row["path"], root)
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        db.execute("SELECT queue_memory_view(%s,%s)", (kind, identifier))
        raise LookupError("Generated note needs regeneration") from None
    if hashlib.sha256(content.encode()).hexdigest() != row["fingerprint"]:
        db.execute("SELECT queue_memory_view(%s,%s)", (kind, identifier))
        raise LookupError("Generated note was changed outside JARVIS; regenerate it from the database")
    return {
        "title": row["title"],
        "markdown": content,
        "filename": path.name,
        "updated_at": row["updated_at"],
    }


def remove_forgotten_copies(db, identifier, root=None):
    """After approved forgetting, remove only manifested derived copies of this memory."""
    rows = db.query(
        "SELECT kind,entity_id,path FROM memory_view_exports WHERE (kind='facts' AND entity_id=%s) OR sources @> ARRAY[%s]",
        (str(identifier), "memory:" + str(identifier)),
    )
    for row in rows:
        remove(row["path"], root)
        db.execute(
            "DELETE FROM memory_view_exports WHERE kind=%s AND entity_id=%s", (row["kind"], row["entity_id"])
        )
