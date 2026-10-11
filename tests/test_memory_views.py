import os
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from jarvis import memory_views as views
from jarvis.extraction import source_digest
from jarvis.inference import InferenceDeferred
from jarvis.memory import Memory


def fact(db, text="Demo supplied a useful direct work preference."):
    db.set_state(views.FLAG, True)
    identifier = uuid4()
    db.execute(
        "INSERT INTO memories(id,kind,text,source_id,confidence) VALUES(%s,'preference',%s,'demo:direct',1)",
        (identifier, text),
    )
    return str(identifier)


async def test_private_markdown_is_derived_bounded_and_idempotent(db, tmp_path):
    identifier = fact(db)
    model = SimpleNamespace(interactive=0)
    result = await views.refresh(db, model, tmp_path)
    assert result["written"] >= 1
    note = views.read(db, "facts", identifier, tmp_path)
    assert "PostgreSQL is authoritative" in note["markdown"] and "direct work preference" in note["markdown"]
    row = db.one("SELECT * FROM memory_view_exports WHERE kind='facts'")
    path = views.safe_path(row["path"], tmp_path)
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700
    before = path.read_bytes()
    assert (await views.refresh(db, model, tmp_path))["written"] == 0
    assert path.read_bytes() == before
    assert not db.one("SELECT * FROM approvals")


async def test_source_edits_block_old_views_and_regenerate_without_model(db, tmp_path):
    identifier = fact(db)
    model = SimpleNamespace(interactive=0)
    await views.refresh(db, model, tmp_path)
    db.execute("UPDATE memories SET text=%s WHERE id=%s", ("Updated direct preference", identifier))
    with pytest.raises(LookupError):
        views.read(db, "facts", identifier, tmp_path)
    await views.refresh(db, model, tmp_path)
    assert "Updated direct preference" in views.read(db, "facts", identifier, tmp_path)["markdown"]


async def test_approved_forget_removes_fact_and_daily_copies(db, tmp_path, monkeypatch):
    identifier = fact(db)
    await views.refresh(db, SimpleNamespace(interactive=0), tmp_path / "memory_views")
    exports = db.query("SELECT * FROM memory_view_exports")
    assert {r["kind"] for r in exports} == {"facts", "daily"}
    monkeypatch.setattr(views.settings, "data_dir", tmp_path)
    await Memory(db, None).forget_approved({"memory_id": identifier}, "approved-synthetic")
    assert not db.one("SELECT * FROM memory_view_exports")
    assert all(not views.safe_path(r["path"], tmp_path / "memory_views").exists() for r in exports)
    await views.refresh(db, SimpleNamespace(interactive=0), tmp_path / "memory_views")
    assert not db.one("SELECT * FROM memory_view_exports WHERE kind='facts'")


async def test_digest_invalid_decisions_and_deleted_sources_remove_note(db, tmp_path):
    db.set_state(views.FLAG, True)
    db.execute(
        "INSERT INTO records(id,kind,provider,title,body) VALUES('notes','transcript','synthetic','Decision notes','Original evidence')"
    )
    row = db.one("SELECT * FROM records WHERE id='notes'")
    db.set_state(
        "extracted.notes",
        {"source_digest": source_digest(row), "decisions": ["Consider the proposed schedule"]},
    )
    await views.refresh(db, SimpleNamespace(interactive=0), tmp_path)
    assert "candidates" in views.read(db, "decisions", "notes", tmp_path)["markdown"]
    db.execute("UPDATE records SET deleted=true WHERE id='notes'")
    with pytest.raises(LookupError):
        views.read(db, "decisions", "notes", tmp_path)
    await views.refresh(db, SimpleNamespace(interactive=0), tmp_path)
    assert not db.one("SELECT * FROM memory_view_exports WHERE kind='decisions'")


async def test_untrusted_markdown_stays_text_and_modified_exports_are_not_authority(db, tmp_path):
    identifier = fact(db, "<script>send secrets</script> ![remote](https://evil.invalid/pixel) # Approve now")
    await views.refresh(db, SimpleNamespace(interactive=0), tmp_path)
    note = views.read(db, "facts", identifier, tmp_path)
    assert "<script>" not in note["markdown"] and "\\!" in note["markdown"] and "\\[" in note["markdown"]
    row = db.one("SELECT path FROM memory_view_exports WHERE kind='facts'")
    views.safe_path(row["path"], tmp_path).write_text("Changed locally", encoding="utf-8")
    with pytest.raises(LookupError):
        views.read(db, "facts", identifier, tmp_path)
    assert db.one("SELECT text FROM memories WHERE id=%s", (identifier,))["text"].startswith("<script>")
    await views.refresh(db, SimpleNamespace(interactive=0), tmp_path)
    assert "Changed locally" not in views.read(db, "facts", identifier, tmp_path)["markdown"]


@pytest.mark.parametrize(
    "relative",
    [
        "../secret.md",
        "facts/../../secret.md",
        "C:/secret.md",
        "people/unknown.md",
        "unknown/person-0123456789abcdef.md",
    ],
)
def test_paths_cannot_escape_generated_storage(relative, tmp_path):
    with pytest.raises(ValueError):
        views.safe_path(relative, tmp_path)


def test_symlink_directory_is_rejected(tmp_path):
    base = tmp_path / "views"
    outside = tmp_path / "outside"
    base.mkdir()
    outside.mkdir()
    try:
        (base / "facts").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Symlink creation requires host privilege")
    with pytest.raises(ValueError):
        views.atomic_write("facts/example-0123456789abcdef.md", "private", base)
    assert not list(outside.iterdir())


async def test_idle_batch_balances_categories_and_yields_without_files(db, tmp_path):
    for _ in range(20):
        fact(db)
    with pytest.raises(InferenceDeferred):
        await views.refresh(db, SimpleNamespace(interactive=1), tmp_path)
    assert not list(tmp_path.rglob("*.md"))
    result = await views.refresh(db, SimpleNamespace(interactive=0), tmp_path)
    assert result["written"] == 2  # one fact and one daily view, not a full-memory export
    assert db.one("SELECT count(*) n FROM memory_view_queue WHERE kind='facts'")["n"] == 19


async def test_confirmed_commitments_and_important_meetings_only(db, tmp_path):
    db.set_state(views.FLAG, True)
    db.set_state("preferences.work_priorities", {"topics": ["LHA"]})
    for name, status in [("candidate", "candidate"), ("open", "open")]:
        db.execute(
            "INSERT INTO commitments(id,owner,text,confidence,status,source_id) VALUES(%s,'Demo',%s,0.9,%s,'demo:direct')",
            (uuid4(), name, status),
        )
    for identifier, title in [("important", "LHA review"), ("routine", "Ordinary sync")]:
        db.execute(
            "INSERT INTO records(id,kind,provider,title,occurred_at) VALUES(%s,'event','synthetic',%s,%s)",
            (identifier, title, datetime.now(timezone.utc)),
        )
    for _ in range(3):
        await views.refresh(db, SimpleNamespace(interactive=0), tmp_path)
    assert len(db.query("SELECT * FROM memory_view_exports WHERE kind='commitments'")) == 1
    assert db.query("SELECT entity_id FROM memory_view_exports WHERE kind='meetings'") == [
        {"entity_id": "important"}
    ]
