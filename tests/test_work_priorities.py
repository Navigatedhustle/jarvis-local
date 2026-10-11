import pytest

from jarvis.attention import briefing_fingerprint
from jarvis.intelligence import context_today, ingest_message, refresh_mail_priorities, triage


@pytest.mark.parametrize(
    "subject,topic",
    [
        ("ZGX Fury partner update", "ZGX Fury"),
        ("ZGX-Nano business", "ZGX Nano"),
        ("LHA planning", "LHA"),
        ("Lighthouse account update", "LHA"),
        ("Sandbox workshop", "Sandbox events"),
        ("Event planning for Sandbox", "Sandbox events"),
    ],
)
def test_explicit_priority_topic_is_important_not_automatically_actionable(subject, topic):
    result = triage({"subject": subject}, priorities={"topics": [topic]})
    assert result["priority"] == "high"
    assert result["actionable"] is False
    assert "Demo's priority topic: " + topic in result["factors"]


@pytest.mark.parametrize("subject", ["nano editor", "sandbox software isolation", "alphabet", "fury movie"])
def test_priority_topics_do_not_match_unrelated_terms(subject):
    assert (
        triage(
            {"subject": subject}, priorities={"topics": ["ZGX Nano", "ZGX Fury", "LHA", "Sandbox events"]}
        )["priority"]
        == "normal"
    )


def test_saved_preferences_and_verified_sender_survive_reingestion(db):
    priorities = {"topics": ["LHA"], "source": "demo:direct"}
    db.set_state("preferences.work_priorities", priorities)
    message = {
        "id": "priority-test",
        "subject": "Routine update",
        "from": {"emailAddress": {"name": "Test Manager", "address": "manager@example.test"}},
    }
    ingest_message(db, message)
    db.execute("UPDATE people SET vip=true WHERE id=%s", ("manager@example.test",))
    assert refresh_mail_priorities(db) == 1
    assert refresh_mail_priorities(db) == 0
    ingest_message(db, message)
    row = db.one("SELECT metadata FROM records WHERE id='graph:mail:priority-test'")
    assert row["metadata"]["triage"]["priority"] == "high"
    assert not row["metadata"]["triage"]["actionable"]
    # Display names are untrusted: another address cannot inherit VIP authority.
    message["id"] = "untrusted-sender"
    message["from"]["emailAddress"]["address"] = "unrelated@example.test"
    ingest_message(db, message)
    assert (
        db.one("SELECT metadata FROM records WHERE id='graph:mail:untrusted-sender'")["metadata"]["triage"][
            "priority"
        ]
        == "normal"
    )
    assert context_today(db)["work_priorities"] == priorities
    assert db.one("SELECT count(*) AS n FROM jobs")["n"] == 0
    assert db.one("SELECT count(*) AS n FROM approvals")["n"] == 0


@pytest.mark.parametrize("folder", ["sentitems", "drafts", "junkemail", "deleteditems"])
def test_refresh_preserves_folder_exclusions(db, folder):
    db.set_state("preferences.work_priorities", {"topics": ["LHA"]})
    ingest_message(db, {"id": folder, "subject": "LHA: please send an update"}, folder_role=folder)
    refresh_mail_priorities(db)
    result = db.one("SELECT metadata FROM records")["metadata"]["triage"]
    assert result["priority"] == "normal"
    assert not result["actionable"]


def test_work_preferences_invalidate_attention_cache():
    context = {"work_priorities": {"topics": []}}
    before = briefing_fingerprint(context)
    context["work_priorities"]["topics"].append("LHA")
    assert briefing_fingerprint(context) != before
