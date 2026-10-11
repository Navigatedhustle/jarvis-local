"""Explicitly approved synthetic UI test. Never permits calendar/mail mutation requests.

Separate from application execution. The CLI action ID acknowledges a human gate;
it does not mint a JARVIS approval or authorize a real Outlook write.
"""

import argparse
import asyncio
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from jarvis.browser_outlook import HOSTS, BrowserOutlook, action_name, read_request_allowed
from jarvis.config import settings

ACTION = "JARVIS-OUTLOOK-BLOCKED-WRITE-QA-20260920-01"
CALENDAR_TITLE = "JARVIS blocked preview: no event created"
CALENDAR_BODY = "Private integration validation. This request is blocked before transmission."
MAIL_TITLE = "JARVIS blocked preview: no message sent"
MAIL_BODY = "Private integration validation. Nothing is transmitted."


async def run(mode):
    blocked = []
    connector = BrowserOutlook(settings)
    async with connector.session() as (context, _), connector.temporary_page(context) as page:
        network = await context.new_cdp_session(page)
        await network.send("Network.setBypassServiceWorker", {"bypass": True})

        async def guard(route):
            request = route.request
            headers = {"action": await request.header_value("action") or ""}
            if read_request_allowed(request.method, request.url, headers):
                await route.continue_()
                return
            # Abort first. No branch in this script permits save, update or send requests.
            await route.abort()
            parsed = urlsplit(request.url)
            if request.method == "POST" and parsed.hostname in HOSTS and parsed.path.endswith("service.svc"):
                try:
                    blocked.append(
                        {
                            "action": action_name(request.url, headers),
                            "body": json.loads(request.post_data or "{}").get("Body"),
                        }
                    )
                except (ValueError, AttributeError):
                    pass

        await page.route("**/*", guard)
        await page.goto("https://outlook.office.com/mail/", wait_until="domcontentloaded")
        mailbox = page.get_by_role("region", name="Navigation pane", exact=True).get_by_role(
            "treeitem", name=settings.outlook_browser_account, exact=True
        )
        await mailbox.wait_for(timeout=15000)
        if mode == "calendar":
            await page.get_by_role("button", name="Calendar", exact=True).click()
            await page.get_by_role("button", name="New event", exact=True).click()
            title = page.get_by_role("textbox", name="Add title", exact=True)
            await title.fill(CALENDAR_TITLE)
            await page.get_by_role("textbox", name="Type / to draft with Copilot", exact=True).fill(
                CALENDAR_BODY
            )
            await page.get_by_role("button", name=re.compile(r"^[A-Z][a-z]{2} [0-9]+")).first.click()
            for label, value in (
                ("Start date", "9/23/2026"),
                ("Start time", "5:00 PM"),
                ("End time", "5:15 PM"),
            ):
                field = page.get_by_role("combobox", name=label, exact=True)
                await field.fill(value)
                await field.press("Tab")
            await page.get_by_role("button", name="Time zone", exact=True).click()
            # Fail closed if the existing Outlook display timezone differs from the reviewed test.
            for label in ("Start Time Zone", "End Time Zone"):
                value = await page.get_by_role("combobox", name=label, exact=True).input_value()
                if "Mountain Time (US & Canada)" not in value:
                    raise ValueError("The test's reviewed timezone no longer matches Outlook")
            await page.get_by_role("textbox", name="Add title", exact=True, include_hidden=True).click(
                force=True
            )
            await page.get_by_role("button", name="Save", exact=True).click(timeout=5000)
        else:
            await page.get_by_role("button", name="New mail", exact=True).click()
            target = page.locator('[contenteditable][aria-label="To"]')
            await target.fill(settings.outlook_browser_account)
            await target.press("Enter")
            await page.get_by_role("textbox", name="Subject", exact=True).fill(MAIL_TITLE)
            await page.get_by_role("textbox", name="Message body", exact=True).fill(MAIL_BODY)
            await page.wait_for_timeout(1500)
            await page.get_by_role("button", name="Send", exact=True).click(timeout=5000)
        await page.wait_for_timeout(2000)
        if not blocked:
            raise ValueError("No blocked mutation was observed; no write-validation claim is possible")
        destination = Path("data") / f"{ACTION}-{mode}.json"
        with os.fdopen(os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as output:
            json.dump(blocked, output)
        return {
            "action_id": ACTION,
            "mode": mode,
            "external_writes": False,
            "blocked_actions": sorted({r["action"] for r in blocked}),
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--approved-action", required=True, choices=[ACTION])
    parser.add_argument("--mode", required=True, choices=["calendar", "mail"])
    args = parser.parse_args()
    try:
        print(json.dumps(asyncio.run(run(args.mode))))
    except Exception as error:
        print(json.dumps({"action_id": ACTION, "failed": True, "error_class": type(error).__name__}))
        raise SystemExit(1) from None
