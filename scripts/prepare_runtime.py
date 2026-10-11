"""Idempotent Nano bootstrap. Never changes CampusCompass or existing credentials."""

import json
import os
import secrets
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VLLM_IMAGE = "vllm/vllm-openai@sha256:18372a7224938643461b846fb64c5c9d3d6e9727e82caf2dc3043e620c9d4d7a"


def run(*args):
    subprocess.run(args, check=True)


def main():
    os.umask(0o077)
    for name in ("data", "secrets", "logs", "models", "runtime", "runtime/huggingface"):
        (ROOT / name).mkdir(parents=True, exist_ok=True)
    env = ROOT / "secrets/runtime.env"
    if not env.exists():
        password = secrets.token_urlsafe(36)
        env.write_text(
            f"JARVIS_DATABASE_URL=postgresql://jarvis:{password}@127.0.0.1:55432/jarvis\n"
            f"JARVIS_API_TOKEN={secrets.token_urlsafe(48)}\n"
        )
        (ROOT / "secrets/postgres.env").write_text(
            f"POSTGRES_USER=jarvis\nPOSTGRES_DB=jarvis\nPOSTGRES_PASSWORD={password}\n"
        )
    campus = Path(os.environ.get("CAMPUSCOMPASS_ASSET_DIR", str(Path.home() / "Projects/campuscompass-local")))
    for kind, file in [
        ("embedding", "Qwen3-Embedding-0.6B-Q8_0.gguf"),
        ("reranker", "Qwen3-Reranker-0.6B-rank-q8_0.gguf"),
    ]:
        target = ROOT / "models" / file
        if not target.exists():
            shutil.copy2(campus / "models" / kind / file, target)
    binary_dir = ROOT / "runtime/llama-bin"
    if not binary_dir.exists():
        shutil.copytree(campus / "runtime/llama.cpp/build/bin", binary_dir, symlinks=True)
    run("docker", "pull", "pgvector/pgvector:pg17")
    exists = subprocess.run(["docker", "inspect", "jarvis-postgres"], capture_output=True).returncode == 0
    if not exists:
        run(
            "docker",
            "create",
            "--name",
            "jarvis-postgres",
            "--restart",
            "unless-stopped",
            "--env-file",
            str(ROOT / "secrets/postgres.env"),
            "-p",
            "127.0.0.1:55432:5432",
            "-v",
            "jarvis-postgres:/var/lib/postgresql/data",
            "pgvector/pgvector:pg17",
        )
    run("docker", "start", "jarvis-postgres")
    unit_dir = Path.home() / ".config/systemd/user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    for source in (ROOT / "infra/systemd").glob("jarvis-*.service"):
        shutil.copy2(source, unit_dir / source.name)
    run("systemctl", "--user", "daemon-reload")
    run("systemctl", "--user", "enable", "--now", "jarvis-embedding", "jarvis-reranker")
    (ROOT / "data/runtime-manifest.json").write_text(json.dumps({"vllm_image": VLLM_IMAGE}))


if __name__ == "__main__":
    main()
