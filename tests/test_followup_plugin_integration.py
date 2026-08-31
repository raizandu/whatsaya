"""Integração fina entre whatsapp_manager e o motor de follow-up."""
from __future__ import annotations

import json
import socket
import tempfile
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import whatsapp_manager as wm


JOB = {
    "id": 7,
    "chat_id": "5511999999999@s.whatsapp.net",
    "context_kind": "pain",
    "context_fact": "o volume alto de perguntas repetidas no WhatsApp",
    "context_source_message_id": "inbound-context-1",
    "context_verified": 1,
    "lease_token": "lease-token-1",
    "step_no": 1,
}


class FakeEngine:
    def __init__(self):
        self.sent = []
        self.uncertain = []
        self.failed = []

    def claim_due(self, **_kwargs):
        return [dict(JOB)]

    def revalidate_claim(self, _job_id, _lease_token, **_kwargs):
        return dict(JOB)

    def mark_sent(self, job_id, message_id, _lease_token):
        self.sent.append((job_id, message_id))

    def mark_uncertain(self, job_id, error, _lease_token):
        self.uncertain.append((job_id, error))

    def mark_failed(self, job_id, error, _lease_token):
        self.failed.append((job_id, error))

    def claim_outbox(self, **_kwargs):
        return []


class FollowupPluginIntegrationTest(unittest.TestCase):
    def setUp(self):
        self._policy_tmp = tempfile.TemporaryDirectory()
        policy_path = Path(self._policy_tmp.name) / "personal_contacts.json"
        policy_path.write_text("{}", encoding="utf-8")
        self._policy_patch = patch.object(wm, "_PERSONAL_CONTACTS_PATH", policy_path)
        self._policy_patch.start()
        self._followup_db_patch = patch.object(
            wm,
            "_FOLLOWUP_DB_PATH",
            Path(self._policy_tmp.name) / "commercial_followups.db",
        )
        self._followup_db_patch.start()
        wm._FOLLOWUP_ENGINE = None

    def tearDown(self):
        wm._FOLLOWUP_ENGINE = None
        self._followup_db_patch.stop()
        self._policy_patch.stop()
        self._policy_tmp.cleanup()

    def test_global_flag_accepts_only_explicit_true_values(self):
        for value in ("true", "1", "yes", "on"):
            with self.subTest(value=value), patch.object(wm, "_followup_env", return_value=value):
                self.assertTrue(wm._followup_enabled())
        for value in ("", "false", "0", "off", "lixo"):
            with self.subTest(value=value), patch.object(wm, "_followup_env", return_value=value):
                self.assertFalse(wm._followup_enabled())

    def test_disabled_tick_does_not_touch_engine(self):
        with patch.object(wm, "_followup_enabled", return_value=False), \
             patch.object(wm, "_followup_engine") as engine:
            self.assertEqual(wm._tick_followups(), 0)
            engine.assert_not_called()

    def test_success_requires_bridge_message_id_and_marks_sent(self):
        engine = FakeEngine()
        with patch.object(wm, "_followup_enabled", return_value=True), \
             patch.object(wm, "_check_bot_paused", return_value=False), \
             patch.object(wm, "_followup_engine", return_value=engine), \
             patch.object(wm, "_assert_delivery_allowed"), \
             patch.object(wm, "_followup_bridge_send", return_value="wamid-123") as send:
            self.assertEqual(wm._tick_followups(), 1)
        send.assert_called_once()
        self.assertEqual(send.call_args.kwargs["require_ai_access"], True)
        self.assertEqual(engine.sent, [(7, "wamid-123")])
        self.assertEqual(engine.uncertain, [])
        self.assertEqual(engine.failed, [])

    def test_timeout_becomes_uncertain_and_never_sent(self):
        engine = FakeEngine()
        with patch.object(wm, "_followup_enabled", return_value=True), \
             patch.object(wm, "_check_bot_paused", return_value=False), \
             patch.object(wm, "_followup_engine", return_value=engine), \
             patch.object(wm, "_assert_delivery_allowed"), \
             patch.object(wm, "_followup_bridge_send", side_effect=socket.timeout("ambiguous")):
            self.assertEqual(wm._tick_followups(), 0)
        self.assertEqual(engine.sent, [])
        self.assertEqual(engine.failed, [])
        self.assertEqual(engine.uncertain[0][0], 7)

    def test_definite_bridge_failure_is_terminal_failed(self):
        engine = FakeEngine()
        error = urllib.error.URLError(ConnectionRefusedError("offline"))
        with patch.object(wm, "_followup_enabled", return_value=True), \
             patch.object(wm, "_check_bot_paused", return_value=False), \
             patch.object(wm, "_followup_engine", return_value=engine), \
             patch.object(wm, "_assert_delivery_allowed"), \
             patch.object(wm, "_followup_bridge_send", side_effect=error):
            self.assertEqual(wm._tick_followups(), 0)
        self.assertEqual(engine.sent, [])
        self.assertEqual(engine.uncertain, [])
        self.assertEqual(engine.failed[0][0], 7)

    def test_bridge_sender_returns_real_id(self):
        response = MagicMock()
        response.read.return_value = json.dumps({"success": True, "messageId": "abc-123"}).encode()
        with patch.object(wm, "_assert_delivery_allowed") as gate, \
             patch("urllib.request.urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value = response
            self.assertEqual(wm._followup_bridge_send(JOB["chat_id"], "mensagem contextual"), "abc-123")
        gate.assert_called_once_with(JOB["chat_id"], require_ai_access=True)

    def test_bridge_sender_rejects_success_without_id(self):
        response = MagicMock()
        response.read.return_value = json.dumps({"success": True}).encode()
        with patch.object(wm, "_assert_delivery_allowed"), \
             patch("urllib.request.urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value = response
            with self.assertRaises(RuntimeError):
                wm._followup_bridge_send(JOB["chat_id"], "mensagem contextual")

    def test_bridge_sender_policy_block_prevents_post(self):
        with patch.object(
            wm,
            "_assert_delivery_allowed",
            side_effect=wm.DeliveryBlocked("ai desabilitada"),
        ) as gate, patch("urllib.request.urlopen") as urlopen:
            with self.assertRaises(wm.DeliveryBlocked):
                wm._followup_bridge_send(JOB["chat_id"], "mensagem contextual")

        gate.assert_called_once_with(JOB["chat_id"], require_ai_access=True)
        urlopen.assert_not_called()

    def test_tick_revalidates_again_under_contact_lock(self):
        engine = FakeEngine()
        response = MagicMock()
        response.read.return_value = json.dumps({"success": True, "messageId": "wamid-locked"}).encode()
        with patch.object(wm, "_followup_enabled", return_value=True), \
             patch.object(wm, "_check_bot_paused", return_value=False), \
             patch.object(wm, "_followup_engine", return_value=engine), \
             patch.object(wm, "_assert_delivery_allowed") as gate, \
             patch("urllib.request.urlopen") as urlopen, \
             patch.object(engine, "revalidate_claim", wraps=engine.revalidate_claim) as revalidate:
            urlopen.return_value.__enter__.return_value = response
            self.assertEqual(wm._tick_followups(), 1)

        self.assertEqual(revalidate.call_count, 2)
        gate.assert_called_once_with(JOB["chat_id"], require_ai_access=True)
        self.assertEqual(urlopen.call_count, 1)
        request = urlopen.call_args.args[0]
        self.assertEqual(json.loads(request.data.decode())["automation"], True)

    def test_final_revalidation_failure_never_posts_or_marks_sent(self):
        engine = FakeEngine()
        with patch.object(wm, "_followup_enabled", return_value=True), \
             patch.object(wm, "_check_bot_paused", return_value=False), \
             patch.object(wm, "_followup_engine", return_value=engine), \
             patch.object(engine, "revalidate_claim", side_effect=[dict(JOB), None]), \
             patch.object(wm, "_assert_delivery_allowed") as gate, \
             patch.object(wm, "_followup_bridge_send") as send:
            self.assertEqual(wm._tick_followups(), 0)

        gate.assert_not_called()
        send.assert_not_called()
        self.assertEqual(engine.sent, [])

    def test_policy_block_never_posts_or_marks_sent(self):
        engine = FakeEngine()
        with patch.object(wm, "_followup_enabled", return_value=True), \
             patch.object(wm, "_check_bot_paused", return_value=False), \
             patch.object(wm, "_followup_engine", return_value=engine), \
             patch.object(
                 wm,
                 "_assert_delivery_allowed",
                 side_effect=wm.DeliveryBlocked("contato removido da IA"),
             ) as gate, \
             patch("urllib.request.urlopen") as urlopen:
            self.assertEqual(wm._tick_followups(), 0)

        gate.assert_called_once_with(JOB["chat_id"], require_ai_access=True)
        urlopen.assert_not_called()
        self.assertEqual(engine.sent, [])
        self.assertEqual(engine.failed[0][0], JOB["id"])

    def test_manual_command_cannot_bypass_disabled_gate(self):
        with patch.object(wm, "_followup_enabled", return_value=False), \
             patch.object(wm, "_tick_followups") as tick:
            result = wm._followup_manual_from_owner("owner@s.whatsapp.net")
        self.assertIn("desativado", result)
        tick.assert_not_called()

    def test_internal_loop_never_competes_with_cron(self):
        with patch.object(wm, "_tick_followups") as tick:
            wm._run_followup_loop()
        tick.assert_not_called()

    def test_inbound_cancels_before_bot_pause_skip(self):
        source = SimpleNamespace(
            platform="whatsapp",
            user_id="5511888888888@s.whatsapp.net",
            chat_id="5511888888888@s.whatsapp.net",
        )
        event = SimpleNamespace(
            source=source,
            text="oi",
            body="oi",
            raw={},
            raw_message={},
            message_id="inbound-real-1",
            has_media=False,
        )
        gateway = MagicMock()
        with patch.dict(wm.os.environ, {"WHATSAPP_OWNER_NUMBER": "5511999999999"}), \
             patch.object(wm, "_followup_note_activity") as note, \
             patch.object(wm, "_ensure_contact_ai_access", return_value=(True, "explicit-flow")), \
             patch.object(wm, "_check_bot_paused", return_value=True):
            result = wm.pre_gateway_dispatch("pre_gateway_dispatch", {"event": event, "gateway": gateway})
        self.assertEqual(result, {"action": "skip", "reason": "bot-pausado"})
        note.assert_called_once_with(
            "5511888888888@s.whatsapp.net",
            inbound=True,
            message_id="inbound-real-1",
            text="oi",
        )

    def test_historical_import_never_touches_followup_state(self):
        source = SimpleNamespace(
            platform="whatsapp",
            user_id="5511888888888@s.whatsapp.net",
            chat_id="5511888888888@s.whatsapp.net",
        )
        event = SimpleNamespace(
            source=source,
            text="mensagem antiga",
            body="mensagem antiga",
            raw={"messageId": "historical-1", "isHistorical": True},
            raw_message={},
            message_id="historical-1",
            is_historical=True,
            has_media=False,
        )
        gateway = MagicMock()
        with patch.dict(wm.os.environ, {"WHATSAPP_OWNER_NUMBER": "5511999999999"}), \
             patch.object(wm, "_followup_note_activity") as note, \
             patch.object(wm, "_check_bot_paused", return_value=True):
            result = wm.pre_gateway_dispatch("pre_gateway_dispatch", {"event": event, "gateway": gateway})
        self.assertEqual(result, {"action": "skip", "reason": "historical-import"})
        note.assert_not_called()

    def test_deterministic_opt_out_disables_lead(self):
        chat_id = "5511777777777@s.whatsapp.net"
        wm._followup_note_activity(
            chat_id,
            inbound=True,
            message_id="opt-out-1",
            text="não me mande mais mensagens",
        )
        lead = wm._followup_engine().get_lead(chat_id)
        self.assertIsNotNone(lead)
        assert lead is not None
        self.assertEqual(lead["opt_out"], 1)
        self.assertEqual(lead["automation_enabled"], 0)

    def test_clinic_turn_schedules_personalized_followup(self):
        from commercial_followups import FollowupEngine, render_contextual_message

        chat = "5511999995750@s.whatsapp.net"
        db = Path(self._policy_tmp.name) / "followups.db"
        engine = FollowupEngine(db)
        with patch.object(wm, "_followup_engine", return_value=engine), \
             patch.object(wm, "_followup_skip_contact", return_value=False):
            wm._followup_remember_turn(
                chat,
                "Tenho uma clínica odontológica e recebo bastante gente "
                "perguntando sobre procedimentos",
                "wamid-in-clinic",
            )
            ids = wm._followup_register_outbound(chat, "wamid-out-clinic")
        self.assertTrue(ids)
        lead = engine.get_lead(chat)
        self.assertIsNotNone(lead)
        assert lead is not None
        self.assertEqual(lead["stage"], "qualification")
        self.assertEqual(lead["cadence_kind"], "silence")
        self.assertIn("clínica odontológica", lead["context_fact"])
        job = engine.get_jobs(chat)[0]
        text = render_contextual_message(job)
        self.assertIn("clínica odontológica", text.lower())
        self.assertNotIn("ainda tá por aí", text.lower())

    def test_price_turn_uses_proposal_cadence(self):
        from commercial_followups import FollowupEngine

        chat = "5511999995750@s.whatsapp.net"
        db = Path(self._policy_tmp.name) / "followups-price.db"
        engine = FollowupEngine(db)
        with patch.object(wm, "_followup_engine", return_value=engine), \
             patch.object(wm, "_followup_skip_contact", return_value=False):
            wm._followup_remember_turn(
                chat,
                "Legal. E quanto custa pra colocar isso na minha clínica?",
                "wamid-in-price",
            )
            wm._followup_register_outbound(chat, "wamid-out-price")
        lead = engine.get_lead(chat)
        assert lead is not None
        self.assertEqual(lead["stage"], "pricing")
        self.assertEqual(lead["cadence_kind"], "proposal")

    def test_no_snapshot_does_not_send_generic_ping(self):
        from commercial_followups import FollowupEngine

        chat = "5511888888888@s.whatsapp.net"
        db = Path(self._policy_tmp.name) / "followups-empty.db"
        engine = FollowupEngine(db)
        with patch.object(wm, "_followup_engine", return_value=engine):
            ids = wm._followup_register_outbound(chat, "wamid-out-empty")
        self.assertEqual(ids, [])
        self.assertIsNone(engine.get_lead(chat))

    def test_personal_greeting_never_arms_commercial_followup(self):
        from commercial_followups import FollowupEngine

        chat = "5511666666666@s.whatsapp.net"
        db = Path(self._policy_tmp.name) / "followups-personal.db"
        engine = FollowupEngine(db)
        with patch.object(wm, "_followup_engine", return_value=engine), \
             patch.object(wm, "_followup_skip_contact", return_value=False):
            wm._followup_remember_turn(chat, "Fala aí, suave?", "wamid-in-personal")
            ids = wm._followup_register_outbound(chat, "wamid-out-personal")

        self.assertEqual(ids, [])
        self.assertIsNone(engine.get_lead(chat))

    def test_failed_turn_cas_does_not_consume_newer_followup_snapshot(self):
        from commercial_followups import FollowupEngine

        chat = "5511999995750@s.whatsapp.net"
        db = Path(self._policy_tmp.name) / "followups-cas.db"
        engine = FollowupEngine(db)
        text_a = "Tenho uma clínica odontológica e recebo perguntas sobre procedimentos"
        text_b = "Tenho uma clínica odontológica e preciso organizar os retornos"
        inbound_a = {"message_id": "msg-a", "at": 100.0, "text": text_a}
        token_a = ("msg-a", 100.0)

        def fail_after_newer_snapshot(*_args, **_kwargs):
            # Simula B chegando enquanto a entrega A ainda está em andamento.
            wm._followup_remember_turn(chat, text_b, "msg-b")
            raise RuntimeError("nenhum messageId confirmado")

        with patch.object(wm, "_followup_engine", return_value=engine), \
             patch.object(wm, "_followup_skip_contact", return_value=False), \
             patch.object(wm, "_HUMAN_DELIVER_SYNC", True), \
             patch.object(wm, "_deliver_contact_reply", side_effect=fail_after_newer_snapshot):
            wm._followup_remember_turn(chat, text_a, "msg-a")
            delivered = wm._schedule_contact_reply(
                chat,
                "Resposta de A",
                "turn-a",
                consumed_inbound_token=token_a,
                inbound_snapshot=inbound_a,
            )
            ids = wm._followup_register_outbound(chat, "out-b")

        self.assertFalse(delivered)
        self.assertTrue(ids)
        jobs = engine.get_jobs(chat)
        self.assertTrue(jobs)
        self.assertTrue(all(job["context_source_message_id"] == "msg-b" for job in jobs))
        self.assertNotIn("msg-a", {job["context_source_message_id"] for job in jobs})

    def test_crm_outbox_drain_is_fail_closed_without_leads_db(self):
        with patch.dict(wm.os.environ, {"NOTION_API_KEY": "secret_x", "NOTION_LEADS_DB": ""}, clear=False), \
             patch.object(wm, "_notion_post") as post:
            self.assertEqual(wm._tick_crm_outbox(), 0)
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
