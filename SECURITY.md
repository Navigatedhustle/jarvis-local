# Security and safe updates

This publication copy starts with clean history. Original working files/history stay on the owner's machine. No real account credentials, databases, contact leads, business correspondence, browser sessions, runtime logs, model weights, screenshots or document exports are included. Synthetic generators/fixtures are explicitly retained.

Install Gitleaks from its official release, then run `bash scripts/install-hooks.sh`. Both commit and push hooks fail closed if the scanner is missing. `python3 scripts/security_check.py --history` scans actual staged blobs and all local commit history, and rejects excluded artifact paths. A `.gitignore` alone cannot remove material from history. Automated scans supplement ownership, data and license review; they do not prove the absence of every kind of confidential information.

For an update, inspect the original runtime changes; copy only approved source into this publication repository. Do not sync `.git`, models, environments, live data or hidden authentication folders. Review `git diff`, stage explicit source paths, run appropriate tests, run the security check, commit, and push normally. Never force-push, mirror-push or import original history. Verify destination owner, private visibility and exact commit before each push. If a credential is ever exposed, rotate/revoke the affected credential before relying on file removal.

The owner explicitly confirmed first-party ownership for private publication on 2026-10-10. Public sharing needs a separate explicit approval. Existing third-party notices are retained; no new open-source license is automatically granted.
