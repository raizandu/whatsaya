"""Auditor diário do atendimento da WhatsAYA.

Módulo puro: não envia mensagem, não chama LLM e não conhece o gateway. Lê linhas
de log e linhas de banco, agrega o dia e produz o material de auditoria. Quem
agenda, chama o modelo e entrega ao dono é o `whatsapp_manager` / o tick de cron.
"""
from __future__ import annotations

import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# O dia do relatório é o dia comercial do dono, não o dia UTC: 23h de Goiânia
# ainda é o mesmo expediente, e por UTC cairia no dia seguinte.
BUSINESS_TZ = ZoneInfo("America/Sao_Paulo")


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
}

_TAG_RE = re.compile(r"\[([a-z][a-z-]*)\]")
# O JID aparece ora em campo nomeado (`chat=...`), ora solto no meio da frase
# (`[handoff] dono avisado sobre '55...@s.whatsapp.net'`).
_JID_RE = re.compile(r"(\d{6,}(?:[:-]\d+)?@[a-z.]+)")


@dataclass
class AuditEvent:
    tag: str
    fields: dict[str, str] = field(default_factory=dict)
    raw: str = ""
    chat_id: str = ""


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
        eventos.append(AuditEvent(
            tag=tag,
            fields=_parse_fields(tail, LOG_FIELDS[tag]),
            raw=line,
            chat_id=jid.group(1) if jid else "",
        ))
    return eventos


@dataclass
class Turn:
    chat_id: str
    at: datetime
    from_me: bool
    body: str


def _day_bounds(day: date) -> tuple[float, float]:
    start = datetime.combine(day, datetime.min.time(), tzinfo=BUSINESS_TZ)
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
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return []
    try:
        rows = conn.execute(
            """
            SELECT chat_id, timestamp, from_me, body
            FROM messages
            WHERE is_historical = 0 AND body IS NOT NULL AND TRIM(body) != ''
            ORDER BY COALESCE(timestamp, 0) DESC LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()

    turnos = [
        Turn(
            chat_id=str(chat_id),
            at=datetime.fromtimestamp(float(ts), BUSINESS_TZ),
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


def group_replies(turns) -> list[Reply]:
    """Agrupa bolhas consecutivas da AYA numa resposta só, por conversa.

    `_human_send` grava uma linha por bolha; sem agrupar, um turno de 4 bolhas
    contaria como 4 respostas e distorceria qualquer placar por resposta.
    """
    abertas: dict[str, Reply] = {}
    ultima_do_lead: dict[str, str] = {}
    respostas: list[Reply] = []
    for turn in sorted(turns, key=lambda t: t.at):
        if not turn.from_me:
            abertas.pop(turn.chat_id, None)
            ultima_do_lead[turn.chat_id] = turn.body
            continue
        aberta = abertas.get(turn.chat_id)
        if aberta is None:
            aberta = Reply(
                chat_id=turn.chat_id,
                at=turn.at,
                lead_message=ultima_do_lead.get(turn.chat_id, ""),
            )
            abertas[turn.chat_id] = aberta
            respostas.append(aberta)
        aberta.bubbles.append(turn.body)
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
    # _PAYMENT_GATE_ASK_MARKET
    "Em qual país sua empresa atua?",
    "Which country does your company operate in?",
    "¿En qué país opera tu empresa?",
    # _PAYMENT_GATE_INTENT_MISSING
    "Posso enviar os dados de pagamento quando você quiser avançar com a contratação.",
    "I can send the payment details when you're ready to move forward.",
    "Puedo enviarte los datos de pago cuando quieras avanzar con la contratación.",
    # _PAYMENT_GATE_OFFICIAL_ONLY
    "Vou usar somente os dados de pagamento oficiais do mercado da sua empresa.",
    "I'll only use the official payment details for your company's market.",
    "Solo voy a usar los datos de pago oficiales correspondientes al mercado de tu empresa.",
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


def build_day_audit(day: date, events, turns, infer_language) -> DayAudit:
    """Junta o que o log provou e o que a conversa mostra num retrato do dia."""
    placar = aggregate_events(events)
    respostas = group_replies(turns)
    dia = DayAudit(
        day=day,
        chats=len({t.chat_id for t in turns}),
        lead_messages=sum(1 for t in turns if not t.from_me),
        guard_hits=placar.guard_hits,
        by_chat=placar.by_chat,
        unattributed=placar.unattributed,
        unanswered=placar.unanswered,
        handoffs=placar.handoffs,
        replies=respostas,
        gate_evidence=_gate_evidence(events),
    )
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
        if evento.tag != "payment-gate" or "reason" not in evento.fields:
            continue
        campos = evento.fields
        partes = [f"motivo={_unquote(campos['reason'])}", f"chat={mask_chat(evento.chat_id)}"]
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
    ]

    if day.guard_hits:
        linhas += ["", "## Disparos de guarda (do log)"]
        linhas += [f"- {motivo}: {n}" for motivo, n in sorted(day.guard_hits.items())]
        if day.unattributed:
            linhas.append(f"- sem conversa atribuída (log de geração antiga): {day.unattributed}")

    if day.gate_evidence:
        linhas += ["", "## Evidência parseada do payment-gate", *day.gate_evidence]

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
    if day.unanswered:
        linhas.append(f"⚠️ {len(day.unanswered)} mensagem(ns) sem resposta.")
    texto = str(verdict or "").strip()
    linhas += ["", texto if texto else "_Auditor sem veredito nesta rodada._"]
    return redact("\n".join(linhas))


def render_report(day: DayAudit, verdict: str, material: str) -> str:
    """Relatório completo gravado em disco, com o material que embasou o veredito."""
    texto = str(verdict or "").strip() or "_Auditor sem veredito nesta rodada._"
    return "\n".join([
        f"# Auditoria — {day.day.isoformat()}",
        "",
        "## Veredito do auditor",
        texto,
        "",
        "---",
        "",
        redact(material),
    ])


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
