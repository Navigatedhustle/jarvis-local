"""Restart only JARVIS's private browser when an enabled browser integration needs it."""

import json
import subprocess
from pathlib import Path

from jarvis.config import settings
from jarvis.db import Database


def ensure(db, runner=subprocess.run):
    if (
        db.get_state("outlook.provider") != "browser"
        or not db.get_state("setup.enabled.outlook", False)
        or db.get_state("demo_mode", False)
    ):
        return {"skipped": "Browser integration is disconnected or paused"}
    result = runner(
        ["systemctl", "--user", "is-active", "--quiet", "jarvis-outlook-desktop"],
        capture_output=True,
        timeout=10,
    )
    if result.returncode == 0:
        return {"running": True}
    runner(
        ["/usr/bin/python3", str(Path(__file__).with_name("nano_outlook_desktop.py")), "start"],
        capture_output=True,
        timeout=30,
        check=True,
    )
    db.audit("outlook.browser_restart_requested", details={"normal_hp_authentication_required": True})
    return {"started": True, "authentication": "Normal HP sign-in may be required"}


if __name__ == "__main__":
    try:
        db = Database(settings.database_url)
        try:
            print(json.dumps(ensure(db)))
        finally:
            db.close()
    except Exception as error:
        # Do not expose connection strings, process arguments or browser data in service logs.
        print(json.dumps({"error_class": type(error).__name__}))
        raise SystemExit(1) from None
