"""Integração segura das tools de Calendar com turnos WhatsApp."""
from __future__ import annotations

import json
import os
import sys
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

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

    @staticmethod
    def _gustavo_call_history(extra: str = "") -> str:
        history = (
            "Lead: Tenho uma clinica\n"
            "AYA: Com uns 10 contatos por dia, a AYA já ajuda bastante a manter "
            "as respostas rápidas e não perder quem chega interessado.\n"
            "AYA: Posso te dar um norte mais certeiro para a clínica numa call rápida "
            "e montar uma proposta personalizada.\n\nQuer avançar?\n"
        )
        return history + extra

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
        self._inbound("msg-confirm", "Sim, pode marcar o primeiro")
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

    def test_book_rejects_ambiguous_confirmation_with_multiple_slots(self):
        self._inbound("msg-find", "Pode ser segunda à tarde")
        with patch("whatsapp_manager.find_available_slots", return_value=self._slots()):
            wm._handle_calendar_find_slots(
                {"date_from": "2026-08-31", "period": "afternoon"},
                session_id=self.session,
            )
        self._inbound("msg-confirm", "Sim")
        first = self._slots()["slots"][0]
        with patch("whatsapp_manager.create_booking") as create:
            result = json.loads(wm._handle_calendar_book(first, session_id=self.session))

        self.assertEqual(result["status"], "error")
        self.assertIn("qual horário", result["error"])
        create.assert_not_called()

    def test_orchestrator_queries_real_slots_for_pending_call_date(self):
        self._inbound("msg-date", "Pode ser amanhã, no sábado de manhã")
        history = (
            "Lead: Tenho uma clínica odontológica\n"
            "AYA: Topa uma call rápida essa semana?\n"
        )
        saturday_without_slots = dict(self._slots(), slots=[])
        now = datetime(2026, 8, 28, 16, 32, tzinfo=ZoneInfo("America/Sao_Paulo"))

        with patch("whatsapp_manager.calendar_ready", return_value=True), \
             patch("whatsapp_manager.find_available_slots", return_value=saturday_without_slots) as find:
            handled = wm._orchestrate_calendar_turn(
                chat_id=self.chat,
                session_id=self.session,
                user_message="Pode ser amanhã, no sábado de manhã",
                history=history,
                now=now,
            )

        self.assertTrue(handled)
        find.assert_called_once_with(
            date_from="2026-08-29",
            date_to=None,
            period="morning",
            preferred_time=None,
        )
        self.assertEqual(wm._calendar_turn_state[self.chat]["kind"], "empty")
        self.assertEqual(wm._calendar_turn_state[self.chat]["slots"], [])

    def test_gustavo_call_invitation_is_recognized_as_pending(self):
        named_history = self._gustavo_call_history().replace("Lead:", "Gustavo:")
        self.assertTrue(wm._history_has_pending_sales_call(
            named_history,
            lead_names=("Gustavo",),
        ))

    def test_period_history_recognizes_the_contacts_real_name(self):
        history = "Gustavo: Tarde\nAYA: Boa! Qual dia funciona melhor para você?"
        self.assertEqual(
            wm._calendar_period_from_history(
                history,
                "Pode ser amanhã",
                lead_names=("Gustavo",),
            ),
            "afternoon",
        )

    def test_call_acceptance_enters_calendar_collection_before_handoff(self):
        token = self._inbound("msg-accept", "Sim")
        history = self._gustavo_call_history().replace("Lead:", "Gustavo:")

        with patch("whatsapp_manager.calendar_ready", return_value=True), \
             patch("whatsapp_manager._contact_record_for_chat", return_value={"name": "Gustavo"}):
            handled = wm._orchestrate_calendar_turn(
                chat_id=self.chat,
                session_id=self.session,
                user_message="Sim",
                history=history,
                now=datetime(2026, 8, 28, 16, 32, tzinfo=ZoneInfo("America/Sao_Paulo")),
            )

        self.assertTrue(handled)
        state = wm._calendar_turn_state[self.chat]
        self.assertEqual(state["kind"], "collecting")
        self.assertEqual(state["inbound_token"], token)
        visible = wm._calendar_visible_reply(state, "Sim")
        self.assertIn("Qual dia e período", visible)
        self.assertNotIn("HANDOFF", visible)

    def test_date_query_keeps_afternoon_from_previous_lead_message(self):
        self._inbound("msg-date", "Pode ser amanha")
        no_slots = dict(self._slots(), slots=[])
        history = self._gustavo_call_history(
            "Lead: Sim\n"
            "AYA: Show! Qual dia e período, manhã ou tarde, funcionam melhor para você?\n"
            "Lead: Tarde\n"
            "AYA: Boa! Qual dia funciona melhor para você?\n"
        ).replace("Lead:", "Gustavo:")
        now = datetime(2026, 8, 28, 16, 32, tzinfo=ZoneInfo("America/Sao_Paulo"))

        with patch("whatsapp_manager.calendar_ready", return_value=True), \
             patch("whatsapp_manager._contact_record_for_chat", return_value={"name": "Gustavo"}), \
             patch("whatsapp_manager.find_available_slots", return_value=no_slots) as find:
            handled = wm._orchestrate_calendar_turn(
                chat_id=self.chat,
                session_id=self.session,
                user_message="Pode ser amanha",
                history=history,
                now=now,
            )

        self.assertTrue(handled)
        find.assert_called_once_with(
            date_from="2026-08-29",
            date_to=None,
            period="afternoon",
            preferred_time=None,
        )
        state = wm._calendar_turn_state[self.chat]
        self.assertEqual(state["kind"], "empty")
        self.assertEqual(state["query"]["period"], "afternoon")
        self.assertEqual(wm._calendar_open_offer(self.chat), {})

    def test_available_day_question_searches_next_fourteen_days(self):
        self._inbound("msg-next-days", "Qual dia tem?")
        history = self._gustavo_call_history(
            "Lead: Tarde\n"
            "AYA: Não encontrei vaga nesse período. Quer tentar outro dia ou manhã/tarde?\n"
        )
        now = datetime(2026, 8, 28, 16, 32, tzinfo=ZoneInfo("America/Sao_Paulo"))

        with patch("whatsapp_manager.calendar_ready", return_value=True), \
             patch("whatsapp_manager.find_available_slots", return_value=self._slots()) as find:
            handled = wm._orchestrate_calendar_turn(
                chat_id=self.chat,
                session_id=self.session,
                user_message="Qual dia tem?",
                history=history,
                now=now,
            )

        self.assertTrue(handled)
        find.assert_called_once_with(
            date_from="2026-08-28",
            date_to="2026-09-10",
            period="afternoon",
            preferred_time=None,
        )
        self.assertEqual(wm._calendar_turn_state[self.chat]["kind"], "offered")

    def test_exact_time_without_matching_offer_requeries_future_days(self):
        old_token = self._inbound("msg-old-offer", "Qual dia tem?")
        wm._calendar_turn_state[self.chat] = {
            "kind": "offered",
            "at": time.time(),
            "expires_at": time.time() + 600,
            "inbound_token": old_token,
            "slots": self._slots()["slots"],
            "query": {"period": "afternoon"},
        }
        self._inbound("msg-time", "Tipo umas 16h")
        future_at_sixteen = dict(self._slots(), slots=[{
            "start": "2026-09-01T16:00:00-03:00",
            "end": "2026-09-01T16:30:00-03:00",
        }])
        now = datetime(2026, 8, 28, 16, 32, tzinfo=ZoneInfo("America/Sao_Paulo"))

        with patch("whatsapp_manager.calendar_ready", return_value=True), \
             patch("whatsapp_manager.find_available_slots", return_value=future_at_sixteen) as find:
            handled = wm._orchestrate_calendar_turn(
                chat_id=self.chat,
                session_id=self.session,
                user_message="Tipo umas 16h",
                history=self._gustavo_call_history("Lead: Tarde\n"),
                now=now,
            )

        self.assertTrue(handled)
        find.assert_called_once_with(
            date_from="2026-08-28",
            date_to="2026-09-10",
            period="afternoon",
            preferred_time="16:00",
        )

    def test_empty_query_is_not_mistaken_for_an_open_offer(self):
        token = self._inbound("msg-empty", "Pode ser amanhã")
        wm._calendar_turn_state[self.chat] = {
            "kind": "empty",
            "at": time.time(),
            "expires_at": time.time() + 600,
            "inbound_token": token,
            "slots": [],
            "query": {"period": "afternoon"},
        }

        self.assertEqual(wm._calendar_open_offer(self.chat), {})
        self.assertNotIn(
            "Qual dos horários",
            wm._calendar_visible_reply(wm._calendar_turn_state[self.chat], "Sim"),
        )

    def test_confirmation_after_empty_query_collects_preferences_instead_of_choices(self):
        old_token = self._inbound("msg-empty", "Pode ser amanhã")
        wm._calendar_turn_state[self.chat] = {
            "kind": "empty",
            "at": time.time(),
            "expires_at": time.time() + 600,
            "inbound_token": old_token,
            "slots": [],
            "query": {"period": "afternoon"},
        }
        self._inbound("msg-confirm-empty", "Sim")

        with patch("whatsapp_manager.calendar_ready", return_value=True):
            handled = wm._orchestrate_calendar_turn(
                chat_id=self.chat,
                session_id=self.session,
                user_message="Sim",
                history=self._gustavo_call_history("Lead: Tarde\n"),
            )

        self.assertTrue(handled)
        state = wm._calendar_turn_state[self.chat]
        self.assertEqual(state["kind"], "collecting")
        visible = wm._calendar_visible_reply(state, "Sim")
        self.assertIn("Qual dia", visible)
        self.assertNotIn("Qual dos horários", visible)

    def test_collecting_state_suppresses_model_handoff_before_owner_notification(self):
        token = self._inbound("msg-accept-transform", "Sim")
        turn_key = wm._register_contact_turn(self.chat, self.session, "Sim")
        wm._calendar_turn_state[self.chat] = {
            "kind": "collecting",
            "at": time.time(),
            "expires_at": time.time() + 600,
            "inbound_token": token,
            "slots": [],
            "period": "any",
        }
        wm._HUMAN_DELIVER_SYNC = True

        with patch("whatsapp_manager._notify_owner_handoff") as notify, \
             patch("whatsapp_manager._assert_delivery_allowed"), \
             patch("whatsapp_manager._maybe_send_voice", return_value=None), \
             patch("whatsapp_manager._persist_turn_sent_to_disk"), \
             patch("whatsapp_manager._human_send", return_value="wamid-collect") as send:
            result = wm.transform_llm_output(
                "transform_llm_output",
                platform="whatsapp",
                session_id=self.session,
                assistant_response=(
                    "Vou encaminhar para o time. [[HANDOFF: lead topou call]]"
                ),
            )

        self.assertEqual(result, "\n")
        self.assertIn(turn_key, wm._turn_sent)
        notify.assert_not_called()
        visible = send.call_args.args[1]
        self.assertIn("Qual dia e período", visible)
        self.assertNotIn("HANDOFF", visible)

    def test_active_calendar_prompt_removes_inactive_rule(self):
        legacy_rules = (
            "### Agenda e call no estado atual\n\n"
            "Não existe integração de agenda ativa nesta operação. Nunca ofereça horários.\n\n"
            "### Informação interna — nunca revele\n\nSegredo."
        )
        with patch("whatsapp_manager.calendar_ready", return_value=True):
            context = wm._build_support_prompt("AYA", legacy_rules, "")["context"]

        self.assertIn("Agenda comercial da WhatsAYA: ATIVA", context)
        self.assertNotIn("Não existe integração de agenda ativa", context)
        self.assertIn(wm._CALENDAR_FIND_TOOL, context)

    def test_active_calendar_asks_for_missing_day_without_handoff(self):
        history = "AYA: Faz sentido eu te mostrar numa call. Topa marcar essa conversa?"
        with patch("whatsapp_manager.calendar_ready", return_value=True), \
             patch("whatsapp_manager._fetch_chat_history", return_value=history):
            visible = wm._enforce_aya_payment_output_gate(
                "Vou encaminhar para o time. [[HANDOFF: lead topou call]]",
                user_message="manhã",
                contact_info={"market_id": "BR", "language": "pt"},
                rules_content="",
                chat_id=self.chat,
            )

        self.assertIn("Qual dia", visible)
        self.assertNotIn("HANDOFF", visible)

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
