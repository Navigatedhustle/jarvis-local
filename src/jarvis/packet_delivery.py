"""Exact, reviewable self-email manifests. No standing authority or automatic sends."""

import base64
import hashlib
from copy import deepcopy
from datetime import date

from .content_packets import RECIPIENT, digest_file, directory
from .outlook_actions import email_payload


def proposal(settings, row):
    if row["status"] != "ready":
        raise ValueError("Packet is not ready")
    edition = str(row["edition"])
    path = directory(settings, edition) / "review.pdf"
    expected = row["content"]["pdf"]
    if not path.is_file() or digest_file(path) != expected["sha256"]:
        raise ValueError("Review PDF is missing or changed")
    if path.stat().st_size > 2_500_000:
        raise ValueError("Review PDF exceeds the supported email attachment limit")
    payload = email_payload(
        "JARVIS | AI research and LinkedIn review | " + edition,
        "Demo,\n\nYour AI research briefing, proposed LinkedIn post and original visual are attached for review. Nothing has been posted to LinkedIn. Verify sources and HP product claims before publishing.\n\nJARVIS",
        [RECIPIENT],
    )
    payload["packet"] = {
        "edition": edition,
        "sha256": expected["sha256"],
        "bytes": path.stat().st_size,
        "filename": "JARVIS-" + edition + "-review.pdf",
        "recipient": RECIPIENT,
    }
    return payload


def attach(settings, payload):
    """Run only inside an already-authorized transport; validates immutable bytes again."""
    manifest = payload["packet"]
    edition = date.fromisoformat(manifest["edition"]).isoformat()
    if set(manifest) != {"edition", "sha256", "bytes", "filename", "recipient"}:
        raise ValueError("Unexpected attachment manifest")
    if manifest["recipient"] != RECIPIENT or manifest["filename"] != f"JARVIS-{edition}-review.pdf":
        raise ValueError("Packet recipient or filename changed")
    message = deepcopy(payload["message"])
    if set(message) != {"subject", "body", "toRecipients"} or message["toRecipients"] != [
        {"emailAddress": {"address": RECIPIENT}}
    ]:
        raise ValueError("Packet delivery is restricted to Demo's exact HP address")
    path = directory(settings, edition) / "review.pdf"
    data = path.read_bytes()
    if (
        not data.startswith(b"%PDF-")
        or not 1 <= len(data) <= 2_500_000
        or len(data) != manifest["bytes"]
        or hashlib.sha256(data).hexdigest() != manifest["sha256"]
    ):
        raise ValueError("The reviewed attachment bytes changed")
    message["attachments"] = [
        {
            "@odata.type": "#microsoft.graph.fileAttachment",
            "name": manifest["filename"],
            "contentType": "application/pdf",
            "contentBytes": base64.b64encode(data).decode("ascii"),
        }
    ]
    return message
