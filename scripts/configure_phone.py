"""Configure an exact private dashboard origin using this Nano's Tailscale identity.

Does not modify Tailscale, ACLs, Funnel, other services or app credentials.
Run on Nano after deploying source. HTTPS Serve needs separate admin authorization.
"""

import json
import os
import subprocess
from pathlib import Path

from jarvis.config import Settings


def main():
    result = subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True, check=True)
    status = json.loads(result.stdout)
    if status.get("BackendState") != "Running" or not status.get("Self", {}).get("Online"):
        raise SystemExit("Sign into Tailscale on Nano before configuring phone access")
    name = status["Self"]["DNSName"].rstrip(".")
    url = Settings(_env_file=None, mobile_url="https://" + name).mobile_url
    path = Path("secrets/runtime.env")
    if not path.is_file():
        raise SystemExit("Run in the deployed JARVIS directory with its existing runtime settings")
    lines = [line for line in path.read_text().splitlines() if not line.startswith("JARVIS_MOBILE_URL=")]
    temporary = path.with_suffix(".phone.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write("\n".join(lines + ["JARVIS_MOBILE_URL=" + url]) + "\n")
    temporary.replace(path)
    path.chmod(0o600)
    print(
        json.dumps(
            {"private_url": url, "restart_api_dashboard_required": True, "https_serve_verified": False}
        )
    )


if __name__ == "__main__":
    main()
