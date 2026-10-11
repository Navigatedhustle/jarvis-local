"""Actual launcher/browser setup QA. Never enters or connects a real provider credential."""

import json
from pathlib import Path

from playwright.sync_api import expect


def validate(page, context):
    folder = Path(__file__).resolve().parents[1] / "artifacts"
    folder.mkdir(exist_ok=True)
    errors, requests = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("request", lambda request: requests.append(request.url))
    page.get_by_role("navigation").get_by_role("button", name="Account Setup", exact=True).click()
    expect(page.get_by_role("heading", name="System Readiness", exact=True)).to_be_visible(timeout=20000)
    assert page.locator("#token").input_value() == "", "Launcher must never fill the credential field"
    for name in ("JARVIS Core", "Local AI", "Microsoft Outlook", "Telegram", "Twilio"):
        expect(page.get_by_role("heading", name=name, exact=True)).to_be_visible()
    for provider in ("outlook", "telegram", "twilio"):
        card = page.locator(f'[data-integration="{provider}"]')
        expect(card.locator(".setup-pill")).to_have_text("Not Connected")
        expect(card).to_contain_text("Last successful connection: Not yet verified")
        card.get_by_role("button", name="Test connection", exact=True).click()
        expect(page.locator(f'[data-integration="{provider}"]')).to_contain_text(
            "No external connection was attempted", timeout=20000
        )
    assert page.locator("#system-readiness .readiness-row").count() == 10
    expect(page.locator("#setup-advanced")).not_to_have_attribute("open", "")
    page.locator("#setup-advanced summary").click()
    expect(page.locator("#setup-advanced")).to_contain_text("127.0.0.1:8181")
    expect(page.locator("#setup-advanced")).not_to_contain_text("JARVIS_API_TOKEN")
    page.locator("#setup-advanced summary").click()
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.evaluate("window.scrollTo(0, 0)")
    expect(page.locator("#toast")).to_be_hidden(timeout=7000)
    page.screenshot(path=str(folder / "account-setup-desktop.png"), full_page=True)
    page.set_viewport_size({"width": 390, "height": 844})
    page.evaluate("window.scrollTo(0, 0)")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Mobile overflow"
    page.screenshot(path=str(folder / "account-setup-mobile.png"), full_page=True)
    # A fresh browser context has no launcher cookie. Manual fallback stays collapsed.
    locked = context.browser.new_context(viewport={"width": 1100, "height": 900})
    try:
        fallback = locked.new_page()
        fallback.goto("http://127.0.0.1:13100")
        expect(fallback.get_by_role("heading", name="Welcome back, Demo.")).to_be_visible()
        expect(fallback.get_by_label("Local access key", exact=True)).to_be_hidden()
        fallback.screenshot(path=str(folder / "launcher-sign-in.png"))
        fallback.locator("#manual-access summary").click()
        expect(fallback.get_by_label("Local access key", exact=True)).to_be_visible()
        fallback.get_by_label("Local access key", exact=True).fill("invalid-key")
        fallback.get_by_role("button", name="Unlock workspace", exact=True).click()
        expect(fallback.locator("#login-error")).to_contain_text("Incorrect access token")
    finally:
        locked.close()
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.evaluate("window.scrollTo(0, 0)")
    page.bring_to_front()
    assert not errors, errors
    forbidden = [
        url
        for url in requests
        if any(
            part in url for part in ("/configure", "/enable", "/disconnect", "/test-call", "/outlook/connect")
        )
    ]
    assert not forbidden, forbidden
    result = {
        "normal_launcher": True,
        "automatic_sign_in_without_key_in_dom": True,
        "integrations": "all unconnected; no credential or authorization actions performed",
        "readiness_checks": 10,
        "unconfigured_tests": "no external request",
        "manual_fallback": "collapsed; invalid key rejected",
        "mobile_overflow": False,
        "page_errors": errors,
    }
    (folder / "onboarding-browser-validation.json").write_text(json.dumps(result, indent=2))
    return result
