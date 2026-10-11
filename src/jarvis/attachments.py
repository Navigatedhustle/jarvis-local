"""Bounded attachment reads through an observed Outlook Download control; never execute files."""

import asyncio
import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from psycopg.types.json import Jsonb

from .documents import extract_text_isolated

SUPPORTED = {".pdf", ".docx", ".pptx", ".xlsx", ".txt", ".md", ".vtt", ".csv", ".tsv"}
MAX_BYTES = 20_000_000


def attachment_manifest(item):
    result = []
    for value in item.get("Attachments", []):
        name, size = value.get("Name", ""), value.get("Size")
        identifier = value.get("AttachmentId", {}).get("Id")
        if (
            not identifier
            or not name
            or len(name) > 255
            or any(char in name for char in "/\\\x00")
            or value.get("IsInline")
            or not value.get("__type", "").startswith("FileAttachment")
            or type(size) is not int
            or not 0 < size <= MAX_BYTES
            or Path(name).suffix.lower() not in SUPPORTED
        ):
            continue
        result.append(
            {
                "id": identifier,
                "name": name,
                "size": size,
                "content_type": value.get("ContentType"),
                "status": "metadata_only",
            }
        )
    return result


async def download_attachment(page, network, attachment):
    """Download only a unique, verified attachment widget. Credentials never leave Chromium."""
    widget = page.get_by_role("option", name=re.compile("^" + re.escape(attachment["name"]) + r" Open\b"))
    if await widget.count() != 1:
        raise ValueError("Attachment is not uniquely visible in the selected thread")
    staging = Path.home() / "snap/chromium/common/jarvis-attachment-downloads"
    staging.mkdir(mode=0o700, exist_ok=True)
    staging.chmod(0o700)
    temporary, download = None, None
    try:
        # Snap has a separate /tmp. Use a private, host-readable directory for this one download.
        await network.send(
            "Browser.setDownloadBehavior",
            {"behavior": "allowAndName", "downloadPath": str(staging), "eventsEnabled": True},
        )
        await widget.click(button="right", timeout=4000)
        async with page.expect_download(timeout=12000) as pending:
            await page.get_by_role("menuitem", name=re.compile("Download$")).click(timeout=4000)
        download = await pending.value
        if download.suggested_filename != attachment["name"]:
            raise ValueError("Download filename did not match the verified attachment")
        reported = Path(await asyncio.wait_for(download.path(), timeout=25))
        guid = str(UUID(reported.name))
        temporary = staging / guid
        if not 0 < temporary.stat().st_size <= MAX_BYTES:
            raise ValueError("Attachment exceeds the local size limit")
        return await asyncio.to_thread(temporary.read_bytes)
    finally:
        if download:
            await download.cancel()
        if temporary and temporary.resolve().parent == staging.resolve():
            temporary.unlink(missing_ok=True)
        await network.send("Browser.setDownloadBehavior", {"behavior": "default"})
        await page.keyboard.press("Escape")


def store_attachment(db, data_dir, item):
    attachment, data = item["attachment"], item["data"]
    if not 0 < len(data) <= MAX_BYTES or Path(attachment["name"]).suffix.lower() not in SUPPORTED:
        raise ValueError("Unsupported attachment")
    digest = hashlib.sha256(data).hexdigest()
    identifier = "outlook_browser:attachment:" + hashlib.sha256(attachment["id"].encode()).hexdigest()
    source_id = "outlook_browser:mail:" + item["message_id"]
    source = db.one("SELECT occurred_at FROM records WHERE id=%s AND NOT deleted", (source_id,))
    if not source:
        raise ValueError("Attachment requires its verified parent message")
    folder = data_dir / "attachments"
    folder.mkdir(mode=0o700, exist_ok=True)
    path = folder / (digest + Path(attachment["name"]).suffix.lower())
    if not path.exists():
        # Files are content-addressed and immutable once published, including during backup.
        temporary = folder / (digest + ".partial")
        with temporary.open("wb") as output:
            temporary.chmod(0o600)
            output.write(data)
        temporary.replace(path)
    error = None
    try:
        text = extract_text_isolated(attachment["name"], data)
    except ValueError as failure:
        text, error = "", type(failure).__name__
    metadata = {
        "attachment_id": attachment["id"],
        "source_id": source_id,
        "sha256": digest,
        "bytes": len(data),
        "trust": "untrusted",
        "coverage": "attachment_text" if text else "attachment_needs_review",
        "extraction_error": error,
        "file": str(path.relative_to(data_dir)),
        "downloaded_at": datetime.now(timezone.utc).isoformat(),
    }
    db.execute(
        """INSERT INTO records(id,kind,provider,title,body,metadata,occurred_at)
        VALUES (%s,'document','outlook_browser',%s,%s,%s,%s) ON CONFLICT(id) DO UPDATE
        SET title=excluded.title,body=excluded.body,metadata=excluded.metadata,deleted=false,updated_at=now()""",
        (identifier, attachment["name"], text, Jsonb(metadata), source["occurred_at"]),
    )
    db.audit(
        "attachment.ingested",
        identifier,
        {"source_id": source_id, "bytes": len(data), "text_available": bool(text)},
    )
    return {"id": identifier, "characters": len(text), "sha256": digest}
