"""Legacy SSH setup entrypoint; account credentials now use the encrypted store."""

import json
import sys
from pathlib import Path

import httpx

from jarvis.account_vault import ACCOUNT_FIELDS, FIELDS, apply_accounts
from jarvis.config import Settings

values = json.load(sys.stdin)
if not isinstance(values, dict):
    raise SystemExit("Invalid configuration")
private = {}
for name, value in values.items():
    field = name.removeprefix("JARVIS_").lower()
    if field not in ACCOUNT_FIELDS or not isinstance(value, str) or any(c in value for c in "\n\r\x00"):
        raise SystemExit("Invalid account setting")
    private[field] = int(value) if field == "telegram_user_id" else value
providers = [name for name, fields in FIELDS.items() if set(private) == set(fields)]
if len(providers) != 1:
    raise SystemExit("Provide exactly one complete account configuration")
folder = Path(__file__).resolve().parents[1] / "secrets"
settings = apply_accounts(Settings(_env_file=folder / "runtime.env", secret_dir=folder))
try:
    response = httpx.post(
        "http://127.0.0.1:8100/setup/" + providers[0] + "/configure",
        headers={"Authorization": "Bearer " + settings.api_token},
        json={"confirm": True, "values": private},
        timeout=20,
    )
    if not response.is_success:
        raise SystemExit("Configuration was not saved. Check the fields and JARVIS core status.")
except httpx.HTTPError:
    raise SystemExit("JARVIS core could not be reached. No connection was enabled.") from None
