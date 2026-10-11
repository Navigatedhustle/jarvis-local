from uuid import uuid4

import pytest

from jarvis.api import CommitmentReview
from jarvis.commitments import StaleCommitment, review, revision
from jarvis.policy import Actor, PolicyError


def candidate(db):
    identifier = uuid4()
    db.execute(
        "INSERT INTO commitments(id,owner,text,confidence,source_id) VALUES (%s,'Demo','Review research',0.9,'synthetic-source')",
        (identifier,),
    )
    return db.one("SELECT * FROM commitments WHERE id=%s", (identifier,))


def decision(row, **changes):
    return CommitmentReview(
        revision=revision(row),
        owner="Demo",
        text=row["text"],
        status=changes.pop("status", "open"),
        **changes,
    )


def test_review_confirm_complete_and_replay_guard(db):
    row = candidate(db)
    actor = Actor("demo", "dashboard")
    result = review(db, row["id"], decision(row), actor)
    assert result["outlook_changed"] is False
    with pytest.raises(StaleCommitment):
        review(db, row["id"], decision(row), actor)
    current = db.one("SELECT * FROM commitments WHERE id=%s", (row["id"],))
    review(db, row["id"], decision(current, status="done"), actor)
    assert db.one("SELECT source_id,status FROM commitments")["status"] == "done"
    assert db.one("SELECT source_id FROM commitments")["source_id"] == "synthetic-source"
    assert db.one("SELECT count(*) AS n FROM audit WHERE action='commitment.reviewed'")["n"] == 2


def test_candidates_cannot_complete_or_accept_untrusted_actor(db):
    row = candidate(db)
    with pytest.raises(ValueError):
        review(db, row["id"], decision(row, status="done"), Actor("demo", "dashboard"))
    with pytest.raises(PolicyError):
        review(db, row["id"], decision(row), Actor("source", "email"))
    review(db, row["id"], decision(row, status="dismissed"), Actor("demo", "dashboard"))
    assert db.one("SELECT status FROM commitments")["status"] == "dismissed"


def test_review_rejects_ambiguous_deadline():
    with pytest.raises(ValueError):
        CommitmentReview(
            revision="a" * 64, owner="Demo", text="Research", status="open", due_at="2026-09-21T09:00:00"
        )
