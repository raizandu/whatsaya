"""Testes unitários do motor Google Calendar, sem rede nem credencial real."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import sys
sys.path.append(str(Path(__file__).parent.parent))

import calendar_booking as cb

TZ = ZoneInfo("America/Sao_Paulo")


class _Request:
    def __init__(self, payload=None, error=None):
        self.payload = payload or {}
        self.error = error

    def execute(self):
        if self.error:
            raise self.error
        return self.payload


class _FreeBusy:
    def __init__(self, service):
        self.service = service

    def query(self, body):
        self.service.freebusy_bodies.append(body)
        return _Request({"calendars": {"primary": {"busy": list(self.service.busy)}}})


class _StatusError(RuntimeError):
    def __init__(self, status, message="google api error"):
        super().__init__(message)
        self.resp = SimpleNamespace(status=status)


class _Events:
    def __init__(self, service):
        self.service = service

    def insert(self, **kwargs):
        self.service.insert_calls.append(kwargs)
        if self.service.insert_error:
            return _Request(error=self.service.insert_error)
        payload = dict(kwargs["body"])
        payload["htmlLink"] = "https://calendar.google.test/event"
        return _Request(payload)

    def get(self, **kwargs):
        self.service.get_calls.append(kwargs)
        payload = self.service.existing_event
        if payload is None and getattr(getattr(self.service.insert_error, "resp", None), "status", None) == 409:
            payload = {
                "id": kwargs["eventId"],
                "summary": "Call WhatsAYA — Lead",
                "htmlLink": "https://calendar.google.test/existing",
            }
        if payload is None:
            return _Request(error=_StatusError(404, "not found"))
        result = dict(payload)
        result.setdefault("id", kwargs["eventId"])
        return _Request(result)


class FakeService:
    def __init__(self, busy=None, insert_error=None, existing_event=None):
        self.busy = busy or []
        self.insert_error = insert_error
        self.existing_event = existing_event
        self.freebusy_bodies = []
        self.insert_calls = []
        self.get_calls = []

    def freebusy(self):
        return _FreeBusy(self)

    def events(self):
        return _Events(self)


class CalendarBookingTests(unittest.TestCase):
    def test_calendar_ready_requires_refresh_token_and_calendar_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "token.json"
            with patch.dict("os.environ", {"WHATSAPP_CALENDAR_TOKEN_PATH": str(path)}):
                self.assertFalse(cb.calendar_ready())
                path.write_text(json.dumps({"refresh_token": "x", "scopes": [cb.CALENDAR_SCOPE]}))
                self.assertTrue(cb.calendar_ready())
                path.write_text(json.dumps({"refresh_token": "x", "scopes": ["gmail"]}))
                self.assertFalse(cb.calendar_ready())

    def test_find_slots_skips_busy_period_and_returns_three_real_slots(self):
        service = FakeService(busy=[{
            "start": "2026-08-31T08:00:00-03:00",
            "end": "2026-08-31T09:00:00-03:00",
        }])
        result = cb.find_available_slots(
            date_from="2026-08-31",
            date_to="2026-08-31",
            period="morning",
            now=datetime(2026, 8, 30, 10, 0, tzinfo=TZ),
            service=service,
        )
        self.assertEqual([slot["start"] for slot in result["slots"]], [
            "2026-08-31T09:00:00-03:00",
            "2026-08-31T09:30:00-03:00",
            "2026-08-31T10:00:00-03:00",
        ])
        self.assertEqual(result["timezone"], "America/Sao_Paulo")

    def test_find_slots_skips_weekend(self):
        service = FakeService()
        result = cb.find_available_slots(
            date_from="2026-08-29",
            date_to="2026-08-31",
            now=datetime(2026, 8, 28, 10, 0, tzinfo=TZ),
            service=service,
        )
        self.assertEqual(result["slots"][0]["start"], "2026-08-31T08:00:00-03:00")

    def test_find_slots_honors_exact_preferred_time_across_days(self):
        service = FakeService(busy=[{
            "start": "2026-08-31T14:00:00-03:00",
            "end": "2026-08-31T14:30:00-03:00",
        }])
        result = cb.find_available_slots(
            date_from="2026-08-29",
            date_to="2026-09-10",
            period="afternoon",
            preferred_time="14:00",
            now=datetime(2026, 8, 28, 16, 32, tzinfo=TZ),
            service=service,
        )

        self.assertEqual([slot["start"] for slot in result["slots"]], [
            "2026-09-01T14:00:00-03:00",
            "2026-09-02T14:00:00-03:00",
            "2026-09-03T14:00:00-03:00",
        ])

    def test_create_booking_rechecks_availability_and_sends_no_invite(self):
        service = FakeService()
        with patch("calendar_booking.datetime") as mocked_datetime:
            mocked_datetime.now.return_value = datetime(2026, 8, 28, 10, 0, tzinfo=TZ)
            mocked_datetime.fromisoformat.side_effect = datetime.fromisoformat
            result = cb.create_booking(
                chat_id="5562999999999@s.whatsapp.net",
                start="2026-08-31T14:00:00-03:00",
                end="2026-08-31T14:30:00-03:00",
                lead_name="Maria",
                purpose="Demonstração da AYA",
                service=service,
            )
        self.assertEqual(result["status"], "created")
        self.assertEqual(len(service.insert_calls), 1)
        call = service.insert_calls[0]
        self.assertEqual(call["sendUpdates"], "none")
        self.assertRegex(call["body"]["id"], r"^[0-9a-v]{5,1024}$")
        self.assertNotIn("attendees", call["body"])
        self.assertIn("Maria", call["body"]["summary"])
        self.assertNotIn("5562999999999", call["body"]["extendedProperties"]["private"]["whatsayaChat"])

    def test_create_booking_fails_closed_when_slot_is_busy(self):
        service = FakeService(busy=[{
            "start": "2026-08-31T14:00:00-03:00",
            "end": "2026-08-31T14:30:00-03:00",
        }])
        with patch("calendar_booking.datetime") as mocked_datetime:
            mocked_datetime.now.return_value = datetime(2026, 8, 28, 10, 0, tzinfo=TZ)
            mocked_datetime.fromisoformat.side_effect = datetime.fromisoformat
            with self.assertRaisesRegex(cb.CalendarBookingError, "ocupado"):
                cb.create_booking(
                    chat_id="5562999999999@s.whatsapp.net",
                    start="2026-08-31T14:00:00-03:00",
                    end="2026-08-31T14:30:00-03:00",
                    service=service,
                )
        self.assertFalse(service.insert_calls)

    def test_create_booking_is_idempotent_on_google_conflict(self):
        class ConflictError(RuntimeError):
            def __init__(self):
                super().__init__("duplicate")
                self.resp = SimpleNamespace(status=409)

        service = FakeService(insert_error=ConflictError())
        with patch("calendar_booking.datetime") as mocked_datetime:
            mocked_datetime.now.return_value = datetime(2026, 8, 28, 10, 0, tzinfo=TZ)
            mocked_datetime.fromisoformat.side_effect = datetime.fromisoformat
            result = cb.create_booking(
                chat_id="5562999999999@s.whatsapp.net",
                start="2026-08-31T14:00:00-03:00",
                end="2026-08-31T14:30:00-03:00",
                service=service,
            )
        self.assertEqual(result["status"], "already_exists")
        self.assertEqual(len(service.get_calls), 1)

    def test_create_booking_retry_recognizes_own_busy_event(self):
        service = FakeService(
            busy=[{
                "start": "2026-08-31T14:00:00-03:00",
                "end": "2026-08-31T14:30:00-03:00",
            }],
            existing_event={
                "summary": "Call WhatsAYA — Maria",
                "htmlLink": "https://calendar.google.test/existing",
            },
        )
        with patch("calendar_booking.datetime") as mocked_datetime:
            mocked_datetime.now.return_value = datetime(2026, 8, 28, 10, 0, tzinfo=TZ)
            mocked_datetime.fromisoformat.side_effect = datetime.fromisoformat
            result = cb.create_booking(
                chat_id="5562999999999@s.whatsapp.net",
                start="2026-08-31T14:00:00-03:00",
                end="2026-08-31T14:30:00-03:00",
                service=service,
            )
        self.assertEqual(result["status"], "already_exists")
        self.assertEqual(len(service.get_calls), 1)
        self.assertFalse(service.insert_calls)


if __name__ == "__main__":
    unittest.main()
