"""Motor transacional e fail-closed de follow-up comercial da WhatsAYA.

Este módulo não conhece o gateway nem envia mensagens. Ele mantém estado, agenda,
lease e idempotência em SQLite. O chamador precisa revalidar o job imediatamente
antes do envio e registrar o resultado real retornado pela bridge.
"""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

BUSINESS_TZ = ZoneInfo("America/Sao_Paulo")
BUSINESS_OPEN = time(8, 0)
BUSINESS_CLOSE = time(18, 0)
TERMINAL_STAGES = {"ganho", "perdido", "cancelado", "concluido", "concluída", "won", "lost", "cancelled"}
CONTEXT_KINDS = {"business", "pain", "question", "objection", "proposal", "payment", "next_step"}
_EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
_LONG_DIGITS_RE = re.compile(r"\d{6,}")
# Copy por (stage, kind, step). Sempre cita o fato verificado; nunca ping genérico.
_FOLLOWUP_COPY = {
    ("qualification", "business", 1):
        "Você comentou {fact}. Se ainda estiver na pauta, me diz o que mais trava no atendimento hoje.",
    ("qualification", "business", 2):
        "Retomando: {fact}. Faz sentido a gente olhar isso numa call curta?",
    ("qualification", "business", 3):
        "Último toque sobre {fact}. Se não for o momento, é só falar.",
    ("qualification", "pain", 1):
        "Você falou de {fact}. Posso te mostrar como a AYA entra nisso, sem enrolação.",
    ("qualification", "pain", 2):
        "Ainda vale {fact}? A gente resolve isso numa call curta.",
    ("qualification", "pain", 3):
        "Último toque: {fact}. Se saiu da pauta, sem problema.",
    ("pricing", "question", 1):
        "Ficou pendente {fact}. A proposta é personalizada e fecha numa call curta — quer que eu encaixe?",
    ("pricing", "question", 2):
        "Sobre o investimento ({fact}): sem tabela neste chat, a gente fecha na call. Prefere um horário?",
    ("pricing", "question", 3):
        "Se {fact} ainda importa, me fala um período que o time te encaixa.",
    ("proposal", "next_step", 1):
        "A gente tinha ficado em {fact}. Me fala um período pra call da proposta.",
    ("proposal", "next_step", 2):
        "Ainda vale {fact}? O time encaixa a call no período que você passar.",
    ("proposal", "next_step", 3):
        "Se {fact} saiu da pauta, sem problema — é só responder por aqui.",
    ("payment", "payment", 1):
        "Os dados oficiais já foram, sobre {fact}. Qualquer trava no pagamento, me chama aqui.",
    ("payment", "payment", 2):
        "Retomando o pagamento de {fact}. Se já fez, manda o comprovante; se não, te ajudo.",
    ("payment", "payment", 3):
        "Último aviso sobre {fact}. Se fechou por outro canal, me confirma.",
}
CADENCES: dict[str, tuple[tuple[str, int], ...]] = {
    "silence": (("business_minutes", 30), ("business_days", 1), ("business_days", 3)),
    "proposal": (("business_days", 1), ("business_days", 3), ("business_days", 7)),
    "payment": (("business_minutes", 240), ("business_days", 1), ("business_days", 3)),
    "post_sale": (("business_days", 1), ("business_days", 7), ("business_days", 30)),
}
OUTBOX_WHITELIST = {
    "chat_id", "stage", "cadence_kind", "automation_enabled", "next_action",
    "next_followup_utc", "attempt_count", "followup_status", "last_error",
}
_SECRET_RE = re.compile(
    r"(?:api[_ -]?key|token|senha|password|secret|bearer|sk-[a-z0-9]|\b\d{16}\b)",
    re.IGNORECASE,
)
NOTION_LEAD_STAGES = ("new", "qualification", "pricing", "proposal", "payment")
NOTION_LEAD_CADENCES = ("silence", "proposal", "payment", "post_sale")
_NOTION_TEXT_CAP = 1900


class ContextGateError(ValueError):
    """O contexto não é seguro/suficiente para mensagem automática."""


def _ensure_utc(value: datetime | None = None) -> datetime:
    value = value or datetime.now(UTC)
    if value.tzinfo is None:
        raise ValueError("datetime precisa ter timezone")
    return value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return _ensure_utc(value).isoformat(timespec="seconds")


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def is_business_time(value: datetime) -> bool:
    local = _ensure_utc(value).astimezone(BUSINESS_TZ)
    return local.weekday() < 5 and BUSINESS_OPEN <= local.time() < BUSINESS_CLOSE


def next_business_time(value: datetime) -> datetime:
    """Retorna o próprio instante se válido; senão, próxima abertura comercial."""
    local = _ensure_utc(value).astimezone(BUSINESS_TZ)
    while True:
        if local.weekday() >= 5:
            local = datetime.combine(local.date() + timedelta(days=1), BUSINESS_OPEN, BUSINESS_TZ)
            continue
        if local.time() < BUSINESS_OPEN:
            return datetime.combine(local.date(), BUSINESS_OPEN, BUSINESS_TZ).astimezone(UTC)
        if local.time() >= BUSINESS_CLOSE:
            local = datetime.combine(local.date() + timedelta(days=1), BUSINESS_OPEN, BUSINESS_TZ)
            continue
        return local.astimezone(UTC)


def add_business_minutes(value: datetime, minutes: int) -> datetime:
    if minutes < 0:
        raise ValueError("minutes precisa ser >= 0")
    current = next_business_time(value).astimezone(BUSINESS_TZ)
    remaining = int(minutes)
    if remaining == 0:
        return current.astimezone(UTC)
    while True:
        close = datetime.combine(current.date(), BUSINESS_CLOSE, BUSINESS_TZ)
        available = int((close - current).total_seconds() // 60)
        if remaining < available:
            return (current + timedelta(minutes=remaining)).astimezone(UTC)
        remaining -= available
        current = next_business_time((close + timedelta(seconds=1)).astimezone(UTC)).astimezone(BUSINESS_TZ)


def add_business_days(value: datetime, days: int) -> datetime:
    if days < 0:
        raise ValueError("days precisa ser >= 0")
    local = next_business_time(value).astimezone(BUSINESS_TZ)
    target_date = local.date()
    remaining = int(days)
    while remaining:
        target_date += timedelta(days=1)
        if target_date.weekday() < 5:
            remaining -= 1
    candidate = datetime.combine(target_date, local.timetz().replace(tzinfo=None), BUSINESS_TZ)
    return next_business_time(candidate.astimezone(UTC))


def cadence_due_times(cadence_kind: str, basis: datetime) -> list[datetime]:
    cadence = CADENCES.get(cadence_kind)
    if not cadence:
        raise ValueError(f"cadência inválida: {cadence_kind}")
    base = _ensure_utc(basis)
    due: list[datetime] = []
    for mode, amount in cadence:
        if mode == "business_minutes":
            due.append(add_business_minutes(base, amount))
        else:
            due.append(add_business_days(base, amount))
    return due


def validate_context(
    kind: str | None,
    fact: str | None,
    source_message_id: str | None = None,
    verified: bool = False,
) -> tuple[str, str, str]:
    clean_kind = (kind or "").strip().lower()
    clean_fact = " ".join((fact or "").strip().split())
    clean_source = (source_message_id or "").strip()
    if not verified:
        raise ContextGateError("contexto não foi verificado")
    if not clean_source:
        raise ContextGateError("context_source_message_id obrigatório")
    if clean_kind not in CONTEXT_KINDS:
        raise ContextGateError("context_kind ausente ou inválido")
    if len(clean_fact) < 4 or len(clean_fact) > 180:
        raise ContextGateError("context_fact precisa ter entre 4 e 180 caracteres")
    if _SECRET_RE.search(clean_fact):
        raise ContextGateError("context_fact parece conter segredo ou dado sensível")
    return clean_kind, clean_fact, clean_source


def sanitize_followup_fact(text: str) -> str:
    """Recorta a fala do lead para caber no follow. Sem credencial, sem ping vazio."""
    clean = " ".join(str(text or "").split())
    clean = _LONG_DIGITS_RE.sub("[DIGITS]", clean)
    clean = _EMAIL_RE.sub("[EMAIL]", clean)
    if _SECRET_RE.search(clean):
        return ""
    if len(clean) < 4:
        return ""
    if len(clean) > 180:
        clipped = clean[:177].rsplit(" ", 1)[0].rstrip(".,;:")
        clean = (clipped or clean[:177]).rstrip() + "…"
    return clean


def followup_policy(
    *,
    asked_price: bool,
    wants_call: bool,
    wants_pay: bool,
    wants_human: bool,
) -> dict[str, Any]:
    """Estágio/cadência a partir do turno — sem LLM. Takeover cancela automação."""
    if wants_human:
        return {"takeover": True}
    if wants_pay:
        return {
            "takeover": False,
            "stage": "payment",
            "cadence_kind": "payment",
            "context_kind": "payment",
        }
    if wants_call:
        return {
            "takeover": False,
            "stage": "proposal",
            "cadence_kind": "proposal",
            "context_kind": "next_step",
        }
    if asked_price:
        return {
            "takeover": False,
            "stage": "pricing",
            "cadence_kind": "proposal",
            "context_kind": "question",
        }
    return {
        "takeover": False,
        "stage": "qualification",
        "cadence_kind": "silence",
        "context_kind": "business",
    }


def _stage_from_kind(kind: str) -> str:
    return {
        "question": "pricing",
        "next_step": "proposal",
        "proposal": "proposal",
        "payment": "payment",
        "pain": "qualification",
        "business": "qualification",
        "objection": "pricing",
    }.get(kind, "qualification")


def render_contextual_message(job: dict[str, Any]) -> str:
    """Copy determinística; nunca gera promessa, desconto ou dado não verificado."""
    kind, fact, _source = validate_context(
        job.get("context_kind"),
        job.get("context_fact"),
        job.get("context_source_message_id"),
        bool(job.get("context_verified")),
    )
    step = max(1, min(int(job.get("step_no") or 1), 3))
    stage = str(job.get("stage") or "").strip().lower() or _stage_from_kind(kind)
    template = _FOLLOWUP_COPY.get((stage, kind, step))
    if not template:
        template = "{fact} — se ainda estiver na pauta, me diz por onde retomar."
    return template.format(fact=fact)


def mask_chat_tail(chat_id: str) -> str:
    """Só os 4 últimos dígitos — a outbox local pode ter o JID, o Notion não."""
    digits = "".join(c for c in str(chat_id or "") if c.isdigit())
    if len(digits) < 4:
        return "…????"
    return "…" + digits[-4:]


def notion_lead_payload(
    snapshot: dict[str, Any],
    database_id: str,
    *,
    api_version: str = "2022-06-28",
) -> dict | None:
    """Página mínima na base de leads. Sem telefone inteiro, sem fato da conversa.

    Propriedades travadas (select inexistente derruba a página inteira):
    título `Lead`; rich_text `Resumo`. Estágio/cadência vão no resumo, não em
    select — a base de tickets já ensinou que opção inventada some o card.
    """
    alvo = str(database_id or "").strip()
    if not alvo or not isinstance(snapshot, dict):
        return None
    stage = str(snapshot.get("stage") or "new").strip().lower()
    if stage not in NOTION_LEAD_STAGES:
        stage = "qualification"
    cadence = str(snapshot.get("cadence_kind") or "").strip().lower()
    if cadence and cadence not in NOTION_LEAD_CADENCES:
        cadence = ""
    tail = mask_chat_tail(str(snapshot.get("chat_id") or ""))
    titulo = f"{tail} · {stage}"[:_NOTION_TEXT_CAP]
    resumo = (
        f"estágio={stage}"
        + (f" cadência={cadence}" if cadence else "")
        + f" próximo={snapshot.get('next_followup_utc') or '—'}"
        + f" ação={snapshot.get('next_action') or '—'}"
        + f" chat={tail}"
    )[:_NOTION_TEXT_CAP]
    pai = (
        {"data_source_id": alvo}
        if str(api_version) >= "2025-09-03"
        else {"database_id": alvo}
    )
    return {
        "parent": pai,
        "properties": {
            "Lead": {"title": [{"type": "text", "text": {"content": titulo}}]},
            "Resumo": {"rich_text": [{"type": "text", "text": {"content": resumo}}]},
        },
    }


class FollowupEngine:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self.db_path), timeout=15, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA busy_timeout=15000")
        return con

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            yield con
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        finally:
            con.close()

    def _init_schema(self) -> None:
        con = self._connect()
        try:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS lead_state (
                    chat_id TEXT PRIMARY KEY,
                    generation INTEGER NOT NULL DEFAULT 0,
                    lead_version INTEGER NOT NULL DEFAULT 0,
                    automation_enabled INTEGER NOT NULL DEFAULT 0,
                    stage TEXT NOT NULL DEFAULT 'new',
                    cadence_kind TEXT,
                    context_kind TEXT,
                    context_fact TEXT,
                    context_source_message_id TEXT,
                    context_verified INTEGER NOT NULL DEFAULT 0,
                    takeover INTEGER NOT NULL DEFAULT 0,
                    opt_out INTEGER NOT NULL DEFAULT 0,
                    terminal INTEGER NOT NULL DEFAULT 0,
                    pause_reason TEXT,
                    last_inbound_id TEXT,
                    last_inbound_utc TEXT,
                    last_outbound_id TEXT,
                    last_outbound_utc TEXT,
                    updated_utc TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS followup_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT NOT NULL REFERENCES lead_state(chat_id) ON DELETE CASCADE,
                    generation INTEGER NOT NULL,
                    cadence_kind TEXT NOT NULL,
                    step_no INTEGER NOT NULL,
                    due_utc TEXT NOT NULL,
                    basis_outbound_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    lease_owner TEXT,
                    lease_token TEXT,
                    lease_until_utc TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    bridge_message_id TEXT,
                    context_kind TEXT,
                    context_fact TEXT,
                    context_source_message_id TEXT,
                    context_verified INTEGER NOT NULL DEFAULT 0,
                    created_utc TEXT NOT NULL,
                    updated_utc TEXT NOT NULL,
                    UNIQUE(chat_id, generation, cadence_kind, step_no)
                );
                CREATE INDEX IF NOT EXISTS idx_followup_due
                    ON followup_jobs(status, due_utc);
                CREATE TABLE IF NOT EXISTS crm_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT NOT NULL,
                    lead_version INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_utc TEXT,
                    last_error TEXT,
                    created_utc TEXT NOT NULL,
                    updated_utc TEXT NOT NULL,
                    UNIQUE(chat_id, lead_version)
                );
                """
            )
            migrations = {
                "lead_state": {
                    "context_source_message_id": "TEXT",
                    "context_verified": "INTEGER NOT NULL DEFAULT 0",
                    "pause_reason": "TEXT",
                },
                "followup_jobs": {
                    "lease_token": "TEXT",
                    "context_kind": "TEXT",
                    "context_fact": "TEXT",
                    "context_source_message_id": "TEXT",
                    "context_verified": "INTEGER NOT NULL DEFAULT 0",
                },
            }
            for table, columns in migrations.items():
                existing = {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
                for column, definition in columns.items():
                    if column not in existing:
                        con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        finally:
            con.close()

    @staticmethod
    def _ensure_lead(con: sqlite3.Connection, chat_id: str, now: datetime) -> None:
        con.execute(
            "INSERT OR IGNORE INTO lead_state(chat_id, updated_utc) VALUES (?, ?)",
            (chat_id, _iso(now)),
        )

    @staticmethod
    def _cancel_open(con: sqlite3.Connection, chat_id: str, now: datetime, reason: str) -> int:
        cur = con.execute(
            """
            UPDATE followup_jobs
               SET status='cancelled', last_error=?, lease_owner=NULL,
                   lease_until_utc=NULL, updated_utc=?
             WHERE chat_id=? AND status IN ('pending', 'leased')
            """,
            (reason, _iso(now), chat_id),
        )
        return int(cur.rowcount)

    def configure_lead(
        self,
        chat_id: str,
        *,
        automation_enabled: bool | None = None,
        stage: str | None = None,
        cadence_kind: str | None = None,
        context_kind: str | None = None,
        context_fact: str | None = None,
        context_source_message_id: str | None = None,
        context_verified: bool | None = None,
        takeover: bool | None = None,
        opt_out: bool | None = None,
        terminal: bool | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        current = _ensure_utc(now)
        clean_id = chat_id.strip()
        if not clean_id:
            raise ValueError("chat_id obrigatório")
        if cadence_kind is not None and cadence_kind not in CADENCES:
            raise ValueError(f"cadência inválida: {cadence_kind}")
        if any(value is not None for value in (
            context_kind, context_fact, context_source_message_id, context_verified,
        )):
            context_kind, context_fact, context_source_message_id = validate_context(
                context_kind,
                context_fact,
                context_source_message_id,
                bool(context_verified),
            )
        fields: dict[str, Any] = {}
        if automation_enabled is not None:
            fields["automation_enabled"] = int(bool(automation_enabled))
        if stage is not None:
            fields["stage"] = stage.strip().lower()
            if fields["stage"] in TERMINAL_STAGES:
                fields["terminal"] = 1
        if cadence_kind is not None:
            fields["cadence_kind"] = cadence_kind
        if context_kind is not None:
            fields["context_kind"] = context_kind
            fields["context_fact"] = context_fact
            fields["context_source_message_id"] = context_source_message_id
            fields["context_verified"] = 1
        if takeover is not None:
            fields["takeover"] = int(bool(takeover))
        if opt_out is not None:
            fields["opt_out"] = int(bool(opt_out))
        if terminal is not None:
            fields["terminal"] = int(bool(terminal))
        with self._tx() as con:
            self._ensure_lead(con, clean_id, current)
            if fields:
                self._cancel_open(con, clean_id, current, "policy_or_context_changed")
                assignments = [f"{name}=?" for name in fields]
                values = list(fields.values())
                assignments.extend(["lead_version=lead_version+1", "updated_utc=?"])
                values.extend([_iso(current), clean_id])
                con.execute(f"UPDATE lead_state SET {', '.join(assignments)} WHERE chat_id=?", values)
            row = dict(con.execute("SELECT * FROM lead_state WHERE chat_id=?", (clean_id,)).fetchone())
            if not self._row_eligible(row):
                self._cancel_open(con, clean_id, current, "lead_not_eligible")
            return row

    @staticmethod
    def _row_eligible(row: dict[str, Any] | sqlite3.Row) -> bool:
        stage = str(row["stage"] or "").lower()
        return bool(row["automation_enabled"]) and not any(
            (bool(row["takeover"]), bool(row["opt_out"]), bool(row["terminal"]), stage in TERMINAL_STAGES)
        )

    def note_inbound(
        self,
        chat_id: str,
        *,
        message_id: str | None = None,
        at: datetime | None = None,
    ) -> int:
        current = _ensure_utc(at)
        clean_id = chat_id.strip()
        with self._tx() as con:
            self._ensure_lead(con, clean_id, current)
            row = con.execute("SELECT last_inbound_id FROM lead_state WHERE chat_id=?", (clean_id,)).fetchone()
            if message_id and row and row["last_inbound_id"] == message_id:
                return 0
            con.execute(
                """
                UPDATE lead_state
                   SET generation=generation+1, lead_version=lead_version+1,
                       last_inbound_id=?, last_inbound_utc=?, updated_utc=?
                 WHERE chat_id=?
                """,
                (message_id, _iso(current), _iso(current), clean_id),
            )
            return self._cancel_open(con, clean_id, current, "lead_replied")

    def note_human_takeover(self, chat_id: str, *, at: datetime | None = None) -> int:
        current = _ensure_utc(at)
        clean_id = chat_id.strip()
        with self._tx() as con:
            self._ensure_lead(con, clean_id, current)
            con.execute(
                """
                UPDATE lead_state
                   SET generation=generation+1, lead_version=lead_version+1,
                       takeover=1, updated_utc=?
                 WHERE chat_id=?
                """,
                (_iso(current), clean_id),
            )
            return self._cancel_open(con, clean_id, current, "human_takeover")

    def note_outbound(
        self,
        chat_id: str,
        *,
        message_id: str,
        at: datetime | None = None,
        cadence_kind: str | None = None,
        context_kind: str | None = None,
        context_fact: str | None = None,
        context_source_message_id: str | None = None,
        context_verified: bool | None = None,
    ) -> list[int]:
        current = _ensure_utc(at)
        clean_id = chat_id.strip()
        if not message_id:
            raise ValueError("message_id real da bridge é obrigatório")
        if any(value is not None for value in (
            context_kind, context_fact, context_source_message_id, context_verified,
        )):
            context_kind, context_fact, context_source_message_id = validate_context(
                context_kind, context_fact, context_source_message_id, bool(context_verified)
            )
        with self._tx() as con:
            self._ensure_lead(con, clean_id, current)
            previous = con.execute("SELECT * FROM lead_state WHERE chat_id=?", (clean_id,)).fetchone()
            if previous["last_outbound_id"] == message_id:
                return []
            selected_cadence = cadence_kind or previous["cadence_kind"]
            if selected_cadence is not None and selected_cadence not in CADENCES:
                raise ValueError(f"cadência inválida: {selected_cadence}")
            updates = [
                "generation=generation+1", "lead_version=lead_version+1",
                "last_outbound_id=?", "last_outbound_utc=?", "updated_utc=?",
            ]
            values: list[Any] = [message_id, _iso(current), _iso(current)]
            if selected_cadence is not None:
                updates.append("cadence_kind=?")
                values.append(selected_cadence)
            if context_kind is not None:
                updates.extend([
                    "context_kind=?", "context_fact=?",
                    "context_source_message_id=?", "context_verified=1",
                ])
                values.extend([context_kind, context_fact, context_source_message_id])
            values.append(clean_id)
            con.execute(f"UPDATE lead_state SET {', '.join(updates)} WHERE chat_id=?", values)
            self._cancel_open(con, clean_id, current, "new_outbound")
            state = dict(con.execute("SELECT * FROM lead_state WHERE chat_id=?", (clean_id,)).fetchone())
            if not self._row_eligible(state) or not selected_cadence:
                return []
            try:
                validate_context(
                    state.get("context_kind"), state.get("context_fact"),
                    state.get("context_source_message_id"), bool(state.get("context_verified")),
                )
            except ContextGateError:
                return []
            ids: list[int] = []
            for step, due in enumerate(cadence_due_times(selected_cadence, current), start=1):
                cur = con.execute(
                    """
                    INSERT OR IGNORE INTO followup_jobs(
                        chat_id, generation, cadence_kind, step_no, due_utc,
                        basis_outbound_id, status, context_kind, context_fact,
                        context_source_message_id, context_verified, created_utc, updated_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, 1, ?, ?)
                    """,
                    (
                        clean_id, state["generation"], selected_cadence, step, _iso(due),
                        message_id, state["context_kind"], state["context_fact"],
                        state["context_source_message_id"], _iso(current), _iso(current),
                    ),
                )
                if cur.rowcount and cur.lastrowid is not None:
                    ids.append(int(cur.lastrowid))
            if ids:
                due_times = cadence_due_times(selected_cadence, current)
                payload = {
                    "chat_id": clean_id,
                    "stage": str(state.get("stage") or ""),
                    "cadence_kind": selected_cadence,
                    "automation_enabled": bool(state.get("automation_enabled")),
                    "next_action": "followup_step_1",
                    "next_followup_utc": _iso(due_times[0]) if due_times else "",
                    "attempt_count": 0,
                    "followup_status": "pending",
                }
                con.execute(
                    """
                    INSERT OR IGNORE INTO crm_outbox(
                        chat_id, lead_version, payload_json, status, created_utc, updated_utc
                    ) VALUES (?, ?, ?, 'pending', ?, ?)
                    """,
                    (
                        clean_id,
                        int(state["lead_version"]),
                        json.dumps(payload, ensure_ascii=False, sort_keys=True),
                        _iso(current),
                        _iso(current),
                    ),
                )
            return ids

    def claim_due(
        self,
        *,
        now: datetime | None = None,
        worker_id: str | None = None,
        lease_seconds: int = 90,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        current = _ensure_utc(now)
        worker = worker_id or f"worker-{uuid.uuid4().hex[:12]}"
        lease_until = current + timedelta(seconds=max(15, lease_seconds))
        claimed: list[dict[str, Any]] = []
        with self._tx() as con:
            expired = con.execute(
                """
                SELECT id, chat_id, generation, cadence_kind, step_no
                  FROM followup_jobs
                 WHERE status='leased' AND lease_until_utc < ?
                """,
                (_iso(current),),
            ).fetchall()
            for stale in expired:
                con.execute(
                    """
                    UPDATE followup_jobs
                       SET status='uncertain', lease_owner=NULL, lease_token=NULL,
                           lease_until_utc=NULL, last_error='lease_expired_ambiguous', updated_utc=?
                     WHERE id=? AND status='leased'
                    """,
                    (_iso(current), stale["id"]),
                )
                con.execute(
                    """
                    UPDATE lead_state
                       SET automation_enabled=0, pause_reason='lease_expired_ambiguous',
                           lead_version=lead_version+1, updated_utc=?
                     WHERE chat_id=?
                    """,
                    (_iso(current), stale["chat_id"]),
                )
                con.execute(
                    """
                    UPDATE followup_jobs
                       SET status='cancelled', last_error='prior_lease_expired', updated_utc=?
                     WHERE chat_id=? AND generation=? AND cadence_kind=?
                       AND step_no>? AND status IN ('pending', 'leased')
                    """,
                    (
                        _iso(current), stale["chat_id"], stale["generation"],
                        stale["cadence_kind"], stale["step_no"],
                    ),
                )
            rows = con.execute(
                """
                SELECT j.*, s.automation_enabled, s.stage, s.context_kind,
                       s.context_fact, s.takeover, s.opt_out, s.terminal,
                       s.generation AS live_generation
                  FROM followup_jobs j
                  JOIN lead_state s ON s.chat_id=j.chat_id
                 WHERE j.status='pending' AND j.due_utc <= ?
                   AND NOT EXISTS (
                       SELECT 1 FROM followup_jobs earlier
                        WHERE earlier.chat_id=j.chat_id
                          AND earlier.generation=j.generation
                          AND earlier.cadence_kind=j.cadence_kind
                          AND earlier.step_no < j.step_no
                          AND earlier.status <> 'sent'
                   )
                 ORDER BY j.due_utc, j.id
                 LIMIT ?
                """,
                (_iso(current), max(1, limit)),
            ).fetchall()
            for raw in rows:
                row = dict(raw)
                if row["generation"] != row["live_generation"] or not self._row_eligible(row):
                    con.execute(
                        "UPDATE followup_jobs SET status='cancelled', last_error='stale_or_ineligible', updated_utc=? WHERE id=?",
                        (_iso(current), row["id"]),
                    )
                    continue
                try:
                    validate_context(
                        row.get("context_kind"), row.get("context_fact"),
                        row.get("context_source_message_id"), bool(row.get("context_verified")),
                    )
                except ContextGateError as exc:
                    con.execute(
                        "UPDATE followup_jobs SET status='manual_review', last_error=?, updated_utc=? WHERE id=?",
                        (str(exc), _iso(current), row["id"]),
                    )
                    continue
                if not is_business_time(current):
                    con.execute(
                        "UPDATE followup_jobs SET due_utc=?, updated_utc=? WHERE id=?",
                        (_iso(next_business_time(current)), _iso(current), row["id"]),
                    )
                    continue
                lease_token = uuid.uuid4().hex
                updated = con.execute(
                    """
                    UPDATE followup_jobs
                       SET status='leased', lease_owner=?, lease_token=?, lease_until_utc=?,
                           attempts=attempts+1, updated_utc=?
                     WHERE id=? AND status='pending'
                    """,
                    (worker, lease_token, _iso(lease_until), _iso(current), row["id"]),
                )
                if updated.rowcount:
                    row.update(
                        status="leased", lease_owner=worker, lease_token=lease_token,
                        lease_until_utc=_iso(lease_until),
                    )
                    claimed.append(row)
        return claimed

    def revalidate_claim(
        self, job_id: int, lease_token: str, *, now: datetime | None = None
    ) -> dict[str, Any] | None:
        current = _ensure_utc(now)
        with self._tx() as con:
            raw = con.execute(
                """
                SELECT j.*, s.automation_enabled, s.stage, s.context_kind,
                       s.context_fact, s.takeover, s.opt_out, s.terminal,
                       s.generation AS live_generation
                  FROM followup_jobs j JOIN lead_state s ON s.chat_id=j.chat_id
                 WHERE j.id=? AND j.lease_token=?
                """,
                (job_id, lease_token),
            ).fetchone()
            if not raw or raw["status"] != "leased":
                return None
            row = dict(raw)
            if row["generation"] != row["live_generation"] or not self._row_eligible(row):
                con.execute(
                    "UPDATE followup_jobs SET status='cancelled', last_error='revalidation_failed', updated_utc=? WHERE id=?",
                    (_iso(current), job_id),
                )
                return None
            try:
                validate_context(
                    row.get("context_kind"), row.get("context_fact"),
                    row.get("context_source_message_id"), bool(row.get("context_verified")),
                )
            except ContextGateError as exc:
                con.execute(
                    "UPDATE followup_jobs SET status='manual_review', last_error=?, updated_utc=? WHERE id=?",
                    (str(exc), _iso(current), job_id),
                )
                return None
            if not is_business_time(current):
                con.execute(
                    """
                    UPDATE followup_jobs
                       SET status='pending', due_utc=?, lease_owner=NULL,
                           lease_token=NULL, lease_until_utc=NULL, updated_utc=?
                     WHERE id=?
                    """,
                    (_iso(next_business_time(current)), _iso(current), job_id),
                )
                return None
            return row

    def mark_sent(
        self, job_id: int, bridge_message_id: str, lease_token: str, *, at: datetime | None = None
    ) -> None:
        if not bridge_message_id:
            raise ValueError("bridge_message_id obrigatório")
        current = _ensure_utc(at)
        with self._tx() as con:
            con.execute(
                """
                UPDATE followup_jobs
                   SET status='sent', bridge_message_id=?, lease_owner=NULL,
                       lease_token=NULL, lease_until_utc=NULL, last_error=NULL, updated_utc=?
                 WHERE id=? AND status='leased' AND lease_token=?
                """,
                (bridge_message_id, _iso(current), job_id, lease_token),
            )

    def mark_uncertain(
        self, job_id: int, error: str, lease_token: str, *, at: datetime | None = None
    ) -> None:
        current = _ensure_utc(at)
        with self._tx() as con:
            job = con.execute(
                """
                SELECT chat_id, generation, cadence_kind, step_no
                  FROM followup_jobs
                 WHERE id=? AND status='leased' AND lease_token=?
                """,
                (job_id, lease_token),
            ).fetchone()
            con.execute(
                """
                UPDATE followup_jobs
                   SET status='uncertain', last_error=?, lease_owner=NULL, lease_token=NULL,
                       lease_until_utc=NULL, updated_utc=?
                 WHERE id=? AND status='leased' AND lease_token=?
                """,
                (error[:500], _iso(current), job_id, lease_token),
            )
            if job:
                con.execute(
                    """
                    UPDATE lead_state
                       SET automation_enabled=0, pause_reason='delivery_uncertain',
                           lead_version=lead_version+1, updated_utc=?
                     WHERE chat_id=?
                    """,
                    (_iso(current), job["chat_id"]),
                )
                con.execute(
                    """
                    UPDATE followup_jobs
                       SET status='cancelled', last_error='prior_send_uncertain', updated_utc=?
                     WHERE chat_id=? AND generation=? AND cadence_kind=?
                       AND step_no>? AND status IN ('pending', 'leased')
                    """,
                    (_iso(current), job["chat_id"], job["generation"], job["cadence_kind"], job["step_no"]),
                )

    def mark_failed(
        self, job_id: int, error: str, lease_token: str, *, at: datetime | None = None
    ) -> None:
        current = _ensure_utc(at)
        with self._tx() as con:
            job = con.execute(
                """
                SELECT chat_id, generation, cadence_kind, step_no
                  FROM followup_jobs
                 WHERE id=? AND status='leased' AND lease_token=?
                """,
                (job_id, lease_token),
            ).fetchone()
            con.execute(
                """
                UPDATE followup_jobs
                   SET status='failed', last_error=?, lease_owner=NULL, lease_token=NULL,
                       lease_until_utc=NULL, updated_utc=?
                 WHERE id=? AND status='leased' AND lease_token=?
                """,
                (error[:500], _iso(current), job_id, lease_token),
            )
            if job:
                con.execute(
                    """
                    UPDATE lead_state
                       SET automation_enabled=0, pause_reason='delivery_failed',
                           lead_version=lead_version+1, updated_utc=?
                     WHERE chat_id=?
                    """,
                    (_iso(current), job["chat_id"]),
                )
                con.execute(
                    """
                    UPDATE followup_jobs
                       SET status='cancelled', last_error='prior_send_failed', updated_utc=?
                     WHERE chat_id=? AND generation=? AND cadence_kind=?
                       AND step_no>? AND status IN ('pending', 'leased')
                    """,
                    (_iso(current), job["chat_id"], job["generation"], job["cadence_kind"], job["step_no"]),
                )

    def enqueue_outbox(self, chat_id: str, lead_version: int, payload: dict[str, Any], *, at: datetime | None = None) -> bool:
        invalid = set(payload) - OUTBOX_WHITELIST
        if invalid:
            raise ValueError(f"campos não permitidos na outbox: {sorted(invalid)}")
        current = _ensure_utc(at)
        with self._tx() as con:
            cur = con.execute(
                """
                INSERT OR IGNORE INTO crm_outbox(
                    chat_id, lead_version, payload_json, status, created_utc, updated_utc
                ) VALUES (?, ?, ?, 'pending', ?, ?)
                """,
                (chat_id, int(lead_version), json.dumps(payload, ensure_ascii=False, sort_keys=True), _iso(current), _iso(current)),
            )
            return bool(cur.rowcount)

    def claim_outbox(self, *, limit: int = 10, at: datetime | None = None) -> list[dict[str, Any]]:
        current = _ensure_utc(at)
        cap = max(1, min(int(limit), 50))
        with self._tx() as con:
            rows = con.execute(
                """
                SELECT * FROM crm_outbox
                 WHERE status='pending'
                   AND (next_attempt_utc IS NULL OR next_attempt_utc <= ?)
                 ORDER BY id
                 LIMIT ?
                """,
                (_iso(current), cap),
            ).fetchall()
            claimed = [dict(row) for row in rows]
            if claimed:
                placeholders = ",".join("?" for _ in claimed)
                con.execute(
                    f"""
                    UPDATE crm_outbox
                       SET status='leased', attempts=attempts+1, updated_utc=?
                     WHERE id IN ({placeholders}) AND status='pending'
                    """,
                    [_iso(current), *(int(row["id"]) for row in claimed)],
                )
            return claimed

    def mark_outbox_sent(self, outbox_id: int, notion_url: str, *, at: datetime | None = None) -> None:
        current = _ensure_utc(at)
        with self._tx() as con:
            con.execute(
                """
                UPDATE crm_outbox
                   SET status='sent', last_error=?, updated_utc=?
                 WHERE id=? AND status='leased'
                """,
                (str(notion_url or "")[:500], _iso(current), int(outbox_id)),
            )

    def mark_outbox_failed(self, outbox_id: int, error: str, *, at: datetime | None = None) -> None:
        current = _ensure_utc(at)
        with self._tx() as con:
            con.execute(
                """
                UPDATE crm_outbox
                   SET status='pending', last_error=?, next_attempt_utc=?, updated_utc=?
                 WHERE id=? AND status='leased'
                """,
                (str(error or "")[:500], _iso(current + timedelta(minutes=15)), _iso(current), int(outbox_id)),
            )

    def get_lead(self, chat_id: str) -> dict[str, Any] | None:
        con = self._connect()
        try:
            row = con.execute("SELECT * FROM lead_state WHERE chat_id=?", (chat_id,)).fetchone()
            return dict(row) if row else None
        finally:
            con.close()

    def get_jobs(self, chat_id: str | None = None) -> list[dict[str, Any]]:
        con = self._connect()
        try:
            if chat_id:
                rows = con.execute("SELECT * FROM followup_jobs WHERE chat_id=? ORDER BY id", (chat_id,)).fetchall()
            else:
                rows = con.execute("SELECT * FROM followup_jobs ORDER BY id").fetchall()
            return [dict(row) for row in rows]
        finally:
            con.close()
