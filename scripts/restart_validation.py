"""Nano recovery exercise with synthetic records only; preserves runtime data."""

import json
import subprocess
import time
from pathlib import Path

import httpx

from jarvis.config import settings
from jarvis.db import Database
from jarvis.policy import Approvals


def wait_api():
    for _ in range(30):
        try:
            if httpx.get("http://127.0.0.1:8100/health/live", timeout=2).is_success:
                return
        except httpx.HTTPError:
            pass
        time.sleep(1)
    raise RuntimeError("API did not recover")


def main():
    db = Database(settings.database_url)
    engine = Approvals(db)
    action = engine.propose("calendar.create", {"subject": "SYNTHETIC restart validation only"})
    subprocess.run(["systemctl", "--user", "restart", "jarvis-api"], check=True)
    wait_api()
    assert engine.get(action["id"])["status"] == "pending"
    subprocess.run(["docker", "restart", "jarvis-postgres"], check=True, capture_output=True)
    for _ in range(20):
        try:
            if db.one("SELECT 1 AS ready"):
                break
        except Exception:
            time.sleep(1)
    assert engine.get(action["id"])["status"] == "pending"
    db.execute("UPDATE approvals SET status='denied' WHERE id=%s", (action["id"],))
    db.audit(
        "validation.restart",
        action["id"],
        {"api_restart": True, "database_restart": True, "approval_survived": True},
    )
    result = {"api_restart": "passed", "database_restart": "passed", "pending_approval_survived": True}
    Path("data/restart-validation.json").write_text(json.dumps(result, indent=2))
    db.close()
    print(json.dumps(result))


if __name__ == "__main__":
    main()
