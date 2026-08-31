"""Integração segura das tools de Calendar com turnos WhatsApp."""
from __future__ import annotations

import json
import os
import sys
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
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
        wm._pending_inbound_queue.clear()
        wm._turn_key.clear()
        wm._turn_inbound.clear()
        wm._turn_sent.clear()
        wm._turn_inflight.clear()
        wm._turn_context_bindings.set(())
        wm._core_turn_bindings.clear()
        wm._sender_to_chat[self.session] = self.chat
        self.core_turn_id = ""
        self.runtime_turn_patcher = patch(
            "whatsapp_manager._runtime_turn_for_session",
            side_effect=self._runtime_turn,
        )
        self.runtime_turn_patcher.start()
        self.contact_access_patcher = patch(
            "whatsapp_manager._contact_has_explicit_ai_access",
            return_value=True,
        )
        self.contact_access_patcher.start()

    def tearDown(self):
        wm._calendar_turn_state.clear()
        wm._sender_to_chat.clear()
        wm._pending_inbound.clear()
        wm._pending_inbound_queue.clear()
        wm._turn_key.clear()
        wm._turn_inbound.clear()
        wm._turn_sent.clear()
        wm._turn_inflight.clear()
        wm._turn_context_bindings.set(())
        wm._core_turn_bindings.clear()
        wm._HUMAN_DELIVER_SYNC = False
        self.contact_access_patcher.stop()
        self.runtime_turn_patcher.stop()

    def _runtime_turn(self, session_id: str):
        if not self.core_turn_id:
            return None
        return SimpleNamespace(
            turn_id=self.core_turn_id,
            closed=False,
            lease=SimpleNamespace(
                session_id=session_id,
                platform="whatsapp",
            ),
        )

    def _inbound(self, message_id: str, text: str) -> tuple[str, float]:
        wm._track_inbound(self.chat, message_id, text)
        turn_id = wm._register_contact_turn(self.chat, self.session, text)
        self.core_turn_id = f"core:{message_id}"
        wm._bind_core_turn(self.session, self.core_turn_id, turn_id)
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
        self._inbound("msg-tool-policy", "Quero ver os horários disponíveis")
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
            "summary": "Reunião WhatsAYA — Lead",
            "start": first["start"],
            "end": first["end"],
            "timezone": "America/Sao_Paulo",
            "meet_link": "https://meet.google.com/test-link",
            "htmlLink": "https://calendar.google.test/private",
        }
        with patch("whatsapp_manager.create_booking", return_value=booked) as create:
            result = json.loads(wm._handle_calendar_book(first, session_id=self.session))

        self.assertEqual(result["status"], "created")
        self.assertEqual(wm._calendar_turn_state[self.chat]["kind"], "booked")
        self.assertEqual(wm._calendar_turn_state[self.chat]["inbound_token"], token)
        self.assertEqual(create.call_args.kwargs["purpose"], "Apresentação comercial da WhatsAYA")

    def test_meet_link_is_sent_only_after_confirmed_booking(self):
        first = self._slots()["slots"][0]
        offer_token = self._inbound("msg-offer-meet", "Quero segunda à tarde")
        wm._calendar_turn_state[self.chat] = {
            "kind": "offered",
            "action": "book",
            "at": time.time(),
            "expires_at": time.time() + 600,
            "inbound_token": offer_token,
            "slots": [first],
            "query": {"period": "afternoon", "max_slots": 1},
        }
        meet_link = "https://meet.google.com/abc-defg-hij"
        before_confirmation = wm._calendar_visible_reply(
            wm._calendar_turn_state[self.chat],
            "Quero segunda à tarde",
        )
        self.assertNotIn(meet_link, before_confirmation)

        self._inbound("msg-confirm-meet", "Sim, pode marcar")
        booked = {
            "status": "created",
            "event_id": "event-meet",
            "summary": "Reunião WhatsAYA — Lead",
            "start": first["start"],
            "end": first["end"],
            "timezone": "America/Sao_Paulo",
            "meet_link": meet_link,
        }
        with patch("whatsapp_manager.create_booking", return_value=booked) as create:
            result = json.loads(wm._handle_calendar_book(first, session_id=self.session))

        self.assertEqual(result["status"], "created")
        create.assert_called_once()
        confirmation = wm._calendar_visible_reply(
            wm._calendar_turn_state[self.chat],
            "Sim, pode marcar",
        )
        self.assertIn(meet_link, confirmation)
        self.assertRegex(confirmation.lower(), r"reuni[aã]o|liga[cç][aã]o")
        self.assertNotRegex(confirmation.lower(), r"\bcall\b")
        self.assertLess(confirmation.index(meet_link), confirmation.rfind("?"))
        self.assertTrue(confirmation.rstrip().endswith("?"), confirmation)

    def test_reschedule_requires_new_slot_confirmation_and_reuses_persisted_booking(self):
        old = {
            "status": "created",
            "event_id": "event-old",
            "start": "2026-08-31T14:00:00-03:00",
            "end": "2026-08-31T14:30:00-03:00",
            "timezone": "America/Sao_Paulo",
            "meet_link": "https://meet.google.com/abc-defg-hij",
        }
        new = {
            "start": "2026-09-01T15:00:00-03:00",
            "end": "2026-09-01T15:30:00-03:00",
        }
        offer_token = self._inbound("msg-reschedule-offer", "Quero remarcar")
        wm._calendar_turn_state[self.chat] = {
            "kind": "offered",
            "action": "reschedule",
            "at": time.time(),
            "expires_at": time.time() + 600,
            "inbound_token": offer_token,
            "slots": [new],
            "booking": old,
            "result": old,
            "query": {"period": "afternoon", "max_slots": 1},
        }

        with patch("whatsapp_manager.reschedule_booking") as reschedule:
            rejected = json.loads(wm._handle_calendar_book(new, session_id=self.session))
        self.assertEqual(rejected["status"], "error")
        reschedule.assert_not_called()

        self._inbound("msg-reschedule-confirm", "Sim, esse horário funciona")
        moved = dict(old, status="rescheduled", start=new["start"], end=new["end"])
        with patch("whatsapp_manager.reschedule_booking", return_value=moved) as reschedule, \
             patch("whatsapp_manager.create_booking") as create:
            result = json.loads(wm._handle_calendar_book(new, session_id=self.session))

        self.assertEqual(result["status"], "rescheduled")
        reschedule.assert_called_once()
        kwargs = reschedule.call_args.kwargs
        self.assertEqual(kwargs["chat_id"], self.chat)
        self.assertEqual(kwargs["start"], new["start"])
        self.assertEqual(kwargs["end"], new["end"])
        create.assert_not_called()
        self.assertEqual(wm._calendar_turn_state[self.chat]["kind"], "booked")
        self.assertEqual(
            wm._calendar_turn_state[self.chat]["result"]["event_id"],
            old["event_id"],
        )

    def test_lead_facing_calendar_language_uses_reuniao_or_ligacao_not_call(self):
        states = [
            {
                "kind": "collecting",
                "period": "any",
                "slots": [],
            },
            {
                "kind": "offered",
                "slots": [self._slots()["slots"][0]],
            },
            {
                "kind": "booked",
                "result": {
                    "start": "2026-08-31T14:00:00-03:00",
                    "end": "2026-08-31T14:30:00-03:00",
                    "meet_link": "https://meet.google.com/abc-defg-hij",
                },
            },
        ]
        for state in states:
            with self.subTest(kind=state["kind"]):
                reply = wm._calendar_visible_reply(state, "Sim")
                self.assertNotRegex(reply.lower(), r"\bcall\b")
        booked_reply = wm._calendar_visible_reply(states[-1], "Sim")
        self.assertRegex(booked_reply.lower(), r"reuni[aã]o|liga[cç][aã]o")

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
            max_slots=1,
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

    def test_call_acceptance_suggests_nearest_real_slot_before_handoff(self):
        token = self._inbound("msg-accept", "Sim")
        history = self._gustavo_call_history().replace("Lead:", "Gustavo:")
        nearest = dict(self._slots(), slots=self._slots()["slots"][:1])
        now = datetime(2026, 8, 28, 16, 32, tzinfo=ZoneInfo("America/Sao_Paulo"))

        with patch("whatsapp_manager.calendar_ready", return_value=True), \
             patch("whatsapp_manager._contact_record_for_chat", return_value={"name": "Gustavo"}), \
             patch("whatsapp_manager.find_available_slots", return_value=nearest) as find:
            handled = wm._orchestrate_calendar_turn(
                chat_id=self.chat,
                session_id=self.session,
                user_message="Sim",
                history=history,
                now=now,
            )

        self.assertTrue(handled)
        find.assert_called_once_with(
            date_from="2026-08-28",
            date_to="2026-09-10",
            period="any",
            preferred_time=None,
            max_slots=1,
        )
        state = wm._calendar_turn_state[self.chat]
        self.assertEqual(state["kind"], "offered")
        self.assertEqual(state["inbound_token"], token)
        visible = wm._calendar_visible_reply(state, "Sim")
        self.assertIn("horário livre mais próximo", visible)
        self.assertIn("às 14:00", visible)
        self.assertIn("Funciona para você?", visible)
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
            max_slots=1,
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
            max_slots=1,
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
            max_slots=1,
        )

    def test_rejected_suggestion_asks_when_would_work_better(self):
        old_token = self._inbound("msg-suggestion", "Sim")
        wm._calendar_turn_state[self.chat] = {
            "kind": "offered",
            "at": time.time(),
            "expires_at": time.time() + 600,
            "inbound_token": old_token,
            "slots": self._slots()["slots"][:1],
            "query": {"period": "any", "max_slots": 1},
        }
        token = self._inbound("msg-decline", "Esse horário não dá")

        with patch("whatsapp_manager.calendar_ready", return_value=True):
            handled = wm._orchestrate_calendar_turn(
                chat_id=self.chat,
                session_id=self.session,
                user_message="Esse horário não dá",
                history=self._gustavo_call_history(),
            )

        self.assertTrue(handled)
        state = wm._calendar_turn_state[self.chat]
        self.assertEqual(state["kind"], "collecting")
        self.assertEqual(state["reply_kind"], "ask_preference_after_decline")
        self.assertEqual(state["inbound_token"], token)
        visible = wm._calendar_visible_reply(state, "Esse horário não dá")
        self.assertIn("Quando ficaria melhor para você?", visible)
        self.assertNotIn("HANDOFF", visible)

    def test_simple_yes_books_the_single_nearest_suggestion(self):
        first = self._slots()["slots"][0]
        old_token = self._inbound("msg-suggestion", "Sim")
        wm._calendar_turn_state[self.chat] = {
            "kind": "offered",
            "at": time.time(),
            "expires_at": time.time() + 600,
            "inbound_token": old_token,
            "slots": [first],
            "query": {"period": "any", "max_slots": 1},
        }
        self._inbound("msg-confirm-nearest", "Sim")
        booked = {
            "status": "created",
            "event_id": "event-nearest",
            "summary": "Reunião WhatsAYA — Gustavo",
            "start": first["start"],
            "end": first["end"],
            "timezone": "America/Sao_Paulo",
            "meet_link": "https://meet.google.com/test-link",
        }

        with patch("whatsapp_manager.calendar_ready", return_value=True), \
             patch("whatsapp_manager.create_booking", return_value=booked) as create:
            handled = wm._orchestrate_calendar_turn(
                chat_id=self.chat,
                session_id=self.session,
                user_message="Sim",
                history=self._gustavo_call_history(),
            )

        self.assertTrue(handled)
        create.assert_called_once()
        self.assertEqual(wm._calendar_turn_state[self.chat]["kind"], "booked")

    def test_natural_confirmation_with_filler_books_single_suggestion(self):
        first = self._slots()["slots"][0]
        old_token = self._inbound("msg-tony-offer", "Funciona para você?")
        wm._calendar_turn_state[self.chat] = {
            "kind": "offered",
            "action": "book",
            "at": time.time(),
            "expires_at": time.time() + 600,
            "inbound_token": old_token,
            "slots": [first],
            "query": {"period": "any", "max_slots": 1},
        }
        self._inbound("msg-tony-confirm", "hum pode sim")
        booked = {
            "status": "created",
            "event_id": "event-tony",
            "summary": "Reunião WhatsAYA — Tony",
            "start": first["start"],
            "end": first["end"],
            "timezone": "America/Sao_Paulo",
            "meet_link": "https://meet.google.com/tony-test-link",
        }

        with patch("whatsapp_manager.calendar_ready", return_value=True), \
             patch("whatsapp_manager.create_booking", return_value=booked) as create:
            handled = wm._orchestrate_calendar_turn(
                chat_id=self.chat,
                session_id=self.session,
                user_message="hum pode sim",
                history=self._gustavo_call_history(),
            )

        self.assertTrue(handled)
        create.assert_called_once()
        self.assertEqual(wm._calendar_turn_state[self.chat]["kind"], "booked")

    def test_funciona_books_single_suggestion_without_asking_again(self):
        first = self._slots()["slots"][0]
        old_token = self._inbound("msg-funciona-offer", "Funciona para você?")
        wm._calendar_turn_state[self.chat] = {
            "kind": "offered",
            "action": "reschedule",
            "at": time.time(),
            "expires_at": time.time() + 600,
            "inbound_token": old_token,
            "slots": [first],
            "query": {"period": "afternoon", "max_slots": 1},
        }
        self._inbound("msg-funciona-confirm", "funciona")
        booked = {
            "status": "rescheduled",
            "event_id": "event-tony-funciona",
            "summary": "Reunião WhatsAYA — Tony",
            "start": first["start"],
            "end": first["end"],
            "timezone": "America/Sao_Paulo",
            "meet_link": "https://meet.google.com/tony-funciona-link",
        }

        with patch("whatsapp_manager.calendar_ready", return_value=True), \
             patch("whatsapp_manager.reschedule_booking", return_value=booked) as reschedule:
            handled = wm._orchestrate_calendar_turn(
                chat_id=self.chat,
                session_id=self.session,
                user_message="funciona",
                history=self._gustavo_call_history(),
            )

        self.assertTrue(handled)
        reschedule.assert_called_once()
        state = wm._calendar_turn_state[self.chat]
        self.assertEqual(state["kind"], "booked")
        self.assertNotEqual(state.get("reply_kind"), "choice_required")

    def test_reconciled_remote_reschedule_is_treated_as_booked(self):
        first = self._slots()["slots"][0]
        old_token = self._inbound(
            "msg-reconciled-reschedule-offer",
            "Pode ser esse horário?",
        )
        wm._calendar_turn_state[self.chat] = {
            "kind": "offered",
            "action": "reschedule",
            "at": time.time(),
            "expires_at": time.time() + 600,
            "inbound_token": old_token,
            "slots": [first],
            "query": {"period": "afternoon", "max_slots": 1},
        }
        self._inbound("msg-reconciled-reschedule-confirm", "sim, pode remarcar")
        reconciled = {
            "status": "already_rescheduled",
            "event_id": "event-reconciled-reschedule",
            "summary": "Reunião WhatsAYA — Tony",
            "start": first["start"],
            "end": first["end"],
            "timezone": "America/Sao_Paulo",
            "meet_link": "https://meet.google.com/reconciled-link",
        }

        with patch("whatsapp_manager.calendar_ready", return_value=True), \
             patch(
                 "whatsapp_manager.reschedule_booking",
                 return_value=reconciled,
             ) as reschedule:
            handled = wm._orchestrate_calendar_turn(
                chat_id=self.chat,
                session_id=self.session,
                user_message="sim, pode remarcar",
                history=self._gustavo_call_history(),
            )

        self.assertTrue(handled)
        reschedule.assert_called_once()
        state = wm._calendar_turn_state[self.chat]
        self.assertEqual(state["kind"], "booked")
        self.assertEqual(state["result"]["status"], "already_rescheduled")

    def test_restart_recovers_lead_reschedule_intent_across_offer_and_confirmation(self):
        old = {
            "event_id": "event-before-restart",
            "start": "2026-08-31T14:00:00-03:00",
            "end": "2026-08-31T14:30:00-03:00",
            "timezone": "America/Sao_Paulo",
            "meet_link": "https://meet.google.com/before-restart",
        }
        new = {
            "start": "2026-09-01T15:00:00-03:00",
            "end": "2026-09-01T15:30:00-03:00",
        }
        available = dict(self._slots(), slots=[new])
        history = (
            "Lead: Quero remarcar minha reunião\n"
            "AYA: Claro. Qual novo dia e horário ficam melhores para você?\n"
        )
        now = datetime(2026, 8, 28, 16, 32, tzinfo=ZoneInfo("America/Sao_Paulo"))
        wm._calendar_turn_state.clear()  # simula um processo novo, sem estado efêmero

        with patch("whatsapp_manager.calendar_ready", return_value=True), \
             patch("whatsapp_manager.get_booking", return_value=old), \
             patch("whatsapp_manager.find_available_slots", return_value=available) as find, \
             patch("whatsapp_manager.reschedule_booking") as reschedule, \
             patch("whatsapp_manager.create_booking") as create:
            self._inbound("msg-after-restart-preference", "Dia 01/09 às 15h")
            offered = wm._orchestrate_calendar_turn(
                chat_id=self.chat,
                session_id=self.session,
                user_message="Dia 01/09 às 15h",
                history=history,
                now=now,
            )

            self.assertTrue(offered)
            self.assertEqual(wm._calendar_turn_state[self.chat]["action"], "reschedule")
            find.assert_called_once()
            reschedule.assert_not_called()
            create.assert_not_called()

            self._inbound("msg-after-restart-confirm", "Sim, funciona")
            moved = dict(old, status="rescheduled", start=new["start"], end=new["end"])
            reschedule.return_value = moved
            confirmed = wm._orchestrate_calendar_turn(
                chat_id=self.chat,
                session_id=self.session,
                user_message="Sim, funciona",
                history=(
                    history
                    + "Lead: Dia 01/09 às 15h\n"
                    + "AYA: Boa! O horário livre mais próximo é terça, 01/09, às 15:00. "
                    + "Funciona para você?\n"
                ),
                now=now,
            )

        self.assertTrue(confirmed)
        reschedule.assert_called_once()
        create.assert_not_called()
        self.assertEqual(wm._calendar_turn_state[self.chat]["kind"], "booked")
        self.assertEqual(wm._calendar_turn_state[self.chat]["action"], "reschedule")

    def test_pending_reschedule_history_ends_on_completion_or_lead_cancellation(self):
        pending = (
            "Lead: Quero remarcar minha reunião\n"
            "AYA: Claro. Qual novo dia e horário ficam melhores para você?\n"
        )
        self.assertTrue(wm._history_has_pending_reschedule(pending))
        self.assertFalse(wm._history_has_pending_reschedule(
            pending
            + "AYA: Fechado, sua reunião ficou remarcada para terça às 15:00.\n"
        ))
        self.assertFalse(wm._history_has_pending_reschedule(
            pending
            + "Lead: Deixa como está, não quero mais remarcar.\n"
        ))

    def test_current_lead_cancellation_clears_reschedule_state_without_mutation(self):
        old_token = self._inbound("msg-reschedule-before-cancel", "Quero remarcar")
        wm._calendar_turn_state[self.chat] = {
            "kind": "collecting",
            "action": "reschedule",
            "at": time.time(),
            "expires_at": time.time() + 600,
            "inbound_token": old_token,
            "slots": [],
        }
        self._inbound("msg-reschedule-cancel", "Deixa como está, não quero mais remarcar")
        current_booking = {
            "event_id": "event-kept-after-cancel",
            "start": "2026-08-31T14:00:00-03:00",
            "end": "2026-08-31T14:30:00-03:00",
            "meet_link": "https://meet.google.com/kept-after-cancel",
        }

        with patch("whatsapp_manager.calendar_ready", return_value=True), \
             patch("whatsapp_manager.get_booking", return_value=current_booking), \
             patch("whatsapp_manager.find_available_slots") as find, \
             patch("whatsapp_manager.reschedule_booking") as reschedule, \
             patch("whatsapp_manager.create_booking") as create:
            handled = wm._orchestrate_calendar_turn(
                chat_id=self.chat,
                session_id=self.session,
                user_message="Deixa como está, não quero mais remarcar",
                history="Lead: Quero remarcar minha reunião\n",
            )

        self.assertFalse(handled)
        self.assertNotIn(self.chat, wm._calendar_turn_state)
        find.assert_not_called()
        reschedule.assert_not_called()
        create.assert_not_called()

    def test_confirmation_language_accepts_natural_yes_but_preserves_negatives(self):
        for message in (
            "funciona",
            "pra mim funciona",
            "isso funciona pra mim",
            "dá certo",
            "vai dar certo",
            "serve pra mim",
            "combinado",
        ):
            with self.subTest(message=message):
                self.assertTrue(wm._calendar_confirmation_present(message))

        for message in (
            "não funciona",
            "não dá certo",
            "não vai dar",
            "talvez funcione",
            "como funciona?",
        ):
            with self.subTest(message=message):
                self.assertFalse(wm._calendar_confirmation_present(message))

    def test_explicit_new_time_wins_over_decline_in_open_offer(self):
        first = self._slots()["slots"][0]
        old_token = self._inbound("msg-reschedule-noon", "Pode ser às 12:00?")
        wm._calendar_turn_state[self.chat] = {
            "kind": "offered",
            "action": "reschedule",
            "at": time.time(),
            "expires_at": time.time() + 600,
            "inbound_token": old_token,
            "slots": [first],
            "query": {"period": "afternoon", "max_slots": 1},
        }
        self._inbound("msg-reschedule-fifteen", "não pode ser tipo as 15:00?")
        at_fifteen = dict(self._slots(), slots=[{
            "start": "2026-08-31T15:00:00-03:00",
            "end": "2026-08-31T15:30:00-03:00",
        }])
        current_booking = {
            "event_id": "event-tony-existing",
            "start": "2026-08-31T12:00:00-03:00",
            "end": "2026-08-31T12:30:00-03:00",
            "meet_link": "https://meet.google.com/tony-existing-link",
        }
        now = datetime(2026, 8, 28, 16, 32, tzinfo=ZoneInfo("America/Sao_Paulo"))

        with patch("whatsapp_manager.calendar_ready", return_value=True), \
             patch("whatsapp_manager.get_booking", return_value=current_booking), \
             patch("whatsapp_manager.find_available_slots", return_value=at_fifteen) as find:
            handled = wm._orchestrate_calendar_turn(
                chat_id=self.chat,
                session_id=self.session,
                user_message="não pode ser tipo as 15:00?",
                history=self._gustavo_call_history(),
                now=now,
            )

        self.assertTrue(handled)
        find.assert_called_once_with(
            date_from="2026-08-28",
            date_to="2026-09-10",
            period="afternoon",
            preferred_time="15:00",
            max_slots=1,
        )
        state = wm._calendar_turn_state[self.chat]
        self.assertEqual(state["kind"], "offered")
        self.assertEqual(state["action"], "reschedule")
        self.assertEqual(state["slots"][0]["start"], "2026-08-31T15:00:00-03:00")

    def test_unrecognized_offer_reply_stays_in_calendar_flow(self):
        first = self._slots()["slots"][0]
        old_token = self._inbound("msg-unclear-offer", "Funciona para você?")
        wm._calendar_turn_state[self.chat] = {
            "kind": "offered",
            "action": "book",
            "at": time.time(),
            "expires_at": time.time() + 600,
            "inbound_token": old_token,
            "slots": [first],
            "query": {"period": "any", "max_slots": 1},
        }
        token = self._inbound("msg-unclear-reply", "vou conferir aqui")

        with patch("whatsapp_manager.calendar_ready", return_value=True), \
             patch("whatsapp_manager.create_booking") as create:
            handled = wm._orchestrate_calendar_turn(
                chat_id=self.chat,
                session_id=self.session,
                user_message="vou conferir aqui",
                history=self._gustavo_call_history(),
            )

        self.assertTrue(handled)
        create.assert_not_called()
        state = wm._calendar_turn_state[self.chat]
        self.assertEqual(state["kind"], "offered")
        self.assertEqual(state["reply_kind"], "choice_required")
        self.assertEqual(state["inbound_token"], token)
        visible = wm._calendar_visible_reply(state, "vou conferir aqui")
        self.assertIn("funciona para você?", visible)

    def test_unverified_model_booking_claim_is_replaced(self):
        fake_confirmation = (
            "Perfeito, Tony. Fica solicitada a reunião para segunda às 08:00. "
            "A equipe vai te enviar a confirmação final com o link."
        )

        with patch("whatsapp_manager.calendar_ready", return_value=True), \
             patch("whatsapp_manager.get_booking", return_value=None):
            guarded = wm._calendar_guard_completion_claim(
                fake_confirmation,
                user_message="hum pode sim",
                chat_id=self.chat,
            )

        self.assertIn("Não consegui concluir a confirmação", guarded)
        self.assertNotIn("equipe", guarded.lower())
        self.assertNotIn("link", guarded.lower())

    def test_transform_never_delivers_unverified_booking_claim(self):
        self._inbound("msg-fake-confirmation", "hum pode sim")
        wm._register_contact_turn(self.chat, self.session, "hum pode sim")
        wm._HUMAN_DELIVER_SYNC = True
        fake_confirmation = (
            "Perfeito. Fica solicitada a reunião para segunda às 08:00. "
            "A equipe vai enviar o link depois."
        )

        with patch("whatsapp_manager.calendar_ready", return_value=True), \
             patch("whatsapp_manager.get_booking", return_value=None), \
             patch("whatsapp_manager._assert_delivery_allowed"), \
             patch("whatsapp_manager._maybe_send_voice", return_value=None), \
             patch("whatsapp_manager._persist_turn_sent_to_disk"), \
             patch("whatsapp_manager._human_send", return_value="wamid-safe") as send:
            result = wm.transform_llm_output(
                "transform_llm_output",
                platform="whatsapp",
                session_id=self.session,
                assistant_response=fake_confirmation,
            )

        self.assertEqual(result, "\n")
        visible = send.call_args.args[1]
        self.assertIn("Não consegui concluir a confirmação", visible)
        self.assertNotIn("equipe", visible.lower())
        self.assertNotIn("link", visible.lower())

    def test_model_booking_claim_uses_only_persisted_meet(self):
        booking = {
            "event_id": "event-real",
            "start": "2026-08-31T08:00:00-03:00",
            "end": "2026-08-31T08:30:00-03:00",
            "timezone": "America/Sao_Paulo",
            "meet_link": "https://meet.google.com/real-safe-link",
            "status": "active",
        }

        with patch("whatsapp_manager.calendar_ready", return_value=True), \
             patch("whatsapp_manager.get_booking", return_value=booking):
            guarded = wm._calendar_guard_completion_claim(
                "Sua reunião está confirmada.",
                user_message="Está confirmado?",
                chat_id=self.chat,
            )

        self.assertIn(booking["meet_link"], guarded)
        self.assertNotRegex(guarded.lower(), r"\bcall\b")

    def test_booking_failure_stays_retryable_without_fake_confirmation(self):
        first = self._slots()["slots"][0]
        old_token = self._inbound("msg-offered-retry", "Quero esse horário")
        wm._calendar_turn_state[self.chat] = {
            "kind": "offered",
            "action": "book",
            "at": time.time(),
            "expires_at": time.time() + 600,
            "inbound_token": old_token,
            "slots": [first],
        }
        token = self._inbound("msg-confirm-retry", "Sim")

        with patch("whatsapp_manager.calendar_ready", return_value=True), \
             patch("whatsapp_manager.create_booking", return_value={
                 "status": "created",
                 "event_id": "event-without-meet",
                 "start": first["start"],
                 "end": first["end"],
                 "meet_link": "",
             }):
            handled = wm._orchestrate_calendar_turn(
                chat_id=self.chat,
                session_id=self.session,
                user_message="Sim",
                history=self._gustavo_call_history(),
            )

        state = wm._calendar_turn_state[self.chat]
        self.assertTrue(handled)
        self.assertEqual(state["kind"], "offered")
        self.assertEqual(state["reply_kind"], "book_retry")
        self.assertEqual(state["inbound_token"], token)
        visible = wm._calendar_visible_reply(state, "Sim")
        self.assertIn("mesmo horário", visible)
        self.assertNotRegex(visible.lower(), r"\bcall\b")

    def test_external_meeting_terminology_never_exposes_call(self):
        self.assertEqual(
            wm._externalize_meeting_term("Topa uma call rápida?", "Quero entender"),
            "Topa uma reunião rápida?",
        )
        self.assertEqual(
            wm._externalize_meeting_term("Can we book a call?", "How does it work?"),
            "Can we book a meeting?",
        )

    def test_active_calendar_prompt_says_to_offer_the_nearest_slot(self):
        block = wm._calendar_prompt_block(True)
        self.assertIn("horário livre mais próximo", block)
        self.assertIn("Se o lead recusar", block)

    def test_period_after_rejection_is_validated_against_nearest_real_slot(self):
        old_token = self._inbound("msg-decline", "Esse horário não dá")
        wm._calendar_turn_state[self.chat] = {
            "kind": "collecting",
            "reply_kind": "ask_preference_after_decline",
            "at": time.time(),
            "expires_at": time.time() + 600,
            "inbound_token": old_token,
            "slots": [],
            "period": "any",
        }
        self._inbound("msg-new-period", "De tarde")
        nearest = dict(self._slots(), slots=self._slots()["slots"][:1])
        now = datetime(2026, 8, 28, 16, 32, tzinfo=ZoneInfo("America/Sao_Paulo"))

        with patch("whatsapp_manager.calendar_ready", return_value=True), \
             patch("whatsapp_manager.find_available_slots", return_value=nearest) as find:
            handled = wm._orchestrate_calendar_turn(
                chat_id=self.chat,
                session_id=self.session,
                user_message="De tarde",
                history=self._gustavo_call_history(),
                now=now,
            )

        self.assertTrue(handled)
        find.assert_called_once_with(
            date_from="2026-08-28",
            date_to="2026-09-10",
            period="afternoon",
            preferred_time=None,
            max_slots=1,
        )
        visible = wm._calendar_visible_reply(wm._calendar_turn_state[self.chat], "De tarde")
        self.assertIn("horário livre mais próximo", visible)

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
