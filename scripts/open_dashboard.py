"""Single-instance Windows launcher. The access key never enters the page DOM."""

import argparse
import json
import msvcrt
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from browser_qa import access_token
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = ROOT / "secrets"
PRIVATE.mkdir(exist_ok=True)
REQUEST = PRIVATE / "launcher-request.json"
parser = argparse.ArgumentParser()
parser.add_argument("--setup", action="store_true")
parser.add_argument("--microsoft-sign-in", action="store_true")
parser.add_argument("--validate-setup", action="store_true")
parser.add_argument("--diagnose-voice", action="store_true")
parser.add_argument("--voice-review", action="store_true")
args = parser.parse_args()
request = vars(args)
temporary = PRIVATE / "launcher-request.tmp"
temporary.write_text(json.dumps(request))
temporary.replace(REQUEST)
lock = (PRIVATE / "launcher.lock").open("a+b")
if os.fstat(lock.fileno()).st_size == 0:
    lock.write(b"1")
    lock.flush()
lock.seek(0)
try:
    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
except OSError:
    sys.exit(0)  # The existing launcher handles the view request and foregrounds its window.


def private_sign_in(context):
    token = access_token()
    response = context.request.post("http://127.0.0.1:13100/api/auth/login", data={"token": token})
    token = None
    valid = response.ok
    response.dispose()
    if not valid:
        raise RuntimeError("Private sign-in unavailable")


try:
    with sync_playwright() as p:
        preferences = PRIVATE / "dashboard-profile/Default/Preferences"
        preferences.parent.mkdir(parents=True, exist_ok=True)
        prefs = json.loads(preferences.read_text()) if preferences.exists() else {}
        prefs["credentials_enable_service"] = False
        prefs.setdefault("profile", {})["password_manager_enabled"] = False
        preferences.write_text(json.dumps(prefs))
        context = p.chromium.launch_persistent_context(
            str(PRIVATE / "dashboard-profile"), headless=False, viewport=None
        )
        page = context.pages[0] if context.pages else context.new_page()
        microsoft = None
        voice_probe = None
        while context.pages:
            if REQUEST.exists():
                task = json.loads(REQUEST.read_text())
                REQUEST.unlink(missing_ok=True)
                # Context request cookies are shared with the browser. Never inject the long-lived key into DOM or URL.
                private_sign_in(context)
                page.goto(
                    "http://127.0.0.1:13100/"
                    + ("#setup" if task.get("setup") or task.get("microsoft_sign_in") else "")
                )
                page.locator("#workspace").wait_for(state="visible", timeout=20000)
                page.bring_to_front()
                (ROOT / "artifacts").mkdir(exist_ok=True)
                (ROOT / "artifacts/launcher-opened.json").write_text(
                    json.dumps(
                        {
                            "view": "Account Setup"
                            if task.get("setup") or task.get("microsoft_sign_in")
                            else "Today",
                            "automatic_auth": True,
                            "at": datetime.now(timezone.utc).isoformat(),
                        }
                    )
                )
                if task.get("microsoft_sign_in"):
                    status = context.request.get("http://127.0.0.1:13100/api/setup")
                    outlook = status.json()["integrations"]["outlook"] if status.ok else {}
                    configured = outlook.get("configured", False)
                    status.dispose()
                    if outlook.get("connection_method") == "browser":
                        subprocess.Popen(
                            [
                                "powershell.exe",
                                "-NoProfile",
                                "-File",
                                str(ROOT / "scripts/open_nano_outlook.ps1"),
                            ],
                            cwd=ROOT,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            creationflags=subprocess.CREATE_NO_WINDOW,
                        )
                        page.evaluate(
                            "toast('Complete normal HP sign-in in the private Nano Outlook window.')"
                        )
                    elif configured and microsoft is None:
                        microsoft = subprocess.Popen(
                            [sys.executable, str(ROOT / "scripts/microsoft_interactive.py")],
                            cwd=ROOT,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            creationflags=subprocess.CREATE_NO_WINDOW,
                        )
                        page.evaluate(
                            "toast('Complete your HP sign-in in the Microsoft window. Return here afterward.')"
                        )
                    elif not configured:
                        page.evaluate(
                            "toast('Add your HP-approved application and tenant IDs in Account Setup first.')"
                        )
                if task.get("validate_setup"):
                    from onboarding_browser_checks import validate

                    report = validate(page, context)
                    (ROOT / "artifacts").mkdir(exist_ok=True)
                    (ROOT / "artifacts/launcher-setup-validation.json").write_text(
                        json.dumps(report, indent=2)
                    )
                if task.get("diagnose_voice"):
                    from voice_browser_probe import VoiceBrowserProbe

                    voice_probe = VoiceBrowserProbe(page, ROOT / "artifacts")
                    voice_probe.start()
                if task.get("voice_review"):
                    page.evaluate("voiceDiagnostics.open = true; voiceLab.open = true;")
                    page.locator("#voice-button").scroll_into_view_if_needed()
            if microsoft is not None and microsoft.poll() is not None:
                success = microsoft.returncode == 0
                microsoft = None
                if success:
                    page.wait_for_timeout(1000)  # The cache import restarts the core API.
                    private_sign_in(context)
                    page.goto("http://127.0.0.1:13100/#setup")
                    page.locator("#workspace").wait_for(state="visible")
                page.evaluate(
                    "message => toast(message)",
                    "Microsoft sign-in completed. Test the connection, then enable Outlook."
                    if success
                    else "Microsoft sign-in was not completed. Review HP consent and application settings in Account Setup.",
                )
            try:
                page.wait_for_timeout(500)
                if voice_probe:
                    voice_probe.sample()
            except Exception:
                break
except Exception as error:
    (ROOT / "artifacts").mkdir(exist_ok=True)
    (ROOT / "artifacts/launcher-error.json").write_text(json.dumps({"error_class": type(error).__name__}))
    import tkinter.messagebox

    tkinter.messagebox.showerror(
        "JARVIS",
        "I could not open your private workspace. Check that the Nano is powered on and connected, then reopen JARVIS. No account credentials were changed.",
    )
finally:
    lock.close()
