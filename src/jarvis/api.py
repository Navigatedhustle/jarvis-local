import asyncio
import hashlib
import hmac
import json
import secrets
import time
from contextlib import asynccontextmanager, suppress
from datetime import date, datetime, timedelta, timezone
from typing import Literal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import httpx
from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from psycopg.types.json import Jsonb
from pydantic import AwareDatetime, BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import (
    content_packets,
    executive,
    memory_graph,
    memory_views,
    public_research,
    second_brain,
    travel_weather,
)
from . import projects as project_brains
from .browser_outlook import BrowserOutlook
from .command_center import read as read_command_center
from .commitment_radar import read as read_commitment_radar
from .commitments import StaleCommitment
from .commitments import review as review_commitment
from .commitments import revision as commitment_revision
from .config import settings
from .consolidation import status as consolidation_status
from .db import Database
from .documents import extract_text_isolated
from .drafts import edit as edit_draft
from .drafts import revision as draft_revision
from .inference import Inference
from .intelligence import context_today
from .meetings import prepare_meeting
from .memory import Memory
from .onboarding import FIELDS, READY, Setup, utcnow
from .orchestrator import Jarvis
from .outlook import AuthRequired, GraphProvider, MicrosoftAuth
from .outlook_actions import email_payload, meeting_payload
from .phone import Phone
from .policy import Actor, Approvals, PolicyError
from .proactive import Proactive
from .relationships import detail as relationship_detail
from .relationships import listing as relationship_listing
from .scheduler import Jobs, serve, serve_outlook
from .source_insights import read as read_source_insights
from .sync import OutlookSync
from .telegram import Telegram
from .voice import Segmenter, SpeechDetector, Voice, wav_bytes
from .voice_profiles import PROFILES, SCRIPTS, profile

db = None
runtime = {}
sessions = {}
session_checks = {}
login_attempts = {}
voice_lab_lock = asyncio.Lock()
ALLOWED_ORIGINS = {
    "http://127.0.0.1:13100",
    "http://localhost:13100",
    "http://127.0.0.1:3100",
    "http://127.0.0.1:18100",
    "http://127.0.0.1:8100",
}
if settings.mobile_url:
    ALLOWED_ORIGINS.add(settings.mobile_url)


@asynccontextmanager
async def lifespan(app):
    global db
    if len(settings.api_token) < 32:
        raise RuntimeError("A strong local API token is required")
    db = Database(settings.database_url)
    db.migrate()
    inference = Inference(settings, db=db)
    approvals = Approvals(db)
    approvals.recover()
    from .packet_schedule import recover as recover_packet_deliveries

    recover_packet_deliveries(db)
    memory = Memory(db, inference)
    auth = MicrosoftAuth(settings)
    graph = GraphProvider(auth)
    browser_outlook = BrowserOutlook(settings)
    runtime.update(
        inference=inference,
        approvals=approvals,
        memory=memory,
        auth=auth,
        graph=graph,
        jarvis=Jarvis(db, inference, memory, approvals),
        sync=OutlookSync(db, graph, inference, browser=browser_outlook),
        browser_outlook=browser_outlook,
    )
    telegram = Telegram(settings, db, runtime["jarvis"], approvals)
    if settings.ms_client_id and db.get_state("outlook.provider") != "browser":
        runtime["jarvis"].executors.update(
            {
                "email.send": graph.send_approved,
                "calendar.create": graph.calendar_create_approved,
                "calendar.move": graph.calendar_move_approved,
                "calendar.cancel": graph.calendar_cancel_approved,
            }
        )
    proactive = Proactive(settings, db, inference, runtime["jarvis"], telegram, Phone(settings, db, telegram))
    runtime.update(telegram=telegram, proactive=proactive)
    runtime["setup"] = Setup(settings, db, graph, auth, browser=browser_outlook)
    tasks = [
        asyncio.create_task(serve(db, inference, runtime["sync"], proactive.handlers())),
        asyncio.create_task(serve_outlook(db, inference, runtime["sync"])),
        asyncio.create_task(telegram.poll()),
    ]
    yield
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    db.close()


app = FastAPI(title="JARVIS", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])


@app.middleware("http")
async def protect(request, call_next):
    origin = request.headers.get("origin")
    if origin and origin not in ALLOWED_ORIGINS:
        return JSONResponse({"detail": "Untrusted origin"}, status_code=403)
    try:
        length = int(request.headers.get("content-length", "0"))
    except ValueError:
        return JSONResponse({"detail": "Invalid content length"}, status_code=400)
    if length > 2_000_000:
        return JSONResponse({"detail": "Request too large"}, status_code=413)
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def session_expiry(digest):
    now = time.time()
    key_id = hashlib.sha256(settings.api_token.encode()).hexdigest()
    cached = sessions.get(digest, 0)
    checked, checked_key = session_checks.get(digest, (0, None))
    if cached > now and now - checked < 2 and checked_key == key_id:
        return cached
    saved = db.get_state("dashboard.session." + digest, {}) if db else {}
    if saved.get("key_id") == key_id and saved.get("expires", 0) > now:
        sessions[digest] = saved["expires"]
        session_checks[digest] = (now, key_id)
        return saved["expires"]
    sessions.pop(digest, None)
    session_checks.pop(digest, None)
    return 0


def authenticate(request: Request):
    bearer = request.headers.get("authorization", "")
    if bearer.startswith("Bearer ") and hmac.compare_digest(bearer[7:], settings.api_token):
        return Actor("demo", "dashboard")
    session = request.cookies.get("jarvis_session", "")
    if session:
        expiry = session_expiry(hashlib.sha256(session.encode()).hexdigest())
        if expiry > time.time():
            if request.method not in ("GET", "HEAD") and request.headers.get("x-jarvis-request") != "1":
                raise HTTPException(403, "Missing CSRF protection header")
            return Actor("demo", "dashboard")
    raise HTTPException(401, "Sign in to JARVIS")


@app.exception_handler(PolicyError)
async def policy_error(request, exc):
    return JSONResponse({"detail": str(exc)}, status_code=409)


@app.exception_handler(RequestValidationError)
async def input_error(request, exc):
    # Framework validation normally echoes rejected input; setup/auth input can contain credentials.
    return JSONResponse(
        {"detail": "Check the required fields and their allowed length or format."}, status_code=422
    )


@app.exception_handler(AuthRequired)
async def auth_error(request, exc):
    return JSONResponse({"detail": str(exc)}, status_code=503)


class Login(BaseModel):
    token: str = Field(min_length=1, max_length=200)


@app.post("/auth/login")
def login(body: Login, request: Request, response: Response):
    key = request.client.host
    attempts = [t for t in login_attempts.get(key, []) if time.time() - t < 60]
    login_attempts[key] = attempts
    if len(attempts) >= 8:
        raise HTTPException(429, "Wait a minute before trying again")
    attempts.append(time.time())
    if not hmac.compare_digest(body.token, settings.api_token):
        raise HTTPException(401, "Incorrect access token")
    value = secrets.token_urlsafe(48)
    digest = hashlib.sha256(value.encode()).hexdigest()
    expiry = time.time() + 8 * 3600
    sessions[digest] = expiry
    db.set_state(
        "dashboard.session." + digest,
        {"expires": expiry, "key_id": hashlib.sha256(settings.api_token.encode()).hexdigest()},
    )
    response.set_cookie(
        "jarvis_session",
        value,
        httponly=True,
        samesite="strict",
        max_age=8 * 3600,
        secure=bool(settings.mobile_url and request.headers.get("origin") == settings.mobile_url),
    )
    return {"authenticated": True}


@app.get("/auth/phone")
def phone_access(actor=Depends(authenticate)):
    from .mobile_access import devices

    return {
        "url": settings.mobile_url,
        "configured": bool(settings.mobile_url),
        "devices": devices(db, settings),
    }


class PhoneDevice(BaseModel):
    device_id: UUID


@app.post("/auth/phone/revoke")
def phone_revoke(body: PhoneDevice, actor=Depends(authenticate)):
    from .mobile_access import revoke

    for digest in revoke(db, body.device_id):
        sessions.pop(digest, None)
        session_checks.pop(digest, None)
    return {"revoked": True}


@app.post("/auth/phone/pair")
def phone_pair(actor=Depends(authenticate)):
    from .mobile_access import issue

    return issue(db, settings)


class PairCode(BaseModel):
    code: str = Field(min_length=1, max_length=24)
    remember: bool = False


@app.post("/auth/phone/redeem")
def phone_redeem(body: PairCode, request: Request, response: Response):
    from .mobile_access import redeem

    if (
        not settings.mobile_url
        or request.headers.get("origin") != settings.mobile_url
        or request.headers.get("x-jarvis-request") != "1"
    ):
        raise HTTPException(403, "Use the private HTTPS phone address to pair")
    value = redeem(db, settings, body.code, remember=body.remember)
    response.set_cookie(
        "jarvis_session",
        value,
        httponly=True,
        secure=True,
        samesite="strict",
        max_age=30 * 86400 if body.remember else 8 * 3600,
    )
    return {"authenticated": True}


@app.post("/auth/logout")
def logout(request: Request, response: Response, actor=Depends(authenticate)):
    digest = hashlib.sha256(request.cookies.get("jarvis_session", "").encode()).hexdigest()
    sessions.pop(digest, None)
    session_checks.pop(digest, None)
    db.execute("DELETE FROM state WHERE key=%s", ("dashboard.session." + digest,))
    response.delete_cookie("jarvis_session")
    return {"authenticated": False}


@app.get("/health/live")
def live():
    return {"status": "alive"}


@app.get("/health")
async def health(actor=Depends(authenticate)):
    async def check(name, url):
        try:
            async with httpx.AsyncClient(timeout=2) as client:
                r = await client.get(url + "/health")
                return name, "ready" if r.is_success else "unavailable"
        except httpx.HTTPError:
            return name, "unavailable"

    models = dict(
        await asyncio.gather(
            *[
                check(name, getattr(settings, name + "_url"))
                for name in ("primary", "background", "embedding", "reranker")
            ]
        )
    )
    last_sync = db.get_state("outlook.last_sync")
    return {
        "database": bool(db.one("SELECT 1 AS ok")),
        "interface": {
            "orb": bool(db.get_state("feature.HPOS_CONVERSATION_ORB", False)),
            "response_evidence": bool(db.get_state("feature.HPOS_RESPONSE_EVIDENCE", False)),
        },
        "models": models,
        "demo_mode": db.get_state("demo_mode", False),
        "outlook": {
            "configured": bool(settings.ms_client_id) or db.get_state("outlook.provider") == "browser",
            "provider": db.get_state("outlook.provider", "graph"),
            "last_sync": last_sync,
            "stale": not last_sync
            or (datetime.now(timezone.utc) - datetime.fromisoformat(last_sync)).total_seconds() > 900,
            "error": db.get_state("outlook.error"),
        },
        "telegram": {"configured": bool(settings.telegram_token and settings.telegram_user_id)},
        "phone": {"configured": bool(settings.twilio_account_sid and settings.demo_phone)},
        "voice": {
            "status": "See local speech service checks",
            "stt": (await check("stt", "http://127.0.0.1:8185"))[1],
            "tts": (await check("tts", "http://127.0.0.1:8186"))[1],
        },
    }


@app.get("/today")
def today(actor=Depends(authenticate)):
    return context_today(db)


@app.get("/system/workload")
def workload_status(actor=Depends(authenticate)):
    return runtime["inference"].workload.status()


@app.get("/command-center")
def command_center(actor=Depends(authenticate)):
    return read_command_center(db)


def setup_provider(provider):
    if provider not in FIELDS:
        raise HTTPException(404, "Unknown connection")
    return runtime["setup"]


async def setup_confirmation(request):
    try:
        body = await request.json()
        if not isinstance(body, dict) or body.get("confirm") is not True:
            raise ValueError()
        return body
    except ValueError:
        raise HTTPException(422, "Confirm this specific setup action before continuing") from None


@app.get("/setup")
async def setup_status(actor=Depends(authenticate)):
    return await runtime["setup"].snapshot(await health(actor))


@app.post("/setup/{provider}/test")
async def setup_test(provider: str, actor=Depends(authenticate)):
    return await setup_provider(provider).test(provider)


@app.post("/setup/{provider}/configure")
async def setup_configure(provider: str, request: Request, actor=Depends(authenticate)):
    setup = setup_provider(provider)
    body = await setup_confirmation(request)
    try:
        return await setup.save(provider, body.get("values"))
    except ValueError:
        # Never echo invalid credentials, even in validation errors.
        raise HTTPException(
            422,
            "Check every required field and the format shown in the setup guide. Saved credentials may be left blank.",
        ) from None


async def webhook_service(command):
    # Fixed service and verbs only; this is not a model/shell tool.
    process = await asyncio.create_subprocess_exec(
        "systemctl",
        "--user",
        command,
        "jarvis-webhooks.service",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        return await asyncio.wait_for(process.wait(), timeout=15) == 0
    except TimeoutError:
        process.kill()
        await process.wait()
        return False


@app.post("/setup/{provider}/enable")
async def setup_enable(provider: str, request: Request, actor=Depends(authenticate)):
    setup = setup_provider(provider)
    await setup_confirmation(request)
    result = await setup.test(provider)
    if result["state"] != READY:
        raise HTTPException(409, "Test and authorize this connection successfully before enabling it.")
    if provider == "twilio" and not await webhook_service("restart"):
        raise HTTPException(
            503,
            "The private phone gateway could not start. Configuration is preserved; check Advanced Details.",
        )
    db.set_state("setup.enabled." + provider, True)
    if provider == "outlook" and db.get_state("outlook.provider") != "browser":
        runtime["jarvis"].executors.update(
            {
                "email.send": runtime["graph"].send_approved,
                "calendar.create": runtime["graph"].calendar_create_approved,
                "calendar.move": runtime["graph"].calendar_move_approved,
                "calendar.cancel": runtime["graph"].calendar_cancel_approved,
            }
        )
    db.audit("setup.enabled", provider, actor="demo", source="dashboard")
    return {"enabled": True}


@app.post("/outlook/browser/select")
async def select_browser(request: Request, actor=Depends(authenticate)):
    await setup_confirmation(request)
    db.set_state("outlook.provider", "browser")
    db.set_state("setup.enabled.outlook", False)
    db.set_state("setup.check.outlook", {})
    for name in ("email.send", "calendar.create", "calendar.move", "calendar.cancel"):
        runtime["jarvis"].executors.pop(name, None)
    db.audit("setup.browser_selected", "outlook", actor="demo", source="dashboard")
    return {
        "selected": "browser",
        "enabled": False,
        "message": "Nano browser selected. Test access, then enable read-only synchronization.",
    }


@app.post("/setup/{provider}/disconnect")
async def setup_disconnect(provider: str, request: Request, actor=Depends(authenticate)):
    setup = setup_provider(provider)
    await setup_confirmation(request)
    result = await setup.disconnect(provider)
    if provider == "twilio":
        result["gateway_stopped"] = await webhook_service("stop")
    return result


@app.post("/setup/twilio/test-call")
async def setup_test_call(request: Request, actor=Depends(authenticate)):
    await setup_confirmation(request)
    result = await runtime["setup"].test("twilio")
    if result["state"] != READY or not db.get_state("setup.enabled.twilio", False):
        raise HTTPException(409, "Verify and enable the Twilio connection before requesting a test call.")
    last = db.get_state("setup.last_test_call")
    if last and (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() < 60:
        raise HTTPException(429, "Wait a minute before requesting another call.")
    from .scheduler import communication_allowed

    if not communication_allowed(db, "phone"):
        raise HTTPException(409, "Your quiet-hours or demo-mode rule currently blocks phone calls.")
    db.set_state("setup.last_test_call", utcnow())
    text = "Demo, this is JARVIS. This call is checking your private phone connection. After this message, ask me a short question so we can verify both directions."
    async with httpx.AsyncClient(timeout=90) as client:
        audio = await client.post("http://127.0.0.1:8186/synthesize", json={"text": text})
        audio.raise_for_status()
    identifier, nonce = uuid4().hex, secrets.token_urlsafe(32)
    folder = settings.data_dir / "briefings"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / (identifier + ".wav")).write_bytes(audio.content)
    db.set_state("audio." + nonce, {"file": identifier + ".wav", "created_at": utcnow()})
    key = "setup.briefing." + identifier
    db.set_state(key, {"text": text, "audio_url": settings.public_webhook_url + "/twilio/audio/" + nonce})
    result = await runtime["proactive"].phone.call_demo(key)
    db.audit("setup.test_call_requested", "twilio", actor="demo", source="dashboard")
    return {
        "requested": bool(result.get("call_sid")),
        "message": "Answer the call and ask JARVIS a short question. Refresh setup after hearing its response.",
    }


class Chat(BaseModel):
    text: str = Field(min_length=1, max_length=16000)


@app.get("/content-packets")
def content_packet_status(actor=Depends(authenticate)):
    return content_packets.listing(db)


class PacketPreferences(BaseModel):
    enabled: bool


@app.post("/content-packets/preferences")
def packet_preferences(body: PacketPreferences, actor=Depends(authenticate)):
    actor.require_demo()
    db.set_state("content_packets.enabled", body.enabled)
    db.audit(
        "content_packet.schedule_changed",
        details={"enabled": body.enabled},
        actor=actor.identity,
        source=actor.channel,
    )
    return {"enabled": body.enabled}


@app.post("/content-packets/delivery-preferences")
def packet_delivery_preferences(body: PacketPreferences, actor=Depends(authenticate)):
    from .packet_schedule import STATE, grant_status

    actor.require_demo()
    if body.enabled and not grant_status(db)["validated"]:
        raise HTTPException(409, "Delivery requires an authorized, validated self-delivery policy")
    grant = db.get_state(STATE, {})
    grant["enabled"] = body.enabled
    db.set_state(STATE, grant)
    db.audit(
        "packet_delivery.preference_changed",
        details={"enabled": body.enabled},
        actor=actor.identity,
        source=actor.channel,
    )
    return grant_status(db)


@app.post("/content-packets/prepare")
def prepare_packet(actor=Depends(authenticate)):
    now = datetime.now(timezone.utc)
    edition = now.astimezone(ZoneInfo("America/Denver")).date().isoformat()
    content_packets.admit(db, Jobs(db), edition, now)
    # Explicit manual retry of an internal-only failed stage; never resets a successful packet.
    db.execute(
        "UPDATE jobs SET status='pending',attempts=0,error=NULL,due_at=now() WHERE dedupe_key=%s AND status='failed'",
        ("content_packet:" + edition,),
    )
    db.audit("content_packet.requested", edition, actor=actor.identity, source=actor.channel)
    return {"edition": edition, "sent": False}


@app.get("/content-packets/{edition}/download/{asset}")
def download_packet(edition: date, asset: Literal["pdf", "image", "copy"], actor=Depends(authenticate)):
    row = db.one("SELECT * FROM content_packets WHERE edition=%s", (edition,))
    if not row or row["status"] != "ready":
        raise HTTPException(404, "This packet is not ready")
    name, media = {
        "pdf": ("review.pdf", "application/pdf"),
        "image": ("visual.png", "image/png"),
        "copy": ("linkedin.txt", "text/plain; charset=utf-8"),
    }[asset]
    path = content_packets.directory(settings, edition.isoformat()) / name
    if not path.is_file():
        raise HTTPException(409, "Artifact needs recovery; saved research is preserved")
    expected = row["content"].get("pdf" if asset == "pdf" else "image", {}).get("sha256")
    if asset != "copy" and content_packets.digest_file(path) != expected:
        raise HTTPException(409, "Artifact checksum changed; download refused")
    return FileResponse(path, media_type=media, filename=f"JARVIS-{edition}-{name}")


@app.post("/content-packets/{edition}/propose-delivery")
def propose_packet_delivery(edition: date, actor=Depends(authenticate)):
    from .packet_delivery import proposal

    actor.require_demo()
    row = db.one("SELECT * FROM content_packets WHERE edition=%s", (edition,))
    if not row:
        raise HTTPException(404, "Packet not found")
    try:
        payload = proposal(settings, row)
    except ValueError as error:
        raise HTTPException(409, str(error)) from None
    with db.transaction() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("packet-delivery:" + str(edition),))
        previous = conn.execute(
            "SELECT * FROM approvals WHERE kind='email.send' AND payload->'packet'->>'edition'=%s AND status NOT IN ('denied','expired','superseded') ORDER BY created_at DESC LIMIT 1",
            (str(edition),),
        ).fetchone()
        if previous:
            return previous
        return runtime["approvals"].propose("email.send", payload, ttl_minutes=24 * 60, actor=actor.identity)


@app.get("/research")
def research_status(actor=Depends(authenticate)):
    return {
        **public_research.brief_state(db),
        "weather": travel_weather.context(db),
        "job": db.one(
            "SELECT status,error,locked_at FROM jobs WHERE kind='research_brief' ORDER BY due_at DESC LIMIT 1"
        ),
    }


@app.post("/research/refresh")
def research_refresh(actor=Depends(authenticate)):
    with db.transaction() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(739201)")
        job = conn.execute(
            "SELECT id,status FROM jobs WHERE kind='research_brief' AND status IN ('pending','running') LIMIT 1"
        ).fetchone()
        if job:
            return {"status": job["status"]}
        # One durable request per minute, even with repeated clicks or multiple sessions.
        now = datetime.now(timezone.utc)
        key = "research_manual:" + str(int(now.timestamp()) // 60)
        previous = conn.execute("SELECT status FROM jobs WHERE dedupe_key=%s", (key,)).fetchone()
        if previous:
            return {"status": previous["status"]}
        conn.execute(
            "INSERT INTO jobs(id,kind,dedupe_key,due_at,payload,priority) VALUES (%s,'research_brief',%s,%s,%s,5)",
            (uuid4(), key, now, Jsonb({})),
        )
        db.audit("research.refresh_requested", actor=actor.identity, source=actor.channel, conn=conn)
    return {"status": "queued"}


class ResearchPreferences(BaseModel):
    daily_enabled: bool


class WeatherTravelPlan(BaseModel):
    city: str = Field(min_length=3, max_length=70)
    start: date
    end: date
    source_id: str = Field(min_length=1, max_length=500)


@app.post("/research/travel")
def confirm_weather_travel(body: WeatherTravelPlan, actor=Depends(authenticate)):
    today = datetime.now(ZoneInfo("America/Denver")).date()
    if (
        body.city not in travel_weather.CITIES.values()
        or body.end < body.start
        or (body.end - body.start).days > 31
        or body.end < today
        or body.start > today + timedelta(days=366)
    ):
        raise HTTPException(422, "Choose a supported city and a current or upcoming trip of at most 31 days.")
    if not db.one(
        "SELECT id FROM records WHERE id=%s AND kind IN ('email','event') AND NOT deleted", (body.source_id,)
    ):
        raise HTTPException(404, "Travel evidence is no longer available")
    value = {
        "city": body.city,
        "start": body.start.isoformat(),
        "end": body.end.isoformat(),
        "source_id": body.source_id,
    }
    db.set_state("research.travel_plan", value)
    db.audit(
        "research.travel_confirmed", body.source_id, details=value, actor=actor.identity, source=actor.channel
    )
    return value


@app.post("/research/travel/clear")
def clear_weather_travel(actor=Depends(authenticate)):
    db.set_state("research.travel_plan", None)
    db.audit("research.travel_cleared", actor=actor.identity, source=actor.channel)
    return {"cleared": True}


@app.post("/research/preferences")
def research_preferences(body: ResearchPreferences, actor=Depends(authenticate)):
    db.set_state("research.daily_enabled", body.daily_enabled)
    db.audit(
        "research.preferences_changed", details=body.model_dump(), actor=actor.identity, source=actor.channel
    )
    return {"daily_enabled": body.daily_enabled}


@app.post("/chat")
async def chat(body: Chat, actor=Depends(authenticate)):
    return await runtime["jarvis"].reply(body.text, actor=actor)


@app.get("/chat")
def chat_history(actor=Depends(authenticate)):
    return list(
        reversed(
            db.query(
                "SELECT id,role,content,metadata,created_at FROM messages WHERE conversation='demo' ORDER BY id DESC LIMIT 50"
            )
        )
    )


@app.get("/records/{kind}")
def records(kind: str, actor=Depends(authenticate)):
    if kind not in {"email", "event", "draft", "document", "transcript"}:
        raise HTTPException(404)
    rows = db.query(
        "SELECT id,title,body,metadata,occurred_at FROM records WHERE kind=%s AND NOT deleted ORDER BY "
        + ("updated_at DESC,id" if kind == "draft" else "occurred_at DESC NULLS LAST,id")
        + " LIMIT 100",
        (kind,),
    )
    if kind == "draft":
        for row in rows:
            row["revision"] = draft_revision(row)
    return rows


@app.get("/people")
def people(actor=Depends(authenticate)):
    return db.query("SELECT * FROM people ORDER BY vip DESC,last_interaction DESC NULLS LAST LIMIT 200")


@app.get("/projects")
def projects(actor=Depends(authenticate)):
    return project_brains.listing(db)


@app.get("/projects/{identifier}")
def project(identifier: str, actor=Depends(authenticate)):
    try:
        return project_brains.detail(db, identifier)
    except LookupError as error:
        raise HTTPException(404, str(error)) from None


class ProjectReview(BaseModel):
    revision: int = Field(ge=1)
    status: Literal["active", "archived"]


@app.post("/projects/{identifier}/review")
def review_project(identifier: str, body: ProjectReview, actor=Depends(authenticate)):
    try:
        return project_brains.review(db, identifier, body.revision, body.status, "demo")
    except ValueError as error:
        raise HTTPException(409, str(error)) from None


@app.get("/relationships")
def relationships(actor=Depends(authenticate)):
    return relationship_listing(db)


@app.get("/relationships/detail")
def relationship(id: str = Query(max_length=320), actor=Depends(authenticate)):
    try:
        return relationship_detail(db, id)
    except LookupError as error:
        raise HTTPException(404, str(error)) from None


@app.get("/commitment-radar")
def commitment_radar(
    view: Literal[
        "due_today", "due_soon", "overdue", "waiting", "at_risk", "candidate", "unassigned", "open"
    ] = "at_risk",
    actor=Depends(authenticate),
):
    return read_commitment_radar(db, view)


@app.get("/mail")
def mail(
    q: str = Query("", max_length=300),
    view: Literal["all", "attention", "unread", "sent"] = "all",
    actor=Depends(authenticate),
):
    conditions = ["kind='email'", "NOT deleted"]
    params = []
    if view == "sent":
        conditions.append("metadata->>'folder_role'='sentitems'")
    elif view in {"attention", "unread"}:
        conditions.append(
            "coalesce(metadata->>'folder_role','') NOT IN ('sentitems','drafts','deleteditems','junkemail')"
        )
        conditions.append(
            "metadata->'triage'->>'priority'='high'" if view == "attention" else "metadata->>'isRead'='false'"
        )
    if q.strip():
        conditions.append(
            "(search @@ websearch_to_tsquery('english',%s) OR metadata->'from'->'emailAddress'->>'name' ILIKE %s OR metadata->'from'->'emailAddress'->>'address' ILIKE %s)"
        )
        literal = "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        params.extend([q.strip(), literal, literal])
    return db.query(
        "SELECT id,title,body,metadata,occurred_at FROM records WHERE "
        + " AND ".join(conditions)
        + " ORDER BY occurred_at DESC NULLS LAST LIMIT 100",
        tuple(params),
    )


@app.get("/documents")
def document_records(q: str = Query("", max_length=300), actor=Depends(authenticate)):
    return db.query(
        """SELECT id,title,body,metadata,occurred_at FROM records
        WHERE kind IN ('document','transcript') AND NOT deleted
        AND (%s='' OR search @@ websearch_to_tsquery('english',%s))
        ORDER BY updated_at DESC LIMIT 100""",
        (q.strip(), q.strip()),
    )


class BrainNote(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=50000)
    kind: Literal["sop", "framework", "working_note", "reference"]
    id: str | None = Field(default=None, max_length=50)
    revision: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


@app.get("/brain/notes")
def brain_notes(q: str = Query("", max_length=300), kind: str = "", actor=Depends(authenticate)):
    try:
        return second_brain.listing(db, q, kind)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None


@app.post("/brain/notes")
def save_brain_note(body: BrainNote, actor=Depends(authenticate)):
    try:
        return second_brain.save(
            db,
            settings.data_dir / "second_brain",
            body.title,
            body.body,
            body.kind,
            actor,
            identifier=body.id,
            expected=body.revision,
        )
    except ValueError as error:
        raise HTTPException(409, str(error)) from None


@app.get("/brain/note")
def brain_note(id: str = Query(max_length=50), download: bool = False, actor=Depends(authenticate)):
    try:
        row = second_brain.read(db, id)
        if download:
            path = second_brain.materialize(settings.data_dir / "second_brain", row)
            return Response(
                path.read_bytes(),
                media_type="text/markdown",
                headers={"Content-Disposition": 'attachment; filename="jarvis-note.md"'},
            )
        return {**row, "revision": second_brain.revision(row)}
    except LookupError as error:
        raise HTTPException(404, str(error)) from None
    except ValueError as error:
        raise HTTPException(409, str(error)) from None


@app.get("/brain/graph")
def brain_graph(actor=Depends(authenticate)):
    return second_brain.graph(db)


@app.get("/calendar/range")
def calendar_window(start: date, days: int = Query(7, ge=1, le=42), actor=Depends(authenticate)):
    zone = ZoneInfo("America/Denver")
    beginning = datetime.combine(start, datetime.min.time(), tzinfo=zone)
    end = beginning + timedelta(days=days)
    rows = db.query(
        """SELECT id,title,body,metadata,occurred_at FROM records
        WHERE kind='event' AND NOT deleted AND occurred_at >= %s AND occurred_at < %s
        ORDER BY occurred_at,title LIMIT 501""",
        (beginning - timedelta(days=7), end),
    )
    events = []
    for row in rows[:500]:
        value = row["metadata"].get("end", {})
        try:
            finish = datetime.fromisoformat(value.get("dateTime", "").replace("Z", "+00:00"))
            if finish.tzinfo is None:
                finish = finish.replace(tzinfo=ZoneInfo(value.get("timeZone", "UTC")))
        except (ValueError, KeyError):
            finish = row["occurred_at"] + timedelta(minutes=30)
        if finish > beginning:
            events.append({**row, "ends_at": finish})
    return {
        "events": events,
        "start": beginning,
        "end": end,
        "timezone": "America/Denver",
        "last_sync": db.get_state("outlook.last_sync"),
        "partial_coverage": True,
        "truncated": len(rows) > 500,
    }


class CalendarProposal(BaseModel):
    subject: str = Field(min_length=1, max_length=200)
    start: AwareDatetime
    end: AwareDatetime
    attendees: list[str] = Field(default_factory=list, max_length=20)
    body: str = Field(default="", max_length=10000)
    replaces: UUID | None = None


@app.post("/calendar/proposals")
def propose_calendar(body: CalendarProposal, actor=Depends(authenticate)):
    try:
        payload = meeting_payload(body.subject, body.start, body.end, body.attendees, body.body)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    if body.replaces:
        old = runtime["approvals"].get(body.replaces)
        if old["kind"] != "calendar.create":
            raise HTTPException(409, "This action is not a meeting proposal")
        return runtime["approvals"].edit(body.replaces, payload, actor)
    return runtime["approvals"].propose("calendar.create", payload, actor="demo")


@app.get("/outlook/write-capabilities")
def outlook_write_capabilities(actor=Depends(authenticate)):
    graph = bool(settings.ms_client_id and db.get_state("outlook.provider") != "browser")
    return {
        "provider": "graph" if graph else "browser",
        "can_execute": graph,
        "approvals_enabled": True,
        "executable_kinds": [
            "email.send",
            "email.save_draft",
            "calendar.create",
            "calendar.move",
            "calendar.cancel",
        ]
        if graph
        else [],
        "message": "Microsoft write consent is required; every action needs approval."
        if graph
        else "Write approvals are enabled. Outlook browser execution is awaiting controlled validation. You can review, edit, approve or deny exact proposals; nothing is sent or scheduled automatically.",
    }


class MailDraftRequest(BaseModel):
    source_id: str = Field(min_length=1, max_length=4096)


@app.get("/mail/drafts/status")
def mail_draft_status(actor=Depends(authenticate)):
    return db.query("""SELECT r.id AS source_id,r.title,j.status FROM jobs j
        JOIN records r ON r.id=j.payload->>'source_id' AND NOT r.deleted
        WHERE j.kind='manual_email' AND j.status IN ('pending','running','failed')
        ORDER BY j.due_at DESC LIMIT 20""")


@app.post("/mail/draft")
def request_mail_draft(body: MailDraftRequest, actor=Depends(authenticate)):
    row = db.one("SELECT id FROM records WHERE id=%s AND kind='email' AND NOT deleted", (body.source_id,))
    if not row:
        raise HTTPException(404, "Saved email not found")
    identifier = "draft:auto:" + body.source_id
    if db.one("SELECT id FROM records WHERE id=%s AND NOT deleted", (identifier,)):
        return {"queued": False, "status": "ready", "draft_id": identifier, "sent": False}
    with db.transaction() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('manual_reply_admission'))")
        job = conn.execute(
            "SELECT * FROM jobs WHERE dedupe_key=%s FOR UPDATE", ("manual-reply:" + body.source_id,)
        ).fetchone()
        if not job or job["status"] not in {"pending", "running"}:
            count = conn.execute(
                "SELECT count(*) n FROM jobs WHERE kind='manual_email' AND status IN ('pending','running')"
            ).fetchone()["n"]
            if count >= 3:
                raise HTTPException(
                    409, "Three replies are already being prepared. Review them before adding more."
                )
            if job:
                conn.execute(
                    "UPDATE jobs SET status='pending',attempts=0,error=NULL,due_at=now() WHERE id=%s",
                    (job["id"],),
                )
            else:
                conn.execute(
                    "INSERT INTO jobs(id,kind,dedupe_key,due_at,payload,priority) VALUES(%s,'manual_email',%s,now(),%s,3)",
                    (
                        uuid4(),
                        "manual-reply:" + body.source_id,
                        Jsonb({"source_id": body.source_id, "requested": True}),
                    ),
                )
    db.audit("draft.requested", body.source_id, actor="demo", source=actor.channel)
    return {"queued": True, "status": "preparing", "draft_id": identifier, "sent": False}


@app.get("/record")
def source_record(id: str = Query(max_length=4096), actor=Depends(authenticate)):
    row = db.one("SELECT id,title,body,metadata,occurred_at FROM records WHERE id=%s AND NOT deleted", (id,))
    if not row:
        raise HTTPException(404, "This source is no longer available.")
    return row


@app.get("/calendar")
def calendar(
    days: int = Query(7, ge=1, le=28), offset: int = Query(0, ge=0, le=28), actor=Depends(authenticate)
):
    zone = ZoneInfo("America/Denver")
    start = datetime.now(zone).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=offset)
    end = start + timedelta(days=days)
    return db.query(
        """SELECT id,title,body,metadata,occurred_at FROM records
        WHERE kind='event' AND NOT deleted AND occurred_at>=%s AND occurred_at<%s
        ORDER BY occurred_at,title LIMIT 300""",
        (start, end),
    )


class Vip(BaseModel):
    enabled: bool


@app.post("/people/{person_id}/vip")
def vip(person_id: str, body: Vip, actor=Depends(authenticate)):
    if not db.execute("UPDATE people SET vip=%s WHERE id=%s", (body.enabled, person_id)):
        raise HTTPException(404, "Person not found")
    db.audit("person.vip_changed", person_id, {"enabled": body.enabled}, actor="demo", source=actor.channel)
    return {"updated": True}


@app.get("/commitments")
def commitments(
    status: Literal["all", "active", "open", "candidate", "done", "dismissed"] = "all",
    limit: int = Query(200, ge=1, le=200),
    offset: int = Query(0, ge=0, le=100000),
    actor=Depends(authenticate),
):
    rows = db.query(
        """SELECT c.*,r.title AS source_title,left(r.body,6000) AS source_excerpt,
        r.occurred_at AS source_date,r.metadata->>'coverage' AS source_coverage
        FROM commitments c LEFT JOIN records r ON r.id=c.source_id AND NOT r.deleted
        WHERE (%s='all' OR (%s='active' AND c.status IN ('open','candidate')) OR c.status=%s)
        ORDER BY CASE c.status WHEN 'open' THEN 0 WHEN 'candidate' THEN 1 ELSE 2 END,
        CASE WHEN c.status='open' THEN c.due_at ELSE NULL END NULLS LAST,c.created_at DESC,c.id
        LIMIT %s OFFSET %s""",
        (status, status, status, limit, offset),
    )
    return [{**row, "revision": commitment_revision(row)} for row in rows]


class MeetingRequest(BaseModel):
    id: str | None = Field(default=None, max_length=1000)


@app.post("/meetings/prepare")
async def meeting_prepare(body: MeetingRequest, actor=Depends(authenticate)):
    return await prepare_meeting(db, runtime["inference"], body.id)


@app.get("/meetings/options")
def meeting_options(actor=Depends(authenticate)):
    enabled = db.get_state("feature.HPOS_MEETING_INTELLIGENCE", False)
    return {
        "enabled": enabled,
        "meetings": db.query(
            "SELECT id,title,occurred_at FROM records WHERE kind='event' AND NOT deleted AND occurred_at>now()-interval '30 days' AND occurred_at<now()+interval '14 days' ORDER BY occurred_at DESC LIMIT 100"
        )
        if enabled
        else [],
    }


@app.get("/documents/insights")
def source_insights(id: str = Query(max_length=4096), actor=Depends(authenticate)):
    if not db.get_state("feature.HPOS_MEETING_INTELLIGENCE", False):
        return {"status": "disabled", "summary": "Structured source notes are disabled.", "decisions": []}
    try:
        return read_source_insights(db, id)
    except LookupError as error:
        raise HTTPException(404, str(error)) from None


@app.post("/documents/notes")
def prepare_source_notes(body: MeetingRequest, actor=Depends(authenticate)):
    if not db.get_state("feature.HPOS_MEETING_INTELLIGENCE", False):
        raise HTTPException(409, "Structured notes are disabled")
    from .extraction import source_digest

    row = db.one(
        "SELECT id,title,body,occurred_at FROM records WHERE id=%s AND NOT deleted AND kind IN ('document','transcript')",
        (body.id,),
    )
    if not row:
        raise HTTPException(404, "Document no longer available")
    dedupe = "source-notes:" + source_digest(row)
    db.execute(
        "UPDATE jobs SET status='pending',attempts=0,due_at=now(),error=NULL WHERE dedupe_key=%s AND status='failed'",
        (dedupe,),
    )
    Jobs(db).enqueue(
        "extract_source",
        dedupe,
        datetime.now(timezone.utc),
        {"source_id": row["id"]},
        priority=6,
    )
    db.audit("source.notes_requested", row["id"], actor="demo", source="dashboard")
    return {"status": "queued"}


class CommitmentReview(BaseModel):
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["open", "done", "dismissed"]
    owner: str = Field(min_length=1, max_length=300, pattern=r"\S")
    beneficiary: str = Field(default="", max_length=300)
    text: str = Field(min_length=1, max_length=3000, pattern=r"\S")
    due_at: AwareDatetime | None = None


@app.post("/commitments/{identifier}/review")
def commitment_review(identifier: UUID, body: CommitmentReview, actor=Depends(authenticate)):
    try:
        return review_commitment(db, identifier, body, actor)
    except LookupError as e:
        raise HTTPException(404, str(e)) from None
    except StaleCommitment as e:
        raise HTTPException(409, str(e)) from None
    except ValueError as e:
        raise HTTPException(422, str(e)) from None


@app.get("/memory")
async def memories(q: str = "", actor=Depends(authenticate)):
    if q:
        return await runtime["memory"].search(q[:500])
    return db.query(
        """SELECT m.id,m.kind,m.text,m.source_id,m.permanent,m.confidence,m.created_at,
        r.title source_title,l.memory_id IS NOT NULL generated
        FROM retrievable_memories m LEFT JOIN records r ON r.id=m.source_id AND NOT r.deleted
        LEFT JOIN memory_lineage l ON l.memory_id=m.id ORDER BY m.created_at DESC LIMIT 100"""
    )


@app.get("/memory/consolidation")
def memory_consolidation_status(actor=Depends(authenticate)):
    return consolidation_status(db)


@app.get("/memory/views")
def memory_view_list(actor=Depends(authenticate)):
    return memory_views.listing(db)


@app.get("/memory/views/read")
def memory_view_read(
    kind: Literal[
        "people", "projects", "organizations", "meetings", "commitments", "decisions", "facts", "daily"
    ],
    id: str = Query(max_length=4096),
    actor=Depends(authenticate),
):
    try:
        return memory_views.read(db, kind, id)
    except LookupError as error:
        raise HTTPException(404, str(error)) from None


@app.get("/memory/graph")
def graph_neighborhood(
    root: str = Query(memory_graph.ROOT, max_length=4096),
    limit: int = Query(45, ge=10, le=75),
    depth: int = Query(2, ge=1, le=2),
    actor=Depends(authenticate),
):
    try:
        return memory_graph.neighborhood(db, root, limit=limit, depth=depth)
    except LookupError as error:
        raise HTTPException(404, str(error)) from None


@app.get("/memory/graph/search")
async def graph_search(q: str = Query(min_length=1, max_length=500), actor=Depends(authenticate)):
    try:
        return await memory_graph.lookup(db, runtime["memory"], q)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None


class Remember(BaseModel):
    text: str = Field(min_length=1, max_length=16000)
    permanent: bool = False


@app.post("/memory")
async def remember(body: Remember, actor=Depends(authenticate)):
    return await runtime["memory"].remember(body.text, permanent=body.permanent)


@app.get("/memory/evidence")
def response_evidence(message_id: int = Query(gt=0), actor=Depends(authenticate)):
    try:
        return memory_graph.response_evidence(db, message_id)
    except LookupError as error:
        raise HTTPException(404, str(error)) from None


@app.post("/memory/{identifier}/forget")
def forget(identifier: UUID, actor=Depends(authenticate)):
    if not db.one("SELECT id FROM memories WHERE id=%s AND forgotten_at IS NULL", (identifier,)):
        raise HTTPException(404, "Memory not found")
    return runtime["approvals"].propose("memory.forget", {"memory_id": str(identifier)}, actor="demo")


@app.get("/approvals")
def approval_list(actor=Depends(authenticate)):
    db.execute(
        "UPDATE approvals SET status='expired' WHERE status IN ('pending','approved') AND expires_at<=now()"
    )
    return db.query("SELECT * FROM approvals ORDER BY created_at DESC LIMIT 100")


class Decision(BaseModel):
    command: str
    digest: str


@app.post("/approvals/{identifier}/decision")
def decide(identifier: UUID, body: Decision, actor=Depends(authenticate)):
    return runtime["approvals"].decide(identifier, body.command, body.digest, actor)


@app.post("/approvals/{identifier}/execute")
async def execute(identifier: UUID, actor=Depends(authenticate)):
    handlers = {"memory.forget": runtime["memory"].forget_approved}
    if settings.ms_client_id and db.get_state("outlook.provider") != "browser":
        handlers.update(
            {
                "email.send": runtime["graph"].send_approved,
                "email.save_draft": runtime["graph"].save_draft_approved,
                "calendar.create": runtime["graph"].calendar_create_approved,
                "calendar.move": runtime["graph"].calendar_move_approved,
                "calendar.cancel": runtime["graph"].calendar_cancel_approved,
            }
        )
    return await runtime["approvals"].execute(identifier, handlers)


@app.post("/drafts/{identifier:path}/propose")
def propose_draft(
    identifier: str, destination: Literal["send", "outlook_draft"] = "send", actor=Depends(authenticate)
):
    draft = db.one("SELECT * FROM records WHERE id=%s AND kind='draft' AND NOT deleted", (identifier,))
    if not draft:
        raise HTTPException(404, "Draft not found")
    if draft["metadata"].get("internal_only"):
        raise HTTPException(
            409, "This writing workflow creates internal content only, not an email send action."
        )
    if draft["metadata"].get("review_required"):
        raise HTTPException(409, "Review and save this draft before proposing any send action.")
    source = db.one(
        "SELECT metadata FROM records WHERE id=%s AND kind='email' AND NOT deleted",
        (draft["metadata"].get("source_id"),),
    )
    recipient = source["metadata"].get("from", {}).get("emailAddress", {}).get("address") if source else None
    if not recipient or "@" not in recipient:
        raise HTTPException(422, "A verified source email recipient is required")
    try:
        payload = email_payload(draft["title"], draft["body"], [recipient])
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    return runtime["approvals"].propose(
        "email.send" if destination == "send" else "email.save_draft", payload, actor="demo"
    )


class EmailProposalEdit(BaseModel):
    subject: str = Field(min_length=1, max_length=500)
    body: str = Field(min_length=1, max_length=20000)
    recipients: list[str] = Field(min_length=1, max_length=20)
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")


@app.post("/approvals/{identifier}/edit-email")
def edit_email_proposal(identifier: UUID, body: EmailProposalEdit, actor=Depends(authenticate)):
    old = runtime["approvals"].get(identifier)
    if "packet" in old["payload"]:
        raise HTTPException(
            409,
            "This proposal binds an exact review PDF. Deny it to cancel; attachment or recipient edits require a new packet proposal.",
        )
    if old["kind"] not in {"email.send", "email.save_draft"} or old["digest"] != body.digest:
        raise HTTPException(409, "Reopen the exact email proposal before editing")
    try:
        payload = email_payload(body.subject, body.body, body.recipients)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    return runtime["approvals"].edit(identifier, payload, actor)


class WritingRequest(BaseModel):
    mode: str = Field(min_length=1, max_length=60)
    topic: str = Field(min_length=2, max_length=300)
    project_id: str | None = Field(default=None, max_length=64)
    request_id: UUID = Field(default_factory=uuid4)


@app.get("/writing")
def writing(actor=Depends(authenticate)):
    return executive.listing(db)


@app.post("/writing")
def writing_request(body: WritingRequest, actor=Depends(authenticate)):
    try:
        return executive.queue(
            db, body.mode, body.topic, actor, request_id=body.request_id, project_id=body.project_id
        )
    except LookupError as error:
        raise HTTPException(404, str(error)) from None
    except ValueError as error:
        raise HTTPException(409, str(error)) from None


@app.post("/writing/{identifier:path}/cancel")
def cancel_writing(identifier: str, actor=Depends(authenticate)):
    try:
        return executive.cancel(db, identifier, actor)
    except LookupError as error:
        raise HTTPException(404, str(error)) from None
    except ValueError as error:
        raise HTTPException(409, str(error)) from None


class DraftEdit(BaseModel):
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    title: str = Field(min_length=1, max_length=500)
    body: str = Field(min_length=1, max_length=20000)


@app.post("/drafts/{identifier:path}/edit")
def review_draft(identifier: str, body: DraftEdit, actor=Depends(authenticate)):
    if not body.title.strip() or not body.body.strip():
        raise HTTPException(422, "A subject and draft body are required.")
    try:
        return edit_draft(db, identifier, body.revision, body.title, body.body, actor)
    except LookupError as error:
        raise HTTPException(404, str(error)) from None
    except ValueError as error:
        raise HTTPException(409, str(error)) from None


@app.post("/documents")
async def ingest_document(
    file: UploadFile = File(...),
    meeting_id: str | None = Form(default=None, max_length=1000),
    transcript: bool = Form(False),
    actor=Depends(authenticate),
):
    meeting = None
    if meeting_id:
        if not db.get_state("feature.HPOS_MEETING_INTELLIGENCE", False):
            raise HTTPException(422, "Meeting links are not enabled")
        meeting = db.one(
            "SELECT id,occurred_at FROM records WHERE id=%s AND kind='event' AND NOT deleted", (meeting_id,)
        )
        if not meeting:
            raise HTTPException(422, "Choose an available saved meeting")
    data = await file.read(1_500_001)
    if len(data) > 1_500_000:
        raise HTTPException(413, "Initial upload limit is 1.5 MB")
    try:
        text = await asyncio.to_thread(extract_text_isolated, file.filename or "document.txt", data)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    identifier = "document:" + str(uuid4())
    kind = "transcript" if transcript or (file.filename or "").lower().endswith(".vtt") else "document"
    metadata = {"sha256": hashlib.sha256(data).hexdigest(), "trust": "untrusted"}
    if meeting:
        metadata.update(source_id=meeting["id"], association="demo")
    db.execute(
        "INSERT INTO records(id,kind,provider,title,body,metadata,occurred_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
        (
            identifier,
            kind,
            "demo",
            file.filename or "Document",
            text,
            Jsonb(metadata),
            meeting["occurred_at"] if meeting else None,
        ),
    )
    Jobs(db).enqueue(
        "extract_source",
        "extract:" + identifier,
        datetime.now(timezone.utc),
        {"source_id": identifier},
        priority=8,
    )
    for offset in range(0, min(len(text), 48000), 4000):
        await runtime["memory"].remember(
            text[offset : offset + 4000], kind="document", source_id=identifier, confidence=1
        )
    db.audit("document.ingested", identifier, {"kind": kind}, actor="demo")
    return {"id": identifier, "characters": len(text), "extraction": "queued"}


@app.get("/activity")
def activity(actor=Depends(authenticate)):
    return db.query("SELECT * FROM audit ORDER BY id DESC LIMIT 100")


@app.post("/outlook/connect")
async def connect(capability: str = "read", actor=Depends(authenticate)):
    if capability not in {"read", "drafts", "send", "calendar"}:
        raise HTTPException(422, "Unknown capability")
    return await runtime["auth"].start_device(capability)


@app.post("/outlook/complete")
async def complete(actor=Depends(authenticate)):
    return await runtime["auth"].finish_device()


@app.post("/outlook/sync")
async def sync(actor=Depends(authenticate)):
    try:
        return await runtime["sync"].tick()
    except AuthRequired as e:
        db.set_state(
            "outlook.error", {"error_class": type(e).__name__, "at": datetime.now(timezone.utc).isoformat()}
        )
        raise
    except Exception as error:
        db.set_state(
            "outlook.error",
            {"error_class": type(error).__name__, "at": datetime.now(timezone.utc).isoformat()},
        )
        db.audit("outlook.sync_failed", details={"error_class": type(error).__name__})
        raise HTTPException(
            503, "Outlook could not be read. Existing records are preserved; check Account Setup."
        ) from None


class CommunicationRule(BaseModel):
    mode: str
    until: datetime


@app.post("/communication")
def communication(body: CommunicationRule, actor=Depends(authenticate)):
    if body.mode not in {"quiet", "text_only", "normal"} or body.until.tzinfo is None:
        raise HTTPException(422, "Use quiet, text_only or normal with a timezone-aware expiration")
    db.set_state("communication_rule", {"mode": body.mode, "until": body.until.isoformat()})
    db.audit(
        "communication.changed", details={"mode": body.mode, "until": body.until.isoformat()}, actor="demo"
    )
    return {"saved": True}


@app.get("/voice/lab")
def voice_lab(actor=Depends(authenticate)):
    folder = settings.data_dir / "voice_lab"
    auditions = []
    if folder.exists():
        for item in sorted(folder.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:24]:
            auditions.append(json.loads(item.read_text()))
    return {
        "profiles": [profile(key) for key in PROFILES],
        "scripts": SCRIPTS,
        "auditions": auditions,
        "selected": db.get_state("voice.selected", "british-balanced"),
        "reference": "Original synthetic British blend from Apache-2.0 Kokoro-82M weights; no actor recording. Final listening approval is pending.",
    }


class AuditionRequest(BaseModel):
    candidate: str
    script: str = "briefing"


@app.post("/voice/lab/generate")
async def voice_audition(body: AuditionRequest, actor=Depends(authenticate)):
    if body.candidate not in PROFILES or body.script not in SCRIPTS:
        raise HTTPException(422, "Unknown audition")
    if voice_lab_lock.locked() or runtime["inference"].interactive:
        raise HTTPException(409, "End the conversation before generating voice auditions.")
    async with voice_lab_lock:
        started = time.monotonic()
        async with httpx.AsyncClient(timeout=150) as client:
            try:
                result = await client.post(
                    "http://127.0.0.1:8186/synthesize",
                    json={"text": SCRIPTS[body.script], "candidate": body.candidate},
                )
                result.raise_for_status()
            except httpx.HTTPError:
                raise HTTPException(503, "Voice synthesis is unavailable. Check System Health.") from None
        identifier = str(uuid4())
        folder = settings.data_dir / "voice_lab"
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = folder / (identifier + ".wav")
        path.write_bytes(result.content)
        path.chmod(0o600)
        metadata = {
            **profile(body.candidate),
            "candidate": body.candidate,
            "id": identifier,
            "script": body.script,
            "text": SCRIPTS[body.script],
            "generation_ms": round((time.monotonic() - started) * 1000),
            "worker_ms": result.headers.get("X-TTS-Milliseconds"),
            "cache": result.headers.get("X-Audio-Cache", "miss"),
            "audio_path": f"/api/voice/lab/audio/{identifier}",
            "created_at": utcnow(),
        }
        path.with_suffix(".json").write_text(json.dumps(metadata, default=str))
        path.with_suffix(".json").chmod(0o600)
        db.audit(
            "voice.audition_generated",
            identifier,
            {"candidate": body.candidate, "script": body.script, "generation_ms": metadata["generation_ms"]},
            actor="demo",
            source=actor.channel,
        )
        return metadata


@app.get("/voice/lab/audio/{identifier}")
def audition_audio(identifier: UUID, actor=Depends(authenticate)):
    path = settings.data_dir / "voice_lab" / (str(identifier) + ".wav")
    if not path.is_file():
        raise HTTPException(404)
    return Response(path.read_bytes(), media_type="audio/wav", headers={"Cache-Control": "no-store"})


@app.post("/voice/lab/select")
async def select_audition(body: AuditionRequest, actor=Depends(authenticate)):
    if body.candidate not in PROFILES:
        raise HTTPException(422, "Unknown voice candidate")
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            result = await client.post(
                "http://127.0.0.1:8186/voice/select", json={"text": "selection", "candidate": body.candidate}
            )
            result.raise_for_status()
        except httpx.HTTPError:
            raise HTTPException(
                503, "Voice selection could not be saved. Try again when speech is healthy."
            ) from None
    db.set_state("voice.selected", body.candidate)
    db.audit("voice.selected", details={"candidate": body.candidate}, actor="demo", source=actor.channel)
    return {"selected": body.candidate}


@app.websocket("/voice")
async def voice_socket(websocket: WebSocket):
    session = websocket.cookies.get("jarvis_session", "")
    if (
        websocket.headers.get("origin") not in ALLOWED_ORIGINS
        or session_expiry(hashlib.sha256(session.encode()).hexdigest()) <= time.time()
    ):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    segmenter = Segmenter(detector=SpeechDetector())
    voice = Voice(runtime["jarvis"], db)
    channel = "phone" if websocket.query_params.get("channel") == "phone" else "dashboard"
    task = None
    active_turn = None
    playback_seen = False
    turn_started = 0.0
    session_id = str(uuid4())
    partial_task = None
    utterance = 0
    last_partial = 0.0
    recognition_task = None
    held_response = False
    response_gate = asyncio.Event()
    response_gate.set()
    last_response_state = "thinking"
    response_audio_sent = False
    runtime["inference"].interactive += 1
    db.audit("voice.session_started", session_id, source=channel)

    async def partial(pcm, number):
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                result = await client.post(
                    "http://127.0.0.1:8185/transcribe?partial=true",
                    files={"file": ("partial.wav", wav_bytes(pcm), "audio/wav")},
                )
                result.raise_for_status()
                text = result.json()["text"].strip()
                if text and number == utterance and segmenter.speech:
                    await websocket.send_json({"type": "partial", "text": text, "session_id": session_id})
        except (httpx.HTTPError, ValueError, KeyError):
            pass  # The final recognition remains authoritative and reports failures visibly.

    async def send_voice(event):
        nonlocal last_response_state, response_audio_sent
        await response_gate.wait()
        if event["type"] == "state":
            last_response_state = event["state"]
            db.audit(
                "voice.state",
                event.get("turn_id", session_id),
                {"state": event["state"], "session_id": session_id},
                source=channel,
            )
        if event["type"] == "audio":
            response_audio_sent = True
        await websocket.send_json(event)

    async def respond(pcm, identifier, speech_end=None, recognized=None):
        nonlocal response_audio_sent
        response_audio_sent = False
        try:
            if pcm is None:
                await voice.preview(send_voice, identifier)
            else:
                await voice.turn(
                    pcm,
                    send_voice,
                    Actor("demo", channel),
                    identifier,
                    speech_end=speech_end,
                    recognized=recognized,
                )
        except Exception as error:
            # A failed turn must not leave an apparently live conversation waiting
            # forever. Cancellation still propagates (it is a BaseException).
            with suppress(Exception):
                db.audit(
                    "voice.error",
                    identifier,
                    {"error_class": type(error).__name__, "session_id": session_id},
                    source=channel,
                )
            await websocket.send_json(
                {
                    "type": "error",
                    "text": "That reply failed. I'm still listening; you can retry or use text.",
                    "turn_id": identifier,
                }
            )

    async def confirm_candidate(pcm, speech_end, number):
        """Noise must not destroy an answer that has not started playing yet."""
        nonlocal task, active_turn, playback_seen, turn_started, held_response
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                response = await client.post(
                    "http://127.0.0.1:8185/transcribe",
                    files={"file": ("candidate.wav", wav_bytes(pcm), "audio/wav")},
                )
                response.raise_for_status()
                text = response.json()["text"].strip()
            if number != utterance:
                return
            if text:
                if task:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                await websocket.send_json({"type": "cancel"})
                active_turn, playback_seen, turn_started = str(uuid4()), False, speech_end
                task = asyncio.create_task(
                    respond(pcm, active_turn, speech_end, (text, started, time.monotonic()))
                )
            else:
                db.audit("voice.noise_ignored", session_id, source=channel)
                await websocket.send_json(
                    {"type": "state", "state": last_response_state, "turn_id": active_turn}
                )
        except (httpx.HTTPError, ValueError, KeyError):
            await websocket.send_json(
                {
                    "type": "progress",
                    "turn_id": active_turn,
                    "text": "I couldn't make out that interruption. Continuing your answer.",
                }
            )
        finally:
            if number == utterance:
                held_response = False
                response_gate.set()
                await websocket.send_json({"type": "speech", "active": False})

    try:
        async with httpx.AsyncClient(timeout=3) as client:
            try:
                result = await client.get("http://127.0.0.1:8185/health")
                asr = (
                    "healthy" if result.is_success and result.json().get("status") == "ready" else "unhealthy"
                )
            except (httpx.HTTPError, ValueError):
                asr = "unhealthy"
        await websocket.send_json({"type": "hello", "session_id": session_id, "asr": asr})
        while True:
            message = await websocket.receive()
            if session_expiry(hashlib.sha256(session.encode()).hexdigest()) <= time.time():
                await websocket.close(code=1008)
                break
            if message["type"] == "websocket.disconnect":
                break
            if message.get("bytes"):
                began, pcm = segmenter.feed(message["bytes"])
                if began:
                    utterance += 1
                    last_partial = time.monotonic()
                    if recognition_task and not recognition_task.done():
                        recognition_task.cancel()
                        await asyncio.gather(recognition_task, return_exceptions=True)
                    if task and not task.done() and not response_audio_sent:
                        held_response = True
                        response_gate.clear()
                    else:
                        if task:
                            task.cancel()
                            await asyncio.gather(task, return_exceptions=True)
                        await websocket.send_json({"type": "cancel"})
                    await websocket.send_json({"type": "speech", "active": True})
                    db.audit(
                        "voice.speech_started",
                        session_id,
                        {"utterance": utterance, "rms": round(segmenter.rms, 4)},
                        source=channel,
                    )
                if (
                    segmenter.speech
                    and time.monotonic() - last_partial >= 0.9
                    and (partial_task is None or partial_task.done())
                ):
                    last_partial = time.monotonic()
                    partial_task = asyncio.create_task(
                        partial(bytes(segmenter.frames[-16000 * 2 * 8 :]), utterance)
                    )
                if segmenter.completed and not pcm and held_response:
                    held_response = False
                    response_gate.set()
                    await websocket.send_json({"type": "speech", "active": False})
                    await websocket.send_json(
                        {"type": "state", "state": last_response_state, "turn_id": active_turn}
                    )
                    db.audit("voice.noise_ignored", session_id, source=channel)
                if pcm:
                    utterance += 1
                    if partial_task:
                        partial_task.cancel()
                        await asyncio.gather(partial_task, return_exceptions=True)
                    await websocket.send_json({"type": "speech", "active": False})
                    if held_response:
                        recognition_task = asyncio.create_task(
                            confirm_candidate(
                                pcm,
                                time.monotonic() - segmenter.completed_silence_samples / segmenter.rate,
                                utterance,
                            )
                        )
                        continue
                    active_turn, playback_seen, turn_started = (
                        str(uuid4()),
                        False,
                        time.monotonic() - segmenter.completed_silence_samples / segmenter.rate,
                    )
                    task = asyncio.create_task(respond(pcm, active_turn, turn_started))
            elif message.get("text") == "preview":
                held_response = False
                response_gate.set()
                if recognition_task:
                    recognition_task.cancel()
                    await asyncio.gather(recognition_task, return_exceptions=True)
                if task:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                active_turn, playback_seen, turn_started = str(uuid4()), False, time.monotonic()
                task = asyncio.create_task(respond(None, active_turn))
            elif message.get("text") == "cancel":
                held_response = False
                response_gate.set()
                if recognition_task:
                    recognition_task.cancel()
                    await asyncio.gather(recognition_task, return_exceptions=True)
                if task:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                segmenter = Segmenter(detector=SpeechDetector())
                utterance += 1
                active_turn = None
                await websocket.send_json({"type": "cancel"})
            elif message.get("text", "").startswith("playback:"):
                if message["text"][9:] == active_turn and not playback_seen:
                    playback_seen = True
                    db.audit(
                        "voice.playback_started",
                        active_turn,
                        {"speech_end_to_playback_ms": round((time.monotonic() - turn_started) * 1000)},
                        actor="demo",
                        source=channel,
                    )
            elif message.get("text", "").startswith("{") and len(message["text"]) <= 2048:
                try:
                    info = json.loads(message["text"])
                    if info.get("type") == "client_diagnostics":
                        db.audit(
                            "voice.client_diagnostics",
                            session_id,
                            {
                                key: str(info.get(key, ""))[:160]
                                for key in ("permission", "device", "secure_context", "sample_rate")
                            },
                            actor="demo",
                            source=channel,
                        )
                except (ValueError, AttributeError):
                    pass
    except (WebSocketDisconnect, ValueError):
        pass
    finally:
        if recognition_task:
            recognition_task.cancel()
            await asyncio.gather(recognition_task, return_exceptions=True)
        if partial_task:
            partial_task.cancel()
            await asyncio.gather(partial_task, return_exceptions=True)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        runtime["inference"].interactive -= 1
        db.audit("voice.session_ended", session_id, source=channel)
