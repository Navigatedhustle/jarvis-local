"""Delegated Microsoft Graph provider. No credentials in source or logs."""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Protocol
from urllib.parse import quote, urlencode, urlparse

import httpx
import msal

GRAPH = "https://graph.microsoft.com/v1.0"
READ_SCOPES = ["User.Read", "Mail.Read", "Calendars.Read"]


class AuthRequired(RuntimeError):
    pass


class MailProvider(Protocol):
    async def search(self, query: str) -> list[dict]: ...
    async def thread(self, conversation_id: str) -> list[dict]: ...
    async def attachments(self, message_id: str) -> list[dict]: ...


class CalendarProvider(Protocol):
    async def events(self, start: str, end: str) -> list[dict]: ...


class MicrosoftAuth:
    def __init__(self, settings):
        self.settings = settings
        self.path = settings.secret_dir / "msal-cache.json"
        self.cache = msal.SerializableTokenCache()
        if self.path.exists():
            self.cache.deserialize(self.path.read_text())
        self.flow = None
        self._lock = asyncio.Lock()

    def app(self):
        if not self.settings.ms_client_id or not self.settings.ms_tenant_id:
            raise AuthRequired("Microsoft application and tenant IDs are required before sign-in")
        return msal.PublicClientApplication(
            self.settings.ms_client_id,
            authority=f"https://login.microsoftonline.com/{self.settings.ms_tenant_id}",
            token_cache=self.cache,
        )

    def save(self):
        if self.cache.has_state_changed:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".tmp")
            temp.write_text(self.cache.serialize())
            temp.chmod(0o600)
            temp.replace(self.path)

    async def token(self, scopes=None):
        async with self._lock:
            app = self.app()
            accounts = app.get_accounts()
            if len(accounts) != 1:
                raise AuthRequired("Sign in to the single authorized HP account")
            result = await asyncio.to_thread(
                app.acquire_token_silent, scopes or READ_SCOPES, account=accounts[0]
            )
            self.save()
            if not result or "access_token" not in result:
                raise AuthRequired("Microsoft authentication expired or consent is required")
            return result["access_token"]

    async def start_device(self, capability="read"):
        # HP conditional access may disallow this. Never circumvent; use normal interactive auth instead.
        additions = {
            "read": [],
            "drafts": ["Mail.ReadWrite"],
            "send": ["Mail.Send"],
            "calendar": ["Calendars.ReadWrite"],
        }
        if capability not in additions:
            raise ValueError("Unknown Microsoft capability")
        self.flow = await asyncio.to_thread(
            self.app().initiate_device_flow, scopes=READ_SCOPES + additions[capability]
        )
        if "user_code" not in self.flow:
            raise AuthRequired(
                "Microsoft device sign-in unavailable; interactive sign-in/tenant consent required"
            )
        return {k: self.flow[k] for k in ("user_code", "verification_uri", "expires_in")}

    async def finish_device(self):
        if not self.flow:
            raise AuthRequired("Start Microsoft sign-in first")
        async with self._lock:
            result = await asyncio.to_thread(
                self.app().acquire_token_by_device_flow, self.flow, exit_condition=lambda flow: True
            )
            self.save()
            if result.get("error") in {"authorization_pending", "slow_down"}:
                return {"connected": False, "message": "Finish Microsoft sign-in and MFA, then check again."}
            self.flow = None
        if "access_token" not in result:
            raise AuthRequired(
                "Microsoft sign-in was not completed or tenant policy requires a different sign-in path"
            )
        return {"connected": True}


class GraphProvider:
    def __init__(self, auth, client=None):
        self.auth = auth
        self.client = client

    @staticmethod
    def safe_url(path):
        url = path if path.startswith("https://") else GRAPH + path
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or parsed.netloc != "graph.microsoft.com"
            or not parsed.path.startswith("/v1.0/")
        ):
            raise ValueError("Graph continuation URL is not trusted")
        return url

    async def request(self, method, path, *, scopes=None, **kwargs):
        url = self.safe_url(path)
        headers = {
            "Authorization": "Bearer " + await self.auth.token(scopes),
            "Prefer": 'IdType="ImmutableId", outlook.body-content-type="text", outlook.timezone="UTC"',
        }

        async def call(client):
            for attempt in range(3):
                r = await client.request(method, url, headers=headers, **kwargs)
                if r.status_code in (401, 403):
                    raise AuthRequired("Microsoft sign-in, permissions, or HP tenant consent is required")
                if r.status_code == 410:
                    raise DeltaExpired("Microsoft delta cursor expired")
                if method == "GET" and r.status_code in (429, 503) and attempt < 2:
                    delay = r.headers.get("Retry-After", "2")
                    await asyncio.sleep(min(float(delay) if delay.isdigit() else 2, 15))
                    continue
                r.raise_for_status()
                return r.json() if r.content else {}

        if self.client:
            return await call(self.client)
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            return await call(client)

    async def pages(self, path, max_pages=100):
        results = []
        for _ in range(max_pages):
            page = await self.request("GET", path)
            results.extend(page.get("value", []))
            path = page.get("@odata.nextLink")
            if not path:
                return results
        raise RuntimeError("Graph pagination limit exceeded; resume with a bounded sync job")

    async def folders(self):
        roots = await self.pages("/me/mailFolders?includeHiddenFolders=true&$top=100")
        known = {}
        for role in ("sentitems", "drafts", "junkemail", "deleteditems"):
            folder = await self.request("GET", "/me/mailFolders/" + role + "?$select=id")
            known[folder["id"]] = role
        for folder in roots:
            folder["jarvis_role"] = known.get(folder["id"], "received")
        result, queue = list(roots), list(roots)
        while queue:
            folder = queue.pop(0)
            if folder.get("childFolderCount", 0):
                children = await self.pages(
                    "/me/mailFolders/"
                    + quote(folder["id"], safe="")
                    + "/childFolders?includeHiddenFolders=true&$top=100"
                )
                for child in children:
                    child["jarvis_role"] = known.get(child["id"], folder["jarvis_role"])
                result.extend(children)
                queue.extend(children)
        return result

    async def search(self, query):
        return await self.pages(
            "/me/messages?" + urlencode({"$search": '"' + query.replace('"', "") + '"', "$top": 25}),
            max_pages=10,
        )

    async def thread(self, conversation_id):
        return await self.pages(
            "/me/messages?"
            + urlencode(
                {"$filter": "conversationId eq '" + conversation_id.replace("'", "''") + "'", "$top": 100}
            )
        )

    async def attachments(self, message_id):
        return await self.pages("/me/messages/" + quote(message_id, safe="") + "/attachments")

    async def events(self, start, end):
        return await self.pages(
            "/me/calendarView?" + urlencode({"startDateTime": start, "endDateTime": end, "$top": 100})
        )

    async def create_draft(self, payload):
        # Optional incremental Mail.ReadWrite consent; no Mail.Send in the baseline scopes.
        return await self.request("POST", "/me/messages", scopes=["Mail.ReadWrite"], json=payload)

    async def send_approved(self, payload, action_id):
        message = payload["message"]
        if "packet" in payload:
            from .packet_delivery import attach

            message = attach(self.auth.settings, payload)
        await self.request(
            "POST", "/me/sendMail", scopes=["Mail.Send"], json={"message": message, "saveToSentItems": True}
        )
        return {"submitted": True, "approval_id": action_id}

    async def save_draft_approved(self, payload, action_id):
        result = await self.create_draft(payload["message"])
        return {"saved_draft": True, "sent": False, "id": result["id"], "approval_id": action_id}

    async def calendar_create_approved(self, payload, action_id):
        event = dict(payload)
        event["transactionId"] = action_id
        return await self.request("POST", "/me/events", scopes=["Calendars.ReadWrite"], json=event)

    async def calendar_move_approved(self, payload, action_id):
        return await self.request(
            "PATCH",
            "/me/events/" + quote(payload["event_id"], safe=""),
            scopes=["Calendars.ReadWrite"],
            json={"start": payload["start"], "end": payload["end"]},
        )

    async def calendar_cancel_approved(self, payload, action_id):
        return await self.request(
            "POST",
            "/me/events/" + quote(payload["event_id"], safe="") + "/cancel",
            scopes=["Calendars.ReadWrite"],
            json={"comment": payload.get("comment", "")},
        )


def initial_mail_delta(folder_id, now=None):
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=90)).isoformat()
    return (
        "/me/mailFolders/"
        + quote(folder_id, safe="")
        + "/messages/delta?"
        + urlencode(
            {
                "$filter": "receivedDateTime ge " + since,
                "$top": 25,
                "$select": "id,subject,body,from,toRecipients,ccRecipients,receivedDateTime,sentDateTime,conversationId,isRead,isDraft,importance,hasAttachments,parentFolderId",
            }
        )
    )


class DeltaExpired(RuntimeError):
    pass


def initial_mail_history(folder_id, now=None):
    # Ordinary pagination avoids the filtered-delta initial 5000-message limit.
    return initial_mail_delta(folder_id, now).replace("/messages/delta?", "/messages?")
