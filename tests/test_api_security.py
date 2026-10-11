from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from jarvis import api
from jarvis.memory import Memory
from jarvis.outlook import AuthRequired
from jarvis.policy import Approvals


@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setattr(api, "db", db)
    monkeypatch.setattr(api.settings, "api_token", "synthetic-test-token-that-is-over-32-characters")
    api.runtime["approvals"] = Approvals(db)
    monkeypatch.setitem(api.runtime, "memory", Memory(db, None))
    monkeypatch.setattr(api.settings, "ms_client_id", "")
    api.sessions.clear()
    api.login_attempts.clear()
    # No lifespan: tests inject isolated DB, never start real proactive workers.
    return TestClient(api.app)


def test_unauthenticated_and_host_origin_boundaries(client):
    for route in (
        "/brain/notes",
        "/brain/note?id=brain:missing",
        "/brain/graph",
        "/calendar/range?start=2026-09-01",
        "/outlook/write-capabilities",
        "/research",
    ):
        assert client.get(route).status_code == 401
    assert client.post("/mail/draft", json={"source_id": "missing"}).status_code == 401
    assert client.post("/research/refresh", json={}).status_code == 401
    assert client.post("/research/preferences", json={"daily_enabled": False}).status_code == 401
    assert client.post("/research/travel/clear", json={}).status_code == 401
    assert client.get("/today").status_code == 401
    assert client.get("/health").status_code == 401
    assert client.get("/system/workload").status_code == 401
    assert client.get("/command-center").status_code == 401
    assert client.get("/relationships").status_code == 401
    assert client.get("/relationships/detail?id=synthetic").status_code == 401
    assert client.get("/commitment-radar").status_code == 401
    assert client.get("/projects").status_code == 401
    assert client.get("/projects/synthetic").status_code == 401
    assert client.get("/meetings/options").status_code == 401
    assert client.get("/documents/insights?id=synthetic").status_code == 401
    assert client.get("/memory/graph").status_code == 401
    assert client.get("/memory/evidence?message_id=1").status_code == 401
    assert client.get("/writing").status_code == 401
    assert client.post("/writing", json={"mode": "leadership_brief", "topic": "Nano"}).status_code == 401
    assert client.get("/memory/graph/search?q=synthetic").status_code == 401
    assert client.get("/memory/consolidation").status_code == 401
    assert client.get("/memory/views").status_code == 401
    assert client.get("/memory/views/read?kind=facts&id=synthetic").status_code == 401
    assert client.post("/documents/notes", json={"id": "synthetic"}).status_code == 401
    assert (
        client.post("/projects/synthetic/review", json={"revision": 1, "status": "active"}).status_code == 401
    )
    assert client.get("/today", headers={"host": "attacker.example"}).status_code == 400
    assert (
        client.post(
            "/auth/login", json={"token": api.settings.api_token}, headers={"origin": "https://evil.example"}
        ).status_code
        == 403
    )
    assert client.get("/health/live").status_code == 200


def test_memory_graph_endpoints_are_read_only_bounded_and_recover_from_restart(client, db):
    assert client.post("/auth/login", json={"token": api.settings.api_token}).status_code == 200
    assert client.get("/memory/graph").json()["enabled"] is False
    db.set_state("feature.HPOS_MEMORY_GRAPH", True)
    response = client.get("/memory/graph")
    assert response.status_code == 200 and response.json()["nodes"][0]["id"] == "actor:demo"
    assert client.get("/memory/graph?limit=10000").status_code == 422
    assert client.get("/memory/graph?depth=3").status_code == 422
    assert client.get("/memory/graph?root=record:missing").status_code == 404
    assert client.get("/memory/graph/search?q=%20").status_code == 422
    api.sessions.clear()
    assert client.get("/memory/graph").status_code == 200
    assert not db.one("SELECT * FROM approvals")


def test_research_refresh_is_durable_bounded_and_preferences_authenticated(client, db):
    headers = {"authorization": "Bearer " + api.settings.api_token}
    assert client.get("/research", headers=headers).json()["weather"]["city"] == "Rio Rancho, NM"
    for _ in range(3):
        assert client.post("/research/refresh", json={}, headers=headers).status_code == 200
    assert db.one("SELECT count(*) AS n FROM jobs WHERE kind='research_brief'")["n"] == 1
    assert (
        client.post("/research/preferences", json={"daily_enabled": False}, headers=headers).status_code
        == 200
    )
    assert db.get_state("research.daily_enabled") is False
    assert not db.one("SELECT * FROM approvals")


def test_weather_travel_confirmation_is_local_expiring_and_source_bound(client, db):
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    from jarvis.travel_weather import context

    headers = {"authorization": "Bearer " + api.settings.api_token}
    now = datetime.now(ZoneInfo("America/Denver"))
    body = {
        "city": "San Jose, CA",
        "start": now.date().isoformat(),
        "end": (now + timedelta(days=3)).date().isoformat(),
        "source_id": "travel:test",
    }
    assert client.post("/research/travel", json=body, headers=headers).status_code == 404
    db.execute(
        "INSERT INTO records(id,kind,provider,title,body) VALUES('travel:test','email','synthetic','Travel itinerary','')"
    )
    assert client.post("/research/travel", json=body, headers=headers).status_code == 200
    assert context(db, now)["basis"] == "confirmed_travel_dates"
    assert context(db, now + timedelta(days=4))["basis"] == "home"
    assert (
        client.post("/research/travel", json={**body, "city": "127.0.0.1"}, headers=headers).status_code
        == 422
    )
    assert client.post("/research/travel/clear", json={}, headers=headers).status_code == 200
    assert context(db, now)["basis"] == "home"


def test_manual_reply_retry_and_admission_bound(client, db):
    headers = {"authorization": "Bearer " + api.settings.api_token}
    for i in range(4):
        db.execute(
            "INSERT INTO records(id,kind,provider,title,body) VALUES(%s,'email','synthetic','Question','Can you review?')",
            (f"reply:{i}",),
        )
    for i in range(3):
        assert (
            client.post("/mail/draft", json={"source_id": f"reply:{i}"}, headers=headers).status_code == 200
        )
    assert client.post("/mail/draft", json={"source_id": "reply:3"}, headers=headers).status_code == 409
    db.execute("UPDATE jobs SET status='failed',attempts=4 WHERE dedupe_key='manual-reply:reply:0'")
    assert client.post("/mail/draft", json={"source_id": "reply:0"}, headers=headers).status_code == 200
    job = db.one("SELECT status,attempts FROM jobs WHERE dedupe_key='manual-reply:reply:0'")
    assert job == {"status": "pending", "attempts": 0}


def test_response_evidence_requires_saved_assistant_identity_and_survives_restart(client, db):
    assert client.post("/auth/login", json={"token": api.settings.api_token}).status_code == 200
    assert client.get("/memory/evidence?message_id=1").json()["enabled"] is False
    db.set_state("feature.HPOS_RESPONSE_EVIDENCE", True)
    row = db.one(
        "INSERT INTO messages(conversation,role,content) VALUES('demo','assistant','A saved reply') RETURNING id"
    )
    path = f"/memory/evidence?message_id={row['id']}"
    assert client.get(path).json()["saved_source_count"] == 0
    api.sessions.clear()
    assert client.get(path).status_code == 200
    assert client.get("/memory/evidence?message_id=-1").status_code == 422
    assert client.get("/memory/evidence?message_id=999999999").status_code == 404


def test_executive_writing_cannot_be_converted_to_send_proposal(client, db):
    from psycopg.types.json import Jsonb

    from jarvis import executive

    assert client.post("/auth/login", json={"token": api.settings.api_token}).status_code == 200
    client.headers["x-jarvis-request"] = "1"
    assert client.get("/writing").json()["enabled"] is False
    db.set_state(executive.FLAG, True)
    response = client.post("/writing", json={"mode": "leadership_brief", "topic": "Nano"})
    assert response.status_code == 200
    identifier = response.json()["id"]
    # Even deliberately adding a real recipient and marking review complete cannot
    # turn an internal writing artifact into an outbound action.
    db.execute(
        "INSERT INTO records(id,kind,provider,title,metadata) VALUES('mail','email','synthetic','Source',%s)",
        (Jsonb({"from": {"emailAddress": {"address": "person@example.invalid"}}}),),
    )
    db.execute(
        "UPDATE records SET metadata=metadata || %s WHERE id=%s",
        (Jsonb({"source_id": "mail", "review_required": False}), identifier),
    )
    assert client.post(f"/drafts/{identifier}/propose").status_code == 409
    assert not db.one("SELECT * FROM approvals")
    assert client.post(f"/writing/{identifier}/cancel").json()["cancelled"]


def test_conversation_orb_flag_is_private_and_independent(client, db, monkeypatch):
    import httpx

    factory = httpx.AsyncClient
    monkeypatch.setattr(
        api.httpx,
        "AsyncClient",
        lambda **kw: factory(transport=httpx.MockTransport(lambda request: httpx.Response(200))),
    )
    assert client.post("/auth/login", json={"token": api.settings.api_token}).status_code == 200
    assert client.get("/health").json()["interface"] == {"orb": False, "response_evidence": False}
    db.set_state("feature.HPOS_CONVERSATION_ORB", True)
    assert client.get("/health").json()["interface"] == {"orb": True, "response_evidence": False}
    assert db.get_state("feature.HPOS_MEMORY_GRAPH", False) is False


def test_voice_reconnect_keeps_auth_across_restart_but_not_logout_or_rotation(client, db, monkeypatch):
    import json

    assert client.post("/auth/login", json={"token": api.settings.api_token}).status_code == 200
    cookie = client.cookies.get("jarvis_session")
    assert cookie not in json.dumps(db.query("SELECT * FROM state"), default=str)
    api.sessions.clear()  # Same durable state, new API process.
    assert client.get("/today").status_code == 200
    assert client.post("/auth/logout", headers={"X-Jarvis-Request": "1"}).status_code == 200
    client.cookies.set("jarvis_session", cookie)
    api.sessions.clear()
    assert client.get("/today").status_code == 401
    client.post("/auth/login", json={"token": api.settings.api_token})
    api.sessions.clear()
    monkeypatch.setattr(api.settings, "api_token", "rotated-synthetic-test-key-long-enough")
    assert client.get("/today").status_code == 401


def test_voice_lab_requires_auth_and_fixed_candidates(client, db, monkeypatch, tmp_path):
    monkeypatch.setattr(api.settings, "data_dir", tmp_path)
    assert client.get("/voice/lab").status_code == 401
    headers = {"authorization": "Bearer " + api.settings.api_token}
    assert len(client.get("/voice/lab", headers=headers).json()["profiles"]) == 4
    assert (
        client.post(
            "/voice/lab/generate", headers=headers, json={"candidate": "../../secrets", "script": "briefing"}
        ).status_code
        == 422
    )
    assert (
        client.post("/voice/lab/select", headers=headers, json={"candidate": "unapproved"}).status_code == 422
    )
    assert client.get("/voice/lab/audio/not-a-uuid", headers=headers).status_code == 422
    assert not list(tmp_path.iterdir())


def test_calendar_range_is_authenticated_ordered_and_dst_aware(client, db, monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 3, 8, 12, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr(api, "datetime", Clock)
    for identifier, date in [
        ("prior", "2026-03-08T06:59:00Z"),
        ("first", "2026-03-08T07:00:00Z"),
        ("last", "2026-03-09T05:59:00Z"),
        ("tomorrow", "2026-03-09T06:00:00Z"),
    ]:
        db.execute(
            "INSERT INTO records(id,kind,provider,title,occurred_at) VALUES (%s,'event','synthetic',%s,%s)",
            (identifier, identifier, date),
        )
    assert client.get("/calendar").status_code == 401
    headers = {"authorization": "Bearer " + api.settings.api_token}
    response = client.get("/calendar?days=1", headers=headers)
    assert response.status_code == 200
    assert [row["id"] for row in response.json()] == ["first", "last"]
    assert [row["id"] for row in client.get("/calendar?days=1&offset=1", headers=headers).json()] == [
        "tomorrow"
    ]
    assert client.get("/calendar?days=365", headers=headers).status_code == 422


def test_saved_mail_search_filters_and_source_reads_preserve_auth(client, db):
    from jarvis.intelligence import ingest_message

    for identifier, role in [("received", "received"), ("sent", "sentitems")]:
        ingest_message(
            db,
            {
                "id": identifier,
                "subject": "ZGX research review",
                "body": {"content": "Please review the research brief"},
                "from": {
                    "emailAddress": {"name": "Synthetic Colleague", "address": "colleague@example.invalid"}
                },
                "isRead": False,
            },
            folder_role=role,
        )
    headers = {"authorization": "Bearer " + api.settings.api_token}
    assert client.get("/mail").status_code == 401
    assert client.get("/documents").status_code == 401
    assert len(client.get("/mail?q=Colleague", headers=headers).json()) == 2
    assert [r["id"] for r in client.get("/mail?view=attention", headers=headers).json()] == [
        "graph:mail:received"
    ]
    assert [r["id"] for r in client.get("/mail?view=sent", headers=headers).json()] == ["graph:mail:sent"]
    assert client.get("/record?id=graph:mail:received").status_code == 401
    assert (
        client.get("/record?id=graph:mail:received", headers=headers).json()["title"] == "ZGX research review"
    )
    db.execute("UPDATE records SET deleted=true WHERE id='graph:mail:received'")
    assert client.get("/record?id=graph:mail:received", headers=headers).status_code == 404


def test_flagged_draft_requires_explicit_review_before_send_proposal(client, db):
    from psycopg.types.json import Jsonb

    from jarvis.drafts import revision
    from jarvis.intelligence import ingest_message

    ingest_message(
        db,
        {
            "id": "source",
            "subject": "Review",
            "from": {"emailAddress": {"address": "synthetic@example.invalid"}},
        },
    )
    db.execute(
        "INSERT INTO records(id,kind,provider,title,body,metadata) VALUES ('draft:review','draft','jarvis','Review','Unfinished [',%s)",
        (Jsonb({"source_id": "graph:mail:source", "review_required": True}),),
    )
    headers = {"authorization": "Bearer " + api.settings.api_token}
    assert client.post("/drafts/draft:review/propose", headers=headers).status_code == 409
    row = db.one("SELECT * FROM records WHERE id='draft:review'")
    response = client.post(
        "/drafts/draft:review/edit",
        headers=headers,
        json={
            "revision": revision(row),
            "title": "Reviewed subject",
            "body": "A complete reviewed response.",
        },
    )
    assert response.status_code == 200 and response.json()["sent"] is False
    assert client.post("/drafts/draft:review/propose", headers=headers).json()["status"] == "pending"
    assert db.one("SELECT count(*) n FROM approvals WHERE status='succeeded'")["n"] == 0


def test_commitment_status_is_filtered_before_paging(client, db):
    from uuid import uuid4

    for index in range(6):
        db.execute(
            "INSERT INTO commitments(id,owner,text,confidence,source_id,status) VALUES (%s,'Demo',%s,0.9,'synthetic',%s)",
            (uuid4(), str(index), "done" if index == 0 else "candidate"),
        )
    headers = {"authorization": "Bearer " + api.settings.api_token}
    assert len(client.get("/commitments?status=done&limit=1", headers=headers).json()) == 1
    first = client.get("/commitments?status=candidate&limit=2", headers=headers).json()
    second = client.get("/commitments?status=candidate&limit=2&offset=2", headers=headers).json()
    assert len(first) == len(second) == 2
    assert not {r["id"] for r in first}.intersection(r["id"] for r in second)


def test_cookie_csrf_and_logout(client):
    assert client.post("/auth/login", json={"token": api.settings.api_token}).status_code == 200
    assert client.get("/today").status_code == 200
    body = {"mode": "text_only", "until": "2026-10-01T15:00:00-06:00"}
    assert client.post("/communication", json=body).status_code == 403
    assert client.post("/communication", json=body, headers={"x-jarvis-request": "1"}).status_code == 200
    assert client.post("/auth/logout", headers={"x-jarvis-request": "1"}).status_code == 200
    assert client.get("/today").status_code == 401


def test_voice_requires_private_authenticated_origin(client):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/voice", headers={"origin": "http://127.0.0.1:13100"}):
            pass
    client.post("/auth/login", json={"token": api.settings.api_token})
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/voice", headers={"origin": "https://untrusted.invalid"}):
            pass


def test_unexpected_voice_failure_reports_error_and_accepts_next_turn(client, db, monkeypatch):
    import httpx

    factory = httpx.AsyncClient
    monkeypatch.setattr(
        api.httpx,
        "AsyncClient",
        lambda **kw: factory(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"status": "ready"}))
        ),
    )
    monkeypatch.setitem(api.runtime, "inference", SimpleNamespace(interactive=0))
    monkeypatch.setitem(api.runtime, "jarvis", SimpleNamespace())
    attempts = []

    async def preview(self, send, turn):
        attempts.append(turn)
        if len(attempts) == 1:
            raise RuntimeError("private failure detail")
        await send({"type": "done", "turn_id": turn})

    monkeypatch.setattr(api.Voice, "preview", preview)
    client.post("/auth/login", json={"token": api.settings.api_token})
    with client.websocket_connect("/voice", headers={"origin": "http://127.0.0.1:13100"}) as ws:
        assert ws.receive_json()["type"] == "hello"
        ws.send_text("preview")
        error = ws.receive_json()
        assert error["type"] == "error" and "private failure" not in error["text"]
        ws.send_text("preview")
        assert ws.receive_json()["type"] == "done"
    assert api.runtime["inference"].interactive == 0


def test_pending_action_cannot_execute_without_consent(client, db):
    action = Approvals(db).propose("email.send", {"message": {"body": "Synthetic"}})
    headers = {"authorization": "Bearer " + api.settings.api_token}
    response = client.post("/approvals/" + str(action["id"]) + "/execute", headers=headers)
    assert response.status_code == 409
    assert Approvals(db).get(action["id"])["status"] == "pending"


@pytest.mark.parametrize("recognized", ["", "A real interruption"])
def test_noise_preserves_pending_answer_but_recognized_speech_replaces_it(
    client, db, monkeypatch, recognized
):
    import asyncio

    import httpx

    release = asyncio.Event()
    cancelled = []

    async def handler(request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ready"})
        release.set()
        return httpx.Response(200, json={"text": recognized})

    factory = httpx.AsyncClient
    monkeypatch.setattr(
        api.httpx, "AsyncClient", lambda **kw: factory(transport=httpx.MockTransport(handler))
    )
    monkeypatch.setitem(api.runtime, "inference", SimpleNamespace(interactive=0))
    monkeypatch.setitem(api.runtime, "jarvis", SimpleNamespace())

    class Segment:
        speech = False
        completed = False
        completed_silence_samples = 0
        rate = 16000
        rms = 0.007

        def __init__(self, **kw):
            self.calls = 0

        def feed(self, data):
            self.calls += 1
            self.speech = self.calls == 1
            self.completed = self.calls == 2
            return self.speech, b"noise" if self.completed else None

    async def preview(self, send, turn):
        try:
            await send({"type": "state", "state": "thinking", "turn_id": turn})
            await release.wait()
            await send({"type": "done", "turn_id": turn})
        except asyncio.CancelledError:
            cancelled.append(turn)
            raise

    async def turn(self, pcm, send, actor, identifier, **kwargs):
        assert kwargs["recognized"][0] == recognized
        await send({"type": "done", "turn_id": identifier})

    monkeypatch.setattr(api, "Segmenter", Segment)
    monkeypatch.setattr(api.Voice, "preview", preview)
    monkeypatch.setattr(api.Voice, "turn", turn)
    client.post("/auth/login", json={"token": api.settings.api_token})
    with client.websocket_connect("/voice", headers={"origin": "http://127.0.0.1:13100"}) as ws:
        ws.receive_json()
        ws.send_text("preview")
        assert ws.receive_json()["state"] == "thinking"
        ws.send_bytes(bytes(640))
        assert ws.receive_json() == {"type": "speech", "active": True}
        ws.send_bytes(bytes(640))
        for _ in range(7):
            if ws.receive_json()["type"] == "done":
                break
        else:
            pytest.fail("Pending answer did not resume or get replaced")
    assert bool(cancelled) is bool(recognized)


@pytest.mark.parametrize(
    "failure", [RuntimeError("private implementation detail"), AuthRequired("private auth detail")]
)
def test_failed_manual_sync_records_safe_attention_state(client, db, monkeypatch, failure):
    monkeypatch.setitem(api.runtime, "sync", SimpleNamespace(tick=AsyncMock(side_effect=failure)))
    headers = {"authorization": "Bearer " + api.settings.api_token}
    response = client.post("/outlook/sync", headers=headers)
    assert response.status_code in (401, 409, 503)
    assert db.get_state("outlook.error")["error_class"] == type(failure).__name__
    assert "private implementation detail" not in response.text


def test_document_meeting_link_is_validated_and_preserves_source_date(client, db, monkeypatch):
    from datetime import timedelta

    headers = {"authorization": "Bearer " + api.settings.api_token}
    monkeypatch.setitem(api.runtime, "memory", SimpleNamespace(remember=AsyncMock(return_value={})))
    db.set_state("feature.HPOS_MEETING_INTELLIGENCE", True)
    when = datetime.now(timezone.utc) - timedelta(days=2)
    db.execute(
        "INSERT INTO records(id,kind,provider,title,occurred_at) VALUES('meeting','event','synthetic','Review',%s)",
        (when,),
    )
    response = client.post(
        "/documents",
        headers=headers,
        data={"meeting_id": "missing", "transcript": "true"},
        files={"file": ("notes.txt", b"Notes")},
    )
    assert response.status_code == 422
    response = client.post(
        "/documents",
        headers=headers,
        data={"meeting_id": "meeting", "transcript": "true"},
        files={"file": ("notes.txt", b"Matt agreed to review the plan.")},
    )
    assert response.status_code == 200
    row = db.one("SELECT * FROM records WHERE id=%s", (response.json()["id"],))
    assert row["kind"] == "transcript" and row["metadata"]["source_id"] == "meeting"
    assert row["metadata"]["trust"] == "untrusted" and row["occurred_at"] == when
    assert (
        client.get("/documents/insights", params={"id": row["id"]}, headers=headers).json()["status"]
        == "pending"
    )
    assert (
        client.post("/documents/notes", json={"id": row["id"]}, headers=headers).json()["status"] == "queued"
    )
    assert not db.one("SELECT * FROM approvals")


def test_brain_notes_authenticated_revision_download_and_restart(client, db, monkeypatch, tmp_path):
    monkeypatch.setattr(api.settings, "data_dir", tmp_path)
    headers = {"authorization": "Bearer " + api.settings.api_token}
    value = {"title": "Launch SOP", "body": "Check the approved milestones.", "kind": "sop"}
    response = client.post("/brain/notes", json=value, headers=headers)
    assert response.status_code == 200
    saved = response.json()
    read = client.get("/brain/note", params={"id": saved["id"]}, headers=headers).json()
    assert read["body"] == value["body"]
    response = client.get("/brain/note", params={"id": saved["id"], "download": "true"}, headers=headers)
    assert response.status_code == 200 and response.text.startswith("# Launch SOP")
    assert (
        client.post(
            "/brain/notes", json={**value, "id": saved["id"], "revision": "0" * 64}, headers=headers
        ).status_code
        == 409
    )
    api.sessions.clear()
    assert client.get("/brain/notes", headers=headers).json()[0]["id"] == saved["id"]


def test_calendar_grid_is_bounded_overlap_aware_and_honest_about_coverage(client, db):
    from jarvis.intelligence import ingest_event

    headers = {"authorization": "Bearer " + api.settings.api_token}
    ingest_event(
        db,
        {
            "id": "overnight",
            "subject": "Overnight",
            "start": {"dateTime": "2026-09-20T05:00:00Z"},
            "end": {"dateTime": "2026-09-20T09:00:00Z", "timeZone": "UTC"},
        },
        provider="synthetic",
    )
    data = client.get("/calendar/range?start=2026-09-20&days=7", headers=headers).json()
    assert data["partial_coverage"] and data["timezone"] == "America/Denver"
    assert len(data["events"]) == 1
    assert client.get("/calendar/range?start=2026-09-20&days=43", headers=headers).status_code == 422


def test_meeting_proposal_edit_and_deny_never_change_outlook(client, db):
    from datetime import timedelta

    headers = {"authorization": "Bearer " + api.settings.api_token}
    start = datetime.now(timezone.utc) + timedelta(days=1)
    payload = {
        "subject": "Private planning",
        "start": start.isoformat(),
        "end": (start + timedelta(minutes=20)).isoformat(),
        "attendees": [],
        "body": "Review agenda",
    }
    first = client.post("/calendar/proposals", json=payload, headers=headers).json()
    assert first["status"] == "pending"
    second = client.post(
        "/calendar/proposals",
        json={**payload, "subject": "Revised", "replaces": first["id"]},
        headers=headers,
    ).json()
    assert second["id"] != first["id"] and second["digest"] != first["digest"]
    assert runtime_status(db, first["id"]) == "superseded"
    assert (
        client.post(
            f"/approvals/{second['id']}/decision",
            json={"command": "DENY", "digest": second["digest"]},
            headers=headers,
        ).json()["status"]
        == "denied"
    )
    assert client.post(f"/approvals/{second['id']}/execute", json={}, headers=headers).status_code == 409
    assert not client.get("/outlook/write-capabilities", headers=headers).json()["can_execute"]


def runtime_status(db, identifier):
    return db.one("SELECT status FROM approvals WHERE id=%s", (identifier,))["status"]


def test_email_edits_supersede_and_outlook_save_cannot_send(client, db):
    from psycopg.types.json import Jsonb

    headers = {"authorization": "Bearer " + api.settings.api_token}
    db.execute(
        "INSERT INTO records(id,kind,provider,title,body,metadata) VALUES('mail:review','email','synthetic','Question','Evidence',%s)",
        (Jsonb({"from": {"emailAddress": {"address": "person@example.test"}}}),),
    )
    db.execute(
        "INSERT INTO records(id,kind,provider,title,body,metadata) VALUES('draft:review','draft','synthetic','Response','Reviewed text',%s)",
        (Jsonb({"source_id": "mail:review", "review_required": False}),),
    )
    first = client.post(
        "/drafts/draft:review/propose?destination=outlook_draft", json={}, headers=headers
    ).json()
    assert first["kind"] == "email.save_draft"
    assert (
        client.post(
            f"/approvals/{first['id']}/decision",
            json={"command": "APPROVE", "digest": first["digest"]},
            headers=headers,
        ).status_code
        == 200
    )
    edit = {
        "subject": "Revised",
        "body": "Reviewed revision",
        "recipients": ["explicit@example.test"],
        "digest": first["digest"],
    }
    replacement = client.post(f"/approvals/{first['id']}/edit-email", json=edit, headers=headers)
    assert replacement.status_code == 200
    new = replacement.json()
    assert new["kind"] == "email.save_draft" and new["status"] == "pending"
    assert runtime_status(db, first["id"]) == "superseded"
    assert client.post(f"/approvals/{first['id']}/edit-email", json=edit, headers=headers).status_code == 409
    assert client.post(f"/approvals/{new['id']}/execute", json={}, headers=headers).status_code == 409
    assert client.get("/outlook/write-capabilities", headers=headers).json()["executable_kinds"] == []


def test_manual_email_draft_requests_are_durable_idempotent_and_internal(client, db):
    headers = {"authorization": "Bearer " + api.settings.api_token}
    db.execute(
        "INSERT INTO records(id,kind,provider,title,body) VALUES('mail','email','synthetic','Question','Can you review?')"
    )
    for _ in range(2):
        result = client.post("/mail/draft", json={"source_id": "mail"}, headers=headers)
        assert result.status_code == 200 and not result.json()["sent"]
    assert db.one("SELECT count(*) n FROM jobs WHERE kind='manual_email'")["n"] == 1
    assert client.get("/mail/drafts/status").status_code == 401
    assert client.get("/mail/drafts/status", headers=headers).json()[0]["status"] == "pending"
    assert not db.one("SELECT * FROM approvals")
