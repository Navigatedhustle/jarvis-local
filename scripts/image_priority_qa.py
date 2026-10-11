"""Synthetic foreground interruption of the real isolated Nano image worker."""

import asyncio
import json
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

from jarvis.inference import InferenceDeferred
from jarvis.packet_image import render


async def main():
    path = Path("data/content_packets/2099-01-01").resolve()
    path.mkdir(parents=True, exist_ok=True)
    # This operator-only fixture is not inserted into the packet database or schedule.
    (path / "image-stage.json").unlink(missing_ok=True)
    (path / "image-metrics.json").unlink(missing_ok=True)
    inference = SimpleNamespace(interactive=0)
    db = SimpleNamespace(get_state=lambda key, default=False: default)
    prompt = "A conceptual blue and white editorial illustration of reproducible university research, showing precise glass experimental equipment on a clean laboratory bench. Square composition, natural light, no text, no logos, no people."
    task = asyncio.create_task(render(path, prompt, inference, db))
    try:
        async with asyncio.timeout(180):
            while not (path / "image-stage.json").exists():
                if task.done():
                    await task
                    raise RuntimeError("The worker completed before interruption could be exercised")
                await asyncio.sleep(0.2)
        started = time.monotonic()
        inference.interactive = 1
        try:
            await task
        except InferenceDeferred:
            pass
        else:
            raise AssertionError("Image worker did not yield")
        elapsed = round((time.monotonic() - started) * 1000)
        running = subprocess.run(
            ["docker", "ps", "--filter", "name=^jarvis-image-job$", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        active = (
            subprocess.run(
                ["systemctl", "--user", "is-active", "--quiet", "jarvis-background-model.service"]
            ).returncode
            == 0
        )
        assert not running and active
        report = {
            "synthetic_foreground_interrupt": True,
            "worker_release_ms": elapsed,
            "image_container_stopped": not running,
            "background_service_restored": active,
            "external_writes": False,
        }
        Path("data/image-priority-qa.json").write_text(json.dumps(report, indent=2))
        print(json.dumps(report))
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
