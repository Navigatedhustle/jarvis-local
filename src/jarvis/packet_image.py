"""On-demand fixed image worker, with foreground cancellation and no public API."""

import asyncio
import hashlib
import json
import os
import signal
import sys
import time
from contextlib import suppress

from .inference import InferenceDeferred


def cached_image(path, prompt_hash):
    try:
        metrics = json.loads((path / "image-metrics.json").read_text())
        checksum = hashlib.sha256((path / "visual.png").read_bytes()).hexdigest()
        if metrics.get("prompt_sha256") == prompt_hash and metrics.get("sha256") == checksum:
            return metrics
    except (OSError, ValueError):
        pass
    return None


async def stop_owned_container(edition):
    check = await asyncio.create_subprocess_exec(
        "docker",
        "inspect",
        "--format",
        '{{index .Config.Labels "jarvis.packet"}}',
        "jarvis-image-job",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    output, _ = await asyncio.wait_for(check.communicate(), 5)
    if check.returncode == 0 and output.decode().strip() == edition:
        stop = await asyncio.create_subprocess_exec(
            "docker",
            "stop",
            "-t",
            "1",
            "jarvis-image-job",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(stop.wait(), 10)


async def render(path, prompt, inference, db):
    if sys.platform != "linux":
        raise RuntimeError("Local image generation runs on Nano")
    guard = getattr(inference, "workload", None)
    if inference.interactive or time.monotonic() - getattr(guard, "last_interactive_at", 0) < 60:
        raise InferenceDeferred("Wait for one minute of conversation quiet before image loading")
    prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()
    request_path = path / "image-request.json"
    prior = json.loads(request_path.read_text()) if request_path.exists() else None
    request = {"prompt": prompt, "seed": int(prompt_hash[:8], 16)}
    if prior != request:
        request_path.write_text(json.dumps(request), encoding="utf-8")
    cached = cached_image(path, prompt_hash)
    if prior == request and cached:
        return cached
    started = time.monotonic()
    log = path / "image-worker.log"
    with log.open("wb") as output:
        process = await asyncio.create_subprocess_exec(
            "bash", "scripts/image_worker.sh", path.name, stdout=output, stderr=output, start_new_session=True
        )
        try:
            while process.returncode is None:
                await asyncio.sleep(0.3)
                if inference.interactive or db.get_state("demo_mode", False):
                    raise InferenceDeferred("Image worker yielded to live interaction")
                if time.monotonic() - started > 960:
                    raise TimeoutError("Image worker deadline exceeded")
        finally:
            if process.returncode is None:
                # A shell trap can wait for CUDA/its foreground docker client. Stop
                # this labelled container directly before asking the wrapper to exit.
                try:
                    await stop_owned_container(path.name)
                except (TimeoutError, OSError):
                    pass
                finally:
                    if process.returncode is None:
                        with suppress(ProcessLookupError):
                            os.killpg(process.pid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(asyncio.shield(process.wait()), 35)
                except TimeoutError:
                    with suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    await process.wait()
    if process.returncode == 75:
        raise InferenceDeferred("Image memory budget is unavailable")
    if process.returncode:
        raise RuntimeError("Local image worker failed; private diagnostics are available")
    metrics = cached_image(path, prompt_hash)
    if not metrics:
        raise ValueError("Image completion manifest failed verification")
    return metrics
