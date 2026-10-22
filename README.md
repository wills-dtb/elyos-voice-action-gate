# Elyos-inspired Voice Action Gate

**Independent, synthetic engineering demonstration. Not affiliated with Elyos AI, not an Elyos integration, and not a voice recognition system.**

## Company-specific problem observed

[Elyos AI](https://elyos.ai/) operates voice and messaging agents that can book jobs in field-service CRMs. Its publicly described engineering challenge includes real-time response latency, complex agent workflows, and production reliability. A natural failure mode in this class of system is a retried or hallucinated `book_job` tool call creating duplicate bookings, or an agent taking action without explicit caller confirmation.

This narrowly scoped service implements the **durable side-effect boundary** between a simulated agent's structured intent and a simulated booking system. It prioritises **correctness and auditability** over model cleverness.

## Assumptions

- The speech-to-text, identity verification, authorisation, tenant isolation and LLM inference happen upstream.
- Incoming event envelopes are synthetic and trusted for this demo; production would require signed/authenticated requests and schema enforcement.
- Each conversation can make one booking; each slot can be reserved once globally (real systems require tenant, technician, timezone and calendar-specific constraints).
- A caller must explicitly confirm a proposed booking; low-confidence intents go to human review.
- `latency_ms` is **caller-supplied simulated telemetry**, not a measured system latency or evidence of sub-500ms voice response.

## Architecture

```text
Synthetic agent intent JSONL
       |
       v
Input validation -> SQLite BEGIN IMMEDIATE transaction
       |                      |
       |                event_id + request hash deduplication
       v                      |
Confidence/confirmation gate -> pending slot -> UNIQUE slot booking commit
       |
       +--> handoff state blocks further autonomous actions
       |
       +--> PII-minimised audit and latency-budget flag
```

- `src/elyos_gate/core.py`: transactional gate and policy checks.
- `src/elyos_gate/cli.py`: replay CLI and machine-readable report.
- `tests/test_gate.py`: functional, retry, persistence, collision and escalation tests.
- `fixtures/`: safe and risky synthetic traces.
- `.github/workflows/test.yml`: automated test/replay gate.

## Run (Python 3.11+, no third-party dependencies)

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
PYTHONPATH=src python -m elyos_gate.cli fixtures/safe.jsonl
PYTHONPATH=src python -m elyos_gate.cli fixtures/risky.jsonl --output risky-report.json
# Durable replay across restarts:
PYTHONPATH=src python -m elyos_gate.cli fixtures/safe.jsonl --db demo.sqlite
PYTHONPATH=src python -m elyos_gate.cli fixtures/safe.jsonl --db demo.sqlite
```

Expected: 12 tests pass; safe fixture shows one booking despite the duplicate confirmation event. Risky fixture shows blocked unconfirmed action, human handoff, and subsequent automation blocked. `committed` counts replayed decisions as well as first-time commits; the `bookings` table is the actual side-effect count.

## Engineering tradeoffs

- SQLite and `BEGIN IMMEDIATE` provide a small, demonstrable transactional boundary and serialized writes; not a distributed, horizontally scalable design.
- Exact payload hashes prevent idempotency-key collisions but would need retention and privacy policy in production.
- Low-confidence actions trigger a sticky handoff; human reactivation is intentionally not implemented.
- This is a **booking commit safety layer**, not a streaming voice latency benchmark, conversational agent, telephony service, CRM integration or formal safety guarantee.

## Deliberately excluded (timebox)

No telephony/ASR/TTS, no live model calls, no real CRM APIs, no slot availability lookup, no customer identity verification, no real multi-tenant data, no retries against external APIs, no tracing SDK, no load tests, and no personally identifiable transcripts.

## What production would require

1. Strong tenant authentication/authorisation and verified caller identity, including GDPR retention/deletion and encrypted audit records.
2. Durable outbox plus external CRM idempotency keys; transactional database writes alone cannot guarantee exactly-once effects in third-party APIs.
3. Per-tenant calendars, technician availability, timezone/DST rules, booking expiry, rescheduling and reconciliation.
4. Queue backpressure, retries with exponential jitter, circuit breakers, DLQs, human escalation SLA and crash recovery.
5. Real p50/p95/p99 end-to-end latency measurement across ASR, LLM, tool call and TTS, with trace correlation and alerting.
6. Adversarial evaluation suites for consent ambiguity, noisy audio, prompt injection, PII handling, multilingual callers, cancellations and out-of-hours emergencies.
7. PostgreSQL/Redis or another distributed architecture with safe concurrency semantics and tenant-scoped uniqueness.

## Founder discussion angle

The difficult engineering problem isn't generating a convincing booking conversation; it's ensuring an autonomous agent can make a customer-visible side effect **once**, with **consent**, under **retries**, **low confidence** and **partial failure**. This demo isolates that boundary and makes the failure modes testable.
