# Verification — 2026-10-10

**Result:** 578 passed.

Full synthetic suite against a dedicated temporary pgvector/PostgreSQL 17 database in a network-disabled container, using Unix sockets. AF_INET/AF_INET6 socket creation denied by seccomp. No live PostgreSQL records, mailbox data, account profiles or provider sends were used. Test container removed after execution.

Checks ran on the owner's actual GB10 ARM64 Ubuntu machine, using its existing project environments. Fresh installation on another machine is not established. All selected Python source was parsed; shell syntax and JavaScript syntax checks are recorded during final publication review. The source-only copies were tested independently of live runtime locations.

## What was not established

Model weights were not downloaded or loaded. Live provider integrations, paid APIs, real emails/calendar writes/calls, physical microphone/camera acceptance, full cold-model startup, physical disconnection and reboot were not tested. Prior historical performance reports are retained locally; this document does not claim their figures were remeasured. Unknown media and original recordings/screenshots are excluded.

## Security review

Gitleaks v8.30.1 scanned proposed source and actual staged contents. Original first-party histories were also scanned, but are not uploaded. Final publication histories are new and separately scanned. Targeted inspections address identifiers, URLs, machine paths, credentials, runtime databases, notebooks and binary/document metadata. No notebooks, screenshots, office documents, authentication files, runtime logs/databases or weights are proposed for upload. Font assets retain bundled notices; source-only model descriptors contain no credentials.

The pre-commit/pre-push checks fail closed on missing scanners and unsafe staged/history paths. Scanner success supplements manual review and does not prove correctness of provider integration or absence of every possible data class.

## Repeating checks

Install the recorded environment, regenerate synthetic fixtures where required, then run `.venv/bin/python -m pytest tests` for projects with unit suites. Jarvis additionally requires a dedicated test PostgreSQL database specified with `JARVIS_TEST_DATABASE_URL`; never point tests at live production data. CampusCompass also needs `frontend/dist` from a production build before API test import. EDUCAUSE hub: `/usr/bin/python3 eval/failure_checks.py`.

Before a push, run `python3 scripts/security_check.py --history` with Gitleaks available. Full model or provider tests require a separate reviewed operator plan using authorized synthetic data.
