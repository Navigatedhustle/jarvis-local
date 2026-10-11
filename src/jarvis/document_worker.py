"""Disposable text parser with resource limits on the Nano. No document code is executed."""

import json
import sys


def main():
    if sys.platform == "linux":
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024, 768 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_CPU, (20, 20))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    from .documents import extract_text

    try:
        data = sys.stdin.buffer.read(20_000_001)
        text = extract_text(sys.argv[1], data)
        sys.stdout.write(json.dumps({"text": text}, ensure_ascii=False))
    except Exception as error:
        sys.stdout.write(json.dumps({"error": type(error).__name__}))


if __name__ == "__main__":
    main()
