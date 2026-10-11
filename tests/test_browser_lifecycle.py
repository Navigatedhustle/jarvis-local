import asyncio
import json
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jarvis.browser_outlook import BrowserOutlook, BrowserSyncPaused, BrowserUnavailable
from jarvis.process_lock import ProcessLock


def test_process_lease_blocks_another_process_and_recovers_after_exit(tmp_path):
    path = tmp_path / "lease"
    lease = ProcessLock(path)
    assert lease.acquire()
    script = "from pathlib import Path; from jarvis.process_lock import ProcessLock; import sys,os; lock=ProcessLock(Path(sys.argv[1])); os._exit(0 if lock.acquire() else 3)"
    assert subprocess.run([sys.executable, "-c", script, str(path)]).returncode == 3
    lease.release()
    assert subprocess.run([sys.executable, "-c", script, str(path)]).returncode == 0
    assert lease.acquire()  # Abrupt child exit released the OS lease.
    lease.release()


class Page:
    def __init__(self, context, identifier):
        self.context, self.identifier = context, identifier
        self.closed = False

    def is_closed(self):
        return self.closed

    async def close(self):
        self.closed = True
        if self in self.context.pages:
            self.context.pages.remove(self)


class Context:
    def __init__(self, identifiers):
        self.pages = [Page(self, i) for i in identifiers]
        self.created = 0

    async def new_page(self):
        self.created += 1
        page = Page(self, "sync-" + str(self.created))
        self.pages.append(page)
        return page

    async def new_cdp_session(self, page):
        return SimpleNamespace(
            send=AsyncMock(return_value={"targetInfo": {"targetId": page.identifier}}),
            detach=AsyncMock(),
        )


def connector(tmp_path):
    result = BrowserOutlook(SimpleNamespace())
    result.profile = tmp_path
    return result


async def test_stale_owned_target_is_cleaned_without_closing_demo_tab(tmp_path):
    context = Context(["demo", "abandoned-sync"])
    original, old = context.pages
    (tmp_path / "jarvis-sync-target.json").write_text(json.dumps({"target_id": "abandoned-sync"}))
    async with connector(tmp_path).temporary_page(context) as page:
        assert old.closed and not original.closed
        assert len(context.pages) == 2
        assert page.identifier == "sync-1"
    assert context.pages == [original]
    assert not (tmp_path / "jarvis-sync-target.json").exists()


async def test_concurrent_connectors_cannot_create_more_tabs(tmp_path):
    context = Context(["demo"])
    async with connector(tmp_path).temporary_page(context):
        with pytest.raises(BrowserUnavailable, match="already running"):
            async with connector(tmp_path).temporary_page(context):
                pytest.fail("Must not enter a second read")
        assert context.created == 1
    assert len(context.pages) == 1


async def test_unknown_tabs_are_preserved_and_capacity_stops_growth(tmp_path):
    context = Context(["demo", "other-user-tab"])
    (tmp_path / "jarvis-sync-target.json").write_text('{"target_id":"old-browser-target"}')
    with pytest.raises(BrowserUnavailable, match="two open tabs"):
        async with connector(tmp_path).temporary_page(context):
            pytest.fail("No extra tab may open")
    assert context.created == 0 and len(context.pages) == 2


@pytest.mark.parametrize("error", [RuntimeError, asyncio.CancelledError])
async def test_failed_or_cancelled_read_closes_its_tab_and_releases_lease(tmp_path, error):
    context = Context(["demo"])
    browser = connector(tmp_path)
    with pytest.raises(error):
        async with browser.temporary_page(context):
            raise error()
    assert len(context.pages) == 1
    async with connector(tmp_path).temporary_page(context):
        assert len(context.pages) == 2


async def test_close_error_after_confirmed_close_is_successful_cleanup(tmp_path):
    context = Context(["demo"])
    async with connector(tmp_path).temporary_page(context) as page:
        close = page.close

        async def closed_then_error():
            await close()
            raise Exception("Synthetic driver disposal race")

        page.close = closed_then_error
    assert len(context.pages) == 1
    assert not (tmp_path / "jarvis-sync-target.json").exists()


async def test_unclosed_target_retains_ownership_and_prevents_growth(tmp_path):
    context = Context(["demo"])
    with pytest.raises(BrowserUnavailable, match="could not be closed"):
        async with connector(tmp_path).temporary_page(context) as page:
            page.close = AsyncMock(side_effect=Exception("Synthetic close failure"))
    assert (tmp_path / "jarvis-sync-target.json").exists()
    assert len(context.pages) == 2
    with pytest.raises(BrowserUnavailable):
        async with connector(tmp_path).temporary_page(context):
            pytest.fail("An unclosed owned tab must block new tabs")
    assert context.created == 1


async def test_cleanup_failure_does_not_replace_foreground_pause(tmp_path):
    context = Context(["demo"])
    with pytest.raises(BrowserSyncPaused):
        async with connector(tmp_path).temporary_page(context) as page:
            page.close = AsyncMock(side_effect=Exception("Synthetic cleanup failure"))
            raise BrowserSyncPaused()
    assert len(context.pages) == 2 and context.created == 1
    assert (tmp_path / "jarvis-sync-target.json").exists()
