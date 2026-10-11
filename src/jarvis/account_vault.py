"""Encrypted account settings. Never expose values through status or audit endpoints."""

import json
import os
import threading
from pathlib import Path

from cryptography.fernet import Fernet

FIELDS = {
    "outlook": ("ms_client_id", "ms_tenant_id"),
    "telegram": ("telegram_token", "telegram_user_id"),
    "twilio": (
        "twilio_account_sid",
        "twilio_auth_token",
        "twilio_from",
        "demo_phone",
        "public_webhook_url",
    ),
}
ACCOUNT_FIELDS = {name for fields in FIELDS.values() for name in fields}
_lock = threading.RLock()


def private_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(data)
    temporary.chmod(0o600)
    temporary.replace(path)


class AccountVault:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.path = self.folder / "accounts.enc"
        self.key_path = self.folder / "accounts.key"

    def load(self):
        with _lock:
            if not self.path.exists():
                return {}
            # Corruption or a missing key must fail closed, not revive older credentials.
            values = json.loads(Fernet(self.key_path.read_bytes()).decrypt(self.path.read_bytes()))
            if not isinstance(values, dict) or not values.keys() <= ACCOUNT_FIELDS:
                raise ValueError("Invalid private account store")
            return values

    def update(self, values):
        if not values.keys() <= ACCOUNT_FIELDS:
            raise ValueError("Unexpected account setting")
        with _lock:
            current = self.load()
            current.update(values)
            if not self.key_path.exists():
                private_write(self.key_path, Fernet.generate_key())
            private_write(self.path, Fernet(self.key_path.read_bytes()).encrypt(json.dumps(current).encode()))
        return current


def apply_accounts(settings):
    for name, value in AccountVault(settings.secret_dir).load().items():
        setattr(settings, name, value)
    return settings
