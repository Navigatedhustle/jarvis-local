import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from .intelligence import ingest_event, ingest_message
from .outlook import DeltaExpired, initial_mail_delta, initial_mail_history
from .scheduler import Jobs


class OutlookSync:
    def __init__(self, db, provider, inference, browser=None):
        self.db, self.provider, self.inference = db, provider, inference
        self.browser = browser

    async def tick(self):
        if (
            self.inference.interactive
            or self.db.get_state("demo_mode", False)
            or not self.db.get_state("setup.enabled.outlook", True)
        ):
            return {"paused": True}
        if self.db.get_state("outlook.provider") == "browser":
            if not self.browser:
                raise RuntimeError("The Outlook browser connector is unavailable")
            return await self.browser.sync(self.db, self.inference)
        folders = self.db.get_state("outlook.folders")
        refreshed = self.db.get_state("outlook.folders_refreshed")
        now = datetime.now(timezone.utc)
        if not folders or not refreshed or now - datetime.fromisoformat(refreshed) > timedelta(hours=6):
            folders = await self.provider.folders()
            self.db.set_state("outlook.folders", folders)
            self.db.set_state("outlook.folders_refreshed", now.isoformat())
        # One page per folder per tick. Transactional upserts make replay after a crash safe.
        count = 0
        for folder in folders:
            if self.inference.interactive:
                break
            key = "outlook.delta." + folder["id"]
            history_done = self.db.get_state(key + ".history_done", False)
            path = self.db.get_state(key) or (
                initial_mail_delta(folder["id"]) if history_done else initial_mail_history(folder["id"])
            )
            try:
                page = await self.provider.request("GET", path)
            except DeltaExpired:
                self.db.set_state(key, None)
                self.db.set_state(key + ".history_done", False)
                continue
            for item in page.get("value", []):
                ingest_message(
                    self.db, item, folder.get("displayName", ""), folder.get("jarvis_role", "received")
                )
                if "@removed" not in item:
                    identifier = "graph:mail:" + item["id"]
                    record = self.db.one("SELECT metadata FROM records WHERE id=%s", (identifier,))
                    explicit_commitment = bool(
                        re.search(
                            r"\b(i(?:'ll| will)|we(?:'ll| will))\s+(send|provide|share|finish|deliver|follow up|review)",
                            item.get("body", {}).get("content", ""),
                            re.I,
                        )
                    )
                    if record["metadata"]["triage"]["actionable"] or explicit_commitment:
                        Jobs(self.db).enqueue(
                            "extract_source",
                            "extract:" + identifier,
                            now,
                            {"source_id": identifier},
                            priority=8,
                        )
                        received = item.get("receivedDateTime")
                        recent = received and datetime.fromisoformat(
                            received.replace("Z", "+00:00")
                        ) >= now - timedelta(days=7)
                        if (
                            record["metadata"]["triage"]["priority"] == "high"
                            and recent
                            and not item.get("isRead", False)
                        ):
                            Jobs(self.db).enqueue(
                                "important_email",
                                "draft:" + identifier,
                                now,
                                {"source_id": identifier},
                                priority=6,
                            )
                count += 1
            continuation = page.get("@odata.nextLink") or page.get("@odata.deltaLink")
            if continuation:
                self.provider.safe_url(continuation)
                self.db.set_state(key, continuation)
            elif not history_done:
                self.db.set_state(key + ".history_done", True)
                self.db.set_state(key, initial_mail_delta(folder["id"]))
        # Rolling calendar window. Reinitialize weekly to include future meetings beyond the old range.
        window = self.db.get_state("outlook.calendar_window")
        if not window or now - datetime.fromisoformat(window) > timedelta(days=7):
            calendar_path = "/me/calendarView/delta?" + urlencode(
                {
                    "startDateTime": (now - timedelta(days=7)).isoformat(),
                    "endDateTime": (now + timedelta(days=90)).isoformat(),
                }
            )
            self.db.set_state("outlook.calendar_window", now.isoformat())
            self.db.set_state("outlook.calendar_delta", calendar_path)
        try:
            page = await self.provider.request("GET", self.db.get_state("outlook.calendar_delta"))
        except DeltaExpired:
            self.db.set_state("outlook.calendar_window", None)
            return {"calendar_cursor_reset": True}
        for event in page.get("value", []):
            ingest_event(self.db, event)
        continuation = page.get("@odata.nextLink") or page.get("@odata.deltaLink")
        if continuation:
            self.provider.safe_url(continuation)
            self.db.set_state("outlook.calendar_delta", continuation)
        self.db.set_state("outlook.last_sync", now.isoformat())
        completed = sum(
            bool(self.db.get_state("outlook.delta." + f["id"] + ".history_done", False)) for f in folders
        )
        self.db.set_state(
            "outlook.ingestion",
            {
                "folders_total": len(folders),
                "folders_history_complete": completed,
                "history_complete": completed == len(folders),
                "calendar_snapshot_complete": "@odata.deltaLink" in page,
            },
        )
        self.db.set_state("outlook.error", None)
        return {"messages_processed": count}
