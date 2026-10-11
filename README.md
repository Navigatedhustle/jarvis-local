# Jarvis Local

A local executive-assistant codebase with evidence-backed mail/calendar reads, memory, internal drafting, research, voice and payload-bound action approvals. This copy uses synthetic identities and contains no live business records.

**Maturity:** Implemented application with extensive unit and database integration tests. Provider calls in tests are mocked. Live Outlook, phone, Telegram, approved packet delivery, physical audio and mobile pairing were not exercised during this preparation.

## How it works

Vanilla JavaScript dashboard → FastAPI → PostgreSQL 17 with pgvector → orchestrator, retrieval, memory and bounded scheduler. Local vLLM generation/background and llama.cpp embedding/reranking services use separate loopback endpoints; STT/TTS have separate adapters. Account vault and immutable-payload approvals bound consequential actions.

## Getting started

Read [installation, configuration and shutdown](docs/OPERATIONS.md) and [model/data retrieval](docs/MODELS.md). Review [dependency evidence](docs/DEPENDENCIES.md) before choosing an environment. The safe review launcher uses loopback and does not install services or load models.

## Security and data

Read [security and safe updates](SECURITY.md) and [ownership/license status](docs/LICENSE-STATUS.md). Runtime originals are preserved separately. This is a sanitized source snapshot with new history: no account state, business records, model weights, unknown media assets or original logs/history are distributed. Third-party notices included in this copy remain applicable.

## Verification

See [verification evidence and limitations](docs/VERIFICATION.md). Tests distinguish synthetic/mocked workflows from live integrations. Original demo screenshots and document recordings are excluded; use the synthetic workflow in the operations guide for your own reviewed demonstration.

## Roadmap

Replace deployment-specific operator assumptions with configurable identity; complete controlled provider acceptance testing; strengthen scheduler observability and failure recovery. Preserve Windows as the original project development authority.

For route/source details see [architecture](docs/ARCHITECTURE.md).
