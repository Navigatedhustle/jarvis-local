"""Source-only validation release, explicitly labelled uncommitted by deploy_nano.ps1."""

import subprocess
import sys
import tarfile
from pathlib import Path, PurePosixPath

from apply_release import allowed_dirs, allowed_files

root = Path(__file__).resolve().parents[1]
names = (
    subprocess.check_output(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=root)
    .decode()
    .split("\0")
)
with tarfile.open(sys.argv[1], "w:gz") as archive:
    for name in sorted(set(names) - {""}):
        relative = PurePosixPath(name)
        if relative.parts[0] not in allowed_dirs and name not in allowed_files:
            continue
        path = root / name
        if any(
            part in {"secrets", "data", "logs", "models", "runtime", ".venv", "node_modules", ".git"}
            for part in relative.parts
        ):
            raise SystemExit("Runtime data cannot enter a source release")
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise SystemExit("Unsafe source path")
        if path.is_file():
            archive.add(path, arcname=name, recursive=False)
