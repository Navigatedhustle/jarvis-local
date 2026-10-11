"""Isolated Nano Outlook browser. Private viewer and loopback-only DOM connector."""

import argparse
import json
import os
import secrets
import signal
import string
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "data/browser-tools"
UNIT = "jarvis-outlook-desktop"


def serve():
    os.umask(0o077)
    private = ROOT / "secrets"
    private.mkdir(mode=0o700, exist_ok=True)
    password = private / "browser-vnc.key"
    if not password.exists():
        password.write_text("".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(8)))
    password.chmod(0o600)
    # Snap Chromium can read its own common directory; no existing browser profile is reused.
    common = Path.home() / "snap/chromium/common"
    profile = common / "jarvis-outlook-profile"
    profile.mkdir(mode=0o700, parents=True, exist_ok=True)
    profile.chmod(0o700)
    default = profile / "Default"
    default.mkdir(exist_ok=True)
    prefs = default / "Preferences"
    contents = json.loads(prefs.read_text()) if prefs.exists() else {}
    contents["credentials_enable_service"] = False
    contents.setdefault("profile", {})["password_manager_enabled"] = False
    prefs.write_text(json.dumps(contents))
    authority = common / "jarvis-outlook.xauth"
    authority.touch(mode=0o600)
    authority.chmod(0o600)
    subprocess.run(
        ["xauth", "-f", str(authority)],
        input=f"add :93 . {secrets.token_hex(16)}\n",
        text=True,
        capture_output=True,
        check=True,
    )
    environment = {**os.environ, "DISPLAY": ":93", "XAUTHORITY": str(authority)}
    binaries = TOOLS / "root/usr/bin"
    native = {**environment, "LD_LIBRARY_PATH": str(TOOLS / "root/usr/lib/aarch64-linux-gnu")}
    processes = []

    def launch(name, command, env):
        child = subprocess.Popen(command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        processes.append((name, child))

    def terminate(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, terminate)
    try:
        launch(
            "display",
            [
                str(binaries / "Xvfb"),
                ":93",
                "-screen",
                "0",
                "1440x900x24",
                "-nolisten",
                "tcp",
                "-auth",
                str(authority),
            ],
            native,
        )
        for _ in range(30):
            result = subprocess.run(
                ["xdpyinfo"], env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            if result.returncode == 0:
                break
            time.sleep(0.2)
        else:
            raise RuntimeError("Private display did not start")
        launch(
            "desktop",
            [
                str(binaries / "x11vnc"),
                "-display",
                ":93",
                "-auth",
                str(authority),
                "-localhost",
                "-listen",
                "127.0.0.1",
                "-no6",
                "-rfbport",
                "5913",
                "-passwdfile",
                str(password),
                "-forever",
                "-shared",
                "-noxdamage",
                "-quiet",
            ],
            native,
        )
        launch(
            "viewer",
            [
                str(TOOLS / "venv/bin/websockify"),
                "--web",
                str(TOOLS / "root/usr/share/novnc"),
                "127.0.0.1:6193",
                "127.0.0.1:5913",
            ],
            environment,
        )
        launch(
            "browser",
            [
                "/snap/bin/chromium",
                "--ozone-platform=x11",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--no-first-run",
                "--no-default-browser-check",
                "--remote-debugging-address=127.0.0.1",
                "--remote-debugging-port=0",
                "--window-size=1440,900",
                "--window-position=0,0",
                "--user-data-dir=" + str(profile),
                "https://outlook.office.com/mail/",
            ],
            environment,
        )
        while True:
            for name, child in processes:
                if child.poll() is not None:
                    raise RuntimeError(f"Private {name} exited with status {child.returncode}")
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        for _, child in reversed(processes):
            if child.poll() is None:
                child.terminate()
        for _, child in reversed(processes):
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["start", "stop", "status", "serve"])
    action = parser.parse_args().action
    if action == "serve":
        serve()
    elif action == "start":
        active = subprocess.run(["systemctl", "--user", "is-active", "--quiet", UNIT]).returncode == 0
        if not active:
            subprocess.run(
                [
                    "systemd-run",
                    "--user",
                    "--collect",
                    "--unit=" + UNIT,
                    "--property=UMask=0077",
                    "--property=TimeoutStopSec=30",
                    "--working-directory=" + str(ROOT),
                    "/usr/bin/python3",
                    str(Path(__file__).resolve()),
                    "serve",
                ],
                check=True,
            )
        print("Private Outlook browser session requested. Microsoft sign-in remains with Demo.")
    else:
        subprocess.run(
            ["systemctl", "--user", action if action == "stop" else "status", UNIT, "--no-pager"], check=True
        )


if __name__ == "__main__":
    main()
