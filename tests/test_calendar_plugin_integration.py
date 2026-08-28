"""Integração segura das tools de Calendar com turnos WhatsApp."""
from __future__ import annotations

import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.append(str(Path(__file__).parent.parent))

import whatsapp_manager as wm


class CalendarPluginIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.session = "calendar-session"
        self.chat = "5562999999999@s.whatsapp.net"
        wm._calendar_turn_state.clear()
        wm._sender_to_chat.clear()
        wm._pending_inbound.clear()
        wm._turn_key.clear()
        wm._turn_inbound.clear()
        wm._turn_sent.clear()
        wm._turn_inflight.clear()
        wm._turn_context_bindings.set(())
        wm._sender_to_chat[self.session] = self.chat

    def tearDown(self):
        wm._calendar_turn_state.clear()
        wm._sender_to_chat.clear()
        wm._pending_inbound.clear()
        wm._turn_key.clear()
        wm._turn_inbound.clear()
        wm._turn_sent.clear()
        wm._turn_inflight.clear()
        wm._turn_context_bindings.set(())
        wm._HUMAN_DELIVER_SYNC = False

    def _inbound(self, message_id: str, text: str) -> tuple[str, float]:
        wm._track_inbound(self.chat, message_id, text)
        record = wm._current_inbound_record(self.chat, self.session)
        token = wm._inbound_record_token(record)
        self.assertIsNotNone(token)
        return token

    @staticmethod
    def _slots():
        return {
            "status": "ok",
            "timezone": "America/Sao_Paulo",
            "duration_minutes": 30,
            "slots": [
                {
                    "start": "2026-08-31T14:00:00-03:00",
                    "end": "2026-08-31T14:30:00-03:00",
                },
                {
                    "start": "2026-08-31T14:30:00-03:00",
                    "end": "2026-08-31T15:00:00-03:00",
                },
            ],
        }

    def test_calendar_tools_are_the_only_new_tools_allowed_for_contact(self):
        for tool_name in (wm._CALENDAR_FIND_TOOL, wm._CALENDAR_BOOK_TOOL):
            with self.subTest(tool_name=tool_name):
                self.assertIsNone(wm.pre_tool_call(
                    "pre_tool_call",
                    platform="whatsapp",
                    session_id=self.session,
                    tool_name=tool_name,
                ))
        blocked = wm.pre_tool_call(
            "pre_tool_call",
            platform="whatsapp",
            session_id=self.session,
            tool_name="terminal",
        )
        self.assertEqual(blocked.get("action"), "block")

    def test_find_stores_only_current_turn_offer(self):
        token = self._inbound("msg-find", "Pode ser segunda à tarde")
        with patch("whatsapp_manager.find_available_slots", return_value=self._slots()):
            result = json.loads(wm._handle_calendar_find_slots(
                {"date_from": "2026-08-31", "period": "afternoon"},
                session_id=self.session,
            ))

        self.assertEqual(result["status"], "ok")
        state = wm._calendar_turn_state[self.chat]
        self.assertEqual(state["kind"], "offered")
        self.assertEqual(state["inbound_token"], token)
        self.assertEqual(len(state["slots"]), 2)

    def test_book_rejects_same_turn_without_later_confirmation(self):
        self._inbound("msg-find", "Pode ser segunda à tarde")
        with patch("whatsapp_manager.find_available_slots", return_value=self._slots()):
            wm._handle_calendar_find_slots(
                {"date_from": "2026-08-31", "period": "afternoon"},
                session_id=self.session,
            )
        first = self._slots()["slots"][0]
        with patch("whatsapp_manager.create_booking") as create:
            result = json.loads(wm._handle_calendar_book(first, session_id=self.session))

        self.assertEqual(result["status"], "error")
        self.assertIn("mensagem posterior", result["error"])
        create.assert_not_called()

    def test_book_accepts_exact_offered_slot_after_explicit_confirmation(self):
        self._inbound("msg-find", "Pode ser segunda à tarde")
        with patch("whatsapp_manager.find_available_slots", return_value=self._slots()):
            wm._handle_calendar_find_slots(
                {"date_from": "2026-08-31", "period": "afternoon"},
                session_id=self.session,
            )

        token = self._inbound("msg-confirm", "Sim, pode marcar o primeiro")
        first = self._slots()["slots"][0]
        booked = {
            "status": "created",
            "event_id": "event-1",
            "summary": "Call WhatsAYA — Lead",
            "start": first["start"],
            "end": first["end"],
            "timezone": "America/Sao_Paulo",
            "htmlLink": "https://calendar.google.test/private",
        }
        with patch("whatsapp_manager.create_booking", return_value=booked) as create:
            result = json.loads(wm._handle_calendar_book(first, session_id=self.session))

        self.assertEqual(result["status"], "created")
        self.assertEqual(wm._calendar_turn_state[self.chat]["kind"], "booked")
        self.assertEqual(wm._calendar_turn_state[self.chat]["inbound_token"], token)
        self.assertEqual(create.call_args.kwargs["purpose"], "Apresentação comercial da WhatsAYA")

    def test_book_rejects_slot_not_returned_by_google(self):
        self._inbound("msg-find", "Pode ser segunda à tarde")
        with patch("whatsapp_manager.find_available_slots", return_value=self._slots()):
            wm._handle_calendar_find_slots(
                {"date_from": "2026-08-31", "period": "afternoon"},
                session_id=self.session,
            )
        self._inbound("msg-confirm", "Sim, pode marcar")
        with patch("whatsapp_manager.create_booking") as create:
            result = json.loads(wm._handle_calendar_book(
                {
                    "start": "2026-09-01T14:00:00-03:00",
                    "end": "2026-09-01T14:30:00-03:00",
                },
                session_id=self.session,
            ))

        self.assertEqual(result["status"], "error")
        self.assertIn("não pertence", result["error"])
        create.assert_not_called()

    def test_transform_uses_verified_offer_and_does_not_expose_event_link(self):
        token = self._inbound("msg-offer", "Quero segunda à tarde")
        turn_key = wm._register_contact_turn(self.chat, self.session, "Quero segunda à tarde")
        wm._calendar_turn_state[self.chat] = {
            "kind": "offered",
            "at": time.time(),
            "expires_at": time.time() + 600,
            "inbound_token": token,
            "slots": self._slots()["slots"],
        }
        wm._HUMAN_DELIVER_SYNC = True

        with patch.dict(os.environ, {"WHATSAPP_OWNER_NUMBER": "5511999999999"}, clear=False), \
             patch("whatsapp_manager._assert_delivery_allowed"), \
             patch("whatsapp_manager._maybe_send_voice", return_value=None), \
             patch("whatsapp_manager._persist_turn_sent_to_disk"), \
             patch("whatsapp_manager._human_send", return_value="wamid-1") as send:
            result = wm.transform_llm_output(
                "transform_llm_output",
                platform="whatsapp",
                session_id=self.session,
                assistant_response=(
                    "Inventei um horário. [[HANDOFF: lead topou call]] "
                    "https://calendar.google.test/private"
                ),
            )

        self.assertEqual(result, "\n")
        self.assertIn(turn_key, wm._turn_sent)
        visible = send.call_args.args[1]
        self.assertIn("14:00", visible)
        self.assertIn("14:30", visible)
        self.assertNotIn("Inventei", visible)
        self.assertNotIn("calendar.google", visible)
        self.assertNotIn("HANDOFF", visible)


if __name__ == "__main__":
    unittest.main()
