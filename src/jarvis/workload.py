"""Explicit lightweight work priorities; existing workers remain the execution boundary."""

import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

CURRENT_PRIORITY = ContextVar("jarvis_work_priority", default=1)
PRIORITIES = {
    "voice": 0,
    "text": 1,
    "approval": 2,
    "meeting_prepare": 3,
    "morning_call": 3,
    "notify": 3,
    "health_notice": 3,
    "important_email": 4,
    "outlook_sync": 4,
    "command_refresh": 4,
    "attention_prepare": 5,
    "morning_prepare": 5,
    "research_brief": 5,
    "content_packet": 7,
    "packet_deliver": 6,
    "project_refresh": 5,
    "executive_draft": 3,
    "manual_email": 3,
    "relationship_refresh": 5,
    "extract_source": 6,
    "memory_consolidate": 6,
    "memory_views": 6,
    "extract_chunk": 7,
    "index_document": 7,
    "history_ingest": 7,
}
RETRYABLE_INTERNAL = {
    "packet_deliver",  # Only InferenceDeferred before any send; transport failures remain uncertain.
    "content_packet",
    "research_brief",
    "manual_email",
    "executive_draft",
    "relationship_refresh",
    "attention_prepare",
    "morning_prepare",
    "important_email",
    "extract_source",
    "extract_chunk",
    "index_document",
    "project_refresh",
    "memory_consolidate",
    "memory_views",
    "history_ingest",
    "command_refresh",
}


@contextmanager
def work_priority(priority):
    token = CURRENT_PRIORITY.set(priority)
    try:
        yield
    finally:
        CURRENT_PRIORITY.reset(token)


def available_memory():
    try:
        values = {
            line.split(":")[0]: int(line.split()[1]) * 1024
            for line in Path("/proc/meminfo").read_text().splitlines()
            if line.startswith(("MemTotal:", "MemAvailable:"))
        }
        return values["MemAvailable"], values["MemTotal"]
    except (OSError, ValueError, KeyError):
        return None, None


class WorkloadGuard:
    def __init__(self, inference, db=None):
        self.inference, self.db = inference, db
        self.cooldown_until = 0.0
        self.last_interactive_ms = None
        self.last_interactive_at = 0.0
        self.active_requests = {0: 0, 1: 0, 2: 0}

    @contextmanager
    def interaction(self, priority=1):
        self.last_interactive_at = time.monotonic()
        self.active_requests[priority] += 1
        self.inference.interactive += 1
        try:
            with work_priority(priority):
                yield
        finally:
            self.inference.interactive -= 1
            self.active_requests[priority] -= 1
            self.last_interactive_at = time.monotonic()

    @property
    def enabled(self):
        return bool(self.db and self.db.get_state("feature.HPOS_WORKLOAD_GUARD", False))

    def reason(self, priority=7):
        if self.inference.interactive:
            return "interactive"
        if not self.enabled or priority < 5:
            return None
        available, total = available_memory()
        if available is not None and available < max(4 * 1024**3, total * 0.05):
            return "memory_pressure"
        if time.monotonic() < self.cooldown_until:
            return "latency_cooldown"
        return None

    def observe(self, elapsed_ms):
        self.last_interactive_ms = elapsed_ms
        if elapsed_ms > 30000:
            self.cooldown_until = time.monotonic() + 60

    def status(self):
        available, total = available_memory()
        return {
            "enabled": self.enabled,
            "interactive_active": self.inference.interactive > 0,
            "active_requests": self.active_requests,
            "background_pause_reason": self.reason(),
            "memory_available_bytes": available,
            "memory_total_bytes": total,
            "last_interactive_ms": self.last_interactive_ms,
            "background_model_concurrency": 1,
            "priorities": PRIORITIES,
        }
