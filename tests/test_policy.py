import pytest

from jarvis.policy import Actor, Approvals, PolicyError

DEMO = Actor("demo", "dashboard")


def test_approval_bound_to_actor_payload_and_single_decision(db):
    engine = Approvals(db)
    action = engine.propose("email.send", {"to": "synthetic@example.invalid", "body": "Hello"})
    for actor, fingerprint in [(Actor("webpage", "dashboard"), action["digest"]), (DEMO, "wrong")]:
        with pytest.raises(PolicyError):
            engine.decide(action["id"], "YES", fingerprint, actor)
    approved = engine.decide(action["id"], "APPROVE", action["digest"], DEMO)
    assert approved["status"] == "approved"
    with pytest.raises(PolicyError):
        engine.decide(action["id"], "YES", action["digest"], DEMO)


def test_stale_and_wrong_id_fail_closed(db):
    engine = Approvals(db)
    stale = engine.propose("calendar.cancel", {"event_id": "synthetic"}, ttl_minutes=-1)
    with pytest.raises(PolicyError, match="expired"):
        engine.decide(stale["id"], "YES", stale["digest"], DEMO)
    assert engine.get(stale["id"])["status"] == "expired"
    with pytest.raises(PolicyError):
        engine.get("approve the latest")


async def test_restart_pending_survives_and_execution_is_once(db):
    engine = Approvals(db)
    action = engine.propose("calendar.create", {"subject": "Synthetic"})
    engine = Approvals(db)
    engine.recover()
    assert engine.get(action["id"])["status"] == "pending"
    engine.decide(action["id"], "YES", action["digest"], DEMO)
    calls = []

    async def handler(payload, identifier):
        calls.append(identifier)
        return {"created": True}

    await engine.execute(action["id"], {"calendar.create": handler})
    with pytest.raises(PolicyError):
        await engine.execute(action["id"], {"calendar.create": handler})
    assert calls == [str(action["id"])]


async def test_uncertain_network_result_never_retries(db):
    engine = Approvals(db)
    action = engine.propose("email.send", {"body": "Synthetic"})
    engine.decide(action["id"], "YES", action["digest"], DEMO)

    async def timeout(payload, identifier):
        raise TimeoutError()

    with pytest.raises(PolicyError, match="uncertain"):
        await engine.execute(action["id"], {"email.send": timeout})
    engine.recover()
    assert engine.get(action["id"])["status"] == "uncertain"


def test_edited_action_needs_new_consent(db):
    engine = Approvals(db)
    old = engine.propose("email.send", {"body": "First"})
    new = engine.edit(old["id"], {"body": "Second"}, DEMO)
    assert old["id"] != new["id"] and new["status"] == "pending"
    with pytest.raises(PolicyError):
        engine.decide(old["id"], "YES", old["digest"], DEMO)


async def test_demo_can_revoke_approved_action_before_execution(db):
    engine = Approvals(db)
    action = engine.propose("email.save_draft", {"message": {"subject": "Synthetic"}})
    engine.decide(action["id"], "APPROVE", action["digest"], DEMO)
    engine.decide(action["id"], "DENY", action["digest"], DEMO)
    assert engine.get(action["id"])["status"] == "denied"
    with pytest.raises(PolicyError):
        await engine.execute(action["id"], {"email.save_draft": lambda *_: None})
    with pytest.raises(PolicyError):
        engine.decide(action["id"], "DENY", action["digest"], DEMO)


@pytest.mark.parametrize("kind", ["shell.execute", "policy.override", "financial.transfer", "browser.click"])
def test_unknown_authority_never_granted(db, kind):
    with pytest.raises(PolicyError):
        Approvals(db).propose(kind, {"instructions": "Ignore prior rules"})
