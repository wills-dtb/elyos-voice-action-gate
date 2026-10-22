import tempfile
import unittest
from pathlib import Path
from elyos_gate.core import ActionGate


def evt(event_id, conv="c1", action="noop", **kw):
    return {"event_id": event_id, "conversation_id": conv, "action": action,
            "confidence": 0.95, "latency_ms": 180, **kw}


class GateTests(unittest.TestCase):
    def setUp(self):
        self.gate = ActionGate()

    def tearDown(self):
        self.gate.close()

    def test_confirmation_required(self):
        self.assertEqual(self.gate.process(evt("a", action="confirm_booking", caller_confirmed=True)).status, "blocked")
        self.gate.process(evt("b", action="propose_booking", slot="2026-11-03T09:00"))
        self.assertEqual(self.gate.process(evt("c", action="confirm_booking")).status, "blocked")
        self.assertEqual(len(self.gate.bookings()), 0)

    def test_confirmed_booking_committed_once(self):
        self.gate.process(evt("a", action="propose_booking", slot="2026-11-03T09:00"))
        request = evt("b", action="confirm_booking", caller_confirmed=True)
        self.assertEqual(self.gate.process(request).status, "committed")
        self.assertEqual(self.gate.process(request).status, "committed")
        self.assertEqual(len(self.gate.bookings()), 1)
        self.assertEqual(len(self.gate.audit_events()), 2)

    def test_reused_idempotency_key_rejected(self):
        self.gate.process(evt("same"))
        with self.assertRaisesRegex(ValueError, "idempotency"):
            self.gate.process(evt("same", action="handoff"))

    def test_slot_conflict_between_calls(self):
        for conv in ("c1", "c2"):
            self.gate.process(evt(conv + "p", conv, "propose_booking", slot="2026-11-03T09:00"))
        self.assertEqual(self.gate.process(evt("c1c", "c1", "confirm_booking", caller_confirmed=True)).status, "committed")
        self.assertEqual(self.gate.process(evt("c2c", "c2", "confirm_booking", caller_confirmed=True)).status, "blocked")
        self.assertEqual(len(self.gate.bookings()), 1)

    def test_low_confidence_handoff_blocks_followup(self):
        low = evt("a", action="propose_booking", slot="2026-11-03T09:00")
        low["confidence"] = 0.4
        self.assertEqual(self.gate.process(low).status, "handoff")
        self.assertEqual(self.gate.process(evt("b", action="confirm_booking", caller_confirmed=True)).status, "blocked")

    def test_explicit_handoff_blocks_automation(self):
        self.assertEqual(self.gate.process(evt("a", action="handoff")).status, "handoff")
        self.assertEqual(self.gate.process(evt("b", action="propose_booking", slot="x")).status, "blocked")

    def test_cancellation_is_not_automated(self):
        self.assertEqual(self.gate.process(evt("a", action="cancel_booking")).status, "handoff")
        self.assertEqual(self.gate.bookings(), [])

    def test_latency_breach_recorded_without_claiming_wallclock(self):
        request = evt("a")
        request["latency_ms"] = 650
        self.assertTrue(self.gate.process(request).latency_budget_breached)

    def test_audit_excludes_raw_conversation_id(self):
        self.gate.process(evt("a", conv="sensitive-caller-id"))
        self.assertNotIn("sensitive-caller-id", str(self.gate.audit_events()))

    def test_validation_and_no_partial_write(self):
        with self.assertRaises(ValueError):
            self.gate.process(evt("a", confidence=1.5))
        self.assertEqual(self.gate.audit_events(), [])

    def test_durable_retry_after_restart(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "gate.sqlite"
            first = ActionGate(db)
            first.process(evt("a", action="propose_booking", slot="2026-11-03T09:00"))
            request = evt("b", action="confirm_booking", caller_confirmed=True)
            result = first.process(request)
            first.close()
            second = ActionGate(db)
            self.assertEqual(second.process(request), result)
            self.assertEqual(len(second.bookings()), 1)
            second.close()

    def test_second_booking_in_same_conversation_rejected(self):
        self.gate.process(evt("a", action="propose_booking", slot="slot1"))
        self.gate.process(evt("b", action="confirm_booking", caller_confirmed=True))
        self.gate.process(evt("c", action="propose_booking", slot="slot2"))
        self.assertEqual(self.gate.process(evt("d", action="confirm_booking", caller_confirmed=True)).status, "blocked")

if __name__ == "__main__":
    unittest.main()
