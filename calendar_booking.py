"""Agenda comercial da WhatsAYA via Google Calendar.

Módulo sem dependência do gateway: valida janelas de negócio, consulta free/busy e
cria eventos idempotentes. O chamador continua responsável por vincular a reunião
ao mesmo chat/turno e exigir confirmação explícita do lead.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sqlite3
import threading
import time as time_module
import urllib.parse
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

BUSINESS_TZ_NAME = "America/Sao_Paulo"
BUSINESS_OPEN = time(8, 0)
BUSINESS_CLOSE = time(18, 0)
DEFAULT_DURATION_MINUTES = 30
MAX_SEARCH_DAYS = 14
MAX_SLOTS = 3
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar"
CALENDAR_EVENTS_SCOPE = "https://www.googleapis.com/auth/calendar.events"
_TOKEN_DEFAULT = "/opt/data/.hermes/google_token.json"
_BOOKINGS_DB_DEFAULT = "/opt/data/.hermes/calendar_bookings.db"
_API_LOCK = threading.RLock()
_BOOKING_DB_LOCK = threading.RLock()


class CalendarBookingError(RuntimeError):
    """Erro seguro e apresentável ao modelo, sem vazar credenciais."""


def business_timezone() -> ZoneInfo:
    name = os.getenv("WHATSAPP_CALENDAR_TZ", BUSINESS_TZ_NAME).strip() or BUSINESS_TZ_NAME
    try:
        return ZoneInfo(name)
    except Exception as exc:
        raise CalendarBookingError(f"Fuso de agenda inválido: {name}") from exc


def token_path() -> Path:
    return Path(os.getenv("WHATSAPP_CALENDAR_TOKEN_PATH", _TOKEN_DEFAULT)).expanduser()


def calendar_id() -> str:
    return os.getenv("WHATSAPP_CALENDAR_ID", "primary").strip() or "primary"


def bookings_db_path(override: str | Path | None = None) -> Path:
    value = override or os.getenv("WHATSAPP_CALENDAR_BOOKINGS_DB", _BOOKINGS_DB_DEFAULT)
    return Path(value).expanduser()


def _booking_chat_key(chat_id: str) -> str:
    digits = "".join(ch for ch in str(chat_id).split("@", 1)[0].split(":", 1)[0] if ch.isdigit())
    material = digits or str(chat_id).strip()
    if not material:
        raise CalendarBookingError("Chat da reserva não foi identificado.")
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def _ensure_booking_store(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(sqlite3.connect(path, timeout=10)) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS current_bookings (
                chat_key TEXT PRIMARY KEY,
                event_id TEXT NOT NULL,
                start TEXT NOT NULL,
                end TEXT NOT NULL,
                timezone TEXT NOT NULL,
                meet_link TEXT NOT NULL,
                html_link TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        conn.commit()
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _persist_booking(
    *,
    chat_id: str,
    result: dict[str, Any],
    db_path: str | Path | None = None,
) -> None:
    path = bookings_db_path(db_path)
    now = time_module.time()
    with _BOOKING_DB_LOCK:
        _ensure_booking_store(path)
        with contextlib.closing(sqlite3.connect(path, timeout=10)) as conn:
            conn.execute(
                """
                INSERT INTO current_bookings (
                    chat_key, event_id, start, end, timezone, meet_link,
                    html_link, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
                ON CONFLICT(chat_key) DO UPDATE SET
                    event_id=excluded.event_id,
                    start=excluded.start,
                    end=excluded.end,
                    timezone=excluded.timezone,
                    meet_link=excluded.meet_link,
                    html_link=excluded.html_link,
                    status='active',
                    updated_at=excluded.updated_at
                """,
                (
                    _booking_chat_key(chat_id),
                    str(result.get("event_id") or ""),
                    str(result.get("start") or ""),
                    str(result.get("end") or ""),
                    str(result.get("timezone") or business_timezone().key),
                    str(result.get("meet_link") or ""),
                    str(result.get("htmlLink") or ""),
                    now,
                    now,
                ),
            )
            conn.commit()


def get_booking(
    chat_id: str,
    *,
    db_path: str | Path | None = None,
) -> dict[str, Any] | None:
    """Recupera a reserva ativa sem depender do estado efêmero do processo."""
    path = bookings_db_path(db_path)
    if not path.is_file():
        return None
    with _BOOKING_DB_LOCK:
        _ensure_booking_store(path)
        with contextlib.closing(
            sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        ) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT event_id, start, end, timezone, meet_link, html_link,
                       status, created_at, updated_at
                FROM current_bookings
                WHERE chat_key = ? AND status = 'active'
                """,
                (_booking_chat_key(chat_id),),
            ).fetchone()
    return dict(row) if row is not None else None


def _token_payload() -> dict[str, Any]:
    path = token_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def calendar_ready() -> bool:
    """True somente quando existe refresh token e escopo de Calendar."""
    payload = _token_payload()
    if not payload.get("refresh_token"):
        return False
    raw = payload.get("scopes") or payload.get("scope") or []
    scopes = set(raw.split() if isinstance(raw, str) else raw)
    return CALENDAR_SCOPE in scopes or CALENDAR_EVENTS_SCOPE in scopes


def _service():
    if not calendar_ready():
        raise CalendarBookingError("Google Calendar ainda não está autenticado com o escopo de agenda.")
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise CalendarBookingError("Bibliotecas do Google Calendar não estão instaladas.") from exc

    path = token_path()
    try:
        creds = Credentials.from_authorized_user_file(str(path))
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            path.write_text(json.dumps(json.loads(creds.to_json()), indent=2), encoding="utf-8")
            path.chmod(0o600)
        if not creds.valid:
            raise CalendarBookingError("Token do Google Calendar inválido; refaça a autorização.")
        return build("calendar", "v3", credentials=creds, cache_discovery=False)
    except CalendarBookingError:
        raise
    except Exception as exc:
        raise CalendarBookingError(f"Falha ao autenticar no Google Calendar: {type(exc).__name__}") from exc


def _parse_date(value: str, field: str) -> datetime.date:
    try:
        return datetime.strptime(str(value or "").strip(), "%Y-%m-%d").date()
    except ValueError as exc:
        raise CalendarBookingError(f"{field} deve estar no formato YYYY-MM-DD.") from exc


def _parse_datetime(value: str, field: str) -> datetime:
    raw = str(value or "").strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise CalendarBookingError(f"{field} deve ser ISO 8601 com fuso horário.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CalendarBookingError(f"{field} precisa incluir o fuso horário.")
    return parsed.astimezone(business_timezone())


def _coerce_period(value: str) -> str:
    period = str(value or "any").strip().lower()
    aliases = {
        "qualquer": "any", "any": "any", "all": "any",
        "manha": "morning", "manhã": "morning", "morning": "morning",
        "tarde": "afternoon", "afternoon": "afternoon",
    }
    if period not in aliases:
        raise CalendarBookingError("period deve ser any, morning ou afternoon.")
    return aliases[period]


def _coerce_preferred_time(value: str | None) -> time | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%H:%M").time()
    except ValueError as exc:
        raise CalendarBookingError("preferred_time deve estar no formato HH:MM.") from exc


def _round_up(value: datetime, minutes: int = 30) -> datetime:
    value = value.replace(second=0, microsecond=0)
    remainder = value.minute % minutes
    if remainder:
        value += timedelta(minutes=minutes - remainder)
    return value


def _business_bounds(day, tz: ZoneInfo, period: str) -> tuple[datetime, datetime]:
    start = datetime.combine(day, BUSINESS_OPEN, tz)
    end = datetime.combine(day, BUSINESS_CLOSE, tz)
    if period == "morning":
        end = min(end, datetime.combine(day, time(12, 0), tz))
    elif period == "afternoon":
        start = max(start, datetime.combine(day, time(12, 0), tz))
    return start, end


def _overlaps(start: datetime, end: datetime, busy: list[tuple[datetime, datetime]]) -> bool:
    return any(start < busy_end and end > busy_start for busy_start, busy_end in busy)


def _freebusy(service, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    payload = service.freebusy().query(body={
        "timeMin": start.isoformat(),
        "timeMax": end.isoformat(),
        "timeZone": business_timezone().key,
        "items": [{"id": calendar_id()}],
    }).execute()
    calendar = (payload.get("calendars") or {}).get(calendar_id()) or {}
    errors = calendar.get("errors") or []
    if errors:
        raise CalendarBookingError("Google Calendar recusou a consulta de disponibilidade.")
    busy: list[tuple[datetime, datetime]] = []
    for item in calendar.get("busy") or []:
        try:
            busy.append((_parse_datetime(item["start"], "busy.start"), _parse_datetime(item["end"], "busy.end")))
        except (KeyError, CalendarBookingError):
            continue
    return busy


def find_available_slots(
    *,
    date_from: str,
    date_to: str | None = None,
    period: str = "any",
    preferred_time: str | None = None,
    duration_minutes: int = DEFAULT_DURATION_MINUTES,
    max_slots: int = MAX_SLOTS,
    now: datetime | None = None,
    service=None,
) -> dict[str, Any]:
    """Retorna vagas reais dentro do expediente, sem expor detalhes dos eventos."""
    tz = business_timezone()
    first_day = _parse_date(date_from, "date_from")
    last_day = _parse_date(date_to or date_from, "date_to")
    if last_day < first_day:
        raise CalendarBookingError("date_to não pode ser anterior a date_from.")
    if (last_day - first_day).days >= MAX_SEARCH_DAYS:
        raise CalendarBookingError(f"A busca pode cobrir no máximo {MAX_SEARCH_DAYS} dias.")
    try:
        duration = int(duration_minutes)
        limit = max(1, min(int(max_slots), MAX_SLOTS))
    except (TypeError, ValueError) as exc:
        raise CalendarBookingError("Duração ou limite de horários inválido.") from exc
    if duration != DEFAULT_DURATION_MINUTES:
        raise CalendarBookingError(f"As reuniões comerciais duram {DEFAULT_DURATION_MINUTES} minutos.")
    normalized_period = _coerce_period(period)
    preferred_clock = _coerce_preferred_time(preferred_time)

    current = (now or datetime.now(tz)).astimezone(tz)
    min_lead = max(0, int(os.getenv("WHATSAPP_CALENDAR_MIN_LEAD_MINUTES", "120")))
    earliest = _round_up(current + timedelta(minutes=min_lead), 30)
    query_start = datetime.combine(first_day, BUSINESS_OPEN, tz)
    query_end = datetime.combine(last_day, BUSINESS_CLOSE, tz)
    if query_end <= earliest:
        return {"status": "ok", "timezone": tz.key, "duration_minutes": duration, "slots": []}

    with _API_LOCK:
        api = service or _service()
        busy = _freebusy(api, max(query_start, current), query_end)

    slots: list[dict[str, str]] = []
    day = first_day
    while day <= last_day and len(slots) < limit:
        if day.weekday() < 5:
            window_start, window_end = _business_bounds(day, tz, normalized_period)
            if preferred_clock is not None:
                cursor = datetime.combine(day, preferred_clock, tz)
                slot_end = cursor + timedelta(minutes=duration)
                if (
                    cursor >= window_start
                    and cursor >= earliest
                    and slot_end <= window_end
                    and not _overlaps(cursor, slot_end, busy)
                ):
                    slots.append({"start": cursor.isoformat(), "end": slot_end.isoformat()})
            else:
                cursor = _round_up(max(window_start, earliest), 30)
                while cursor + timedelta(minutes=duration) <= window_end and len(slots) < limit:
                    slot_end = cursor + timedelta(minutes=duration)
                    if not _overlaps(cursor, slot_end, busy):
                        slots.append({"start": cursor.isoformat(), "end": slot_end.isoformat()})
                    cursor += timedelta(minutes=30)
        day += timedelta(days=1)

    return {
        "status": "ok",
        "timezone": tz.key,
        "duration_minutes": duration,
        "slots": slots,
    }


def _clean_text(value: str, limit: int) -> str:
    text = " ".join(str(value or "").split())
    text = re.sub(r"[\x00-\x1f\x7f]", " ", text).strip()
    return text[:limit]


def _event_id(chat_id: str, start: datetime, end: datetime) -> str:
    material = f"{chat_id}|{start.isoformat()}|{end.isoformat()}".encode("utf-8")
    # IDs customizados do Google Calendar aceitam somente base32hex (0-9, a-v).
    # O digest SHA-256 hexadecimal já respeita isso; o prefixo também precisa respeitar.
    return "c" + hashlib.sha256(material).hexdigest()[:40]


def _existing_event(api, event_id: str) -> dict[str, Any] | None:
    """Busca apenas o ID determinístico da WhatsAYA; 404 significa ausente."""
    try:
        return api.events().get(calendarId=calendar_id(), eventId=event_id).execute()
    except Exception as exc:
        status = getattr(getattr(exc, "resp", None), "status", None)
        if status == 404:
            return None
        raise CalendarBookingError(
            f"Falha ao verificar reserva existente no Google Calendar: {type(exc).__name__}"
        ) from exc


def _safe_meet_link(event: dict[str, Any] | None) -> str:
    payload = event or {}
    candidates = [payload.get("hangoutLink")]
    conference = payload.get("conferenceData") or {}
    candidates.extend(
        entry.get("uri")
        for entry in conference.get("entryPoints") or []
        if isinstance(entry, dict) and entry.get("entryPointType") == "video"
    )
    for candidate in candidates:
        raw = str(candidate or "").strip()
        try:
            parsed = urllib.parse.urlparse(raw)
        except ValueError:
            continue
        if (
            parsed.scheme == "https"
            and parsed.netloc.lower() == "meet.google.com"
            and parsed.path.strip("/")
        ):
            return urllib.parse.urlunparse(("https", "meet.google.com", parsed.path, "", "", ""))
    return ""


def _meet_conference_request(event_id: str) -> dict[str, Any]:
    return {
        "createRequest": {
            "requestId": f"whatsaya-{event_id}",
            "conferenceSolutionKey": {"type": "hangoutsMeet"},
        }
    }


def _ensure_event_meet(api, event: dict[str, Any], event_id: str) -> tuple[dict[str, Any], str]:
    meet_link = _safe_meet_link(event)
    if meet_link:
        return event, meet_link
    try:
        conference = event.get("conferenceData") or {}
        request = conference.get("createRequest") or {}
        status = str((request.get("status") or {}).get("statusCode") or "")
        if status == "failure":
            raise CalendarBookingError("O Google Calendar não conseguiu gerar o link do Google Meet.")
        if not request:
            event = api.events().patch(
                calendarId=calendar_id(),
                eventId=event_id,
                body={"conferenceData": _meet_conference_request(event_id)},
                conferenceDataVersion=1,
                sendUpdates="none",
            ).execute()

        # A criação da conferência é assíncrona. A confirmação só pode sair depois
        # que o Google realmente devolver o entryPoint do Meet.
        wait_seconds = max(
            0.0,
            min(float(os.getenv("WHATSAPP_CALENDAR_MEET_WAIT_SECONDS", "6")), 15.0),
        )
        deadline = time_module.monotonic() + wait_seconds
        while not (meet_link := _safe_meet_link(event)):
            conference = event.get("conferenceData") or {}
            request = conference.get("createRequest") or {}
            status = str((request.get("status") or {}).get("statusCode") or "")
            if status == "failure" or time_module.monotonic() >= deadline:
                break
            time_module.sleep(min(0.4, max(0.0, deadline - time_module.monotonic())))
            refreshed = _existing_event(api, event_id)
            if refreshed is not None:
                event = refreshed
    except CalendarBookingError:
        raise
    except Exception as exc:
        raise CalendarBookingError(
            f"Falha ao gerar o link do Google Meet: {type(exc).__name__}"
        ) from exc
    if not meet_link:
        raise CalendarBookingError(
            "A reunião foi criada, mas o link do Google Meet ainda não ficou disponível; tente confirmar novamente."
        )
    return event, meet_link


def _validated_booking_window(start: str, end: str) -> tuple[datetime, datetime]:
    start_dt = _parse_datetime(start, "start")
    end_dt = _parse_datetime(end, "end")
    if end_dt <= start_dt:
        raise CalendarBookingError("O fim precisa ser posterior ao início.")
    if int((end_dt - start_dt).total_seconds() // 60) != DEFAULT_DURATION_MINUTES:
        raise CalendarBookingError(f"A reserva precisa ter {DEFAULT_DURATION_MINUTES} minutos.")
    if start_dt.weekday() >= 5:
        raise CalendarBookingError("A reunião precisa ser em dia útil.")
    if start_dt.time() < BUSINESS_OPEN or end_dt.time() > BUSINESS_CLOSE:
        raise CalendarBookingError("A reunião precisa ficar entre 08:00 e 18:00 no fuso de Goiânia.")
    if start_dt <= datetime.now(business_timezone()):
        raise CalendarBookingError("Não é possível reservar um horário no passado.")
    return start_dt, end_dt


def create_booking(
    *,
    chat_id: str,
    start: str,
    end: str,
    lead_name: str = "",
    purpose: str = "",
    service=None,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Revalida, cria Google Meet e persiste a reunião ativa de forma idempotente."""
    start_dt, end_dt = _validated_booking_window(start, end)

    event_id = _event_id(chat_id, start_dt, end_dt)
    digits = "".join(ch for ch in str(chat_id).split("@", 1)[0].split(":", 1)[0] if ch.isdigit())
    safe_name = _clean_text(lead_name, 100) or (f"+{digits}" if digits else "Lead")
    safe_purpose = _clean_text(purpose, 280) or "Apresentação comercial da WhatsAYA"
    description = "\n".join([
        "Origem: WhatsApp / AYA",
        f"Contato: {safe_name}",
        f"WhatsApp: +{digits}" if digits else f"Chat: {_clean_text(chat_id, 80)}",
        f"Assunto: {safe_purpose}",
    ])
    body = {
        "id": event_id,
        "summary": f"Reunião WhatsAYA — {safe_name}",
        "description": description,
        "start": {"dateTime": start_dt.isoformat(), "timeZone": business_timezone().key},
        "end": {"dateTime": end_dt.isoformat(), "timeZone": business_timezone().key},
        "conferenceData": _meet_conference_request(event_id),
        "extendedProperties": {"private": {
            "whatsayaBookingKey": event_id,
            "whatsayaChat": hashlib.sha256(str(chat_id).encode()).hexdigest()[:16],
        }},
    }

    with _API_LOCK:
        api = service or _service()
        if _freebusy(api, start_dt, end_dt):
            # Em um retry real, o próprio evento criado pela primeira chamada aparece
            # como ocupado. Confirme o ID determinístico antes de tratar como conflito.
            event = _existing_event(api, event_id)
            if event is None:
                raise CalendarBookingError("Esse horário acabou de ficar ocupado; consulte novas opções.")
            created = False
        else:
            try:
                event = api.events().insert(
                    calendarId=calendar_id(),
                    body=body,
                    sendUpdates="none",
                    conferenceDataVersion=1,
                ).execute()
                created = True
            except Exception as exc:
                status = getattr(getattr(exc, "resp", None), "status", None)
                if status != 409:
                    raise CalendarBookingError(f"Falha ao criar evento no Google Calendar: {type(exc).__name__}") from exc
                event = _existing_event(api, event_id)
                if event is None:
                    raise CalendarBookingError("O Google reportou conflito, mas a reserva existente não foi encontrada.")
                created = False

        event, meet_link = _ensure_event_meet(api, event, event_id)

    result = {
        "status": "created" if created else "already_exists",
        "event_id": event.get("id") or event_id,
        "summary": event.get("summary") or body["summary"],
        "start": start_dt.isoformat(),
        "end": end_dt.isoformat(),
        "timezone": business_timezone().key,
        "meet_link": meet_link,
        "htmlLink": event.get("htmlLink") or "",
    }
    _persist_booking(chat_id=chat_id, result=result, db_path=db_path)
    return result


def reschedule_booking(
    *,
    chat_id: str,
    start: str,
    end: str,
    lead_name: str = "",
    purpose: str = "",
    service=None,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Move o evento ativo do lead sem criar duplicata e mantém o mesmo Google Meet."""
    current = get_booking(chat_id, db_path=db_path)
    if current is None:
        raise CalendarBookingError("Não encontrei uma reunião ativa para remarcar.")
    start_dt, end_dt = _validated_booking_window(start, end)
    if current.get("start") == start_dt.isoformat() and current.get("end") == end_dt.isoformat():
        return dict(current, status="already_exists")

    event_id = str(current.get("event_id") or "")
    if not event_id:
        raise CalendarBookingError("A reunião ativa está sem identificador do Google Calendar.")
    body: dict[str, Any] = {
        "start": {"dateTime": start_dt.isoformat(), "timeZone": business_timezone().key},
        "end": {"dateTime": end_dt.isoformat(), "timeZone": business_timezone().key},
    }
    current_meet_link = _safe_meet_link({"hangoutLink": current.get("meet_link")})
    if not current_meet_link:
        body["conferenceData"] = _meet_conference_request(event_id)

    with _API_LOCK:
        api = service or _service()
        if _freebusy(api, start_dt, end_dt):
            raise CalendarBookingError("Esse horário acabou de ficar ocupado; consulte novas opções.")
        try:
            event = api.events().patch(
                calendarId=calendar_id(),
                eventId=event_id,
                body=body,
                sendUpdates="none",
                conferenceDataVersion=1,
            ).execute()
        except Exception as exc:
            raise CalendarBookingError(
                f"Falha ao remarcar no Google Calendar: {type(exc).__name__}"
            ) from exc
        meet_link = _safe_meet_link(event) or current_meet_link
        if not meet_link:
            event, meet_link = _ensure_event_meet(api, event, event_id)

    result = {
        "status": "rescheduled",
        "event_id": event.get("id") or event_id,
        "summary": event.get("summary") or "Reunião WhatsAYA",
        "start": start_dt.isoformat(),
        "end": end_dt.isoformat(),
        "timezone": business_timezone().key,
        "meet_link": meet_link,
        "htmlLink": event.get("htmlLink") or str(current.get("html_link") or ""),
    }
    _persist_booking(chat_id=chat_id, result=result, db_path=db_path)
    return result
