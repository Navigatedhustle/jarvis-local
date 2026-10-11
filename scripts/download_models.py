"""Download only the approved exact checkpoints into JARVIS-owned storage."""

import os
from pathlib import Path

root = Path(__file__).resolve().parents[1]
os.environ["HF_HOME"] = str(root / "runtime/huggingface")
os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
from huggingface_hub import snapshot_download  # noqa: E402

for role, repo in [
    ("primary", "Qwen/Qwen3.8-27B-FP8"),
    ("background", "nvidia/NVIDIA-Nemotron-3-Nano-4B-FP8"),
]:
    snapshot_download(repo, local_dir=root / "models" / role, token=False)
    print(f"{role} download complete", flush=True)
