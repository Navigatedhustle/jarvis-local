"""Narrow, fail-closed Outlook UI transport for approved self-review PDF packets.

No cookies or tokens are exported. Only the browser's own observed requests run.
Every permitted mutation is bound to one action, one draft and immutable PDF bytes.
"""

import asyncio
import base64
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote, urlsplit

from .browser_outlook import HOSTS, BrowserOutlook, action_name, body_text, read_request_allowed
from .content_packets import RECIPIENT
from .packet_delivery import attach
from .policy import Approvals, digest
from .process_lock import ProcessLock

EXTRA_READS = {"FindItem", "GetDlpPolicyTips", "GetMailTips", "GetExtensibilityContext"}


def normalize(value):
    return " ".join(value.split())


def recipient_matches(values):
    return (
        isinstance(values, list)
        and len(values) == 1
        and values[0].get("EmailAddress", "").lower() == RECIPIENT
        and values[0].get("RoutingType", "SMTP") == "SMTP"
    )


def exchange_items(value):
    if isinstance(value, dict):
        if value.get("__type") == "Message:#Exchange" and "ItemId" in value:
            yield value
        for child in value.values():
            yield from exchange_items(child)
    elif isinstance(value, list):
        for child in value:
            yield from exchange_items(child)


def success_body(data):
    body = data.get("Body", {})
    items = body.get("ResponseMessages", {}).get("Items", [])
    if not items or any(
        item.get("ResponseClass") != "Success" or item.get("ResponseCode") != "NoError" for item in items
    ):
        raise ValueError("Outlook did not confirm the operation")
    return body


class PacketGuard:
    def __init__(self, payload, state):
        self.payload, self.state = payload, state

    def message_matches(self, item, allow_empty_to=False):
        message = self.payload["message"]
        return (
            item.get("__type") == "Message:#Exchange"
            and item.get("Subject") == message["subject"]
            and normalize(body_text(item.get("Body"))) == normalize(message["body"]["content"])
            and (
                recipient_matches(item.get("ToRecipients"))
                or (allow_empty_to and item.get("ToRecipients") == [])
            )
            and not any(
                item.get(k)
                for k in (
                    "CcRecipients",
                    "BccRecipients",
                    "ReplyTo",
                    "Attachments",
                    "appendOnSend",
                    "prependOnSend",
                    "atMentionedRecipients",
                    "internetHeaders",
                    "internetHeadersToRemove",
                )
            )
            and (not item.get("From") or item["From"].get("EmailAddress", "").lower() == RECIPIENT)
        )

    def create(self, body):
        items = body.get("Items", [])
        if (
            self.state.get("draft_started")
            or body.get("MessageDisposition") != "SaveOnly"
            or len(items) != 1
            or not self.message_matches(items[0], True)
        ):
            return False
        item = items[0]
        if item.get("mailboxInfo", {}).get("mailboxSmtpAddress", "").lower() != RECIPIENT:
            return False
        # Outlook may autosave before resolving its recipient chip. Pin it to the grant.
        item["ToRecipients"] = [{"EmailAddress": RECIPIENT, "RoutingType": "SMTP", "MailboxType": "OneOff"}]
        return True

    def upload(self, body, data):
        files, manifest = body.get("Attachments", []), self.payload["packet"]
        return (
            not self.state.get("upload_started")
            and bool(self.state.get("draft_id"))
            and body.get("ParentItemId", {}).get("Id") == self.state["draft_id"]
            and body.get("SliceNumber") == 0
            and body.get("TotalSlices") == 1
            and len(files) == 1
            and files[0].get("Name") == manifest["filename"]
            and files[0].get("ContentType") == "application/pdf"
            and files[0].get("Size") == manifest["bytes"] == len(data)
            and not files[0].get("IsInline")
            and not files[0].get("Content")
            and files[0].get("AttachmentId", {}).get("mailboxInfo", {}).get("mailboxSmtpAddress", "").lower()
            == RECIPIENT
            and hashlib.sha256(data).hexdigest() == manifest["sha256"]
        )

    def send(self, body):
        if (
            not self.state.get("attachment_id")
            or self.state.get("send_started")
            or not self.state.get("draft_verified")
            or body.get("MessageDisposition") != "SendAndSaveCopy"
        ):
            return False
        changes = body.get("ItemChanges", [])
        if len(changes) != 1 or changes[0].get("ItemId", {}).get("Id") != self.state.get("draft_id"):
            return False
        fields = {}
        for change in changes[0].get("Updates", []):
            path, item = change.get("Path", {}), change.get("Item", {})
            if change.get("__type") != "SetItemField:#Exchange" or item.get("__type") != "Message:#Exchange":
                return False
            field = path.get("FieldURI")
            if field:
                if field in fields or set(item) != {"__type", field}:
                    return False
                fields[field] = item[field]
            else:
                # Do not clear or override corporate labels. The normal UI may echo empty labels.
                name = path.get("PropertyName")
                props = item.get("ExtendedProperty", [])
                if (
                    name not in {"msip_labels", "InformationProtectionLabelId"}
                    or len(props) != 1
                    or props[0].get("Value") != ""
                    or props[0].get("ExtendedFieldURI") != path
                ):
                    return False
        required = {"ToRecipients", "CcRecipients", "BccRecipients", "Subject", "Body"}
        defaults = {
            "Importance": "Normal",
            "Sensitivity": "Normal",
            "IsReadReceiptRequested": False,
            "IsDeliveryReceiptRequested": False,
            "IsSendIndividually": False,
            "ReplyTo": [],
            "MentionsEx": [],
        }
        if not required.issubset(fields) or set(fields) - required - defaults.keys():
            return False
        if any(fields[k] != v for k, v in defaults.items() if k in fields):
            return False
        return self.message_matches({"__type": "Message:#Exchange", **fields})

    def verify_draft(self, item):
        clean = {k: v for k, v in item.items() if k != "Attachments"}
        files = item.get("Attachments", [])
        if (
            not self.message_matches(clean)
            or item.get("ItemId", {}).get("Id") != self.state.get("draft_id")
            or not item.get("IsDraft")
        ):
            return False
        if any(
            p.get("Value")
            for p in item.get("ExtendedProperty", [])
            if p.get("ExtendedFieldURI", {}).get("PropertyName")
            in {"msip_labels", "InformationProtectionLabelId"}
        ):
            return False
        return (
            len(files) == 1
            and files[0].get("Name") == self.payload["packet"]["filename"]
            and files[0].get("AttachmentId", {}).get("Id") == self.state.get("attachment_id")
        )


class PacketBrowser:
    def __init__(self, settings, db):
        self.settings, self.db = settings, db
        self.connector = BrowserOutlook(settings)

    async def send(self, payload, action_id):
        approval = Approvals(self.db).get(action_id)
        if (
            approval["status"] != "executing"
            or approval["digest"] != digest("email.send", payload)
            or approval["expires_at"] <= datetime.now(timezone.utc)
        ):
            raise ValueError("An exact executing approval is required")
        message = attach(self.settings, payload)
        if self.settings.outlook_browser_account.lower() != RECIPIENT:
            raise ValueError("Only Demo's verified HP mailbox is supported")
        key = "packet.transport:" + action_id
        state = self.db.get_state(key, {})
        if state.get("send_started"):
            raise ValueError("Previous send needs reconciliation; retry refused")
        guard = PacketGuard(payload, state)
        state["draft_verified"] = False
        captures, tasks, failures = [], [], []

        def save():
            self.db.set_state(key, state)

        self.db.set_state(
            "outlook.packet_write_lease",
            {
                "action_id": action_id,
                "until": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
            },
        )
        try:
            # The read worker observes the short lease and checkpoints its work.
            for _ in range(100):
                lease = ProcessLock(self.connector.profile / "jarvis-sync.lock")
                if lease.acquire():
                    lease.release()
                    break
                await asyncio.sleep(0.3)
            async with (
                asyncio.timeout(180),
                self.connector.session() as (context, _),
                self.connector.temporary_page(context) as page,
            ):
                network = await context.new_cdp_session(page)
                await network.send("Network.setBypassServiceWorker", {"bypass": True})

                async def capture(response):
                    try:
                        request = response.request
                        if urlsplit(request.url).path != "/owa/service.svc":
                            return
                        action = action_name(
                            request.url, {"action": await request.header_value("action") or ""}
                        )
                        if action in {"GetItem", "FindItem", "GetConversationItems"} and response.ok:
                            captures.extend(exchange_items(await response.json()))
                    except Exception:
                        # An unrelated response can be evicted by Chromium. Only an
                        # actually verified saved draft can unlock sending below.
                        pass

                async def route_request(route):
                    try:
                        request = route.request
                        parsed = urlsplit(request.url)
                        headers = {"action": await request.header_value("action") or ""}
                        action = action_name(request.url, headers)
                        endpoint = (
                            parsed.scheme == "https"
                            and parsed.hostname in HOSTS
                            and parsed.port in (None, 443)
                            and not parsed.username
                            and not parsed.password
                            and request.method == "POST"
                        )
                        if read_request_allowed(request.method, request.url, headers) or (
                            endpoint and parsed.path == "/owa/service.svc" and action in EXTRA_READS
                        ):
                            await route.continue_()
                            return
                        if self.db.get_state("demo_mode", False) or approval["expires_at"] <= datetime.now(
                            timezone.utc
                        ):
                            await route.abort()
                            return
                        from .packet_schedule import POLICY, eligible

                        if approval["actor"] == POLICY and not eligible(
                            self.db, payload["packet"]["edition"], datetime.now(timezone.utc)
                        ):
                            await route.abort()
                            return
                        operation = None
                        replacement = None
                        if endpoint and parsed.path == "/owa/service.svc":
                            envelope = json.loads(request.post_data or "{}")
                            body = envelope.get("Body", {})
                            if action == "CreateItem" and guard.create(body):
                                operation = "draft"
                                body["Items"][0]["Body"] = {
                                    "BodyType": "Text",
                                    "Value": message["body"]["content"],
                                }
                                replacement = json.dumps(envelope)
                            elif action == "UpdateItem" and guard.send(body):
                                operation = "send"
                                body["SendOnNotFoundError"] = False
                                for update in body["ItemChanges"][0]["Updates"]:
                                    if update.get("Path", {}).get("FieldURI") == "Body":
                                        update["Item"]["Body"] = {
                                            "BodyType": "Text",
                                            "Value": message["body"]["content"],
                                        }
                                replacement = json.dumps(envelope)
                        elif endpoint and parsed.path == "/owa/service.svc/CreateAttachmentFromLocalFile":
                            body = json.loads(
                                unquote(await request.header_value("x-owa-urlpostdata") or "{}")
                            ).get("Body", {})
                            if guard.upload(body, request.post_data_buffer or b""):
                                operation = "upload"
                        if not operation:
                            await route.abort()
                            return
                        state[operation + "_started"] = True
                        save()  # Persist before I/O; a crash can never cause a blind repeat.
                        response = await route.fetch(
                            max_retries=0,
                            max_redirects=0,
                            **({"post_data": replacement} if replacement else {}),
                        )
                        body = success_body(await response.json())
                        if operation == "draft":
                            items = list(exchange_items(body))
                            if len(items) != 1 or not guard.message_matches(items[0]):
                                raise ValueError("Created draft differs from reviewed message")
                            state["draft_id"] = items[0]["ItemId"]["Id"]
                        elif operation == "upload":
                            attachment = body["ResponseMessages"]["Items"][0]["Attachments"][0][
                                "AttachmentId"
                            ]
                            if attachment["RootItemId"] != state["draft_id"]:
                                raise ValueError("Attachment belongs to another message")
                            state["attachment_id"] = attachment["Id"]
                        else:
                            state["send_confirmed"] = True
                        save()
                        await route.fulfill(response=response)
                    except Exception as error:
                        failures.append(type(error).__name__)
                        # Never let Playwright stringify request bodies/headers in an event error.
                        try:
                            await route.abort()
                        except Exception:
                            pass

                page.on("response", lambda response: tasks.append(asyncio.create_task(capture(response))))
                await page.route("**/*", route_request)
                await page.goto("https://outlook.office.com/mail/", wait_until="domcontentloaded")
                await (
                    page.get_by_role("region", name="Navigation pane", exact=True)
                    .get_by_role("treeitem", name=RECIPIENT, exact=True)
                    .wait_for(timeout=30000)
                )

                async def open_draft():
                    await page.get_by_role("treeitem", name=re.compile(r"^Drafts")).click()
                    await page.get_by_text(message["subject"], exact=True).click(timeout=20000)
                    await page.get_by_role("textbox", name="Subject", exact=True).wait_for()

                if state.get("draft_id"):
                    await open_draft()
                else:
                    if state.get("draft_started"):
                        raise ValueError("Draft creation needs reconciliation")
                    await page.get_by_role("button", name="New mail", exact=True).click()
                    target = page.locator('[contenteditable][aria-label="To"]')
                    await target.fill(RECIPIENT)
                    await target.press("Enter")
                    await page.get_by_role("textbox", name="Subject", exact=True).fill(message["subject"])
                    await page.get_by_role("textbox", name="Message body", exact=True).fill(
                        message["body"]["content"]
                    )
                if not state.get("attachment_id"):
                    if state.get("upload_started"):
                        raise ValueError("Attachment upload needs reconciliation")
                    file = message["attachments"][0]
                    await (
                        page.locator('input[type="file"]')
                        .nth(1)
                        .set_input_files(
                            {
                                "name": file["name"],
                                "mimeType": "application/pdf",
                                "buffer": base64.b64decode(file["contentBytes"]),
                            }
                        )
                    )
                    for _ in range(150):
                        if state.get("attachment_id") or failures:
                            break
                        await page.wait_for_timeout(200)
                    if not state.get("attachment_id"):
                        raise ValueError("Outlook has not confirmed the PDF upload")
                    # Reopen the saved draft so send validation uses server evidence.
                    await page.goto("https://outlook.office.com/mail/", wait_until="domcontentloaded")
                    await open_draft()
                for _ in range(50):
                    if any(guard.verify_draft(item) for item in captures):
                        state["draft_verified"] = True
                        save()
                        break
                    await page.wait_for_timeout(200)
                if not state.get("draft_verified") or failures:
                    raise ValueError("Saved draft or attachment could not be verified")
                await page.get_by_role("button", name="Send", exact=True).click()
                for _ in range(150):
                    if state.get("send_confirmed") or failures:
                        break
                    await page.wait_for_timeout(200)
                if not state.get("send_confirmed"):
                    raise ValueError("Outlook send result needs reconciliation")
                await asyncio.gather(*tasks)
                return {
                    "sent": True,
                    "provider": "outlook_browser",
                    "recipient": RECIPIENT,
                    "attachment_sha256": payload["packet"]["sha256"],
                    "outlook_confirmed": True,
                }
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            lease = self.db.get_state("outlook.packet_write_lease", {})
            if lease.get("action_id") == action_id:
                self.db.set_state("outlook.packet_write_lease", {})
