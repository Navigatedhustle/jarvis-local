"""Only serve extraction that still matches the underlying authorized source."""

from .extraction import source_digest


def read(db, identifier):
    row = db.one("SELECT id,title,body,occurred_at FROM records WHERE id=%s AND NOT deleted", (identifier,))
    if not row:
        raise LookupError("Source no longer available")
    result = db.get_state("extracted." + identifier, {})
    if result.get("source_digest") != source_digest(row):
        return {
            "status": "pending",
            "summary": "Current structured notes are not available yet. Read the source for details.",
            "decisions": [],
            "source_id": identifier,
        }
    return {
        "status": "extracted",
        "source_id": identifier,
        "summary": str(result.get("summary", ""))[:1800],
        "decisions": [
            {"text": s[:500], "status": "extracted candidate", "source_id": identifier}
            for s in (result.get("decisions") if isinstance(result.get("decisions"), list) else [])[:10]
            if isinstance(s, str)
        ],
        "coverage": result.get("extraction_coverage"),
        "note": "Extracted interpretations retain source provenance. They do not approve actions or establish a confirmed commitment.",
    }
