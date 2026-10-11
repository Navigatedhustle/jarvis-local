"""Read-only Sent/Inbox verification for an exact self-email action. Never retries sends."""

import argparse
import asyncio
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from jarvis.browser_outlook import HOSTS, BrowserOutlook, action_name, body_text, read_request_allowed
from jarvis.config import settings
from jarvis.content_packets import RECIPIENT
from jarvis.db import Database
from jarvis.packet_browser import EXTRA_READS, exchange_items, normalize, recipient_matches
from jarvis.packet_delivery import attach
from jarvis.policy import Approvals
from jarvis.process_lock import ProcessLock


async def main(action_id):
    db = Database(settings.database_url)
    row = Approvals(db).get(action_id)
    message = attach(settings, row["payload"])
    connector = BrowserOutlook(settings)
    result, tasks = {}, []
    db.set_state(
        "outlook.packet_write_lease",
        {"action_id": action_id, "until": (datetime.now(timezone.utc) + timedelta(minutes=3)).isoformat()},
    )
    try:
        for _ in range(100):
            lease = ProcessLock(connector.profile / "jarvis-sync.lock")
            if lease.acquire():
                lease.release()
                break
            await asyncio.sleep(0.3)
        async with connector.session() as (context, _), connector.temporary_page(context) as page:
            network = await context.new_cdp_session(page)
            await network.send("Network.setBypassServiceWorker", {"bypass": True})
            items = []

            async def guard(route):
                request = route.request
                headers = {"action": await request.header_value("action") or ""}
                parsed = urlsplit(request.url)
                if read_request_allowed(request.method, request.url, headers) or (
                    parsed.scheme == "https"
                    and parsed.hostname in HOSTS
                    and parsed.path == "/owa/service.svc"
                    and action_name(request.url, headers) in EXTRA_READS
                ):
                    await route.continue_()
                else:
                    await route.abort()

            async def capture(response):
                try:
                    if urlsplit(response.url).path == "/owa/service.svc" and action_name(
                        response.url, {"action": await response.request.header_value("action") or ""}
                    ) in {"GetItem", "GetConversationItems"}:
                        items.extend(exchange_items(await response.json()))
                except Exception:
                    pass

            page.on("response", lambda r: tasks.append(asyncio.create_task(capture(r))))
            await page.route("**/*", guard)
            await page.goto("https://outlook.office.com/mail/", wait_until="domcontentloaded")
            for folder in ("Sent Items", "Inbox"):
                items.clear()
                if folder == "Inbox":
                    await page.locator('[role="treeitem"][data-folder-name="inbox"]').first.click()
                else:
                    await page.get_by_role("treeitem", name=folder, exact=True).first.click()
                target = page.get_by_text(message["subject"], exact=True).first
                await target.wait_for(timeout=20000)
                await target.click()
                await page.wait_for_timeout(1500)
                await asyncio.gather(*tasks)
                found = [
                    i
                    for i in items
                    if i.get("Subject") == message["subject"]
                    and not i.get("IsDraft")
                    and normalize(body_text(i.get("NormalizedBody") or i.get("Body")))
                    == normalize(message["body"]["content"])
                    and recipient_matches(i.get("ToRecipients"))
                    and i.get("From", {}).get("Mailbox", i.get("From", {})).get("EmailAddress", "").lower()
                    == RECIPIENT
                    and len(i.get("Attachments", [])) == 1
                    and i["Attachments"][0].get("Name") == row["payload"]["packet"]["filename"]
                ]
                result[folder] = {
                    "visible": True,
                    "content_verified": bool(found),
                    "attachment_visible": await page.get_by_text(
                        row["payload"]["packet"]["filename"], exact=False
                    ).count()
                    > 0,
                }
            result["verified_at"] = datetime.now(timezone.utc).isoformat()
            result["action_id"] = action_id
            db.set_state("packet.reconciliation:" + action_id, result)
            db.audit(
                "packet_delivery.reconciled", action_id, {k: v for k, v in result.items() if k != "action_id"}
            )
            print(json.dumps(result))
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if db.get_state("outlook.packet_write_lease", {}).get("action_id") == action_id:
            db.set_state("outlook.packet_write_lease", {})
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--action-id", required=True)
    args = parser.parse_args()
    try:
        asyncio.run(main(args.action_id))
    except Exception as error:
        print(json.dumps({"failed": True, "error_class": type(error).__name__}))
        raise SystemExit(1) from None
