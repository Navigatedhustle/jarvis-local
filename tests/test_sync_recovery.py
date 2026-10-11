from types import SimpleNamespace

from jarvis.outlook import DeltaExpired, GraphProvider
from jarvis.sync import OutlookSync


class Provider:
    safe_url = staticmethod(GraphProvider.safe_url)

    def __init__(self, expire=False):
        self.expire = expire

    async def folders(self):
        return [{"id": "inbox", "displayName": "Inbox"}]

    async def request(self, method, path):
        if "calendarView" in path:
            return {
                "value": [],
                "@odata.deltaLink": "https://graph.microsoft.com/v1.0/me/calendarView/delta?token=end",
            }
        if self.expire:
            self.expire = False
            raise DeltaExpired()
        return {
            "value": [{"id": "synthetic", "subject": "Routine FYI", "body": {"content": "No action needed"}}]
        }


async def test_90_day_history_transitions_to_delta_and_replays_safely(db):
    sync = OutlookSync(db, Provider(), SimpleNamespace(interactive=0))
    await sync.tick()
    assert db.get_state("outlook.delta.inbox.history_done")
    assert "/messages/delta?" in db.get_state("outlook.delta.inbox")
    await sync.tick()
    assert db.one("SELECT count(*) AS count FROM records")["count"] == 1
    assert db.get_state("outlook.ingestion")["history_complete"]


async def test_expired_delta_resets_to_recoverable_history(db):
    db.set_state("outlook.delta.inbox.history_done", True)
    sync = OutlookSync(db, Provider(expire=True), SimpleNamespace(interactive=0))
    await sync.tick()
    assert not db.get_state("outlook.delta.inbox.history_done")
    assert db.get_state("outlook.delta.inbox") is None


async def test_interactive_work_and_demo_pause_sync(db):
    inference = SimpleNamespace(interactive=1)
    sync = OutlookSync(db, Provider(), inference)
    assert (await sync.tick())["paused"]
    inference.interactive = 0
    db.set_state("demo_mode", True)
    assert (await sync.tick())["paused"]
