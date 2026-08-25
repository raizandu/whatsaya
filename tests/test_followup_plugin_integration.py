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


class FollowupPluginIntegrationTest(unittest.TestCase):
    def setUp(self):
        self._policy_tmp = tempfile.TemporaryDirectory()
        policy_path = Path(self._policy_tmp.name) / "personal_contacts.json"
        policy_path.write_text("{}", encoding="utf-8")
        self._policy_patch = patch.object(wm, "_PERSONAL_CONTACTS_PATH", policy_path)
        self._policy_patch.start()

    def tearDown(self):
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
             patch.object(wm, "_followup_bridge_send", return_value="wamid-123") as send:
            self.assertEqual(wm._tick_followups(), 1)
        send.assert_called_once()
        self.assertEqual(engine.sent, [(7, "wamid-123")])
        self.assertEqual(engine.uncertain, [])
        self.assertEqual(engine.failed, [])

    def test_timeout_becomes_uncertain_and_never_sent(self):
        engine = FakeEngine()
        with patch.object(wm, "_followup_enabled", return_value=True), \
             patch.object(wm, "_check_bot_paused", return_value=False), \
             patch.object(wm, "_followup_engine", return_value=engine), \
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
             patch.object(wm, "_followup_bridge_send", side_effect=error):
            self.assertEqual(wm._tick_followups(), 0)
        self.assertEqual(engine.sent, [])
        self.assertEqual(engine.uncertain, [])
        self.assertEqual(engine.failed[0][0], 7)

    def test_bridge_sender_returns_real_id(self):
        response = MagicMock()
        response.read.return_value = json.dumps({"success": True, "messageId": "abc-123"}).encode()
        with patch("urllib.request.urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value = response
            self.assertEqual(wm._followup_bridge_send(JOB["chat_id"], "mensagem contextual"), "abc-123")

    def test_bridge_sender_rejects_success_without_id(self):
        response = MagicMock()
        response.read.return_value = json.dumps({"success": True}).encode()
        with patch("urllib.request.urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value = response
            with self.assertRaises(RuntimeError):
                wm._followup_bridge_send(JOB["chat_id"], "mensagem contextual")

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


if __name__ == "__main__":
    unittest.main()
