"""Trusted operator CLI; never exposed as an LLM tool."""

import argparse
import subprocess
from pathlib import Path

from .backups import create_backup
from .config import settings
from .db import Database

MODELS = [
    "jarvis-primary-model",
    "jarvis-background-model",
    "jarvis-embedding",
    "jarvis-reranker",
    "jarvis-stt",
    "jarvis-tts",
]
APPS = ["jarvis-api", "jarvis-dashboard"]


def service(command, *names, check=True):
    return subprocess.run(["systemctl", "--user", command, *names], check=check)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["start", "stop", "status", "demo-mode", "resume", "backup"])
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    if args.command == "status":
        service("is-active", *APPS, *MODELS, check=False)
        return
    if args.command == "backup":
        print("Private backup created:", create_backup(root))
        return
    db = Database(settings.database_url)
    try:
        if args.command in {"demo-mode", "stop"}:
            (root / "data/demo-mode").touch(mode=0o600)
            with db.transaction() as conn:
                db.set_state("demo_mode", True, conn=conn)
                db.audit(
                    "mode.demo" if args.command == "demo-mode" else "mode.stopped",
                    actor="demo",
                    source="local_cli",
                    conn=conn,
                )
            service("stop", "jarvis-api")
            service("stop", *MODELS)
            if args.command == "stop":
                service("stop", "jarvis-dashboard")
            else:
                service("start", "jarvis-api")
            print("JARVIS proactive contact and background work paused. Database preserved.")
        else:
            campus = [
                "campuscompass-app",
                "campuscompass-model",
                "campuscompass-embedding",
                "campuscompass-reranker",
            ]
            for scope in (["--user"], []):
                for unit in campus:
                    if (
                        subprocess.run(
                            ["systemctl", *scope, "is-active", "--quiet", unit], check=False
                        ).returncode
                        == 0
                    ):
                        raise SystemExit(
                            "CampusCompass is active. Stop its workload deliberately before resuming JARVIS."
                        )
            (root / "data/demo-mode").unlink(missing_ok=True)
            service("start", *MODELS)
            db.set_state("demo_mode", False)
            db.audit("mode.resumed", actor="demo", source="local_cli")
            service("start", *APPS)
            print("JARVIS services started. Models warm up independently; check dashboard readiness.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
