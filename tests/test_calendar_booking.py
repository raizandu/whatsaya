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
        if self.service.conference_data is not None:
            payload["conferenceData"] = self.service.conference_data
        return _Request(payload)

    def patch(self, **kwargs):
        self.service.patch_calls.append(kwargs)
        payload = dict(self.service.existing_event or {})
        payload.update(kwargs.get("body") or {})
        payload["id"] = kwargs["eventId"]
        payload.setdefault("htmlLink", "https://calendar.google.test/event")
        if self.service.conference_data is not None:
            payload["conferenceData"] = self.service.conference_data
        self.service.existing_event = payload
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
    def __init__(self, busy=None, insert_error=None, existing_event=None, conference_data=None):
        self.busy = busy or []
        self.insert_error = insert_error
        self.existing_event = existing_event
        self.conference_data = conference_data or {
            "entryPoints": [{
                "entryPointType": "video",
                "uri": "https://meet.google.com/test-link",
            }],
        }
        self.freebusy_bodies = []
        self.insert_calls = []
        self.patch_calls = []
        self.get_calls = []

    def freebusy(self):
        return _FreeBusy(self)

    def events(self):
        return _Events(self)


class CalendarBookingTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._db_path = Path(self._tmpdir.name) / "calendar-bookings.db"

    def tearDown(self):
        self._tmpdir.cleanup()

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
                db_path=self._db_path,
            )
        self.assertEqual(result["status"], "created")
        self.assertEqual(len(service.insert_calls), 1)
        call = service.insert_calls[0]
        self.assertEqual(call["sendUpdates"], "none")
        self.assertRegex(call["body"]["id"], r"^[0-9a-v]{5,1024}$")
        self.assertNotIn("attendees", call["body"])
        self.assertIn("Maria", call["body"]["summary"])
        self.assertNotIn("5562999999999", call["body"]["extendedProperties"]["private"]["whatsayaChat"])

    def test_create_booking_requests_google_meet_returns_safe_link_and_persists_date(self):
        chat_id = "5562999999999@s.whatsapp.net"
        meet_link = "https://meet.google.com/abc-defg-hij"
        raw_meet_link = f"{meet_link}?authuser=0#details"
        service = FakeService(conference_data={
            "entryPoints": [{
                "entryPointType": "video",
                "uri": raw_meet_link,
                "label": "meet.google.com/abc-defg-hij",
            }],
        })
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "calendar-bookings.db"
            with patch("calendar_booking.datetime") as mocked_datetime:
                mocked_datetime.now.return_value = datetime(2026, 8, 28, 10, 0, tzinfo=TZ)
                mocked_datetime.fromisoformat.side_effect = datetime.fromisoformat
                result = cb.create_booking(
                    chat_id=chat_id,
                    start="2026-08-31T14:00:00-03:00",
                    end="2026-08-31T14:30:00-03:00",
                    lead_name="Maria",
                    purpose="Demonstração da AYA",
                    service=service,
                    db_path=str(db_path),
                )

            call = service.insert_calls[0]
            self.assertEqual(call.get("conferenceDataVersion"), 1)
            request = call["body"]["conferenceData"]["createRequest"]
            self.assertTrue(request["requestId"])
            self.assertEqual(request["conferenceSolutionKey"]["type"], "hangoutsMeet")
            self.assertEqual(result["meet_link"], meet_link)
            self.assertNotIn("conferenceData", result)

            stored = cb.get_booking(chat_id, db_path=str(db_path))
            self.assertEqual(stored["start"], result["start"])
            self.assertEqual(stored["end"], result["end"])
            self.assertEqual(stored["meet_link"], meet_link)

    def test_pending_google_meet_is_polled_before_confirmation(self):
        meet_link = "https://meet.google.com/abc-defg-hij"
        pending = {
            "id": "event-pending",
            "conferenceData": {
                "createRequest": {"status": {"statusCode": "pending"}},
            },
        }
        service = FakeService(
            existing_event={
                "id": "event-pending",
                "conferenceData": {
                    "entryPoints": [{"entryPointType": "video", "uri": meet_link}],
                    "createRequest": {"status": {"statusCode": "success"}},
                },
            },
        )

        with patch("calendar_booking.time_module.sleep") as sleep:
            event, returned_link = cb._ensure_event_meet(service, pending, "event-pending")

        self.assertEqual(returned_link, meet_link)
        self.assertEqual(event["id"], "event-pending")
        self.assertEqual(len(service.get_calls), 1)
        self.assertFalse(service.patch_calls)
        sleep.assert_called_once()

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
                    db_path=self._db_path,
                )
        self.assertFalse(service.insert_calls)

    def test_reschedule_patches_existing_event_and_updates_persisted_booking(self):
        chat_id = "5562999999999@s.whatsapp.net"
        old_start = "2026-08-31T14:00:00-03:00"
        old_end = "2026-08-31T14:30:00-03:00"
        new_start = "2026-09-01T15:00:00-03:00"
        new_end = "2026-09-01T15:30:00-03:00"
        meet_link = "https://meet.google.com/abc-defg-hij"
        service = FakeService(conference_data={
            "entryPoints": [{"entryPointType": "video", "uri": meet_link}],
        })

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "calendar-bookings.db"
            with patch("calendar_booking.datetime") as mocked_datetime:
                mocked_datetime.now.return_value = datetime(2026, 8, 28, 10, 0, tzinfo=TZ)
                mocked_datetime.fromisoformat.side_effect = datetime.fromisoformat
                original = cb.create_booking(
                    chat_id=chat_id,
                    start=old_start,
                    end=old_end,
                    lead_name="Maria",
                    service=service,
                    db_path=str(db_path),
                )

            service.insert_calls.clear()
            service.patch_calls.clear()
            with patch("calendar_booking.datetime") as mocked_datetime:
                mocked_datetime.now.return_value = datetime(2026, 8, 28, 10, 0, tzinfo=TZ)
                mocked_datetime.fromisoformat.side_effect = datetime.fromisoformat
                moved = cb.reschedule_booking(
                    chat_id=chat_id,
                    start=new_start,
                    end=new_end,
                    lead_name="Maria",
                    service=service,
                    db_path=str(db_path),
                )

            self.assertEqual(moved["status"], "rescheduled")
            self.assertEqual(moved["event_id"], original["event_id"])
            self.assertEqual(moved["meet_link"], meet_link)
            self.assertEqual(len(service.patch_calls), 1)
            self.assertFalse(service.insert_calls)
            self.assertEqual(service.patch_calls[0]["eventId"], original["event_id"])

            stored = cb.get_booking(chat_id, db_path=str(db_path))
            self.assertEqual(stored["event_id"], original["event_id"])
            self.assertEqual(stored["start"], moved["start"])
            self.assertEqual(stored["end"], moved["end"])
            self.assertEqual(stored["meet_link"], meet_link)

            service.patch_calls.clear()
            service.insert_calls.clear()
            with patch("calendar_booking.datetime") as mocked_datetime:
                mocked_datetime.now.return_value = datetime(2026, 8, 28, 10, 0, tzinfo=TZ)
                mocked_datetime.fromisoformat.side_effect = datetime.fromisoformat
                repeated = cb.reschedule_booking(
                    chat_id=chat_id,
                    start=new_start,
                    end=new_end,
                    lead_name="Maria",
                    service=service,
                    db_path=str(db_path),
                )

            self.assertEqual(repeated["status"], "already_exists")
            self.assertEqual(repeated["event_id"], original["event_id"])
            self.assertFalse(service.patch_calls)
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
                db_path=self._db_path,
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
                db_path=self._db_path,
            )
        self.assertEqual(result["status"], "already_exists")
        self.assertEqual(len(service.get_calls), 1)
        self.assertFalse(service.insert_calls)


if __name__ == "__main__":
    unittest.main()
