import pytest
from psycopg.types.json import Jsonb

from jarvis.drafts import edit, revision
from jarvis.policy import Actor


def test_draft_edit_is_internal_and_rejects_stale_review(db):
    db.execute(
        "INSERT INTO records(id,kind,provider,title,body,metadata) VALUES ('draft:qa','draft','jarvis','Draft','Incomplete [',%s)",
        (Jsonb({"source_id": "synthetic:source", "review_required": True}),),
    )
    row = db.one("SELECT * FROM records WHERE id='draft:qa'")
    result = edit(
        db,
        row["id"],
        revision(row),
        "Reviewed draft",
        "Complete internal text.",
        Actor("demo", "dashboard"),
    )
    assert result == {"saved": True, "sent": False}
    with pytest.raises(ValueError, match="changed"):
        edit(
            db,
            row["id"],
            revision(row),
            "Stale edit",
            "Must not replace the new text.",
            Actor("demo", "dashboard"),
        )
    assert (
        db.one("SELECT body,metadata FROM records WHERE id='draft:qa'")["body"] == "Complete internal text."
    )
    assert db.one("SELECT count(*) n FROM approvals")["n"] == 0
