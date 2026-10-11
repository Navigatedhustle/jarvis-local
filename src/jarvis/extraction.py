"""Bounded, overlapping source slices for durable low-priority extraction jobs."""

import hashlib
import json


def source_digest(source):
    return hashlib.sha256(
        json.dumps(
            {key: source.get(key) for key in ("id", "title", "body", "occurred_at")},
            default=str,
            sort_keys=True,
        ).encode()
    ).hexdigest()


def source_chunks(text, budget=4000, maximum=64):
    if budget < 1000 or maximum < 1:
        raise ValueError("Invalid extraction budget")
    encoded = text.encode("utf-8")
    chunks, start, covered = [], 0, 0
    while start < len(encoded) and len(chunks) < maximum:
        fragment = encoded[start : start + budget].decode("utf-8", errors="ignore")
        if start + budget < len(encoded) and fragment.rfind("\n") > len(fragment) * 0.75:
            fragment = fragment[: fragment.rfind("\n") + 1]
        stop = start + len(fragment.encode("utf-8"))
        if stop <= start:
            raise ValueError("Source could not be segmented")
        chunks.append(fragment)
        covered = max(covered, stop)
        if stop >= len(encoded):
            break
        start = max(start + 1, stop - 320)
        # Keep the next overlapping slice on a valid UTF-8 character boundary.
        while start < stop and (encoded[start] & 0xC0) == 0x80:
            start += 1
    return chunks or [""], covered >= len(encoded)
