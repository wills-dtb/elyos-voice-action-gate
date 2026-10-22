"""Durable, idempotent booking gate for simulated voice-agent tool calls.

Only structured intent envelopes enter this boundary. Raw voice/audio/PII should never
enter this module. A real product would authenticate agents, callers and tenant access.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

MIN_CONFIDENCE = 0.82
LATENCY_BUDGET_MS = 500


@dataclass(frozen=True)
class Decision:
    event_id: str
    conversation_id_hash: str
    status: str
    reason: str
    booking_id: str | None = None
    latency_budget_breached: bool = False


class ActionGate:
    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self.conn = sqlite3.connect(str(db_path), isolation_level=None, timeout=5)
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                request_hash TEXT NOT NULL,
                decision_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pending (
                conversation_id TEXT PRIMARY KEY,
                slot TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS bookings (
                booking_id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                slot TEXT NOT NULL UNIQUE
            );
            CREATE TABLE IF NOT EXISTS handoffs (
                conversation_id TEXT PRIMARY KEY,
                reason TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                conversation_hash TEXT NOT NULL,
                status TEXT NOT NULL,
                reason TEXT NOT NULL
            );
        """)

    def close(self) -> None:
        self.conn.close()

    def process(self, event: dict[str, Any]) -> Decision:
        """Execute a structured intent atomically. Identical retries are replayed safely."""
        required = {"event_id", "conversation_id", "action", "confidence", "latency_ms"}
        if not required.issubset(event):
            raise ValueError(f"missing fields: {sorted(required - set(event))}")
        event_id = event["event_id"]
        conv = event["conversation_id"]
        action = event["action"]
        confidence = event["confidence"]
        latency = event["latency_ms"]
        if not isinstance(event_id, str) or not event_id or not isinstance(conv, str) or not conv:
            raise ValueError("event_id and conversation_id must be non-empty strings")
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0 <= confidence <= 1:
            raise ValueError("confidence must be a number between 0 and 1")
        if not isinstance(latency, (int, float)) or isinstance(latency, bool) or latency < 0:
            raise ValueError("latency_ms must be nonnegative")
        if action not in {"propose_booking", "confirm_booking", "handoff", "cancel_booking", "noop"}:
            raise ValueError("unsupported action")
        if action == "propose_booking" and (not isinstance(event.get("slot"), str) or not event["slot"]):
            raise ValueError("propose_booking requires slot")
        # Hash the full event to prevent accidental reuse of an idempotency key for a different request.
        canonical = json.dumps(event, sort_keys=True, separators=(",", ":"), allow_nan=False)
        request_hash = hashlib.sha256(canonical.encode()).hexdigest()
        conv_hash = hashlib.sha256(conv.encode()).hexdigest()[:16]
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            prior = self.conn.execute("SELECT request_hash,decision_json FROM events WHERE event_id=?", (event_id,)).fetchone()
            if prior:
                if prior[0] != request_hash:
                    raise ValueError("idempotency key reused with different payload")
                result = Decision(**json.loads(prior[1]))
                self.conn.execute("COMMIT")
                return result
            status, reason, booking_id = self._decide(event)
            result = Decision(event_id, conv_hash, status, reason, booking_id, latency > LATENCY_BUDGET_MS)
            self.conn.execute("INSERT INTO events VALUES (?,?,?)", (event_id, request_hash, json.dumps(asdict(result))))
            self.conn.execute("INSERT INTO audit(event_id,conversation_hash,status,reason) VALUES (?,?,?,?)", (event_id, conv_hash, status, reason))
            self.conn.execute("COMMIT")
            return result
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def _decide(self, event: dict[str, Any]) -> tuple[str, str, str | None]:
        conv, action = event["conversation_id"], event["action"]
        if self.conn.execute("SELECT 1 FROM handoffs WHERE conversation_id=?", (conv,)).fetchone():
            return "blocked", "conversation already handed to human", None
        if action == "handoff" or event["confidence"] < MIN_CONFIDENCE:
            self.conn.execute("INSERT OR IGNORE INTO handoffs VALUES (?,?)", (conv, "explicit or low-confidence escalation"))
            self.conn.execute("DELETE FROM pending WHERE conversation_id=?", (conv,))
            return "handoff", "human review required", None
        if action == "noop":
            return "ignored", "no external action requested", None
        if action == "cancel_booking":
            self.conn.execute("INSERT OR IGNORE INTO handoffs VALUES (?,?)", (conv, "cancellation requires human"))
            return "handoff", "cancellation requires verified identity and human review", None
        if action == "propose_booking":
            self.conn.execute("INSERT INTO pending VALUES (?,?) ON CONFLICT(conversation_id) DO UPDATE SET slot=excluded.slot", (conv, event["slot"]))
            return "pending_confirmation", "explicit caller confirmation required", None
        if action == "confirm_booking":
            if event.get("caller_confirmed") is not True:
                return "blocked", "caller confirmation missing", None
            pending = self.conn.execute("SELECT slot FROM pending WHERE conversation_id=?", (conv,)).fetchone()
            if not pending:
                return "blocked", "no matching pending booking", None
            slot = pending[0]
            # One booking per conversation in this intentionally narrow demo.
            if self.conn.execute("SELECT 1 FROM bookings WHERE conversation_id=?", (conv,)).fetchone():
                return "blocked", "conversation already booked", None
            booking_id = "bk_" + hashlib.sha256(f"{conv}|{slot}".encode()).hexdigest()[:12]
            try:
                self.conn.execute("INSERT INTO bookings VALUES (?,?,?)", (booking_id, conv, slot))
            except sqlite3.IntegrityError:
                return "blocked", "slot already reserved", None
            self.conn.execute("DELETE FROM pending WHERE conversation_id=?", (conv,))
            return "committed", "booking committed once", booking_id
        raise AssertionError("unreachable")

    def audit_events(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT event_id,conversation_hash,status,reason FROM audit ORDER BY seq").fetchall()
        return [dict(zip(("event_id", "conversation_hash", "status", "reason"), row)) for row in rows]

    def bookings(self) -> list[dict[str, str]]:
        rows = self.conn.execute("SELECT booking_id,slot FROM bookings ORDER BY booking_id").fetchall()
        return [dict(zip(("booking_id", "slot"), row)) for row in rows]
