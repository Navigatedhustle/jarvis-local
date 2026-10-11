"""Download only the fixed public image model. No account token or remote Python."""

import json
from pathlib import Path

from huggingface_hub import snapshot_download

MODEL = "Tongyi-MAI/Z-Image-Turbo"
REVISION = "f332072aa78be7aecdf3ee76d5c247082da564a6"
if __name__ == "__main__":
    revision = REVISION
    snapshot_download(
        MODEL,
        revision=revision,
        token=False,
        local_dir="/models/z-image-turbo",
        allow_patterns=["*.json", "*.safetensors", "*.txt", "*.model", "LICENSE", "README.md"],
        max_workers=2,
    )
    Path("/models/manifest.json").write_text(json.dumps({"model": MODEL, "revision": revision}))
    print(json.dumps({"model": MODEL, "revision": revision, "downloaded": True}))
