import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

from jarvis import inference as module
from jarvis import workload
from jarvis.inference import Inference, InferenceDeferred
from jarvis.scheduler import Jobs
from jarvis.workload import CURRENT_PRIORITY, WorkloadGuard, work_priority


def test_pressure_and_latency_guard_does_not_block_interactive_or_urgent(db, monkeypatch):
    db.set_state("feature.HPOS_WORKLOAD_GUARD", True)
    model = SimpleNamespace(interactive=0)
    guard = WorkloadGuard(model, db)
    monkeypatch.setattr(workload, "available_memory", lambda: (2 * 1024**3, 128 * 1024**3))
    assert guard.reason(7) == "memory_pressure"
    assert guard.reason(3) is None
    monkeypatch.setattr(workload, "available_memory", lambda: (30 * 1024**3, 128 * 1024**3))
    guard.observe(31000)
    assert guard.reason(5) == "latency_cooldown"
    assert guard.reason(1) is None
    model.interactive = 1
    assert guard.reason(7) == "interactive"


def test_flag_off_and_priority_context_are_safe(db):
    guard = WorkloadGuard(SimpleNamespace(interactive=0), db)
    assert not guard.enabled
    assert CURRENT_PRIORITY.get() == 1
    with guard.interaction(0):
        assert guard.inference.interactive == 1 and guard.active_requests[0] == 1
    assert guard.inference.interactive == 0 and guard.active_requests[0] == 0
    with work_priority(6):
        assert CURRENT_PRIORITY.get() == 6
    assert CURRENT_PRIORITY.get() == 1


async def test_background_generation_is_cancelled_when_interaction_begins(monkeypatch):
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def request(_):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    original = httpx.AsyncClient
    monkeypatch.setattr(
        module.httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(request))
    )
    model = Inference(SimpleNamespace(background_url="http://local.invalid"))
    pending = asyncio.create_task(model.chat([], background=True))
    await asyncio.wait_for(started.wait(), 1)
    model.interactive = 1
    with pytest.raises(InferenceDeferred):
        await asyncio.wait_for(pending, 1)
    assert cancelled.is_set()


async def test_background_attention_does_not_mark_itself_interactive(db, monkeypatch):
    db.set_state("feature.HPOS_WORKLOAD_GUARD", True)
    monkeypatch.setattr(workload, "available_memory", lambda: (30 * 1024**3, 128 * 1024**3))
    model = Inference(SimpleNamespace(primary_url="http://local.invalid"), db=db)
    original = httpx.AsyncClient

    async def request(_):
        assert model.interactive == 0
        return httpx.Response(200, json={"choices": [{"message": {"content": "Prepared."}}]})

    monkeypatch.setattr(
        module.httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(request))
    )
    with work_priority(5):
        await model.chat([])
    assert model.interactive == 0


async def test_pressure_defers_internal_job_without_exhausting_retries(db, monkeypatch):
    db.set_state("feature.HPOS_WORKLOAD_GUARD", True)
    monkeypatch.setattr(workload, "available_memory", lambda: (1, 128 * 1024**3))
    model = Inference(SimpleNamespace(), db=db)
    jobs = Jobs(db)
    jobs.enqueue("attention_prepare", "guarded", datetime.now(timezone.utc))

    async def must_not_run(_):
        pytest.fail("Resource-deferred handler ran")

    await jobs.run_one({"attention_prepare": must_not_run}, inference=model)
    assert db.one("SELECT status,attempts FROM jobs") == {"status": "pending", "attempts": 0}


def test_guarded_queue_uses_authoritative_work_priorities(db):
    db.set_state("feature.HPOS_WORKLOAD_GUARD", True)
    jobs = Jobs(db)
    now = datetime.now(timezone.utc)
    jobs.enqueue("extract_chunk", "history", now, priority=0)
    jobs.enqueue("important_email", "important", now, priority=9)
    assert jobs.claim()["kind"] == "important_email"
