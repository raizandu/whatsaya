"""Testes do motor transacional de follow-up; não importam o plugin vivo."""
from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from commercial_followups import (
    ContextGateError,
    FollowupEngine,
    add_business_minutes,
    cadence_due_times,
    is_business_time,
    next_business_time,
    render_contextual_message,
    validate_context,
)


MONDAY = datetime(2026, 8, 17, 13, 0, tzinfo=UTC)  # 10:00 Goiânia
FRIDAY_1750 = datetime(2026, 8, 21, 20, 50, tzinfo=UTC)


class FollowupEngineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="whatsaya-followup-test-")
        self.db = Path(self.tmp.name) / "followups.db"
        self.engine = FollowupEngine(self.db)
        self.chat = "5511999999999@s.whatsapp.net"

    def tearDown(self):
        self.tmp.cleanup()

    def configure_safe(self, *, cadence="silence"):
        return self.engine.configure_lead(
            self.chat,
            automation_enabled=True,
            stage="qualification",
            cadence_kind=cadence,
            context_kind="pain",
            context_fact="o volume alto de perguntas repetidas no WhatsApp",
            context_source_message_id="inbound-context-1",
            context_verified=True,
            now=MONDAY,
        )

    def schedule(self, *, cadence="silence") -> list[int]:
        self.configure_safe(cadence=cadence)
        return self.engine.note_outbound(
            self.chat,
            message_id="bridge-out-1",
            at=MONDAY,
        )

    def test_schema_and_default_are_fail_closed(self):
        self.engine.note_inbound(self.chat, message_id="in-1", at=MONDAY)
        lead = self.engine.get_lead(self.chat)
        self.assertIsNotNone(lead)
        assert lead is not None
        self.assertEqual(lead["automation_enabled"], 0)
        con = sqlite3.connect(self.db)
        try:
            tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            con.close()
        self.assertTrue({"lead_state", "followup_jobs", "crm_outbox"}.issubset(tables))

    def test_business_minutes_cross_weekend(self):
        due = add_business_minutes(FRIDAY_1750, 30)
        self.assertEqual(due, datetime(2026, 8, 24, 11, 20, tzinfo=UTC))  # segunda 08:20
        self.assertTrue(is_business_time(due))

    def test_proposal_and_payment_cadence_due_times(self):
        proposal = cadence_due_times("proposal", MONDAY)
        payment = cadence_due_times("payment", MONDAY)
        self.assertEqual(proposal[0], datetime(2026, 8, 18, 13, 0, tzinfo=UTC))
        self.assertEqual(proposal[1], datetime(2026, 8, 20, 13, 0, tzinfo=UTC))
        self.assertEqual(payment[0], datetime(2026, 8, 17, 17, 0, tzinfo=UTC))

    def test_next_business_time_shifts_night(self):
        night = datetime(2026, 8, 17, 23, 30, tzinfo=UTC)  # segunda 20:30
        self.assertEqual(next_business_time(night), datetime(2026, 8, 18, 11, 0, tzinfo=UTC))

    def test_context_gate_rejects_missing_and_secrets(self):
        with self.assertRaises(ContextGateError):
            validate_context(None, None)
        with self.assertRaises(ContextGateError):
            validate_context("pain", "API key sk-abc123", "in-1", True)

    def test_no_context_means_no_jobs(self):
        self.engine.configure_lead(
            self.chat,
            automation_enabled=True,
            stage="qualification",
            cadence_kind="silence",
            now=MONDAY,
        )
        jobs = self.engine.note_outbound(self.chat, message_id="bridge-out-1", at=MONDAY)
        self.assertEqual(jobs, [])
        self.assertEqual(self.engine.get_jobs(self.chat), [])

    def test_safe_outbound_schedules_three_idempotent_jobs(self):
        first = self.schedule()
        second = self.engine.note_outbound(self.chat, message_id="bridge-out-1", at=MONDAY)
        jobs = self.engine.get_jobs(self.chat)
        self.assertEqual(len(first), 3)
        self.assertEqual(second, [])
        self.assertEqual(len(jobs), 3)
        self.assertEqual([job["step_no"] for job in jobs], [1, 2, 3])
        self.assertEqual(jobs[0]["due_utc"], "2026-08-17T13:30:00+00:00")

    def test_inbound_cancels_every_open_job_and_invalidates_generation(self):
        self.schedule()
        cancelled = self.engine.note_inbound(self.chat, message_id="in-new", at=MONDAY + timedelta(minutes=10))
        self.assertEqual(cancelled, 3)
        self.assertEqual({job["status"] for job in self.engine.get_jobs(self.chat)}, {"cancelled"})
        lead = self.engine.get_lead(self.chat)
        self.assertIsNotNone(lead)
        assert lead is not None
        self.assertEqual(lead["generation"], 2)

    def test_human_takeover_cancels_and_pauses(self):
        self.schedule()
        self.engine.note_human_takeover(self.chat, at=MONDAY + timedelta(minutes=5))
        lead = self.engine.get_lead(self.chat)
        self.assertIsNotNone(lead)
        assert lead is not None
        self.assertEqual(lead["takeover"], 1)
        self.assertEqual({job["status"] for job in self.engine.get_jobs(self.chat)}, {"cancelled"})

    def test_opt_out_or_terminal_cancels_jobs(self):
        self.schedule()
        self.engine.configure_lead(self.chat, opt_out=True, now=MONDAY + timedelta(minutes=5))
        self.assertEqual({job["status"] for job in self.engine.get_jobs(self.chat)}, {"cancelled"})
        self.engine.configure_lead(self.chat, opt_out=False, terminal=False, automation_enabled=True, now=MONDAY)
        self.engine.note_outbound(self.chat, message_id="bridge-out-2", at=MONDAY, cadence_kind="silence")
        self.engine.configure_lead(self.chat, stage="ganho", now=MONDAY + timedelta(minutes=6))
        self.assertTrue(all(job["status"] == "cancelled" for job in self.engine.get_jobs(self.chat)))

    def test_claim_outside_window_reschedules_without_lease(self):
        self.schedule()
        outside = datetime(2026, 8, 17, 23, 0, tzinfo=UTC)  # 20:00
        claimed = self.engine.claim_due(now=outside, worker_id="w1")
        self.assertEqual(claimed, [])
        first = self.engine.get_jobs(self.chat)[0]
        self.assertEqual(first["status"], "pending")
        self.assertEqual(first["due_utc"], "2026-08-18T11:00:00+00:00")

    def test_two_workers_cannot_claim_same_job(self):
        self.schedule()
        due = MONDAY + timedelta(minutes=31)
        results: list[list[dict]] = []
        barrier = threading.Barrier(3)

        def worker(name: str):
            engine = FollowupEngine(self.db)
            barrier.wait()
            results.append(engine.claim_due(now=due, worker_id=name, limit=1))

        threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=10)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(sum(len(batch) for batch in results), 1)

    def test_backlog_claims_only_next_step_in_sequence(self):
        self.schedule()
        far_future = MONDAY + timedelta(days=10)
        first = self.engine.claim_due(now=far_future, worker_id="w1", limit=20)
        self.assertEqual([job["step_no"] for job in first], [1])
        self.engine.mark_sent(
            first[0]["id"], "bridge-follow-1", first[0]["lease_token"], at=far_future
        )
        second = self.engine.claim_due(now=far_future, worker_id="w1", limit=20)
        self.assertEqual([job["step_no"] for job in second], [2])

    def test_expired_lease_becomes_uncertain_without_retry(self):
        self.schedule()
        claimed_at = MONDAY + timedelta(minutes=31)
        job = self.engine.claim_due(
            now=claimed_at, worker_id="crashed-worker", lease_seconds=15, limit=1
        )[0]
        again = self.engine.claim_due(
            now=claimed_at + timedelta(minutes=1), worker_id="new-worker", limit=20
        )
        self.assertEqual(again, [])
        jobs = self.engine.get_jobs(self.chat)
        self.assertEqual(jobs[0]["status"], "uncertain")
        self.assertEqual({row["status"] for row in jobs[1:]}, {"cancelled"})
        lead = self.engine.get_lead(self.chat)
        self.assertIsNotNone(lead)
        assert lead is not None
        self.assertEqual(lead["automation_enabled"], 0)

    def test_stale_lease_token_cannot_finalize_job(self):
        self.schedule()
        job = self.engine.claim_due(
            now=MONDAY + timedelta(minutes=31), worker_id="w1", limit=1
        )[0]
        self.engine.mark_sent(job["id"], "bridge-1", "token-errado", at=MONDAY)
        self.assertEqual(self.engine.get_jobs(self.chat)[0]["status"], "leased")
        self.engine.mark_sent(job["id"], "bridge-1", job["lease_token"], at=MONDAY)
        self.assertEqual(self.engine.get_jobs(self.chat)[0]["status"], "sent")

    def test_restart_preserves_queue_without_resending_sent_step(self):
        self.schedule()
        restarted = FollowupEngine(self.db)
        first = restarted.claim_due(
            now=MONDAY + timedelta(days=10), worker_id="after-restart", limit=20
        )[0]
        restarted.mark_sent(
            first["id"], "bridge-after-restart", first["lease_token"],
            at=MONDAY + timedelta(days=10),
        )
        restarted_again = FollowupEngine(self.db)
        next_jobs = restarted_again.claim_due(
            now=MONDAY + timedelta(days=10), worker_id="second-restart", limit=20
        )
        self.assertEqual([job["step_no"] for job in next_jobs], [2])

    def test_revalidation_cancels_after_reply(self):
        self.schedule()
        job = self.engine.claim_due(now=MONDAY + timedelta(minutes=31), worker_id="w1", limit=1)[0]
        self.engine.note_inbound(self.chat, message_id="in-after-claim", at=MONDAY + timedelta(minutes=31, seconds=1))
        self.assertIsNone(self.engine.revalidate_claim(
            job["id"], job["lease_token"], now=MONDAY + timedelta(minutes=31, seconds=2)
        ))
        self.assertEqual(self.engine.get_jobs(self.chat)[0]["status"], "cancelled")

    def test_uncertain_is_never_retried_automatically(self):
        self.schedule()
        job = self.engine.claim_due(now=MONDAY + timedelta(minutes=31), worker_id="w1", limit=1)[0]
        self.engine.mark_uncertain(
            job["id"], "timeout depois de possível envio", job["lease_token"],
            at=MONDAY + timedelta(minutes=31),
        )
        claimed_again = self.engine.claim_due(now=MONDAY + timedelta(days=2), worker_id="w2")
        self.assertNotIn(job["id"], {item["id"] for item in claimed_again})
        jobs = self.engine.get_jobs(self.chat)
        self.assertEqual(jobs[0]["status"], "uncertain")
        self.assertEqual({row["status"] for row in jobs[1:]}, {"cancelled"})
        lead = self.engine.get_lead(self.chat)
        self.assertIsNotNone(lead)
        assert lead is not None
        self.assertEqual(lead["automation_enabled"], 0)

    def test_definite_failure_pauses_future_cadence(self):
        self.schedule()
        job = self.engine.claim_due(now=MONDAY + timedelta(minutes=31), worker_id="w1", limit=1)[0]
        self.engine.mark_failed(
            job["id"], "bridge offline", job["lease_token"],
            at=MONDAY + timedelta(minutes=31),
        )
        jobs = self.engine.get_jobs(self.chat)
        self.assertEqual(jobs[0]["status"], "failed")
        self.assertEqual({row["status"] for row in jobs[1:]}, {"cancelled"})

    def test_contextual_copy_mentions_verified_fact(self):
        message = render_contextual_message({
            "context_kind": "question",
            "context_fact": "a integração com a agenda atual",
            "context_source_message_id": "inbound-context-2",
            "context_verified": 1,
            "step_no": 1,
        })
        self.assertIn("integração com a agenda atual", message)
        self.assertNotIn("ainda tá por aí", message.lower())
        self.assertNotIn("qualquer coisa é só chamar", message.lower())

    def test_outbox_rejects_transcript_and_deduplicates_version(self):
        with self.assertRaises(ValueError):
            self.engine.enqueue_outbox(self.chat, 1, {"transcript": "conteúdo bruto"}, at=MONDAY)
        payload = {"chat_id": self.chat, "stage": "qualification", "automation_enabled": False}
        self.assertTrue(self.engine.enqueue_outbox(self.chat, 1, payload, at=MONDAY))
        self.assertFalse(self.engine.enqueue_outbox(self.chat, 1, payload, at=MONDAY))


if __name__ == "__main__":
    unittest.main()
