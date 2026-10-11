import hashlib
from copy import deepcopy

import pytest

from jarvis.content_packets import RECIPIENT
from jarvis.outlook_actions import email_payload
from jarvis.packet_browser import PacketGuard, success_body


@pytest.fixture
def guard():
    payload = email_payload("Packet", "Approved body", [RECIPIENT])
    payload["packet"] = {
        "filename": "review.pdf",
        "bytes": 8,
        "sha256": hashlib.sha256(b"%PDF-123").hexdigest(),
    }
    return PacketGuard(payload, {"draft_id": "owned", "attachment_id": "attachment", "draft_verified": True})


def send_body():
    fields = {
        "Subject": "Packet",
        "Body": {"BodyType": "HTML", "Value": "<div>Approved body</div>"},
        "ToRecipients": [{"EmailAddress": RECIPIENT}],
        "CcRecipients": [],
        "BccRecipients": [],
    }
    return {
        "MessageDisposition": "SendAndSaveCopy",
        "ItemChanges": [
            {
                "ItemId": {"Id": "owned"},
                "Updates": [
                    {
                        "__type": "SetItemField:#Exchange",
                        "Path": {"FieldURI": k},
                        "Item": {"__type": "Message:#Exchange", k: v},
                    }
                    for k, v in fields.items()
                ],
            }
        ],
    }


def test_exact_send_and_replay_refusal(guard):
    assert guard.send(send_body())
    guard.state["send_started"] = True
    assert not guard.send(send_body())


@pytest.mark.parametrize(
    "field,value",
    [
        ("Subject", "Different"),
        ("Body", {"BodyType": "Text", "Value": "Injected instruction"}),
        ("ToRecipients", [{"EmailAddress": "other@example.invalid"}]),
        ("CcRecipients", [{"EmailAddress": RECIPIENT}]),
        ("BccRecipients", [{"EmailAddress": RECIPIENT}]),
    ],
)
def test_changed_content_and_recipients_rejected(guard, field, value):
    body = send_body()
    for update in body["ItemChanges"][0]["Updates"]:
        if field in update["Item"]:
            update["Item"][field] = value
    assert not guard.send(body)


def test_other_draft_unverified_attachment_unknown_field_and_duplicate_updates_rejected(guard):
    body = send_body()
    body["ItemChanges"][0]["ItemId"]["Id"] = "someone-elses-draft"
    assert not guard.send(body)
    body = send_body()
    body["ItemChanges"][0]["Updates"].append(deepcopy(body["ItemChanges"][0]["Updates"][0]))
    assert not guard.send(body)
    body = send_body()
    body["ItemChanges"][0]["Updates"].append(
        {
            "__type": "SetItemField:#Exchange",
            "Path": {"FieldURI": "Attachments"},
            "Item": {"__type": "Message:#Exchange", "Attachments": []},
        }
    )
    assert not guard.send(body)
    guard.state["draft_verified"] = False
    assert not guard.send(send_body())


def test_exact_pdf_upload_binding_and_duplicate_refusal(guard):
    body = {
        "ParentItemId": {"Id": "owned"},
        "SliceNumber": 0,
        "TotalSlices": 1,
        "Attachments": [
            {
                "Name": "review.pdf",
                "Size": 8,
                "ContentType": "application/pdf",
                "IsInline": False,
                "Content": "",
                "AttachmentId": {"mailboxInfo": {"mailboxSmtpAddress": RECIPIENT}},
            }
        ],
    }
    assert guard.upload(body, b"%PDF-123")
    assert not guard.upload(body, b"%PDF-456")
    changed = deepcopy(body)
    changed["ParentItemId"]["Id"] = "different"
    assert not guard.upload(changed, b"%PDF-123")
    changed = deepcopy(body)
    changed["Attachments"][0]["IsInline"] = True
    assert not guard.upload(changed, b"%PDF-123")
    guard.state["upload_started"] = True
    assert not guard.upload(body, b"%PDF-123")


def test_server_errors_not_reported_as_success():
    for data in (
        {},
        {
            "Body": {
                "ResponseMessages": {
                    "Items": [{"ResponseClass": "Error", "ResponseCode": "ErrorAccessDenied"}]
                }
            }
        },
    ):
        with pytest.raises(ValueError):
            success_body(data)


def test_save_only_exact_message_pins_recipient_and_never_sends(guard):
    item = {
        "__type": "Message:#Exchange",
        "Subject": "Packet",
        "Body": {"BodyType": "Text", "Value": "Approved body"},
        "ToRecipients": [],
        "CcRecipients": [],
        "BccRecipients": [],
        "mailboxInfo": {"mailboxSmtpAddress": RECIPIENT},
    }
    body = {"MessageDisposition": "SaveOnly", "Items": [item]}
    assert guard.create(body)
    assert item["ToRecipients"][0]["EmailAddress"] == RECIPIENT
    body["MessageDisposition"] = "SendAndSaveCopy"
    assert not guard.create(body)
    body["MessageDisposition"] = "SaveOnly"
    guard.state["draft_started"] = True
    assert not guard.create(body)
