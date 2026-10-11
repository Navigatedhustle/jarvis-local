"""Private DOM access to Demo's dedicated, normally authenticated Outlook browser.

This module never reads cookies, storage, passwords, bearer tokens or authentication responses.
Only Outlook's normal read-data responses caused by allowlisted UI navigation are collected.
"""

import asyncio
import hashlib
import json
import os
import re
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from .attachments import attachment_manifest, download_attachment, store_attachment
from .intelligence import addressed_to_someone_else, ingest_event, ingest_message
from .outlook import AuthRequired
from .process_lock import ProcessLock
from .scheduler import Jobs

HOSTS = {"outlook.office.com", "outlook.office365.com", "outlook.cloud.microsoft"}
READ_ACTIONS = {
    "StartupData",
    "FindFolder",
    "FindConversation",
    "GetFolder",
    "GetTimeZone",
    "GetTimeZoneOffsets",
    "GetCalendarFoldersV2",
    "GetCalendarView",
    "GetCalendarEvent",
    "GetRegionalConfiguration",
    "GetOwaUserConfiguration",
    "GetOwaNonBootUserConfiguration",
    "GetOtherMailboxConfiguration",
    "GetMailboxByIdentity",
    "GetUserUnifiedGroups",
    "GetUnifiedGroupsSettings",
    "GetComplianceConfiguration",
    "GetPersonalSchedulingPage",
    "GetConversationItems",
    "GetItem",
}
DATA_ACTIONS = {
    "FindFolder",
    "FindConversation",
    "GetCalendarView",
    "GetCalendarEvent",
    "GetConversationItems",
    "GetItem",
    "GetTimeZone",
}


def folder_roles(payload):
    result = {}
    for item in payload.get("Body", {}).get("ResponseMessages", {}).get("Items", []):
        if item.get("ResponseClass") != "Success":
            continue
        root = item.get("RootFolder", {})
        for folder in [root.get("ParentFolder", {})] + root.get("Folders", []):
            role = folder.get("DistinguishedFolderId", "").lower()
            identifier = folder.get("FolderId", {}).get("Id")
            if role in {"inbox", "sentitems"} and identifier:
                result[identifier] = role
    return result


def bind_visible_folder(visible_ids, previews, excluded_folders=()):
    """Resolve a real response folder ID from the visible list, not response arrival time."""
    visible = set(visible_ids)
    if not visible:
        raise ValueError("No visible conversations available to verify folder provenance")
    groups = {}
    for folder_id, cid in previews:
        groups.setdefault(folder_id, set()).add(cid)
    matches = [
        folder_id for folder_id, ids in groups.items() if visible <= ids and folder_id not in excluded_folders
    ]
    if len(matches) != 1:
        raise ValueError("The visible mail folder cannot be matched unambiguously")
    return matches[0]


def needs_body(preview, known_body_time, browser_zone=None):
    """A new reply must reopen a previously visited conversation; missing dates never prove coverage."""
    latest = source_time(preview.get("LastDeliveryTime"), browser_zone)
    return (
        not latest
        or not known_body_time
        or datetime.fromisoformat(latest) > datetime.fromisoformat(known_body_time)
    )


def history_progress(items, cutoff, rounds, stalled, browser_zone=None):
    dates = [source_time(item.get("LastDeliveryTime"), browser_zone) for item in items]
    valid = [datetime.fromisoformat(value) for value in dates if value]
    oldest = min(valid) if valid else None
    return {
        "loaded": len(items),
        "oldest_preview_at": oldest.isoformat() if oldest else None,
        # Seeing a date boundary does not prove complete messages, all folders, or attachments.
        "boundary_seen": bool(oldest and oldest <= cutoff),
        "scroll_rounds": rounds,
        "stalled": stalled,
        "next_rounds": min(240, max(12, rounds + 12)),
    }


def merge_history(previous, current):
    """A slow/partial pass cannot erase already observed progress or lower its next target."""
    result = {**current, "last_pass_oldest": current.get("oldest_preview_at")}
    dates = [item["oldest_preview_at"] for item in (previous, current) if item.get("oldest_preview_at")]
    result["oldest_preview_at"] = min(dates) if dates else None
    result["next_rounds"] = max(previous.get("next_rounds", 0), current.get("next_rounds", 12))
    result["boundary_seen"] = bool(previous.get("boundary_seen") or current.get("boundary_seen"))
    return result


def queue_browser_intelligence(db, message, role, account):
    """Process full evidence only; never turn a preview or historical backfill into an alert flood."""
    if message.get("coverage") != "message_body":
        return
    identifier = "outlook_browser:mail:" + message["id"]
    row = db.one("SELECT metadata FROM records WHERE id=%s AND NOT deleted", (identifier,))
    if not row:
        return
    importance = row["metadata"].get("triage", {})
    body = message.get("body", {}).get("content", "")
    explicit_commitment = re.search(
        r"\b(I(?:'ll| will)|we(?:'ll| will))\s+(send|provide|share|finish|deliver|follow up|review)\b",
        body,
        re.I,
    )
    if not importance.get("actionable") and not explicit_commitment:
        return
    now = datetime.now(timezone.utc)
    digest = hashlib.sha256((identifier + body).encode()).hexdigest()
    jobs = Jobs(db)
    jobs.enqueue("extract_source", "extract-browser:" + digest, now, {"source_id": identifier}, priority=8)
    date = message.get("receivedDateTime")
    recipients = message.get("toRecipients", [])
    directly_addressed = any(
        p.get("emailAddress", {}).get("address", "").lower() == account.lower() for p in recipients
    )
    if (
        role == "inbox"
        and importance.get("priority") == "high"
        and importance.get("actionable")
        and date
        and now - timedelta(days=2) <= datetime.fromisoformat(date) <= now
        and directly_addressed
        and not addressed_to_someone_else(body)
        and len(recipients) <= 10
        and not db.one(
            """SELECT id FROM records WHERE provider='outlook_browser' AND NOT deleted
            AND metadata->>'folder_role'='sentitems' AND metadata->>'conversationId'=%s AND occurred_at>%s LIMIT 1""",
            (message.get("conversationId"), date),
        )
    ):
        jobs.enqueue("important_email", "draft-browser:" + digest, now, {"source_id": identifier}, priority=6)


def action_name(url, headers):
    values = parse_qs(urlsplit(url).query).get("action", [""])
    if len(values) != 1:
        return ""
    query = values[0]
    header = headers.get("action", "")
    if query and header and query != header:
        return ""
    return header or query


def read_request_allowed(method, url, headers):
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443):
        return False
    if method in {"GET", "HEAD", "OPTIONS"}:
        if parsed.path.endswith("service.svc"):
            return host in HOSTS and action_name(url, headers) in READ_ACTIONS
        return host in HOSTS or any(
            host == suffix or host.endswith("." + suffix)
            for suffix in (
                "microsoft.com",
                "microsoftonline.com",
                "office.net",
                "office.com",
                "msauth.net",
                "msftauth.net",
                "live.com",
            )
        )
    if method != "POST":
        return False
    if host == "login.microsoftonline.com" and parsed.path.endswith("/oauth2/v2.0/token"):
        return True  # Let Microsoft's own browser client renew its session; never inspect the response.
    return (
        host in HOSTS
        and parsed.path in {"/owa/service.svc", "/owa/published/service.svc", "/owa/startupdata.ashx"}
        and action_name(url, headers) in READ_ACTIONS
    )


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        if tag in {"p", "div", "br", "tr", "li"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def body_text(value, limit=40000):
    if not value:
        return ""
    if value.get("BodyType") == "HTML":
        parser = PlainText()
        parser.feed(value.get("Value", "")[:200000])
        return "\n".join(line.strip() for line in "".join(parser.parts).splitlines() if line.strip())[:limit]
    return value.get("Value", "")[:limit]


def source_time(value, browser_zone=None):
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        zones = {"Mountain Standard Time": "America/Denver", "UTC": "UTC"}
        if browser_zone not in zones:
            raise ValueError("Outlook returned an unverified timezone")
        parsed = parsed.replace(tzinfo=ZoneInfo(zones[browser_zone]))
    return parsed.astimezone(timezone.utc).isoformat()


def address(mailbox):
    mailbox = mailbox.get("Mailbox", mailbox)
    return {"emailAddress": {"name": mailbox.get("Name", ""), "address": mailbox.get("EmailAddress", "")}}


def message_record(item, folder, preview=False, browser_zone=None):
    if preview:
        cid = item["ConversationId"]["Id"]
        return {
            "id": "conversation:" + folder + ":" + cid,
            "conversationId": cid,
            "subject": item.get("ConversationTopic", "(No subject)"),
            "from": address(item.get("LastSender", {})),
            "body": {"content": item.get("Preview", "")[:40000]},
            "receivedDateTime": source_time(item.get("LastDeliveryTime"), browser_zone),
            "isRead": item.get("UnreadCount", 0) == 0,
            "importance": item.get("Importance", "normal").lower(),
            "hasAttachments": bool(item.get("HasAttachments")),
            "coverage": "conversation_preview",
            "source_action": "FindConversation",
        }
    raw_body = item.get("NormalizedBody") or item.get("Body")
    if not raw_body or not item.get("Subject") or item.get("IsDraft"):
        return None  # Ignore partial metadata patches and never ingest a draft as incoming mail.
    plain_body = body_text(raw_body, limit=40001)
    truncated = (
        raw_body.get("IsTruncated") or len(plain_body) > 40000 or len(raw_body.get("Value", "")) > 200000
    )
    return {
        "id": item["ItemId"]["Id"],
        "conversationId": item.get("ConversationId", {}).get("Id"),
        "subject": item["Subject"],
        "from": address(item.get("From", item.get("Sender", {}))),
        "toRecipients": [address(value) for value in item.get("ToRecipients", [])],
        "body": {"content": plain_body[:40000]},
        "receivedDateTime": source_time(item.get("DateTimeReceived"), browser_zone),
        "sentDateTime": source_time(item.get("DateTimeSent"), browser_zone),
        "isRead": bool(item.get("IsRead")),
        "isDraft": False,
        "importance": item.get("Importance", "normal").lower(),
        "hasAttachments": bool(item.get("HasAttachments")),
        "attachments": attachment_manifest(item),
        "coverage": "truncated_body" if truncated else "message_body",
        "source_action": "GetItem/GetConversationItems",
        "source_folder": folder,
    }


def event_record(item, browser_zone=None, detail=False):
    roster_verified = (
        detail
        and not item.get("HideAttendees", False)
        and all(key in item for key in ("RequiredAttendees", "OptionalAttendees"))
    )
    return {
        "id": item["ItemId"]["Id"],
        "subject": item.get("Subject", "(No subject)"),
        "start": {"dateTime": source_time(item["Start"], browser_zone), "timeZone": "UTC"},
        "end": {"dateTime": source_time(item["End"], browser_zone), "timeZone": "UTC"},
        "isAllDay": bool(item.get("IsAllDayEvent")),
        "isCancelled": bool(item.get("IsCancelled")),
        "showAs": item.get("FreeBusyType", "busy").lower(),
        "organizer": address(item.get("Organizer", {})),
        "body": {"content": body_text(item.get("Body")) if detail else item.get("Preview", "")[:40000]},
        "location": {"displayName": item.get("Location", {}).get("DisplayName", "")},
        "coverage": "calendar_detail" if detail else "calendar_summary",
        "source_action": "GetCalendarEvent" if detail else "GetCalendarView",
        "attendees": [
            {**address(value), "type": role}
            for key, role in (("RequiredAttendees", "required"), ("OptionalAttendees", "optional"))
            for value in item.get(key, [])
        ]
        if roster_verified
        else [],
        "attendees_complete": roster_verified,
        "details_as_of": datetime.now(timezone.utc).isoformat() if detail else None,
        "hasAttachments": bool(item.get("HasAttachments")),
        "attachments_indexed": False,
    }


class BrowserUnavailable(RuntimeError):
    def __init__(self, message, stage=None):
        super().__init__(message)
        self.stage = stage


class BrowserSyncPaused(Exception):
    pass


def authentication_page(url):
    """Classify normal identity redirects without permitting or inspecting authentication traffic."""
    parsed = urlsplit(url)
    return (
        parsed.scheme == "https"
        and parsed.hostname in {"login.microsoftonline.com", "login.external.hp.com"}
        and parsed.port in (None, 443)
        and not parsed.username
        and not parsed.password
    )


def outlook_page(url, section=None):
    parsed = urlsplit(url)
    return (
        parsed.scheme == "https"
        and parsed.hostname in HOSTS
        and parsed.port in (None, 443)
        and not parsed.username
        and not parsed.password
        and any(
            parsed.path.startswith("/" + value + "/")
            for value in ([section] if section else ["mail", "calendar"])
        )
    )


class BrowserOutlook:
    def __init__(self, settings):
        self.settings = settings
        self.profile = Path.home() / "snap/chromium/common/jarvis-outlook-profile"
        self.lock = asyncio.Lock()
        self.last_stage = "session"

    @asynccontextmanager
    async def session(self):
        # Import lazily: Graph-only installations do not need an active browser.
        from playwright.async_api import Error, async_playwright

        async with self.lock:
            try:
                port = int((self.profile / "DevToolsActivePort").read_text().splitlines()[0])
                if not 1024 <= port <= 65535:
                    raise ValueError()
            except (OSError, ValueError, IndexError):
                raise BrowserUnavailable("Open the private Nano Outlook browser to restore access.") from None
            async with async_playwright() as playwright:
                try:
                    browser = await playwright.chromium.connect_over_cdp(
                        f"http://127.0.0.1:{port}", timeout=10000
                    )
                    context = browser.contexts[0]
                    pages = [page for page in context.pages if outlook_page(page.url)]
                    if not pages:
                        raise AuthRequired("Complete normal HP sign-in in the Nano Outlook browser.")
                    yield context, pages[0]
                    # Stopping the Playwright transport detaches; never close Demo's browser/context.
                except Error:
                    raise BrowserUnavailable(
                        "Outlook could not complete "
                        + self.last_stage
                        + ". Check the private viewer and retry.",
                        stage=self.last_stage,
                    ) from None

    async def inspect(self):
        async with self.session() as (_, page):
            return {
                "signed_in_page": outlook_page(page.url),
                "mail_page": outlook_page(page.url, "mail"),
                "calendar_page": outlook_page(page.url, "calendar"),
                "coverage": "Browser session only; ingestion is not yet enabled.",
            }

    @asynccontextmanager
    async def temporary_page(self, context):
        """One read tab across API, tests and restarts; never close Demo's own tab."""
        lease = ProcessLock(self.profile / "jarvis-sync.lock")
        if not lease.acquire():
            raise BrowserUnavailable("An Outlook read is already running. No additional tab was opened.")
        marker = self.profile / "jarvis-sync-target.json"
        page = None
        try:
            try:
                owned = json.loads(marker.read_text()).get("target_id") if marker.exists() else None
            except (ValueError, OSError):
                owned = None
            if owned:
                # Only a target explicitly created and recorded by this connector is ours to close.
                for candidate in list(context.pages):
                    session = await context.new_cdp_session(candidate)
                    try:
                        target = (await session.send("Target.getTargetInfo"))["targetInfo"]["targetId"]
                    finally:
                        await session.detach()
                    if target == owned:
                        await self.close_owned_page(candidate)
                        break
            marker.unlink(missing_ok=True)
            if len(context.pages) >= 2:
                raise BrowserUnavailable(
                    "Outlook already has two open tabs. Close an unused tab before retrying; JARVIS will not open more.",
                    stage="browser capacity",
                )
            page = await context.new_page()
            session = await context.new_cdp_session(page)
            try:
                target = (await session.send("Target.getTargetInfo"))["targetInfo"]["targetId"]
            finally:
                await session.detach()
            with marker.open("w") as handle:
                if os.name != "nt":
                    os.fchmod(handle.fileno(), 0o600)
                json.dump({"target_id": target}, handle)
            yield page
        finally:
            original_error = sys.exc_info()[1]
            try:
                if page is not None:
                    await self.close_owned_page(page)
                    marker.unlink(missing_ok=True)
            except Exception:
                # Keep ownership for the next recovery attempt. Never mask an
                # authenticated read failure, deliberate pause or cancellation.
                if original_error is None:
                    raise
            finally:
                lease.release()

    @staticmethod
    async def close_owned_page(page):
        if page.is_closed():
            return
        try:
            await asyncio.wait_for(page.close(), timeout=5)
        except Exception:
            # Playwright can report a protocol error after the close event was
            # delivered. A confirmed closed tab is already successfully cleaned.
            if not page.is_closed():
                raise BrowserUnavailable(
                    "The temporary Outlook tab could not be closed; its ownership is retained for recovery.",
                    stage="temporary tab cleanup",
                ) from None

    async def read_snapshot(
        self, expand=4, history=None, known_bodies=None, should_pause=None, known_attachments=None
    ):
        """Bounded UI-driven read. Never replay private APIs or expose arbitrary browser actions."""
        from playwright.async_api import Error as BrowserError
        from playwright.async_api import TimeoutError as BrowserTimeout

        async with self.session() as (context, _), self.temporary_page(context) as page:
            # Route all requests from this temporary tab through the write filter, including pages with
            # an existing Outlook service worker. Demo's original tab is left unchanged.
            network = await context.new_cdp_session(page)
            await network.send("Network.setBypassServiceWorker", {"bypass": True})
            snapshots = {
                "previews": {},
                "messages": {},
                "events": {},
                "event_details": {},
                "folders": {},
                "blocked": set(),
                "zone": None,
                "folder_roles": {},
                "folder_pages": {},
                "history": {},
                "unavailable_responses": 0,
                "detail_timeouts": 0,
                "attachments": {},
                "attachment_failures": {},
                "thread_links": {},
            }
            active_folder = "inbox"
            tasks = []

            async def guard(route):
                request = route.request
                headers = {"action": await request.header_value("action") or ""}
                if read_request_allowed(request.method, request.url, headers):
                    await route.continue_()
                else:
                    # Store only fixed action class, never URLs, credentials or source text.
                    snapshots["blocked"].add(action_name(request.url, headers) or request.method)
                    await route.abort()

            async def capture(response, folder):
                parsed = urlsplit(response.url)
                if parsed.hostname not in HOSTS or parsed.path != "/owa/service.svc":
                    return
                try:
                    action = action_name(
                        response.url, {"action": await response.request.header_value("action") or ""}
                    )
                except Exception:
                    snapshots["unavailable_responses"] += 1
                    return
                if (
                    parsed.hostname not in HOSTS
                    or parsed.path != "/owa/service.svc"
                    or action not in DATA_ACTIONS
                    or not response.ok
                ):
                    return
                try:
                    payload = await response.json()
                except Exception:
                    # Chromium can evict a completed response before Playwright reads it. Required
                    # folder/calendar checks below still fail closed; missing bodies remain partial.
                    snapshots["unavailable_responses"] += 1
                    return
                try:
                    body = payload.get("Body", {})
                    if action == "GetTimeZone":
                        snapshots["zone"] = payload.get("CurrentTimeZone")
                    elif action == "FindFolder":
                        snapshots["folder_roles"].update(folder_roles(payload))
                    elif action == "FindConversation" and body.get("ResponseClass") == "Success":
                        folder_id = body["FolderId"]["Id"]
                        snapshots["folder_pages"][folder_id] = {
                            "total_conversations": body.get("TotalConversationsInView"),
                            "loaded": len(body.get("Conversations", [])),
                        }
                        for item in body.get("Conversations", []):
                            snapshots["previews"][(folder_id, item["ConversationId"]["Id"])] = item
                    elif action == "GetCalendarView" and body.get("ResponseClass") == "Success":
                        snapshots["calendar_verified"] = True
                        for item in body.get("Items", []):
                            snapshots["events"][item["ItemId"]["Id"]] = item
                    elif action == "GetCalendarEvent":
                        for result in body.get("ResponseMessages", {}).get("Items", []):
                            if result.get("ResponseClass") == "Success":
                                for item in result.get("Items", []):
                                    if item.get("Start") and item.get("End"):
                                        snapshots["event_details"][item["ItemId"]["Id"]] = item
                    elif action in {"GetItem", "GetConversationItems"}:
                        for result in body.get("ResponseMessages", {}).get("Items", []):
                            if result.get("ResponseClass") != "Success":
                                continue
                            items = result.get("Items", []) + [
                                item
                                for node in result.get("Conversation", {}).get("ConversationNodes", [])
                                for item in node.get("Items", [])
                            ]
                            for item in items:
                                if item.get("NormalizedBody") or item.get("Body"):
                                    snapshots["messages"][item["ItemId"]["Id"]] = (
                                        item.get("ParentFolderId", {}).get("Id"),
                                        item,
                                    )
                except (ValueError, KeyError, TypeError):
                    snapshots["schema_error"] = True

            page.on(
                "response",
                lambda response: tasks.append(asyncio.create_task(capture(response, active_folder))),
            )
            await page.route("**/*", guard)

            async def bind_folder(role, name):
                selected = page.locator(
                    '[role="treeitem"][aria-selected="true"][data-folder-name="' + name + '"]'
                )
                await selected.first.wait_for(timeout=10000)
                for _ in range(30):
                    visible = await page.locator('[role="option"][data-convid]').evaluate_all(
                        "es=>es.slice(0,10).map(e=>e.getAttribute('data-convid'))"
                    )
                    try:
                        # Conversations can belong to both Inbox and Sent. A deep
                        # Inbox scan must not make every subsequent Sent row ambiguous.
                        # Exclude only folders already bound to a different verified
                        # role; remaining unknown ambiguity still fails closed.
                        excluded = [
                            fid
                            for fid, verified_role in snapshots["folder_roles"].items()
                            if verified_role != role
                        ]
                        folder_id = bind_visible_folder(visible, snapshots["previews"], excluded)
                        snapshots["folder_roles"][folder_id] = role
                        return folder_id
                    except ValueError:
                        await page.wait_for_timeout(200)
                raise BrowserUnavailable(
                    "The selected Outlook folder could not be verified against its source records."
                )

            async def scan_folder(role, name):
                folder_id = await bind_folder(role, name)
                rounds, opened, stalled = 0, 0, False
                cutoff = datetime.now(timezone.utc) - timedelta(days=90)
                target = min(240, max(0, (history or {}).get(role, {}).get("next_rounds", 12)))
                deadline = asyncio.get_running_loop().time() + 60
                visited = set()
                box = page.get_by_role("listbox", name=re.compile("^Message list"))

                async def open_visible(limit):
                    nonlocal opened
                    options = box.get_by_role("option").filter(has_not_text="[Draft]")
                    identifiers = await options.evaluate_all(
                        "es=>es.map(e=>e.getAttribute('data-convid')).filter(Boolean)"
                    )
                    for cid in identifiers:
                        if should_pause and should_pause():
                            raise BrowserSyncPaused()
                        if opened >= limit:
                            break
                        preview = snapshots["previews"].get((folder_id, cid))
                        if (
                            cid in visited
                            or not re.fullmatch(r"[A-Za-z0-9+/=]+", cid)
                            or not preview
                            or not needs_body(preview, (known_bodies or {}).get(cid), snapshots["zone"])
                        ):
                            continue
                        visited.add(cid)
                        row = box.locator('[role="option"][data-convid="' + cid + '"]')
                        if await row.count():
                            try:
                                await row.click(timeout=5000)
                                await page.wait_for_timeout(700)
                                await asyncio.gather(*tasks)
                                if outlook_page(page.url, "mail") and "/id/" in urlsplit(page.url).path:
                                    snapshots["thread_links"][cid] = (
                                        urlsplit(page.url)._replace(query="", fragment="").geturl()
                                    )
                                if len(snapshots["attachments"]) + len(snapshots["attachment_failures"]) < 2:
                                    candidates = {}
                                    for _, raw in snapshots["messages"].values():
                                        if (
                                            raw.get("ConversationId", {}).get("Id") == cid
                                            and raw.get("Subject")
                                            and not raw.get("IsDraft")
                                        ):
                                            for attachment in attachment_manifest(raw):
                                                candidates[attachment["id"]] = (raw, attachment)
                                    for raw, attachment in candidates.values():
                                        if (
                                            attachment["id"] in (known_attachments or set())
                                            or attachment["id"] in snapshots["attachments"]
                                        ):
                                            continue
                                        if (
                                            len(snapshots["attachments"])
                                            + len(snapshots["attachment_failures"])
                                            >= 2
                                        ):
                                            break
                                        if (
                                            sum(
                                                a[1]["name"] == attachment["name"]
                                                for a in candidates.values()
                                            )
                                            != 1
                                        ):
                                            continue  # Same filename in multiple messages is ambiguous.
                                        if should_pause and should_pause():
                                            raise BrowserSyncPaused()
                                        try:
                                            data = await download_attachment(page, network, attachment)
                                            snapshots["attachments"][attachment["id"]] = {
                                                "attachment": attachment,
                                                "data": data,
                                                "message_id": raw["ItemId"]["Id"],
                                            }
                                        except (BrowserError, OSError, ValueError, TimeoutError):
                                            snapshots["attachment_failures"][attachment["id"]] = (
                                                "Download unavailable"
                                            )
                            except BrowserTimeout:
                                snapshots["detail_timeouts"] += 1
                                await page.keyboard.press("Escape")
                            opened += 1

                    await asyncio.gather(*tasks)

                # Newest mail first; the remainder of the budget follows a durable body cursor.
                await open_visible(min(expand, 2))
                while True:
                    if should_pause and should_pause():
                        raise BrowserSyncPaused()
                    await asyncio.gather(*tasks)
                    items = [item for (fid, _), item in snapshots["previews"].items() if fid == folder_id]
                    progress = history_progress(items, cutoff, rounds, stalled, snapshots["zone"])
                    if (
                        history is None
                        or progress["boundary_seen"]
                        or rounds >= target
                        or asyncio.get_running_loop().time() >= deadline
                    ):
                        break
                    before = len(items)
                    # Locate the observed virtual scroller by geometry, without brittle generated classes.
                    scroll = """e=>{
                        const n=[...e.querySelectorAll('.customScrollBar'),e,...e.querySelectorAll('*')].find(n=>n.clientHeight>100 &&
                          n.scrollHeight>n.clientHeight+10 && ['auto','scroll'].includes(getComputedStyle(n).overflowY));
                        if(!n) return false;
                        n.scrollTop=n.scrollHeight;
                        return true;
                    }"""
                    await page.wait_for_timeout(300)  # Give the virtual list time to render its new page.
                    moved = await box.evaluate(scroll)
                    if not moved:
                        stalled = True
                        break
                    for _ in range(15):
                        await page.wait_for_timeout(200)
                        await asyncio.gather(*tasks)
                        if sum(fid == folder_id for fid, _ in snapshots["previews"]) > before:
                            break
                    if sum(fid == folder_id for fid, _ in snapshots["previews"]) <= before:
                        # A jump may land before Outlook has extended its virtual scroll height.
                        await box.evaluate("""e=>{
                            const n=[...e.querySelectorAll('.customScrollBar'),e,...e.querySelectorAll('*')].find(n=>n.clientHeight>100 &&
                              n.scrollHeight>n.clientHeight+10 && ['auto','scroll'].includes(getComputedStyle(n).overflowY));
                            if(n) n.scrollTop=Math.max(0,n.scrollTop-n.clientHeight);
                        }""")
                        await page.wait_for_timeout(250)
                        await box.evaluate(scroll)
                        await page.wait_for_timeout(1500)
                        await asyncio.gather(*tasks)
                        if sum(fid == folder_id for fid, _ in snapshots["previews"]) <= before:
                            stalled = True
                            break
                    rounds += 1
                items = [item for (fid, _), item in snapshots["previews"].items() if fid == folder_id]
                snapshots["history"][role] = history_progress(
                    items, cutoff, rounds, stalled, snapshots["zone"]
                )
                if progress["boundary_seen"]:
                    snapshots["history"][role]["next_rounds"] = max(12, rounds)
                snapshots["history"][role] = merge_history(
                    (history or {}).get(role, {}), snapshots["history"][role]
                )

                if expand > opened:
                    body_deadline = asyncio.get_running_loop().time() + 25
                    cursor = max(0, min(10_000_000, (history or {}).get(role, {}).get("body_scroll", 0)))
                    scroll_to = """(e,y)=>{
                        const n=[...e.querySelectorAll('.customScrollBar'),e,...e.querySelectorAll('*')].find(n=>n.clientHeight>100 &&
                          n.scrollHeight>n.clientHeight+10 && ['auto','scroll'].includes(getComputedStyle(n).overflowY));
                        if(!n) return {top:0,height:e.clientHeight,max:0};
                        n.scrollTop=Math.min(y,n.scrollHeight-n.clientHeight);
                        return {top:n.scrollTop,height:n.clientHeight,max:n.scrollHeight-n.clientHeight};
                    }"""
                    for _ in range(80):
                        if should_pause and should_pause():
                            raise BrowserSyncPaused()
                        position = await box.evaluate(scroll_to, cursor)
                        cursor = position["top"]
                        await page.wait_for_timeout(250)
                        await open_visible(expand)
                        if opened >= expand or asyncio.get_running_loop().time() >= body_deadline:
                            break
                        if position["top"] >= position["max"] - 2:
                            cursor = 0  # Rescan for changed/new replies on a subsequent pass.
                            break
                        cursor += max(100, position["height"] * 0.75)
                    snapshots["history"][role]["body_scroll"] = cursor
                else:
                    snapshots["history"][role]["body_scroll"] = (
                        (history or {}).get(role, {}).get("body_scroll", 0)
                    )
                snapshots["history"][role]["body_read_attempts"] = opened

            async def read_meeting_details():
                if not expand:
                    return  # Connection tests remain bounded summary reads.
                view = page.get_by_role("main", name=re.compile("^calendar view"))
                events = view.locator('div[role="button"][aria-description][title]')
                for index in range(min(4, await events.count())):
                    if should_pause and should_pause():
                        raise BrowserSyncPaused()
                    try:
                        await events.nth(index).click(timeout=5000)
                        await page.wait_for_timeout(900)
                    except BrowserTimeout:
                        snapshots["detail_timeouts"] += 1
                    await page.keyboard.press("Escape")
                    await asyncio.gather(*tasks)

            try:
                self.last_stage = "mail navigation"
                await page.goto("https://outlook.office.com/mail/", wait_until="domcontentloaded")
                if not outlook_page(page.url, "mail"):
                    raise AuthRequired("Outlook needs normal HP sign-in again.")
                await page.get_by_role("listbox", name=re.compile("^Message list")).wait_for(timeout=30000)
                mailbox = page.get_by_role("region", name="Navigation pane", exact=True).get_by_role(
                    "treeitem", name=self.settings.outlook_browser_account, exact=True
                )
                if not await mailbox.count():
                    raise AuthRequired(
                        "The signed-in Outlook mailbox does not match Demo's configured HP identity."
                    )
                await page.wait_for_timeout(1200)
                await asyncio.gather(*tasks)
                self.last_stage = "Inbox history"
                await page.locator('[role="treeitem"][data-folder-name="inbox"]').first.click()
                await page.wait_for_timeout(500)
                await scan_folder("inbox", "inbox")
                active_folder = "sentitems"
                self.last_stage = "Sent navigation"
                await page.get_by_role("treeitem", name="Sent Items", exact=True).first.click()
                await page.wait_for_timeout(1800)
                await asyncio.gather(*tasks)
                self.last_stage = "Sent history"
                await scan_folder("sentitems", "sent items")
                self.last_stage = "calendar navigation"
                await page.get_by_role("button", name="Calendar", exact=True).click()
                await page.get_by_role("main", name=re.compile("^calendar view")).wait_for(timeout=30000)
                await page.wait_for_timeout(1200)
                self.last_stage = "calendar week view"
                # Include weekends in this temporary tab. The write filter blocks saved preference updates.
                await page.get_by_role("button", name="Week", exact=True).click()
                await page.wait_for_timeout(1000)
                self.last_stage = "next calendar week"
                await page.get_by_role("button", name=re.compile("^Go to next week")).click()
                await page.wait_for_timeout(1600)
                await asyncio.gather(*tasks)
                # Finish all range navigation before opening popovers: some Outlook detail views
                # keep navigation inaccessible even after Escape. Monday-Friday prefers this week.
                if datetime.now(ZoneInfo("America/Denver")).weekday() < 5:
                    self.last_stage = "current calendar week"
                    await page.get_by_role("button", name=re.compile("^Go to previous week")).click()
                    await page.wait_for_timeout(1000)
                self.last_stage = "selected meeting details"
                await read_meeting_details()
                await asyncio.gather(*tasks)
                snapshots["events"].update(snapshots["event_details"])
                roles = snapshots["folder_roles"]
                snapshots["folders"] = {
                    roles[key]: value for key, value in snapshots["folder_pages"].items() if key in roles
                }
                for role, progress in snapshots["history"].items():
                    snapshots["folders"][role]["loaded"] = progress["loaded"]
                snapshots["previews"] = {
                    (roles[folder_id], cid): item
                    for (folder_id, cid), item in snapshots["previews"].items()
                    if folder_id in roles
                }
                snapshots["messages"] = {
                    key: (roles.get(folder_id, "thread"), item)
                    for key, (folder_id, item) in snapshots["messages"].items()
                }
                if (
                    not snapshots["folders"].get("inbox")
                    or not snapshots["folders"].get("sentitems")
                    or not snapshots.get("calendar_verified")
                ):
                    raise BrowserUnavailable(
                        "Outlook did not return verified Inbox, Sent and calendar reads."
                    )
                if snapshots.get("schema_error"):
                    raise BrowserUnavailable(
                        "Outlook's read response format changed; synchronization stopped safely."
                    )
                return snapshots
            except BrowserError:
                if authentication_page(page.url):
                    raise AuthRequired(
                        "HP needs normal sign-in again in the private Nano Outlook browser. Saved records remain available."
                    ) from None
                raise
            finally:
                self.last_stage = "temporary tab cleanup" if not sys.exc_info()[0] else self.last_stage
                # The owned-page context performs the one bounded close. Cancel
                # in-flight response parsing before it tears down the target.
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    async def test(self):
        snapshot = await self.read_snapshot(expand=0)
        return {"mail": True, "sent": True, "calendar": True, "zone": snapshot["zone"], "read_only": True}

    async def sync(self, db, inference):
        if self.lock.locked():
            return {"already_running": True}

        def paused():
            write_lease = db.get_state("outlook.packet_write_lease", {})
            write_until = write_lease.get("until")
            return (
                inference.interactive
                or db.get_state("demo_mode", False)
                or not db.get_state("setup.enabled.outlook", False)
                or bool(write_until and datetime.fromisoformat(write_until) > datetime.now(timezone.utc))
            )

        if paused():
            return {"paused": True}
        known = db.query("""SELECT metadata->>'conversationId' AS cid,max(occurred_at) AS newest
            FROM records WHERE provider='outlook_browser' AND NOT deleted
            AND metadata->>'coverage'='message_body' GROUP BY metadata->>'conversationId'""")
        known_bodies = {
            row["cid"]: row["newest"].isoformat() for row in known if row["cid"] and row["newest"]
        }
        known_attachments = {
            row["id"]
            for row in db.query(
                "SELECT metadata->>'attachment_id' AS id FROM records WHERE kind='document' AND provider='outlook_browser' AND NOT deleted"
            )
            if row["id"]
        }
        failures = db.get_state("outlook.attachment.failures", {})
        for row in db.query(
            "SELECT metadata FROM records WHERE kind='email' AND provider='outlook_browser' AND NOT deleted AND metadata ? 'attachments'"
        ):
            for attachment in row["metadata"].get("attachments", []):
                if failures.get(hashlib.sha256(attachment["id"].encode()).hexdigest(), 0) >= 3:
                    known_attachments.add(attachment["id"])
                if attachment["id"] not in known_attachments:
                    known_bodies.pop(row["metadata"].get("conversationId"), None)
        try:
            snapshot = await self.read_snapshot(
                expand=8,
                history=db.get_state("outlook.browser.history", {}),
                known_bodies=known_bodies,
                should_pause=paused,
                known_attachments=known_attachments,
            )
        except BrowserSyncPaused:
            return {"paused": True}
        if (
            inference.interactive
            or db.get_state("demo_mode", False)
            or not db.get_state("setup.enabled.outlook", False)
        ):
            return {"paused": True}
        now = datetime.now(timezone.utc).isoformat()
        counts = {"previews": 0, "messages": 0, "events": 0, "attachments": 0}
        full_sources = []
        for (folder, _), item in snapshot["previews"].items():
            if counts["previews"] % 25 == 0:
                await asyncio.sleep(0)
                if paused():
                    return {"paused": True, "processed": counts}
            message = message_record(item, folder, preview=True, browser_zone=snapshot["zone"])
            role = (
                "sentitems"
                if message["from"]["emailAddress"]["address"].lower()
                == self.settings.outlook_browser_account.lower()
                else folder
            )
            ingest_message(db, message, folder, role, provider="outlook_browser")
            counts["previews"] += 1
        for folder, item in snapshot["messages"].values():
            await asyncio.sleep(0)
            if paused():
                return {"paused": True, "processed": counts}
            message = message_record(item, folder, browser_zone=snapshot["zone"])
            if message:
                link = snapshot.get("thread_links", {}).get(message.get("conversationId"))
                if link:
                    message["webLink"] = link
                role = (
                    "sentitems"
                    if message["from"]["emailAddress"]["address"].lower()
                    == self.settings.outlook_browser_account.lower()
                    else folder
                )
                ingest_message(db, message, folder, role, provider="outlook_browser")
                full_sources.append((message, role))
                counts["messages"] += 1
        for item in snapshot.get("attachments", {}).values():
            if paused():
                return {"paused": True, "processed": counts}
            try:
                stored = await asyncio.to_thread(store_attachment, db, self.settings.data_dir, item)
            except (OSError, ValueError):
                snapshot.setdefault("attachment_failures", {})[item["attachment"]["id"]] = (
                    "Local attachment read unavailable"
                )
                continue
            if stored["characters"]:
                Jobs(db).enqueue(
                    "index_document",
                    "index-attachment:" + stored["id"] + ":" + stored["sha256"],
                    datetime.now(timezone.utc),
                    {"source_id": stored["id"]},
                    priority=8,
                )
                Jobs(db).enqueue(
                    "extract_source",
                    "extract-attachment:" + stored["id"] + ":" + stored["sha256"],
                    datetime.now(timezone.utc),
                    {"source_id": stored["id"]},
                    priority=8,
                )
            counts["attachments"] += 1
        for identifier in snapshot.get("attachment_failures", {}):
            digest = hashlib.sha256(identifier.encode()).hexdigest()
            failures[digest] = failures.get(digest, 0) + 1
        if snapshot.get("attachment_failures"):
            db.set_state("outlook.attachment.failures", failures)
        for identifier, item in snapshot["events"].items():
            ingest_event(
                db,
                event_record(item, snapshot["zone"], detail=identifier in snapshot.get("event_details", {})),
                provider="outlook_browser",
            )
            counts["events"] += 1
        for message, role in full_sources:
            queue_browser_intelligence(db, message, role, self.settings.outlook_browser_account)
        # Suppress a preview only when a body at least as recent was captured; retain newer unseen replies.
        db.execute("""UPDATE records p SET deleted=true WHERE p.provider='outlook_browser' AND p.metadata->>'coverage'='conversation_preview'
            AND EXISTS (SELECT 1 FROM records m WHERE m.provider='outlook_browser' AND NOT m.deleted
            AND m.metadata->>'coverage'='message_body' AND m.metadata->>'conversationId'=p.metadata->>'conversationId'
            AND m.occurred_at>=p.occurred_at)""")
        indexed = db.one("""SELECT
            count(*) FILTER (WHERE kind='email' AND metadata->>'coverage'='conversation_preview') AS previews,
            count(*) FILTER (WHERE kind='email' AND metadata->>'coverage'='message_body') AS messages,
            count(*) FILTER (WHERE kind='event') AS events,
            count(*) FILTER (WHERE kind='document') AS attachments
            FROM records WHERE provider='outlook_browser' AND NOT deleted""")
        coverage = {
            "provider": "outlook_browser",
            "history_complete": False,
            "calendar_snapshot_complete": False,
            "scope": "Progressive Inbox and Sent previews toward 90 days, bounded thread bodies and supported visible attachments, current and next displayed calendar week with selected meeting details. Not a full mailbox or 90-day ingestion. Remaining attachments and meeting details are incomplete.",
            "last_batch": counts,
            "timezone": snapshot["zone"],
            "folders": snapshot["folders"],
            "history": snapshot.get("history", {}),
            "indexed": indexed,
            "unavailable_responses": snapshot.get("unavailable_responses", 0),
            "meeting_details_in_batch": len(snapshot.get("event_details", {})),
            "detail_timeouts": snapshot.get("detail_timeouts", 0),
            "attachment_failures_in_batch": len(snapshot.get("attachment_failures", {})),
        }
        if snapshot.get("history"):
            db.set_state("outlook.browser.history", snapshot["history"])
        db.set_state("outlook.ingestion", coverage)
        db.set_state("outlook.last_sync", now)
        db.set_state("outlook.error", None)
        db.set_state(
            "setup.check.outlook",
            {
                "state": "Ready",
                "checked_at": now,
                "last_success": now,
                "detail": "Read-only Nano browser synchronization verified Inbox, Sent and calendar access. Coverage is partial; 90-day history is not complete.",
            },
        )
        db.audit(
            "outlook.browser_synced",
            details={**counts, "read_only": True, "blocked_request_classes": sorted(snapshot["blocked"])},
        )
        return {**counts, "coverage": coverage}
