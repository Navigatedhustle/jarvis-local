"""Controlled API restart acceptance; preserves the existing Outlook browser and human tab."""

import asyncio
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import httpx
from playwright.async_api import async_playwright

from jarvis.config import settings
from jarvis.db import Database


async def browser_state():
    port = int(
        (Path.home() / "snap/chromium/common/jarvis-outlook-profile/DevToolsActivePort")
        .read_text()
        .splitlines()[0]
    )
    async with async_playwright() as playwright:
        browser = await playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        targets = []
        for context in browser.contexts:
            for page in context.pages:
                session = await context.new_cdp_session(page)
                targets.append((await session.send("Target.getTargetInfo"))["targetInfo"]["targetId"])
                await session.detach()
        return {"contexts": len(browser.contexts), "tabs": len(targets)}, targets


async def main():
    db = Database(settings.database_url)
    async with httpx.AsyncClient(
        headers={"Authorization": "Bearer " + settings.api_token}, timeout=10
    ) as client:
        health = await client.get("http://127.0.0.1:8100/system/workload")
        health.raise_for_status()
        if health.json().get("interactive_active"):
            raise RuntimeError("Live interaction prevents restart acceptance")
    before, targets = await browser_state()
    marker = Path.home() / "snap/chromium/common/jarvis-outlook-profile/jarvis-sync-target.json"
    # Identify the human page by excluding the connector-owned target when present.
    owned = json.loads(marker.read_text()).get("target_id") if marker.exists() else None
    human = set(targets) - {owned}
    active_read = bool(db.one("SELECT id FROM jobs WHERE kind='outlook_sync' AND status='running'"))
    began = datetime.now(timezone.utc)
    subprocess.run(["systemctl", "--user", "restart", "jarvis-api.service"], check=True, timeout=60)
    await asyncio.sleep(2)
    after, remaining = await browser_state()
    failures = db.one(
        "SELECT count(*) n FROM audit WHERE at>=%s AND action IN ('job.pending','job.failed') AND details->>'kind'='outlook_sync'",
        (began,),
    )["n"]
    assert after["contexts"] == 1 and after["tabs"] <= 2
    assert human <= set(remaining), "Human Outlook page changed during API restart"
    assert failures == 0, "API restart was reported as an Outlook read failure"
    report = {
        "restarted_api": True,
        "active_read_at_restart": active_read,
        "browser_before": before,
        "browser_after": after,
        "human_page_preserved": True,
        "new_outlook_failures": failures,
        "captured_at": began.isoformat(),
    }
    Path("data/restart-phase11.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report))
    db.close()


if __name__ == "__main__":
    asyncio.run(main())
