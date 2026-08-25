"""Auditor diário do atendimento da WhatsAYA.

Módulo puro: não envia mensagem, não chama LLM e não conhece o gateway. Lê linhas
de log e linhas de banco, agrega o dia e produz o material de auditoria. Quem
agenda, chama o modelo e entrega ao dono é o `whatsapp_manager` / o tick de cron.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# O dia do relatório é o dia comercial do dono, não o dia UTC: 23h de Goiânia
# ainda é o mesmo expediente, e por UTC cairia no dia seguinte.
BUSINESS_TZ_DEFAULT = "America/Sao_Paulo"


def business_tz() -> ZoneInfo:
    """Fuso do relatório — o mesmo `TZ` do container.

    O log é gravado em hora local do container e os turnos são recortados por
    este fuso: fixar São Paulo aqui fazia as duas metades do relatório cobrirem
    janelas diferentes para quem roda com outro `TZ`.
    """
    nome = os.getenv("TZ", "").strip() or BUSINESS_TZ_DEFAULT
    try:
        return ZoneInfo(nome)
    except Exception:
        return ZoneInfo(BUSINESS_TZ_DEFAULT)


# Compatibilidade: quem só precisa do padrão continua importando a constante.
BUSINESS_TZ = ZoneInfo(BUSINESS_TZ_DEFAULT)


# Campos que cada tag emite, na ordem do `logger.*` correspondente em
# `whatsapp_manager.py`. A lista é fechada de propósito: o valor de `digits`
# ("official:BR:pix cnpj") e o de `preview` contêm espaço, então o corte por
# `chave=` só é confiável contra um conjunto conhecido de chaves.
LOG_FIELDS: dict[str, tuple[str, ...]] = {
    "payment-gate": (
        # `methods` é da geração antiga do log (antes de `markets`/`prices`);
        # o dia de um deploy tem as duas no mesmo arquivo.
        "reason", "market", "intent", "methods", "markets", "prices", "price_roles",
        "digits", "emails", "restante", "payment_content", "unofficial",
        "chat", "n",
    ),
    "human-send": ("chat", "bubbles", "sizes", "status"),
    "handoff": ("motivo", "message_id", "chat"),
    "inbound-watchdog": ("chat", "message_id", "esperando", "preview"),
    "onboarding-gate": ("chat", "n", "restante"),
    "contact-reply": ("chat",),
    # Emitida em toda montagem de prompt de cliente: diz se a dica determinística
    # de idioma foi para o prompt daquele turno, e de onde veio o sinal.
    "language-hint": ("chat", "lead", "hint", "fonte"),
}

_TAG_RE = re.compile(r"\[([a-z][a-z-]*)\]")
# O JID aparece ora em campo nomeado (`chat=...`), ora solto no meio da frase
# (`[handoff] dono avisado sobre '55...@s.whatsapp.net'`).
_JID_RE = re.compile(r"(\d{6,}(?:[:-]\d+)?@[a-z.]+)")
# Hora que o handler de arquivo do plugin escreve no começo da linha.
_LINE_AT_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})")


@dataclass
class AuditEvent:
    tag: str
    fields: dict[str, str] = field(default_factory=dict)
    raw: str = ""
    chat_id: str = ""
    at: datetime | None = None


def _parse_fields(tail: str, keys: tuple[str, ...]) -> dict[str, str]:
    marks = [
        (match.start(1), match.group(1))
        for match in re.finditer(r"(?:^|\s)([a-z_]+)=", tail)
        if match.group(1) in keys
    ]
    parsed: dict[str, str] = {}
    for index, (start, key) in enumerate(marks):
        end = marks[index + 1][0] if index + 1 < len(marks) else len(tail)
        parsed[key] = tail[start + len(key) + 1:end].strip()
    return parsed


def parse_log_lines(lines) -> list[AuditEvent]:
    """Converte linhas de log do plugin em eventos, ignorando o resto do stdout."""
    eventos: list[AuditEvent] = []
    for line in lines:
        line = str(line or "").rstrip("\n")
        # O handler do plugin escreve '[whatsapp-manager] [tag] ...', então a tag do
        # evento é o segundo colchete. Procurar o primeiro colchete conhecido em vez
        # de assumir posição também sobrevive ao prefixo do `docker logs -t`.
        for match in _TAG_RE.finditer(line):
            if match.group(1) in LOG_FIELDS:
                break
        else:
            continue
        tag = match.group(1)
        tail = line[match.end():]
        jid = _JID_RE.search(tail)
        quando = _LINE_AT_RE.match(line)
        at = None
        if quando:
            try:
                at = datetime.fromisoformat(quando.group(1)).replace(tzinfo=business_tz())
            except ValueError:
                at = None
        eventos.append(AuditEvent(
            tag=tag,
            fields=_parse_fields(tail, LOG_FIELDS[tag]),
            raw=line,
            chat_id=jid.group(1) if jid else "",
            at=at,
        ))
    return eventos


@dataclass
class Turn:
    chat_id: str
    at: datetime
    from_me: bool
    body: str


def _day_bounds(day: date) -> tuple[float, float]:
    start = datetime.combine(day, datetime.min.time(), tzinfo=business_tz())
    return start.timestamp(), (start + timedelta(days=1)).timestamp()


def read_day_turns(db_path, day: date, *, limit: int = 4000) -> list[Turn]:
    """Turnos reais do dia comercial, em ordem cronológica.

    O recorte é feito em Python porque `WHERE timestamp >= ?` já devolveu vazio
    contra este banco; o caminho confiável é ordenar por timestamp, cortar em
    LIMIT e filtrar depois. Importação histórica fica de fora: ela não é
    atendimento do dia.
    """
    path = Path(db_path)
    if not path.is_file():
        return []
    inicio, fim = _day_bounds(day)
    tz = business_tz()
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return []
    corpo = """
            SELECT chat_id, timestamp, from_me, body
            FROM messages
            WHERE is_historical = 0 AND body IS NOT NULL AND TRIM(body) != ''
    """
    try:
        # Recortar o dia no SQL é o que permite reprocessar uma data antiga: sem
        # isso o LIMIT come as linhas do dia pedido antes do filtro em Python.
        rows = conn.execute(
            corpo + " AND timestamp IS NOT NULL AND timestamp >= ? AND timestamp < ?"
            " ORDER BY COALESCE(timestamp, 0) DESC LIMIT ?",
            (inicio, fim, max(1, int(limit))),
        ).fetchall()
        if not rows:
            # O handoff registra `WHERE timestamp >=` devolvendo vazio contra este
            # banco. Não reproduzi (a afinidade REAL da coluna converte texto
            # numérico), mas é observação de produção: se o recorte no SQL não
            # trouxer nada, tenta o caminho antigo antes de declarar dia vazio.
            rows = conn.execute(
                corpo + " ORDER BY COALESCE(timestamp, 0) DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()

    turnos = [
        Turn(
            chat_id=str(chat_id),
            at=datetime.fromtimestamp(float(ts), tz),
            from_me=bool(from_me),
            body=str(body),
        )
        for chat_id, ts, from_me, body in rows
        if ts is not None and inicio <= float(ts) < fim
    ]
    turnos.sort(key=lambda t: t.at)
    return turnos


@dataclass
class Unanswered:
    chat_id: str
    waited_s: int
    message_id: str = ""


@dataclass
class EventScore:
    """Placar do dia vindo só do log — sem texto de conversa."""
    guard_hits: dict[str, int] = field(default_factory=dict)
    by_chat: dict[str, int] = field(default_factory=dict)
    unattributed: int = 0
    unanswered: list[Unanswered] = field(default_factory=list)
    handoffs: list[tuple[str, str]] = field(default_factory=list)


def _unquote(value: str) -> str:
    value = str(value or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def _int(value: str, default: int = 0) -> int:
    match = re.search(r"-?\d+", str(value or ""))
    return int(match.group()) if match else default


def aggregate_events(eventos) -> EventScore:
    """Agrega os eventos do dia por motivo e por conversa."""
    placar = EventScore()
    for evento in eventos:
        tag, campos = evento.tag, evento.fields
        chave = ""
        if tag == "payment-gate" and "reason" in campos:
            chave = f"payment-gate:{_unquote(campos['reason'])}"
        elif tag == "payment-gate" and "market" in campos:
            # O disparo de recorte ("parágrafo de mercado errado removido") não
            # emite `reason=`. Exigir reason descartava esse disparo inteiro —
            # do placar e da evidência.
            chave = "payment-gate:market_strip"
        elif tag == "onboarding-gate" and "n" in campos:
            chave = "onboarding-gate"
        elif tag == "inbound-watchdog":
            # Só o alerta traz `esperando=`; sem ele a linha é o banner de boot,
            # emitido a cada subida do gateway.
            if "esperando" in campos:
                placar.unanswered.append(Unanswered(
                    chat_id=evento.chat_id,
                    waited_s=_int(campos["esperando"]),
                    message_id=_unquote(campos.get("message_id", "")),
                ))
            continue
        elif tag == "handoff":
            # Linha de sucesso: as de erro/cooldown não têm `motivo=`.
            if "motivo" in campos:
                placar.handoffs.append((evento.chat_id, _unquote(campos["motivo"])))
            continue
        if not chave:
            continue
        placar.guard_hits[chave] = placar.guard_hits.get(chave, 0) + 1
        if evento.chat_id:
            placar.by_chat[evento.chat_id] = placar.by_chat.get(evento.chat_id, 0) + 1
        else:
            placar.unattributed += 1
    return placar


@dataclass
class Reply:
    """Um turno de resposta da AYA: as bolhas seguidas que saíram de uma vez."""
    chat_id: str
    at: datetime
    bubbles: list[str] = field(default_factory=list)
    lead_message: str = ""
    lead_at: datetime | None = None
    # Hora da última bolha, para saber onde o turno termina.
    bubbles_last_at: datetime | None = None


# Bolhas do mesmo turno saem em segundos (`_human_send` dorme 2–8s entre elas).
# Uma pausa maior que isto é outro turno — um follow-up agendado, ou o dono
# voltando ao assunto horas depois.
REPLY_GAP_S = 15 * 60


def group_replies(turns) -> list[Reply]:
    """Agrupa bolhas consecutivas da AYA numa resposta só, por conversa.

    `_human_send` grava uma linha por bolha; sem agrupar, um turno de 4 bolhas
    contaria como 4 respostas e distorceria qualquer placar por resposta. O
    turno fecha na mensagem do lead OU numa pausa longa: sem o corte por tempo,
    um follow-up de horas depois grudava no turno anterior e virava violação de
    formato inventada, com o trecho apontando para a parte errada da conversa.
    """
    abertas: dict[str, Reply] = {}
    ultima_do_lead: dict[str, tuple[str, datetime]] = {}
    respostas: list[Reply] = []
    for turn in sorted(turns, key=lambda t: t.at):
        if not turn.from_me:
            abertas.pop(turn.chat_id, None)
            ultima_do_lead[turn.chat_id] = (turn.body, turn.at)
            continue
        aberta = abertas.get(turn.chat_id)
        if aberta is not None and (turn.at - aberta.bubbles_last_at).total_seconds() > REPLY_GAP_S:
            aberta = None
        if aberta is None:
            pergunta = ultima_do_lead.get(turn.chat_id)
            aberta = Reply(
                chat_id=turn.chat_id,
                at=turn.at,
                lead_message=pergunta[0] if pergunta else "",
                lead_at=pergunta[1] if pergunta else None,
            )
            aberta.bubbles_last_at = turn.at
            abertas[turn.chat_id] = aberta
            respostas.append(aberta)
        aberta.bubbles.append(turn.body)
        aberta.bubbles_last_at = turn.at
    return respostas


# Frases que a guarda determinística entrega no lugar da resposta do modelo.
# Cópia deliberada das constantes de `whatsapp_manager.py`: este módulo é puro e
# não importa o plugin. `FallbackCatalogDriftTest` é o que impede as duas cópias
# de divergirem — sem ele, uma frase reescrita no plugin viraria "modelo acertou"
# no placar e o relatório mentiria justamente sobre o que ele existe para medir.
FALLBACK_PHRASES: tuple[str, ...] = (
    # _NO_PRICE_CONTINUATION
    "Me conta como funciona seu atendimento hoje que eu te explico como a AYA se encaixa.",
    "Tell me how your customer service works today and I'll explain how AYA fits in.",
    "Cuéntame cómo funciona tu atención hoy y te explico cómo encaja la AYA.",
    # _NO_PRICE_CONTINUATION_REPEAT
    "Quando quiser, te mostro como a AYA ficaria no seu atendimento.",
    "Whenever you're ready, I can show you how AYA would fit your customer service.",
    "Cuando quieras, te muestro cómo quedaría la AYA en tu atención.",
    # _OFFICIAL_PAYMENT_INTRO
    "Perfeito — seguem os dados oficiais para o pagamento:",
    "Great — here are the official payment details:",
    "Perfecto — estos son los datos oficiales de pago:",
    # _ONBOARDING_GATE_FALLBACK
    "Essa parte de configuração a gente ajusta junto depois da contratação. "
    "O que falta para você tomar a decisão?",
    "We'll sort out that configuration together after you sign up. "
    "What else do you need to decide?",
    "Esa parte de configuración la ajustamos juntos después de la contratación. "
    "¿Qué te falta para decidir?",
    # _PAYMENT_GATE_ASK_MARKET (frase antiga fica para o log dos dias anteriores)
    "Em qual país sua empresa atua?",
    "Which country does your company operate in?",
    "¿En qué país opera tu empresa?",
    "Vocês atendem de onde hoje?",
    "Where are you based?",
    "¿De dónde atienden hoy?",
    # _PAYMENT_GATE_INTENT_MISSING
    "Posso enviar os dados de pagamento quando você quiser avançar com a contratação.",
    "I can send the payment details when you're ready to move forward.",
    "Puedo enviarte los datos de pago cuando quieras avanzar con la contratación.",
    # _PAYMENT_GATE_OFFICIAL_ONLY
    "Vou usar somente os dados de pagamento oficiais do mercado da sua empresa.",
    "I'll only use the official payment details for your company's market.",
    "Solo voy a usar los datos de pago oficiales correspondientes al mercado de tu empresa.",
    # _PAYMENT_RECEIPT_ASK
    "Assim que fizer o pagamento, me envie o comprovante por aqui para "
    "seguirmos com a validação e o onboarding.",
    "As soon as you pay, send the receipt here so we can validate it "
    "and start onboarding.",
    "En cuanto hagas el pago, envíame el comprobante por aquí para "
    "validarlo y seguir con el onboarding.",
    # _PAYMENT_CLAIMED_RECEIPT
    "Perfeito. Me envia o comprovante por aqui. Assim que o pagamento "
    "for validado, seguimos com o onboarding.",
    "Perfect. Send the receipt here. Once the payment is confirmed, "
    "we continue with onboarding.",
    "Perfecto. Envíame el comprobante por aquí. Cuando el pago esté "
    "validado, seguimos con el onboarding.",
    # _MARKET_CORRECTION_LINE
    "Como sua empresa opera nos Estados Unidos, os valores são em dólar. "
    "Quer que eu te passe a condição certa?",
    "Since your company operates in the United States, the pricing is in US dollars. "
    "Want me to walk you through it?",
    "Como tu empresa opera en Estados Unidos, los valores son en dólares. "
    "¿Quieres que te pase la condición correcta?",
    "Como sua empresa opera no Brasil, os valores são em reais. "
    "Quer que eu te passe a condição certa?",
    "Since your company operates in Brazil, the pricing is in Brazilian reais. "
    "Want me to walk you through it?",
    "Como tu empresa opera en Brasil, los valores son en reales. "
    "¿Quieres que te pase la condición correcta?",
)

# `_MARKET_PRICE_SENTENCE` é template com `{setup}`/`{monthly}`: casar o literal
# nunca bateria, então cada um vira um padrão com o valor em aberto.
FALLBACK_TEMPLATES: tuple[str, ...] = (
    "{setup} de implantação e {monthly} por mês.",
    "{setup} setup and {monthly} per month.",
    "{setup} de implementación y {monthly} al mes.",
)


def _fold(text: str) -> str:
    """Minúsculas, sem acento e com espaço normalizado."""
    folded = unicodedata.normalize("NFD", str(text or "").lower())
    folded = "".join(c for c in folded if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", folded).strip()


def _template_regex(template: str) -> re.Pattern:
    partes = [re.escape(_fold(p)) for p in re.split(r"\{\w+\}", template)]
    return re.compile(r".+?".join(partes))


_FALLBACK_FOLDED = tuple(_fold(p) for p in FALLBACK_PHRASES)
_FALLBACK_PATTERNS = tuple(_template_regex(t) for t in FALLBACK_TEMPLATES)


def classify_reply(reply: Reply) -> str:
    """"guarda" quando a resposta saiu de uma frase da guarda; "modelo" senão.

    Basta uma bolha: `_payment_gate_fallback` devolve "sobra do modelo + frase da
    guarda", e esse turno só existe porque a guarda entrou.
    """
    texto = _fold(" ".join(reply.bubbles))
    if any(frase in texto for frase in _FALLBACK_FOLDED):
        return "guarda"
    if any(padrao.search(texto) for padrao in _FALLBACK_PATTERNS):
        return "guarda"
    return "modelo"


# Tetos que o QA de 24/08 fixou para o que chega ao lead. Conversa comum vai em
# até 3 bolhas; lista e turno mais longo são do bloco oficial de pagamento, que é
# estruturado de propósito. Cobrar o bloco pelas regras de conversa comum encheria
# o relatório de falso positivo todo dia.
BUBBLE_CAP = 3
BUBBLE_MAX_CHARS = 400
_LIST_LINE = re.compile(r"^\s*(?:[-*•·–—]|\d+[.)])\s+\S", re.M)
_PAYMENT_INTROS = tuple(
    _fold(p) for p in (
        "Perfeito — seguem os dados oficiais para o pagamento:",
        "Great — here are the official payment details:",
        "Perfecto — estos son los datos oficiales de pago:",
    )
)


@dataclass
class FormatViolation:
    kind: str
    chat_id: str
    at: datetime
    detail: str = ""


def is_payment_reply(reply: Reply) -> bool:
    """True quando o turno entregou o bloco oficial de pagamento."""
    texto = _fold(" ".join(reply.bubbles))
    return any(intro in texto for intro in _PAYMENT_INTROS)


def find_format_violations(reply: Reply) -> list[FormatViolation]:
    """Violações de formato do turno, na régua de conversa comum."""
    achados: list[FormatViolation] = []

    def achado(kind: str, detail: str = "") -> None:
        achados.append(FormatViolation(kind, reply.chat_id, reply.at, detail))

    estruturado = is_payment_reply(reply)
    if not estruturado and len(reply.bubbles) > BUBBLE_CAP:
        achado("bolhas_demais", str(len(reply.bubbles)))
    for bolha in reply.bubbles:
        if len(bolha) > BUBBLE_MAX_CHARS:
            achado("bolha_longa", str(len(bolha)))
    if not estruturado and any(_LIST_LINE.search(b) for b in reply.bubbles):
        achado("lista_em_conversa_comum")
    return achados


@dataclass
class LanguageMismatch:
    chat_id: str
    at: datetime
    lead_language: str
    reply_language: str


def find_language_mismatch(reply: Reply, infer_language) -> LanguageMismatch | None:
    """Troca de idioma indevida: o lead escreve numa língua e a AYA responde noutra.

    `infer_language` é injetado (na produção, `_infer_message_language` do plugin)
    porque ele já devolve None em mensagem ambígua — e ambíguo não é achado.
    """
    do_lead = infer_language(reply.lead_message)
    da_aya = infer_language(" ".join(reply.bubbles))
    if not do_lead or not da_aya or do_lead == da_aya:
        return None
    return LanguageMismatch(reply.chat_id, reply.at, do_lead, da_aya)


# Um valor de credencial nunca pode chegar ao auditor nem ao relatório: o que
# prova o achado é a classificação do log (`official:`/`unknown:`), não o número.
# Preço fica de fora do corte de propósito — apagar "R$ 997,00" destruiria
# justamente a evidência de preço errado que o auditor precisa julgar.
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
# O valor precisa ter forma de dinheiro (milhar e centavos) e não pode ser
# seguido de `/` ou `-` com dígito: sem isso, "R$ 44.249.819/0001-62" era lido
# como preço e o CNPJ saía inteiro no material do auditor.
# Data ISO e hora têm a mesma forma de um documento (8 dígitos com separador) e
# caíam na regra do CPF: o cabeçalho do relatório saía "[dígitos:8]".
_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?\b")
_MONEY_RE = re.compile(
    r"(?:R\$|US\$|\$|€)\s*\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{2})?(?![\d.,]*[/-]\d)",
    re.I,
)
# Sequência de dígitos com pontuação de documento/telefone no meio. O corte é por
# 8+ dígitos: CPF, CNPJ e telefone entram; preço e número de item não.
_DIGIT_RUN_RE = re.compile(r"\+?\d[\d\s.\-/()]{6,}\d")


def redact(text: str) -> str:
    """Remove valor de credencial, documento, telefone e e-mail, preservando preço."""
    texto = str(text or "")
    reservas: list[str] = []

    def reservar(match: re.Match) -> str:
        reservas.append(match.group(0))
        return f"\x00{len(reservas) - 1}\x00"

    texto = _DATE_RE.sub(reservar, texto)
    texto = _MONEY_RE.sub(reservar, texto)
    texto = _EMAIL_RE.sub("[email]", texto)
    texto = _UUID_RE.sub("[chave]", texto)

    def marcar(match: re.Match) -> str:
        digitos = "".join(c for c in match.group(0) if c.isdigit())
        if len(digitos) < 8:
            return match.group(0)
        return f"[dígitos:{len(digitos)}]"

    texto = _DIGIT_RUN_RE.sub(marcar, texto)
    for indice, original in enumerate(reservas):
        texto = texto.replace(f"\x00{indice}\x00", original)
    return texto


def mask_chat(chat_id: str) -> str:
    """Últimos 4 dígitos e origem — o self-chat do dono já tem o contato inteiro."""
    bruto = str(chat_id or "").strip()
    if not bruto:
        return "sem chat"
    local, _, dominio = bruto.partition("@")
    digitos = "".join(c for c in local.split(":")[0] if c.isdigit())
    if dominio == "lid":
        origem = "lid"
    elif digitos.startswith("55"):
        origem = "BR"
    elif digitos.startswith("1"):
        origem = "US"
    else:
        origem = "?"
    return f"…{digitos[-4:] or '????'} ({origem})"


@dataclass
class DayAudit:
    day: date
    chats: int = 0
    lead_messages: int = 0
    replies_guard: int = 0
    replies_model: int = 0
    guard_hits: dict[str, int] = field(default_factory=dict)
    by_chat: dict[str, int] = field(default_factory=dict)
    unattributed: int = 0
    format_violations: list[FormatViolation] = field(default_factory=list)
    language_mismatches: list[LanguageMismatch] = field(default_factory=list)
    unanswered: list[Unanswered] = field(default_factory=list)
    handoffs: list[tuple[str, str]] = field(default_factory=list)
    replies: list[Reply] = field(default_factory=list)
    gate_evidence: list[str] = field(default_factory=list)
    # Mensagens que o dono digitou na conversa do lead (takeover). Ficam fora do
    # placar da AYA, mas contam como sinal do dia.
    owner_manual: int = 0
    language_hints: list[AuditEvent] = field(default_factory=list)
    lead_latencies: list[float] = field(default_factory=list)
    model_seconds: list[float] = field(default_factory=list)
    api_calls: int = 0


def build_day_audit(day: date, events, turns, infer_language, *, gateway_turns=()) -> DayAudit:
    """Junta o que o log provou e o que a conversa mostra num retrato do dia."""
    placar = aggregate_events(events)
    # O dono digitando na conversa do lead também é `from_me=1` no banco; sem
    # separar, a digitação dele entraria no placar da AYA e levaria violação de
    # formato pelo que ele mesmo escreveu.
    auditaveis, do_dono = split_owner_manual(turns, events)
    respostas = group_replies(auditaveis)
    dia = DayAudit(
        day=day,
        chats=len({t.chat_id for t in turns}),
        lead_messages=sum(1 for t in turns if not t.from_me),
        owner_manual=len(do_dono),
        guard_hits=placar.guard_hits,
        by_chat=placar.by_chat,
        unattributed=placar.unattributed,
        unanswered=placar.unanswered,
        handoffs=placar.handoffs,
        replies=respostas,
        gate_evidence=_gate_evidence(events),
        language_hints=[
            e for e in events
            if e.tag == "language-hint" and e.at is not None and e.chat_id
        ],
    )
    dia.lead_latencies = reply_latencies(respostas)
    dia.model_seconds = [t.seconds for t in gateway_turns]
    dia.api_calls = sum(t.api_calls for t in gateway_turns)
    for resposta in respostas:
        if classify_reply(resposta) == "guarda":
            dia.replies_guard += 1
        else:
            dia.replies_model += 1
        dia.format_violations.extend(find_format_violations(resposta))
        achado = find_language_mismatch(resposta, infer_language)
        if achado:
            dia.language_mismatches.append(achado)
    return dia


def _median(valores) -> float:
    ordenados = sorted(valores)
    meio = len(ordenados) // 2
    if not ordenados:
        return 0.0
    if len(ordenados) % 2:
        return ordenados[meio]
    return (ordenados[meio - 1] + ordenados[meio]) / 2


WINDOW = 3  # mensagens antes e depois do achado, como pedido no desenho
# Uma bolha de 400+ caracteres é o próprio achado: despejá-la inteira no material
# só engorda o que vai para o provider externo sem ajudar a julgar.
EXCERPT_CHARS = 200


def _window(turns, at: datetime, chat_id: str) -> list[str]:
    """±WINDOW mensagens da mesma conversa em torno do achado, já redigidas."""
    da_conversa = [t for t in sorted(turns, key=lambda t: t.at) if t.chat_id == chat_id]
    posicoes = [i for i, t in enumerate(da_conversa) if t.at >= at]
    centro = posicoes[0] if posicoes else len(da_conversa)
    recorte = da_conversa[max(0, centro - WINDOW):centro + WINDOW + 1]
    linhas = []
    for t in recorte:
        corpo = redact(t.body)
        if len(corpo) > EXCERPT_CHARS:
            corpo = f"{corpo[:EXCERPT_CHARS]}… (+{len(corpo) - EXCERPT_CHARS} caracteres)"
        linhas.append(f"{'AYA' if t.from_me else 'Lead'}: {corpo}")
    return linhas


def _gate_evidence(events) -> list[str]:
    """Evidência do payment-gate como o log a classificou — nunca o valor."""
    linhas = []
    for evento in events:
        if evento.tag != "payment-gate":
            continue
        campos = evento.fields
        if "reason" in campos:
            motivo = _unquote(campos["reason"])
        elif "market" in campos:
            motivo = "market_strip"
        else:
            continue
        partes = [f"motivo={motivo}", f"chat={mask_chat(evento.chat_id)}"]
        for chave in ("market", "intent", "digits", "emails", "prices", "unofficial"):
            if chave in campos:
                partes.append(f"{chave}={campos[chave]}")
        linhas.append("- " + " ".join(partes))
    return linhas


def compile_material(day: DayAudit, turns) -> str:
    """Material que vai ao modelo auditor — redigido, agregado e com trechos.

    Tudo o que sai daqui atravessa `redact` e `mask_chat`: este texto deixa a
    máquina e vai para um provider externo.
    """
    linhas = [
        f"# Auditoria do atendimento — {day.day.isoformat()}",
        "",
        "## Números do dia",
        f"- conversas: {day.chats}",
        f"- mensagens de lead: {day.lead_messages}",
        f"- turnos de resposta: {day.replies_guard + day.replies_model}",
        f"- guarda salvou: {day.replies_guard}",
        f"- modelo acertou: {day.replies_model}",
        f"- mensagens em que o dono assumiu a conversa: {day.owner_manual}",
    ]

    if day.guard_hits:
        linhas += ["", "## Disparos de guarda (do log)"]
        linhas += [f"- {motivo}: {n}" for motivo, n in sorted(day.guard_hits.items())]
        if day.unattributed:
            linhas.append(f"- sem conversa atribuída (log de geração antiga): {day.unattributed}")

    if day.gate_evidence:
        linhas += ["", "## Evidência parseada do payment-gate", *day.gate_evidence]

    if day.lead_latencies or day.model_seconds:
        linhas += ["", "## Tempo de resposta"]
        if day.lead_latencies:
            linhas.append(
                f"- lead esperou (s): mediana {_median(day.lead_latencies):.0f}, "
                f"pior {max(day.lead_latencies):.0f}"
            )
        if day.model_seconds:
            linhas.append(
                f"- modelo (s): mediana {_median(day.model_seconds):.1f}, "
                f"pior {max(day.model_seconds):.1f} · api_calls no dia: {day.api_calls}"
            )

    idioma = language_hint_scoreboard(day)
    if idioma["dica_ignorada"] or idioma["sem_dica"]:
        linhas += ["", "## Dica determinística de idioma"]
        if idioma["dica_ignorada"]:
            linhas.append(
                f"- dica de idioma estava no prompt e foi ignorada: {idioma['dica_ignorada']}"
            )
        if idioma["sem_dica"]:
            fontes = ", ".join(f"{k}={v}" for k, v in sorted(idioma["fontes_sem_dica"].items()))
            linhas.append(f"- troca de idioma sem dica emitida: {idioma['sem_dica']} ({fontes})")
        if idioma["dica_funcionou"]:
            linhas.append(f"- turnos com dica e idioma certo: {idioma['dica_funcionou']}")

    if day.format_violations:
        linhas += ["", "## Violações de formato"]
        # Agrupadas por turno: um mesmo turno costuma disparar duas regras
        # (bolhas demais e bolha longa), e sem agrupar o trecho da conversa saía
        # repetido a cada achado.
        por_turno: dict[tuple[str, datetime], list[FormatViolation]] = {}
        for violacao in day.format_violations:
            por_turno.setdefault((violacao.chat_id, violacao.at), []).append(violacao)
        for (chat_id, at), achados in por_turno.items():
            regras = ", ".join(
                f"{v.kind}({v.detail})" if v.detail else v.kind for v in achados
            )
            linhas.append(f"- {regras} em {mask_chat(chat_id)}")
            linhas += [f"  > {l}" for l in _window(turns, at, chat_id)]

    if day.language_mismatches:
        linhas += ["", "## Troca de idioma"]
        for achado in day.language_mismatches:
            linhas.append(
                f"- lead escreveu em {achado.lead_language}, AYA respondeu em "
                f"{achado.reply_language} ({mask_chat(achado.chat_id)})"
            )
            linhas += [f"  > {l}" for l in _window(turns, achado.at, achado.chat_id)]

    if day.unanswered:
        linhas += ["", "## Mensagens sem resposta"]
        linhas += [
            f"- {mask_chat(u.chat_id)} esperou {u.waited_s}s (msg {u.message_id or 'sem id'})"
            for u in day.unanswered
        ]

    if day.handoffs:
        linhas += ["", "## Handoffs entregues"]
        linhas += [f"- {mask_chat(chat)}: {redact(motivo)}" for chat, motivo in day.handoffs]

    return "\n".join(linhas)


def render_owner_summary(day: DayAudit, verdict: str) -> str:
    """Resumo curto para o self-chat do dono.

    O placar é sempre o do código. O veredito do modelo entra como texto ao lado,
    nunca no lugar dele: o ponto do relatório é justamente poder ver "a guarda
    salvou N turnos" quando o modelo diria que o dia correu bem.
    """
    achados = (
        len(day.format_violations)
        + len(day.language_mismatches)
        + len(day.unanswered)
    )
    linhas = [
        f"🔍 *Auditoria do dia {day.day.strftime('%d/%m')}*",
        f"{day.chats} conversa(s), {day.replies_guard + day.replies_model} turno(s) de resposta.",
        f"*Placar:* {day.replies_guard} guarda salvou × {day.replies_model} modelo acertou.",
        f"*Disparos de guarda:* {sum(day.guard_hits.values())} · *achados de formato/idioma/silêncio:* {achados}",
    ]
    if day.owner_manual:
        linhas.append(f"Você assumiu a conversa em {day.owner_manual} mensagem(ns).")
    if day.unanswered:
        linhas.append(f"⚠️ {len(day.unanswered)} mensagem(ns) sem resposta.")
    texto = str(verdict or "").strip()
    linhas += ["", texto if texto else "_Auditor sem veredito nesta rodada._"]
    return redact("\n".join(linhas))


def render_report(day: DayAudit, verdict: str, material: str, proposals=()) -> str:
    """Relatório completo gravado em disco, com o material que embasou o veredito."""
    # `material` já sai redigido de `compile_material`; redigir de novo só
    # destruiria o que a primeira passada preservou de propósito. O veredito, sim,
    # vem de fora e pode repetir um documento que o modelo leu.
    propostas = render_proposals(proposals) if proposals else ""
    texto = propostas or redact(str(verdict or "").strip()) \
        or "_Auditor sem veredito nesta rodada._"
    partes = [
        f"# Auditoria — {day.day.isoformat()}",
        "",
        "## Veredito do auditor",
        texto,
    ]
    tickets = [render_code_ticket(p, day.day) for p in proposals or ()]
    tickets = [t for t in tickets if t]
    if tickets:
        # Prontos para copiar para a base de tickets: a criação automática não é
        # feita daqui de propósito (ver o comentário em `_run_daily_audit`).
        partes += ["", "---", "", "## Tickets de código propostos", ""]
        partes.append("\n\n".join(tickets))
    partes += ["", "---", "", material]
    return "\n".join(partes)


_LOG_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})T\d{2}:\d{2}:\d{2}")


def read_day_log_lines(log_path, day: date) -> list[str]:
    """Linhas do dia no arquivo espelho do log do plugin, incluindo os rotacionados.

    A rotação parte o dia em dois arquivos; ler só o atual perderia a manhã.
    Linha sem timestamp próprio (traceback, mensagem multi-linha) acompanha a
    última linha datada — descartá-la cortaria o evento ao meio.
    """
    base = Path(log_path)
    arquivos = sorted(
        (p for p in base.parent.glob(base.name + "*") if p.is_file()),
        key=lambda p: p.name,
        reverse=True,
    ) if base.parent.is_dir() else []

    alvo = day.isoformat()
    colhidas: list[str] = []
    for arquivo in arquivos:
        try:
            conteudo = arquivo.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        dentro = False
        for linha in conteudo.splitlines():
            match = _LOG_DATE_RE.match(linha)
            if match:
                dentro = match.group(1) == alvo
            if dentro:
                colhidas.append(linha)
    return colhidas


def split_owner_manual(turns, events) -> tuple[list[Turn], list[Turn]]:
    """Separa o que a AYA enviou do que o dono digitou na conversa do lead.

    `from_me=1` no banco é ambíguo: o bridge tem dois writers para mensagem
    própria (`persistLiveMessage`, sem guarda, e o writer de mensagem manual do
    dono), ambos `INSERT OR IGNORE` contra o mesmo índice único — quem grava
    primeiro vence, então `message_type` é corrida e não separa nada.

    O log `[human-send]` é a autoridade: registra o chat e o tamanho de cada
    bolha que a AYA realmente enviou. Casa-se tamanho a tamanho, consumindo, para
    que bolhas repetidas não atribuam demais.

    A decisão é POR CONVERSA. Numa conversa em que o log registrou envios, um
    turno próprio sem tamanho correspondente é do dono. Numa conversa sem nenhum
    envio registrado não há como discriminar — e aí tudo é mantido, porque o
    contrário faria um log rotacionado ou truncado zerar o dia em silêncio, que é
    pior do que superestimar.
    """
    disponiveis: dict[str, list[int]] = {}
    for evento in events:
        if evento.tag != "human-send" or "sizes" not in evento.fields:
            continue
        tamanhos = [int(n) for n in re.findall(r"\d+", evento.fields["sizes"])]
        disponiveis.setdefault(evento.chat_id, []).extend(tamanhos)

    do_bot: list[Turn] = []
    do_dono: list[Turn] = []
    for turn in turns:
        if not turn.from_me:
            do_bot.append(turn)
            continue
        if turn.chat_id not in disponiveis:
            do_bot.append(turn)
            continue
        restantes = disponiveis[turn.chat_id]
        tamanho = len(turn.body)
        if tamanho in restantes:
            restantes.remove(tamanho)
            do_bot.append(turn)
        else:
            do_dono.append(turn)
    return do_bot, do_dono


def language_hint_scoreboard(day: DayAudit) -> dict:
    """Mede se a dica determinística de idioma funciona, em três baldes.

    A regra medida nesta base é "instruir não funciona, filtrar funciona", e esta
    é a medição que decide o caso da dica de idioma:

    - `dica_ignorada`: houve troca de idioma E a dica estava no prompt. Instrução
      emitida e desobedecida — o caso é de filtro de saída, não de mais texto.
    - `sem_dica`: houve troca e a dica não foi emitida. Falta sinal, e
      `fontes_sem_dica` diz se o limite foi o detector ("nenhuma") ou o cadastro.
    - `dica_funcionou`: a dica foi emitida e o turno saiu no idioma certo.

    A correlação é pela dica mais recente da MESMA conversa emitida ATÉ a hora do
    turno: uma dica posterior não pode explicar uma resposta anterior.
    """
    def dica_ate(chat_id: str, quando: datetime) -> AuditEvent | None:
        anteriores = [
            e for e in day.language_hints
            if e.chat_id == chat_id and e.at <= quando
        ]
        return max(anteriores, key=lambda e: e.at) if anteriores else None

    placar = {
        "dica_ignorada": 0,
        "sem_dica": 0,
        "dica_funcionou": 0,
        "fontes_sem_dica": {},
    }

    trocas = {(m.chat_id, m.at) for m in day.language_mismatches}
    for resposta in day.replies:
        dica = dica_ate(resposta.chat_id, resposta.at)
        tem_dica = bool(dica and dica.fields.get("hint") == "True")
        trocou = (resposta.chat_id, resposta.at) in trocas
        if trocou and tem_dica:
            placar["dica_ignorada"] += 1
        elif trocou:
            fonte = (dica.fields.get("fonte") if dica else None) or "sem registro"
            placar["sem_dica"] += 1
            placar["fontes_sem_dica"][fonte] = placar["fontes_sem_dica"].get(fonte, 0) + 1
        elif tem_dica:
            placar["dica_funcionou"] += 1
    return placar


# `gateway.log` é a única fonte de latência do modelo e de `api_calls`. Não tem o
# prefixo `[whatsapp-manager]`, usa hora local e o `chat=` vem sem aspas.
_GATEWAY_READY_RE = re.compile(
    r"^(?P<at>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+\s+\w+\s+"
    r"gateway\.run: response ready: platform=(?P<platform>\S+)\s+"
    r"chat=(?P<chat>\S+)\s+time=(?P<time>[\d.]+)s\s+api_calls=(?P<calls>\d+)"
)


@dataclass
class GatewayTurn:
    chat_id: str
    at: datetime
    seconds: float
    api_calls: int


def parse_gateway_lines(lines) -> list[GatewayTurn]:
    """Turnos do gateway com latência do modelo e número de chamadas de API."""
    turnos: list[GatewayTurn] = []
    for line in lines:
        match = _GATEWAY_READY_RE.match(str(line or "").strip())
        if not match or match.group("platform") != "whatsapp":
            continue
        try:
            quando = datetime.fromisoformat(match.group("at")).replace(tzinfo=business_tz())
        except ValueError:
            continue
        turnos.append(GatewayTurn(
            chat_id=match.group("chat"),
            at=quando,
            seconds=float(match.group("time")),
            api_calls=int(match.group("calls")),
        ))
    return turnos


def read_gateway_day_lines(log_path, day: date) -> list[str]:
    """Linhas do gateway.log do dia. Formato de data diferente do log do plugin."""
    caminho = Path(log_path)
    if not caminho.is_file():
        return []
    alvo = day.isoformat()
    try:
        conteudo = caminho.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return [l for l in conteudo.splitlines() if l.startswith(alvo)]


def reply_latencies(replies) -> list[float]:
    """Segundos entre a pergunta do lead e a primeira bolha da resposta.

    Diferente do `time=` do gateway, que mede só o modelo: esta inclui debounce e
    a espera humanizada do `_human_send` — é o tempo que o lead sente. Turno sem
    pergunta antes (follow-up) não entra: não há o que medir.
    """
    return [
        (r.at - r.lead_at).total_seconds()
        for r in replies
        if r.lead_at is not None and r.at >= r.lead_at
    ]


# --- Fase 2: propostas com portão ---------------------------------------------
#
# O tipo da proposta define o portão, e os limites abaixo são decisões de
# segurança do desenho, não preferência:
#
# - DADO é o único aplicável por sim/não no chat do dono, e mesmo assim só em
#   campo de dado de operação.
# - PROMPT nunca é automático: o texto vai direto para o prompt de produção sem
#   suíte cobrindo, e a regra medida aqui é que instruir não funciona.
# - CODIGO nunca é automático: o agente de atendimento não se automodifica.
#   Contaminado com poder de escrita, reescreveria as próprias guardas.
#
# `pix_key` e `link` ficam fora do aplicável mesmo sendo campo de catálogo: são
# destino de dinheiro e de tráfego. Errar a chave manda o pagamento do cliente
# para a conta errada, e um "sim" distraído não é consentimento suficiente para
# isso — esses dois o dono edita à mão.
CONTACT_FIELDS_APPLICABLE = {"notes"}
CATALOG_FIELDS_APPLICABLE = {"name", "description", "price", "delivery_fee"}
CATALOG_FIELDS_OWNER_ONLY = {"pix_key", "link"}


@dataclass
class Proposal:
    kind: str            # dado | prompt | codigo | nota
    title: str = ""
    evidence: str = ""
    proposal: str = ""
    target: dict = field(default_factory=dict)
    applicable: bool = False
    reason: str = ""


def _verdict_json(text: str) -> dict | None:
    bruto = str(text or "").strip()
    if not bruto:
        return None
    cerca = re.search(r"```(?:json)?\s*(.+?)```", bruto, re.S)
    if cerca:
        bruto = cerca.group(1).strip()
    inicio, fim = bruto.find("{"), bruto.rfind("}")
    if inicio == -1 or fim <= inicio:
        return None
    try:
        dados = json.loads(bruto[inicio:fim + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    return dados if isinstance(dados, dict) else None


def _judge_applicability(kind: str, target: dict) -> tuple[bool, str]:
    if kind != "dado":
        return False, f"tipo {kind} não é aplicável por aqui"
    alvo = str(target.get("tipo") or "").lower()
    campo = str(target.get("campo") or "")
    if alvo == "contato":
        if campo in CONTACT_FIELDS_APPLICABLE:
            return True, ""
        return False, f"campo de contato {campo!r} não é aplicável (auto-gerado ou desconhecido)"
    if alvo == "catalogo":
        if campo in CATALOG_FIELDS_OWNER_ONLY:
            return False, f"{campo} é destino de dinheiro/tráfego — só o dono altera à mão"
        if campo in CATALOG_FIELDS_APPLICABLE:
            return True, ""
        return False, f"campo de catálogo {campo!r} não é aplicável"
    return False, f"alvo {alvo!r} não é dado de operação"


def parse_verdict(text: str) -> list[Proposal]:
    """Converte o veredito do auditor em propostas tipadas com portão.

    Fail-safe: veredito sem estrutura vira uma nota só, sem portão — o dono lê o
    texto cru. Melhor perder a automação de um dia do que aplicar uma proposta
    que ninguém conseguiu ler direito.
    """
    bruto = str(text or "").strip()
    if not bruto:
        return []
    dados = _verdict_json(bruto)
    achados = (dados or {}).get("findings")
    if not isinstance(achados, list) or not achados:
        return [Proposal(kind="nota", proposal=redact(bruto),
                         reason="veredito sem estrutura — sem portão")]

    propostas: list[Proposal] = []
    for achado in achados:
        if not isinstance(achado, dict):
            continue
        tipo = str(achado.get("tipo") or "").strip().lower()
        kind = tipo if tipo in {"dado", "prompt", "codigo"} else "nota"
        alvo = achado.get("alvo") if isinstance(achado.get("alvo"), dict) else {}
        alvo = {k: redact(str(v)) if isinstance(v, str) else v for k, v in alvo.items()}
        aplicavel, motivo = _judge_applicability(kind, alvo)
        propostas.append(Proposal(
            kind=kind,
            title=redact(str(achado.get("titulo") or "")),
            evidence=redact(str(achado.get("evidencia") or "")),
            proposal=redact(str(achado.get("proposta") or "")),
            target=alvo,
            applicable=aplicavel,
            reason=motivo,
        ))
    return propostas


_PROPOSAL_LABEL = {
    "dado": "DADO (aplicável por sim/não)",
    "prompt": "PROMPT (aplique à mão)",
    "codigo": "CÓDIGO (vira ticket)",
    "nota": "NOTA",
}


def render_proposals(proposals) -> str:
    """Propostas do auditor, numeradas, com o portão de cada uma explícito.

    O dono precisa ver POR QUE algo não é aplicável — senão "o auditor sugeriu e
    não fez" vira desconfiança da ferramenta em vez de informação.
    """
    if not proposals:
        return ""
    linhas: list[str] = []
    for indice, proposta in enumerate(proposals, start=1):
        if proposta.kind == "nota" and not proposta.title:
            linhas.append(proposta.proposal)
            continue
        rotulo = _PROPOSAL_LABEL.get(proposta.kind, proposta.kind)
        if proposta.kind == "dado" and not proposta.applicable:
            # Anunciar "aplicável por sim/não" e logo abaixo dizer que só o dono
            # altera é contradição na cara do dono.
            rotulo = "DADO recusado (aplique à mão)"
        linhas.append(f"*{indice}. {proposta.title}* — {rotulo}")
        if proposta.evidence:
            linhas.append(f"_{proposta.evidence}_")
        if proposta.proposal:
            linhas.append(proposta.proposal)
        if not proposta.applicable and proposta.reason and proposta.kind == "dado":
            linhas.append(f"⚠️ {proposta.reason}")
        linhas.append("")
    return "\n".join(linhas).strip()


def render_code_ticket(proposal, day: date) -> str:
    """Corpo de ticket para uma proposta de CODIGO, no ciclo que funcionou.

    O template é o ciclo de 24/08 — achado com texto cru, teste vermelho com a
    frase literal, filtro determinístico, deploy — porque foi ele que rendeu 7
    correções numa rodada. Ticket sem a frase literal chega como opinião, e a
    sessão de dev perde o tempo de reconstruir o caso antes de poder corrigir.
    """
    if getattr(proposal, "kind", "") != "codigo":
        return ""
    evidencia = proposal.evidence or "(sem texto cru no material)"
    return "\n".join([
        f"# {proposal.title or 'Achado do auditor'}",
        "",
        f"Auditoria de {day.isoformat()}.",
        "",
        "## Achado (texto cru)",
        evidencia,
        "",
        "## Teste vermelho",
        "Escrever o caso com a frase literal acima e vê-lo falhar antes de corrigir:",
        f"> {evidencia}",
        "",
        "## Filtro determinístico",
        proposal.proposal or "(proposta não detalhada)",
        "",
        "_Instruir não funciona, filtrar funciona: preferir guarda na saída a mais_",
        "_uma regra no prompt._",
        "",
        "## Deploy",
        "Suíte no container (é o veredito), push na main, `git pull --ff-only` no",
        "clone e `docker restart hermes`.",
    ])


# Opções válidas da base "Tickets — Suporte". Inventar valor de `select` faz a API
# recusar a página inteira, e aí o achado do dia se perde em silêncio.
NOTION_TICKET_TYPE = "Melhoria"
NOTION_TICKET_ORIGIN = "Interno"
# "Triagem", não "Aberto": ticket criado por máquina ainda não foi aceito por
# ninguém, e entrar como Aberto mente sobre o estado dele.
NOTION_TICKET_STATUS = "Triagem"
NOTION_TICKET_PRIORITY = "Média"
_NOTION_TEXT_CAP = 1900  # a API corta em 2000; folga para não raspar o limite


def _rich_text(texto: str) -> list[dict]:
    """Quebra em pedaços que cabem num rich_text da API."""
    bruto = str(texto or "")
    if not bruto:
        return [{"type": "text", "text": {"content": ""}}]
    return [
        {"type": "text", "text": {"content": bruto[i:i + _NOTION_TEXT_CAP]}}
        for i in range(0, len(bruto), _NOTION_TEXT_CAP)
    ]


def _notion_blocks(corpo: str) -> list[dict]:
    blocos: list[dict] = []
    for linha in corpo.splitlines():
        texto = linha.rstrip()
        if not texto.strip():
            continue
        if texto.startswith("## "):
            blocos.append({"object": "block", "type": "heading_2",
                           "heading_2": {"rich_text": _rich_text(texto[3:])}})
        elif texto.startswith("# "):
            continue  # o título já é a propriedade Ticket
        elif texto.startswith("> "):
            blocos.append({"object": "block", "type": "quote",
                           "quote": {"rich_text": _rich_text(texto[2:])}})
        else:
            blocos.append({"object": "block", "type": "paragraph",
                           "paragraph": {"rich_text": _rich_text(texto)}})
    return blocos


# A partir desta versão o Notion suporta base com múltiplas data sources, e o
# pai da página passa a ser `data_source_id`. Numa base assim, a API antiga
# devolve 400 validation_error — que NÃO é 404 e por isso não se confunde com
# "não compartilhada".
NOTION_DATA_SOURCE_VERSION = "2025-09-03"


def notion_ticket_payload(
    proposal, day: date, database_id: str, *, api_version: str = "2022-06-28"
) -> dict | None:
    """Corpo pronto para `POST /v1/pages` na base de tickets.

    Puro de propósito: montar o payload é o que erra na prática (opção de select
    inexistente, bloco acima do limite), e isso dá para travar em teste sem rede.

    O corpo já vem de `render_code_ticket`, que passa por `redact` — o TKT-1
    desta mesma base é "credenciais em texto aberto no Notion", e o auditor não
    pode piorar isso.
    """
    corpo = render_code_ticket(proposal, day)
    if not corpo or not str(database_id or "").strip():
        return None
    titulo = proposal.title or "Achado do auditor"
    descricao = redact(
        f"Auditoria de {day.isoformat()}: {proposal.proposal or proposal.title}"
    )[:_NOTION_TEXT_CAP]
    alvo = str(database_id).strip()
    pai = (
        {"data_source_id": alvo}
        if str(api_version) >= NOTION_DATA_SOURCE_VERSION
        else {"database_id": alvo}
    )
    return {
        "parent": pai,
        "properties": {
            "Ticket": {"title": [{"type": "text", "text": {"content": titulo[:_NOTION_TEXT_CAP]}}]},
            "Descrição": {"rich_text": _rich_text(descricao)},
            "Tipo": {"select": {"name": NOTION_TICKET_TYPE}},
            "Origem": {"select": {"name": NOTION_TICKET_ORIGIN}},
            "Status": {"select": {"name": NOTION_TICKET_STATUS}},
            "Prioridade": {"select": {"name": NOTION_TICKET_PRIORITY}},
        },
        "children": _notion_blocks(corpo),
    }
