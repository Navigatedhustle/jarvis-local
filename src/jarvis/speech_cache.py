"""Bounded, short-lived audio in RAM; no speech text or audio is written to disk."""

import hashlib
import time
from collections import OrderedDict


class SpeechCache:
    def __init__(self, max_bytes=32_000_000, max_entries=64, ttl=900, clock=time.monotonic):
        self.max_bytes, self.max_entries, self.ttl, self.clock = max_bytes, max_entries, ttl, clock
        self.items = OrderedDict()
        self.bytes = 0
        self.hits = 0

    def get(self, text):
        key = hashlib.sha256(text.encode()).digest()
        item = self.items.get(key)
        if not item:
            return None
        created, data = item
        if self.clock() - created >= self.ttl:
            self.items.pop(key)
            self.bytes -= len(data)
            return None
        self.items.move_to_end(key)
        self.hits += 1
        return data

    def put(self, text, data):
        if len(data) > self.max_bytes:
            return
        key = hashlib.sha256(text.encode()).digest()
        old = self.items.pop(key, None)
        if old:
            self.bytes -= len(old[1])
        self.items[key] = (self.clock(), data)
        self.bytes += len(data)
        while self.bytes > self.max_bytes or len(self.items) > self.max_entries:
            _, (_, removed) = self.items.popitem(last=False)
            self.bytes -= len(removed)
