"""Use the NVIDIA GB10 torch build, not Chatterbox's incompatible generic torch pin."""

import subprocess
import tomllib
from pathlib import Path

project = tomllib.loads(Path("/opt/chatterbox/pyproject.toml").read_text())
deps = [d for d in project["project"]["dependencies"] if not d.startswith(("torch", "numpy", "gradio"))]
subprocess.run(["pip", "install", *deps], check=True)
subprocess.run(["pip", "install", "--no-deps", "/opt/chatterbox"], check=True)
subprocess.run(
    ["pip", "install", "--force-reinstall", "--no-deps", "--no-build-isolation", "/opt/torchaudio"],
    check=True,
    env={**__import__("os").environ, "USE_CUDA": "0", "BUILD_SOX": "0", "BUILD_RNNT": "0"},
)
