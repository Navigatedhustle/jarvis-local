"""Trusted SSH-only OAuth cache transfer. Not exposed to HTTP or model tools."""

import argparse
import json
import subprocess
import sys

import msal

from jarvis.config import settings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["settings", "import"])
    args = parser.parse_args()
    if args.action == "settings":
        print(json.dumps({"client_id": settings.ms_client_id, "tenant_id": settings.ms_tenant_id}))
        return
    contents = sys.stdin.read(2_000_001)
    if len(contents) > 2_000_000:
        raise SystemExit("OAuth cache exceeds size limit")
    cache = msal.SerializableTokenCache()
    cache.deserialize(contents)
    accounts = cache.find(msal.TokenCache.CredentialType.ACCOUNT)
    tokens = cache.find(msal.TokenCache.CredentialType.ACCESS_TOKEN)
    if len(accounts) != 1 or not tokens:
        raise SystemExit("Exactly one authorized Microsoft account is required")
    if any(token.get("client_id") != settings.ms_client_id for token in tokens):
        raise SystemExit("OAuth client does not match configured application")
    if accounts[0].get("realm", "").lower() != settings.ms_tenant_id.lower():
        raise SystemExit("OAuth tenant does not match configured HP directory")
    settings.secret_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = settings.secret_dir / "msal-cache.json"
    temp = path.with_suffix(".tmp")
    temp.touch(mode=0o600, exist_ok=True)
    temp.chmod(0o600)
    temp.write_text(cache.serialize())
    temp.replace(path)
    subprocess.run(["systemctl", "--user", "restart", "jarvis-api"], check=True)
    print("Authorized cache stored")


if __name__ == "__main__":
    main()
