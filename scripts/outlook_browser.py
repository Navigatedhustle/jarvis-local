"""Legitimate Outlook Web fallback preparation on Windows. Never reads passwords or exports cookies.

Login creates a dedicated persistent browser session; read-only visible snapshots are available
after normal HP sign-in. Full folder/attachment parity requires verification against Demo's UI.
"""

import argparse
import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--snapshot",
        action="store_true",
        help="Capture the visible signed-in Outlook view into private local data",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    profile = root / "secrets/outlook-profile"
    profile.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(str(profile), headless=False, channel="msedge")
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto("https://outlook.office.com/mail/")
        print(
            "Complete normal HP Microsoft sign-in in the dedicated browser window. JARVIS does not collect passwords or bypass MFA."
        )
        if args.snapshot:
            await page.wait_for_url("https://outlook.office.com/mail/**", timeout=300000)
            await page.get_by_role("main").first.wait_for(timeout=300000)
            text = await page.get_by_role("main").first.inner_text()
            folder = root / "data/browser-outlook"
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "visible-view.json").write_text(
                json.dumps(
                    {"url": page.url, "text": text, "trust": "untrusted", "coverage": "visible view only"}
                ),
                encoding="utf-8",
            )
            print("Visible view saved privately. No email or calendar writes performed.")
        while context.pages:
            try:
                await page.wait_for_timeout(1000)
            except Exception:
                break


if __name__ == "__main__":
    asyncio.run(main())
