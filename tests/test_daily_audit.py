"""Testes do auditor diário; não importam o plugin vivo.

As linhas de log são as de produção do dia 24/08 (`docker logs hermes`), com o
JID trocado por um número fictício de mesmo formato. O handler do plugin escreve
`[whatsapp-manager] %(message)s`, então a tag do evento é o SEGUNDO colchete —
e `docker logs -t` ainda acrescenta um timestamp UTC antes de tudo.
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from daily_audit import (
    BUSINESS_TZ,
    build_day_audit,
    compile_material,
    language_hint_scoreboard,
    parse_gateway_lines,
    parse_verdict,
    notion_ticket_payload,
    render_code_ticket,
    render_proposals,
    reply_latencies,
    render_owner_summary,
    render_report,
    Turn,
    aggregate_events,
    FALLBACK_PHRASES,
    FALLBACK_TEMPLATES,
    classify_reply,
    find_format_violations,
    find_language_mismatch,
    group_replies,
    mask_chat,
    split_owner_manual,
    redact,
    parse_log_lines,
    read_day_log_lines,
    read_day_turns,
)


CHAT_A = "556299990000@s.whatsapp.net"
CHAT_B = "556299991111@s.whatsapp.net"
LID_C = "226199502602474@lid"

# `[payment-gate]` derivou três vezes em 48h (o plugin foi redeployado no meio da
# janela). O parser precisa ler as três, senão o relatório do dia do deploy some.
GATE_P1 = (
    "[whatsapp-manager] [payment-gate] resposta comercial substituída "
    "reason=market_mismatch market='US' intent=False methods=['BR']"
)
GATE_P2 = (
    "[whatsapp-manager] [payment-gate] resposta comercial substituída "
    "reason=market_mismatch market='US' intent=False methods=['BR'] "
    "restante=0 payment_content=False unofficial=True"
)
GATE_P3 = (
    "[whatsapp-manager] [payment-gate] resposta comercial substituída "
    "reason=market_mismatch market='US' intent=True markets=['BR'] "
    "prices={'BR': ['397.00', '997.00']} "
    "price_roles={'BR': {'setup': ['997.00'], 'monthly': ['397.00']}} "
    "digits=['official:BR:pix cnpj'] emails=[] restante=0 payment_content=True "
    "unofficial=['destination_without_official_set', 'family_without_official_set', "
    "'label:chave pix (cnpj)', 'unknown_digits', 'wrong_price_amount', 'wrong_price_role']"
)
# Shape atual: `chat=` foi acrescentado para o auditor conseguir fechar o placar
# por conversa — sem ele o disparo de guarda não tem a quem ser atribuído.
GATE_P4 = (
    "[whatsapp-manager] [payment-gate] resposta comercial substituída "
    f"chat='{CHAT_A}' reason=unofficial_details market='BR' intent=True "
    "markets=['BR'] prices={} price_roles={} digits=['official:BR:pix cnpj'] "
    "emails=[] restante=0 payment_content=True unofficial=['unknown_digits']"
)
# `parágrafo de mercado errado removido` é o OUTRO disparo do payment-gate e não
# traz `reason=` — contar só as linhas com reason perdia esse disparo inteiro.
GATE_STRIP = (
    "[whatsapp-manager] [payment-gate] parágrafo de mercado errado removido "
    f"chat='{CHAT_A}' market='US' markets=['BR', 'US'] restante=142"
)
HUMAN_SEND = (
    f"[whatsapp-manager] [human-send] chat='{CHAT_A}' bubbles=4 sizes=[407, 61, 261, 64]"
)
HUMAN_SEND_COM_TS_DO_DOCKER = (
    "2026-08-24T22:42:55.861821119Z "
    f"[whatsapp-manager] [human-send] chat='{CHAT_B}' bubbles=3 sizes=[35, 39, 44]"
)
ONBOARDING = (
    "[whatsapp-manager] [onboarding-gate] pergunta de implantação removida "
    f"chat='{CHAT_A}' n=1 restante=355"
)
# Banner de boot, emitido uma vez por subida do gateway (28 vezes em 48h). Contar
# isso como incidente daria 28 falsos positivos no relatório.
WATCHDOG_BANNER = "[whatsapp-manager] [inbound-watchdog] ativo (alerta em 180s sem resposta)"
WATCHDOG_ALERTA = (
    f"[whatsapp-manager] [inbound-watchdog] mensagem sem resposta chat='{LID_C}' "
    "message_id='3EB075E259' esperando=194s preview='Quero falar com uma pessoa'"
)
HANDOFF = (
    f"[whatsapp-manager] [handoff] dono avisado sobre '{LID_C}' "
    "motivo='lead pediu falar com uma pessoa' message_id='3EB0ABC'"
)
CONTACT_REPLY = (
    "[whatsapp-manager] [contact-reply] enquadramento de rascunho removido do início da resposta"
)
RUIDO = (
    "2026-08-24 20:38:33,732 WARNING tools.mcp_tool: MCP server 'notion' failed "
    "initial authentication, parking until credentials change"
)


LANGUAGE_HINT = (
    "2026-08-24T19:44:00 [whatsapp-manager] [language-hint] "
    f"chat='{CHAT_A}' lead=pt hint=True fonte=mensagem"
)
LANGUAGE_HINT_SEM_SINAL = (
    "2026-08-24T19:50:00 [whatsapp-manager] [language-hint] "
    f"chat='{CHAT_B}' lead=? hint=False fonte=nenhuma"
)


class ParseLogLinesTest(unittest.TestCase):
    def test_guarda_a_hora_da_linha_para_correlacionar_com_o_turno(self):
        (evento,) = parse_log_lines([LANGUAGE_HINT])

        self.assertEqual(evento.at.strftime("%Y-%m-%dT%H:%M:%S"), "2026-08-24T19:44:00")

    def test_linha_sem_hora_fica_sem_hora(self):
        (evento,) = parse_log_lines([HUMAN_SEND])

        self.assertIsNone(evento.at)

    def test_le_a_dica_de_idioma(self):
        a, b = parse_log_lines([LANGUAGE_HINT, LANGUAGE_HINT_SEM_SINAL])

        self.assertEqual((a.fields["lead"], a.fields["hint"], a.fields["fonte"]),
                         ("pt", "True", "mensagem"))
        self.assertEqual((b.fields["lead"], b.fields["fonte"]), ("?", "nenhuma"))

    def test_le_a_tag_no_segundo_colchete(self):
        (evento,) = parse_log_lines([GATE_P4])

        self.assertEqual(evento.tag, "payment-gate")
        self.assertEqual(evento.fields["reason"], "unofficial_details")
        self.assertEqual(evento.chat_id, CHAT_A)

    def test_le_a_linha_com_timestamp_do_docker_logs_t(self):
        (evento,) = parse_log_lines([HUMAN_SEND_COM_TS_DO_DOCKER])

        self.assertEqual(evento.tag, "human-send")
        self.assertEqual(evento.fields["sizes"], "[35, 39, 44]")

    def test_le_as_tres_gerações_do_payment_gate(self):
        p1, p2, p3 = parse_log_lines([GATE_P1, GATE_P2, GATE_P3])

        self.assertEqual(p1.fields["methods"], "['BR']")
        self.assertNotIn("restante", p1.fields)
        self.assertEqual(p2.fields["unofficial"], "True")
        self.assertEqual(p3.fields["markets"], "['BR']")

    def test_nao_quebra_nos_valores_com_virgula_espaco_e_chaves(self):
        # `price_roles` é um dict aninhado e `unofficial` traz rótulos com vírgula
        # e parêntese; um corte ingênuo por espaço truncaria a evidência.
        (evento,) = parse_log_lines([GATE_P3])

        self.assertEqual(
            evento.fields["price_roles"],
            "{'BR': {'setup': ['997.00'], 'monthly': ['397.00']}}",
        )
        self.assertIn("'label:chave pix (cnpj)'", evento.fields["unofficial"])
        self.assertEqual(evento.fields["digits"], "['official:BR:pix cnpj']")

    def test_reconhece_as_seis_tags_e_ignora_o_resto_do_stdout(self):
        eventos = parse_log_lines([
            GATE_P4, HUMAN_SEND, HANDOFF, WATCHDOG_ALERTA,
            ONBOARDING, CONTACT_REPLY, RUIDO,
        ])

        self.assertEqual(
            [e.tag for e in eventos],
            ["payment-gate", "human-send", "handoff",
             "inbound-watchdog", "onboarding-gate", "contact-reply"],
        )

    def test_le_o_formato_do_turno_no_human_send(self):
        (evento,) = parse_log_lines([HUMAN_SEND])

        self.assertEqual(evento.fields["bubbles"], "4")
        self.assertEqual(evento.fields["sizes"], "[407, 61, 261, 64]")

    def test_le_a_espera_do_watchdog_sem_engolir_o_preview(self):
        (evento,) = parse_log_lines([WATCHDOG_ALERTA])

        self.assertEqual(evento.fields["esperando"], "194s")
        self.assertEqual(evento.fields["preview"], "'Quero falar com uma pessoa'")

    def test_atribui_chat_de_jid_lid_e_de_jid_solto_na_frase(self):
        # O `[handoff]` escreve o JID no meio da frase, sem campo nomeado, e o
        # lead pode chegar por `@lid` em vez de `@s.whatsapp.net`.
        (evento,) = parse_log_lines([HANDOFF])

        self.assertEqual(evento.chat_id, LID_C)

    def test_linha_sem_jid_fica_sem_chat(self):
        (evento,) = parse_log_lines([CONTACT_REPLY])

        self.assertEqual(evento.chat_id, "")


class AggregateEventsTest(unittest.TestCase):
    def test_conta_disparo_de_guarda_por_motivo(self):
        placar = aggregate_events(parse_log_lines([GATE_P4, GATE_P3, ONBOARDING]))

        self.assertEqual(placar.guard_hits["payment-gate:unofficial_details"], 1)
        self.assertEqual(placar.guard_hits["payment-gate:market_mismatch"], 1)
        self.assertEqual(placar.guard_hits["onboarding-gate"], 1)

    def test_conta_o_disparo_de_recorte_de_mercado_que_nao_tem_reason(self):
        placar = aggregate_events(parse_log_lines([GATE_STRIP]))

        self.assertEqual(placar.guard_hits["payment-gate:market_strip"], 1)
        self.assertEqual(placar.by_chat[CHAT_A], 1)

    def test_banner_de_boot_do_watchdog_nao_e_incidente(self):
        # 28 dessas em 48h são 28 subidas do gateway, não 28 mensagens sem resposta.
        placar = aggregate_events(parse_log_lines([WATCHDOG_BANNER] * 28))

        self.assertEqual(placar.unanswered, [])

    def test_alerta_real_do_watchdog_conta_como_turno_sem_resposta(self):
        placar = aggregate_events(parse_log_lines([WATCHDOG_BANNER, WATCHDOG_ALERTA]))

        self.assertEqual([u.chat_id for u in placar.unanswered], [LID_C])
        self.assertEqual(placar.unanswered[0].waited_s, 194)

    def test_conta_handoff_entregue(self):
        placar = aggregate_events(parse_log_lines([HANDOFF]))

        self.assertEqual(placar.handoffs, [(LID_C, "lead pediu falar com uma pessoa")])

    def test_atribui_disparos_por_conversa(self):
        placar = aggregate_events(parse_log_lines([GATE_P4, ONBOARDING, GATE_P3]))

        # GATE_P3 é da geração sem `chat=`: entra no total, mas não tem a quem ser
        # atribuído — e some-lo a uma conversa qualquer seria inventar evidência.
        self.assertEqual(placar.by_chat[CHAT_A], 2)
        self.assertEqual(placar.unattributed, 1)


MESSAGES_SCHEMA = """
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL,
    sender_id TEXT,
    sender_name TEXT,
    message_id TEXT NOT NULL,
    message_type TEXT,
    body TEXT,
    timestamp REAL,
    from_me INTEGER NOT NULL DEFAULT 0,
    is_historical INTEGER NOT NULL DEFAULT 0,
    has_media INTEGER NOT NULL DEFAULT 0,
    media_type TEXT,
    sync_type TEXT,
    inserted_at REAL NOT NULL DEFAULT 0
);
"""
CHAT = "5562999990000@s.whatsapp.net"


def _epoch(local_iso: str) -> float:
    return datetime.fromisoformat(local_iso).replace(tzinfo=BUSINESS_TZ).timestamp()


class ReadDayTurnsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="whatsaya-audit-test-")
        self.db = Path(self.tmp.name) / "whatsapp_messages.db"
        conn = sqlite3.connect(self.db)
        conn.executescript(MESSAGES_SCHEMA)
        self.conn = conn

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def insert(self, *, body, ts, from_me=0, chat=CHAT, historical=0, mid=None):
        self.conn.execute(
            "INSERT INTO messages (chat_id, message_id, body, timestamp, from_me, is_historical)"
            " VALUES (?,?,?,?,?,?)",
            (chat, mid or f"m{ts}{from_me}", body, ts, from_me, historical),
        )
        self.conn.commit()

    def test_devolve_so_o_dia_pedido_em_ordem_cronologica(self):
        self.insert(body="ontem", ts=_epoch("2026-08-23T18:00:00"))
        self.insert(body="segunda do dia", ts=_epoch("2026-08-24T09:00:00"))
        self.insert(body="primeira do dia", ts=_epoch("2026-08-24T08:00:00"))
        self.insert(body="amanha", ts=_epoch("2026-08-25T08:00:00"))

        turnos = read_day_turns(self.db, date(2026, 8, 24))

        self.assertEqual([t.body for t in turnos], ["primeira do dia", "segunda do dia"])

    def test_o_dia_e_o_dia_local_do_negocio_nao_o_utc(self):
        # 23h de Goiânia é 02h UTC do dia seguinte: filtrar por UTC jogaria o
        # turno para o dia errado e o relatório do dia sairia furado.
        self.insert(body="fim da noite", ts=_epoch("2026-08-24T23:30:00"))

        self.assertEqual([t.body for t in read_day_turns(self.db, date(2026, 8, 24))],
                         ["fim da noite"])
        self.assertEqual(read_day_turns(self.db, date(2026, 8, 25)), [])

    def test_linha_sem_timestamp_nao_derruba_a_leitura(self):
        # A coluna é nullable no schema real; uma linha sem timestamp não pode
        # ser atribuída a dia nenhum, mas também não pode matar o relatório.
        self.insert(body="sem hora", ts=None)
        self.insert(body="com hora", ts=_epoch("2026-08-24T10:00:00"))

        self.assertEqual([t.body for t in read_day_turns(self.db, date(2026, 8, 24))], ["com hora"])

    def test_ignora_importacao_historica_e_corpo_vazio(self):
        self.insert(body="importada", ts=_epoch("2026-08-24T10:00:00"), historical=1)
        self.insert(body="   ", ts=_epoch("2026-08-24T10:01:00"))
        self.insert(body=None, ts=_epoch("2026-08-24T10:02:00"))
        self.insert(body="real", ts=_epoch("2026-08-24T10:03:00"))

        self.assertEqual([t.body for t in read_day_turns(self.db, date(2026, 8, 24))], ["real"])

    def test_marca_quem_falou(self):
        self.insert(body="quanto custa?", ts=_epoch("2026-08-24T10:00:00"), from_me=0)
        self.insert(body="te explico", ts=_epoch("2026-08-24T10:01:00"), from_me=1)

        lead, aya = read_day_turns(self.db, date(2026, 8, 24))

        self.assertFalse(lead.from_me)
        self.assertTrue(aya.from_me)
        self.assertEqual(lead.chat_id, CHAT)

    def test_dia_antigo_nao_e_cortado_pelo_limite(self):
        # O recorte por dia é feito em Python (WHERE timestamp já devolveu vazio
        # neste banco). Sem restringir a consulta ao dia, reprocessar uma data
        # antiga devolvia "0 conversas" em silêncio assim que o banco passasse
        # do LIMIT — que é justamente o caminho documentado no tick.
        for i in range(60):
            self.insert(body=f"recente {i}", ts=_epoch("2026-08-30T10:00:00") + i)
        self.insert(body="do dia auditado", ts=_epoch("2026-08-24T10:00:00"))

        turnos = read_day_turns(self.db, date(2026, 8, 24), limit=10)

        self.assertEqual([t.body for t in turnos], ["do dia auditado"])

    def test_timestamp_gravado_como_texto_nao_some_do_relatorio(self):
        # Armadilha documentada no handoff: "WHERE timestamp >= devolve vazio".
        # SQLite é dinamicamente tipado — uma linha com timestamp TEXT nunca
        # satisfaz a comparação numérica e sumiria do dia inteiro em silêncio.
        self.conn.execute(
            "INSERT INTO messages (chat_id, message_id, body, timestamp, from_me)"
            " VALUES (?,?,?,?,?)",
            (CHAT, "texto1", "gravada como texto", str(_epoch("2026-08-24T10:00:00")), 0),
        )
        self.conn.commit()

        turnos = read_day_turns(self.db, date(2026, 8, 24))

        self.assertEqual([t.body for t in turnos], ["gravada como texto"])

    def test_banco_ausente_nao_explode(self):
        self.assertEqual(read_day_turns(Path(self.tmp.name) / "nao-existe.db", date(2026, 8, 24)), [])


def _turn(minuto, from_me, body, chat=CHAT_A):
    return Turn(
        chat_id=chat,
        at=datetime(2026, 8, 24, 10, minuto, tzinfo=BUSINESS_TZ),
        from_me=from_me,
        body=body,
    )


class GroupRepliesTest(unittest.TestCase):
    def test_bolhas_seguidas_viram_uma_resposta_so(self):
        # `_human_send` grava uma linha por bolha. Contar bolha como resposta
        # inflaria o placar: um turno de 4 bolhas viraria 4 respostas.
        turnos = [
            _turn(0, False, "quanto custa?"),
            _turn(1, True, "Boa pergunta."),
            _turn(2, True, "Me conta como funciona seu atendimento hoje."),
        ]

        (resposta,) = group_replies(turnos)

        self.assertEqual(resposta.bubbles, ["Boa pergunta.", "Me conta como funciona seu atendimento hoje."])
        self.assertEqual(resposta.chat_id, CHAT_A)

    def test_mensagem_do_lead_no_meio_separa_as_respostas(self):
        turnos = [
            _turn(0, False, "oi"),
            _turn(1, True, "oi, tudo bem?"),
            _turn(2, False, "quanto custa?"),
            _turn(3, True, "depende do plano"),
        ]

        primeira, segunda = group_replies(turnos)

        self.assertEqual(primeira.bubbles, ["oi, tudo bem?"])
        self.assertEqual(segunda.bubbles, ["depende do plano"])

    def test_turnos_separados_por_horas_nao_viram_um_turno_so(self):
        # Sem fechar por tempo, um follow-up agendado horas depois grudava no
        # turno anterior: virava `bolhas_demais` falso, e o trecho anexado
        # apontava para a parte errada da conversa.
        from datetime import timedelta

        primeiro = _turn(0, False, "quanto custa?")
        resposta = _turn(1, True, "te explico")
        followup = _turn(1, True, "passou por aqui?")
        followup.at = resposta.at + timedelta(hours=4)
        turnos = [primeiro, resposta, followup]

        primeira, segunda = group_replies(turnos)

        self.assertEqual(primeira.bubbles, ["te explico"])
        self.assertEqual(segunda.bubbles, ["passou por aqui?"])

    def test_bolhas_do_mesmo_turno_continuam_juntas(self):
        turnos = [_turn(0, False, "oi")] + [_turn(1, True, b) for b in ("a", "b")]

        (resposta,) = group_replies(turnos)

        self.assertEqual(resposta.bubbles, ["a", "b"])

    def test_conversas_diferentes_nao_se_misturam(self):
        turnos = [
            _turn(0, True, "resposta pro A", chat=CHAT_A),
            _turn(1, True, "resposta pro B", chat=CHAT_B),
        ]

        respostas = group_replies(turnos)

        self.assertEqual({r.chat_id for r in respostas}, {CHAT_A, CHAT_B})
        self.assertEqual([len(r.bubbles) for r in respostas], [1, 1])

    def test_guarda_a_pergunta_do_lead_que_originou_a_resposta(self):
        # Sem a mensagem do lead, o trecho anexado ao auditor não tem contexto.
        turnos = [_turn(0, False, "quanto q custa?"), _turn(1, True, "te explico")]

        (resposta,) = group_replies(turnos)

        self.assertEqual(resposta.lead_message, "quanto q custa?")


class ClassifyReplyTest(unittest.TestCase):
    """Placar obrigatório do relatório: guarda salvou × modelo acertou.

    Sem essa distinção o relatório diz "o dia correu bem" quando o que houve foi
    a guarda determinística segurando um erro do modelo a cada turno.
    """

    def _resposta(self, *bolhas, lead="quanto custa?"):
        turnos = [_turn(0, False, lead)]
        turnos += [_turn(i + 1, True, b) for i, b in enumerate(bolhas)]
        (resposta,) = group_replies(turnos)
        return resposta

    def test_frase_de_guarda_conta_como_guarda_salvou(self):
        resposta = self._resposta(
            "Me conta como funciona seu atendimento hoje que eu te explico como a AYA se encaixa."
        )

        self.assertEqual(classify_reply(resposta), "guarda")

    def test_resposta_propria_do_modelo_conta_como_modelo(self):
        resposta = self._resposta("A AYA atende seus clientes no WhatsApp 24h por dia.")

        self.assertEqual(classify_reply(resposta), "modelo")

    def test_uma_bolha_de_guarda_no_meio_do_turno_marca_o_turno_inteiro(self):
        # `_payment_gate_fallback` devolve "sobra do modelo + frase da guarda":
        # o turno saiu porque a guarda entrou, mesmo com texto do modelo junto.
        resposta = self._resposta(
            "A AYA responde na hora.",
            "Vou usar somente os dados de pagamento oficiais do mercado da sua empresa.",
        )

        self.assertEqual(classify_reply(resposta), "guarda")

    def test_reconhece_a_frase_de_guarda_nos_tres_idiomas(self):
        for frase in (
            "Which country does your company operate in?",
            "¿En qué país opera tu empresa?",
            "Em qual país sua empresa atua?",
        ):
            with self.subTest(frase=frase):
                self.assertEqual(classify_reply(self._resposta(frase)), "guarda")

    def test_linha_de_preco_da_guarda_e_reconhecida_apesar_do_valor_variavel(self):
        # `_MARKET_PRICE_SENTENCE` é template ("{setup} de implantação e {monthly}
        # por mês"): casar literal nunca bateria.
        resposta = self._resposta("R$ 997,00 de implantação e R$ 397,00 por mês.")

        self.assertEqual(classify_reply(resposta), "guarda")

    def test_acento_e_caixa_nao_mudam_a_classificacao(self):
        resposta = self._resposta("EM QUAL PAIS SUA EMPRESA ATUA?")

        self.assertEqual(classify_reply(resposta), "guarda")


class FallbackCatalogDriftTest(unittest.TestCase):
    """O catálogo de frases é dono do auditor, mas as frases são do plugin.

    Este teste é a única coisa que impede as duas cópias de divergirem em
    silêncio — se alguém reescrever uma frase da guarda no plugin, o auditor
    passaria a contar aquele turno como "modelo acertou" e o placar mentiria.
    """

    def test_toda_frase_de_guarda_do_plugin_esta_no_catalogo(self):
        import whatsapp_manager as wm

        do_plugin: set[str] = set()
        for nome in (
            "_NO_PRICE_CONTINUATION", "_NO_PRICE_CONTINUATION_REPEAT",
            "_OFFICIAL_PAYMENT_INTRO", "_ONBOARDING_GATE_FALLBACK",
            "_MARKET_PRICE_SENTENCE", "_PAYMENT_GATE_ASK_MARKET",
            "_PAYMENT_GATE_INTENT_MISSING", "_PAYMENT_GATE_OFFICIAL_ONLY",
            "_PAYMENT_RECEIPT_ASK", "_PAYMENT_CLAIMED_RECEIPT",
        ):
            do_plugin.update(getattr(wm, nome).values())
        for por_mercado in wm._MARKET_CORRECTION_LINE.values():
            do_plugin.update(por_mercado.values())

        catalogo = set(FALLBACK_PHRASES) | set(FALLBACK_TEMPLATES)
        self.assertEqual(do_plugin - catalogo, set())


class FormatViolationsTest(unittest.TestCase):
    """Regras de formato que o QA de 24/08 fixou para o que chega ao lead."""

    def _resposta(self, *bolhas, lead="oi"):
        turnos = [_turn(0, False, lead)]
        turnos += [_turn(i + 1, True, b) for i, b in enumerate(bolhas)]
        (resposta,) = group_replies(turnos)
        return resposta

    def test_turno_de_quatro_bolhas_em_conversa_comum_e_violacao(self):
        # Caso real do log de 24/08: bubbles=4 sizes=[407, 61, 261, 64].
        resposta = self._resposta("a" * 407, "b" * 61, "c" * 261, "d" * 64)

        tipos = {v.kind for v in find_format_violations(resposta)}

        self.assertIn("bolhas_demais", tipos)

    def test_bolha_acima_de_400_caracteres_e_violacao(self):
        resposta = self._resposta("x" * 401)

        (violacao,) = find_format_violations(resposta)

        self.assertEqual(violacao.kind, "bolha_longa")
        self.assertEqual(violacao.detail, "401")

    def test_bolha_de_400_exatos_passa(self):
        self.assertEqual(find_format_violations(self._resposta("x" * 400)), [])

    def test_lista_em_conversa_comum_e_violacao(self):
        resposta = self._resposta("A AYA faz:\n- atende\n- qualifica\n- agenda")

        tipos = {v.kind for v in find_format_violations(resposta)}

        self.assertIn("lista_em_conversa_comum", tipos)

    def test_dados_de_pagamento_podem_passar_de_tres_bolhas_e_usar_lista(self):
        # O bloco oficial é entregue em lista de propósito; cobrá-lo pelas regras
        # de conversa comum encheria o relatório de falso positivo todo dia.
        resposta = self._resposta(
            "Perfeito — seguem os dados oficiais para o pagamento:",
            "Chave Pix (CNPJ): informada no bloco oficial",
            "- Banco: informado\n- Titular: informado",
            "Me avisa quando fizer que eu confirmo.",
            lead="como faço o pagamento?",
        )

        self.assertEqual(find_format_violations(resposta), [])

    def test_turno_normal_de_tres_bolhas_curtas_nao_gera_achado(self):
        self.assertEqual(find_format_violations(self._resposta("oi", "tudo bem?", "como posso ajudar?")), [])


class LanguageMismatchTest(unittest.TestCase):
    """Em 24/08 o modelo respondeu em inglês a lead que escrevia em português.

    O detector é injetado: quem sabe inferir idioma é o plugin
    (`_infer_message_language`), e duplicar a heurística aqui criaria duas
    respostas diferentes para a mesma pergunta.
    """

    def _resposta(self, lead, *bolhas):
        turnos = [_turn(0, False, lead)]
        turnos += [_turn(i + 1, True, b) for i, b in enumerate(bolhas)]
        (resposta,) = group_replies(turnos)
        return resposta

    def test_usa_o_detector_do_plugin_como_fonte_unica(self):
        import whatsapp_manager as wm

        resposta = self._resposta(
            "oi, quero saber quanto custa o serviço",
            "Hello! I can send the payment details when you're ready to move forward.",
        )

        achado = find_language_mismatch(resposta, wm._infer_message_language)

        self.assertIsNotNone(achado)
        self.assertEqual((achado.lead_language, achado.reply_language), ("pt", "en"))

    def test_mesma_lingua_nao_gera_achado(self):
        import whatsapp_manager as wm

        resposta = self._resposta(
            "oi, quero saber quanto custa o serviço",
            "Oi! Me conta como funciona seu atendimento hoje.",
        )

        self.assertIsNone(find_language_mismatch(resposta, wm._infer_message_language))

    def test_mensagem_ambigua_nao_vira_achado(self):
        # "ok" não identifica idioma; acusar troca aqui seria inventar problema.
        detector = lambda _texto: None

        self.assertIsNone(find_language_mismatch(self._resposta("ok", "ok"), detector))

    def test_so_o_lead_identificavel_nao_basta(self):
        detector = lambda texto: "pt" if "quero" in texto else None

        resposta = self._resposta("quero saber o preço", "...")

        self.assertIsNone(find_language_mismatch(resposta, detector))


class RedactTest(unittest.TestCase):
    """Regra de segurança do handoff: valor de credencial nunca vai ao auditor.

    O que vale é a classificação do log (`official:`/`unknown:`), não o valor.
    """

    def test_cnpj_com_pontuacao_nao_sobrevive(self):
        saida = redact("Pode pagar no CNPJ 44.249.819/0001-62, tudo certo?")

        self.assertNotIn("44249819", saida.replace(".", "").replace("/", "").replace("-", ""))
        self.assertNotIn("44.249.819", saida)
        self.assertIn("[dígitos:14]", saida)

    def test_cpf_e_telefone_tambem_somem(self):
        saida = redact("meu cpf é 123.456.789-09 e o zap é +55 62 99999-0000")

        self.assertNotIn("123456789", saida.replace(".", "").replace("-", ""))
        self.assertNotIn("999990000", saida.replace("-", "").replace(" ", ""))

    def test_email_vira_marcador(self):
        saida = redact("manda pro financeiro@empresa.com.br por favor")

        self.assertNotIn("financeiro@empresa.com.br", saida)
        self.assertIn("[email]", saida)

    def test_preco_sobrevive_porque_e_a_evidencia_do_achado(self):
        # Apagar "R$ 997,00" destruiria justamente o que o auditor precisa ler
        # para julgar preço errado.
        saida = redact("A implantação fica em R$ 997,00 e a mensalidade R$ 397,00.")

        self.assertIn("R$ 997,00", saida)
        self.assertIn("R$ 397,00", saida)

    def test_credencial_disfarcada_de_preco_nao_escapa(self):
        # A preservação de preço abria um buraco: "R$" na frente de um CNPJ fazia
        # o valor inteiro ser tratado como dinheiro e sair intacto.
        saida = redact("Pague R$ 44.249.819/0001-62 hoje")

        self.assertNotIn("44.249.819", saida)
        self.assertIn("[dígitos:14]", saida)

    def test_preco_com_milhar_e_centavos_continua_intacto(self):
        self.assertEqual(redact("são R$ 1.200,00 no total"), "são R$ 1.200,00 no total")

    def test_data_nao_e_confundida_com_documento(self):
        # "2026-08-24" tem 8 dígitos com separador: caía na mesma regra do CPF e o
        # cabeçalho do relatório saía "# Auditoria do atendimento — [dígitos:8]".
        self.assertEqual(redact("no dia 2026-08-24 às 19:42"), "no dia 2026-08-24 às 19:42")

    def test_texto_comum_passa_intacto(self):
        texto = "Me conta como funciona seu atendimento hoje."

        self.assertEqual(redact(texto), texto)

    def test_chave_pix_aleatoria_nao_sobrevive(self):
        saida = redact("chave pix: 7f3e9a21-4b6c-4d8e-9f10-2a3b4c5d6e7f")

        self.assertNotIn("7f3e9a21", saida)


class MaskChatTest(unittest.TestCase):
    def test_relatorio_nao_cita_numero_completo_do_lead(self):
        # O self-chat do dono já tem o contato; repetir o número inteiro no
        # relatório só aumenta a superfície de vazamento.
        self.assertEqual(mask_chat(CHAT_A), "…0000 (BR)")

    def test_jid_lid_nao_finge_ser_telefone(self):
        self.assertEqual(mask_chat(LID_C), "…2474 (lid)")

    def test_chat_vazio_nao_explode(self):
        self.assertEqual(mask_chat(""), "sem chat")


def _pt(texto):
    """Detector fixo: só distingue pt de en, o bastante para os testes."""
    baixo = texto.lower()
    if any(p in baixo for p in ("quanto", "custa", "conta", "atendimento", "oi")):
        return "pt"
    if any(p in baixo for p in ("hello", "payment", "ready", "forward")):
        return "en"
    return None


class BuildDayAuditTest(unittest.TestCase):
    def test_placar_separa_guarda_de_modelo(self):
        turnos = [
            _turn(0, False, "quanto custa?"),
            _turn(1, True, "Me conta como funciona seu atendimento hoje que eu te explico como a AYA se encaixa."),
            _turn(2, False, "e como funciona?"),
            _turn(3, True, "A AYA responde seus clientes no WhatsApp."),
        ]

        dia = build_day_audit(date(2026, 8, 24), [], turnos, _pt)

        self.assertEqual((dia.replies_guard, dia.replies_model), (1, 1))

    def test_reune_achados_de_formato_idioma_e_log(self):
        turnos = [
            _turn(0, False, "quanto custa?"),
            _turn(1, True, "Hello! I can send the payment details when you're ready to move forward."),
            _turn(2, True, "x" * 401),
        ]
        eventos = parse_log_lines([GATE_P4, WATCHDOG_ALERTA, HANDOFF])

        dia = build_day_audit(date(2026, 8, 24), eventos, turnos, _pt)

        self.assertEqual(dia.guard_hits["payment-gate:unofficial_details"], 1)
        self.assertEqual([v.kind for v in dia.format_violations], ["bolha_longa"])
        self.assertEqual([m.reply_language for m in dia.language_mismatches], ["en"])
        self.assertEqual(len(dia.unanswered), 1)
        self.assertEqual(len(dia.handoffs), 1)

    def test_conta_conversas_e_mensagens_de_lead_do_dia(self):
        turnos = [
            _turn(0, False, "oi", chat=CHAT_A),
            _turn(1, True, "oi!", chat=CHAT_A),
            _turn(2, False, "oi", chat=CHAT_B),
        ]

        dia = build_day_audit(date(2026, 8, 24), [], turnos, _pt)

        self.assertEqual((dia.chats, dia.lead_messages), (2, 2))

    def test_takeover_do_dono_nao_entra_no_placar_da_aya(self):
        # Durante QA o dono digita muito na conversa do lead. Sem separar, cada
        # mensagem dele virava "turno da AYA" e levava violação de formato.
        turnos = [
            _turn(0, False, "quanto custa?"),
            _turn(1, True, "Em qual país sua empresa atua?"),
            _turn(2, False, "brasil"),
            _turn(3, True, "opa, aqui é o Gustavo, " + "d" * 420),
        ]
        eventos = parse_log_lines([
            f"[whatsapp-manager] [human-send] chat='{CHAT_A}' bubbles=1 sizes=[30]"
        ])

        dia = build_day_audit(date(2026, 8, 24), eventos, turnos, _pt)

        self.assertEqual((dia.replies_guard, dia.replies_model), (1, 0))
        self.assertEqual(dia.format_violations, [])
        self.assertEqual(dia.owner_manual, 1)

    def test_reune_latencia_do_lead_e_do_modelo(self):
        from datetime import timedelta

        lead = _turn(0, False, "quanto custa?")
        resposta = _turn(0, True, "te explico")
        resposta.at = lead.at + timedelta(seconds=30)

        dia = build_day_audit(
            date(2026, 8, 24), [], [lead, resposta], _pt,
            gateway_turns=parse_gateway_lines([GATEWAY_READY, GATEWAY_LENTO]),
        )

        self.assertEqual(dia.lead_latencies, [30.0])
        self.assertEqual(dia.model_seconds, [7.7, 15.3])
        self.assertEqual(dia.api_calls, 4)

    def test_dia_sem_movimento_nao_explode(self):
        dia = build_day_audit(date(2026, 8, 24), [], [], _pt)

        self.assertEqual((dia.chats, dia.replies_guard, dia.replies_model), (0, 0, 0))


class CompileMaterialTest(unittest.TestCase):
    """O material é o que sai desta máquina para um provider externo.

    Duas regras do handoff valem aqui e são a razão destes testes existirem:
    valor de credencial nunca vai junto, e número de lead não vai inteiro.
    """

    def _dia(self, turnos, eventos=()):
        return build_day_audit(date(2026, 8, 24), list(eventos), turnos, _pt)

    def test_credencial_no_texto_da_conversa_nao_sai_da_maquina(self):
        turnos = [
            _turn(0, False, "meu CNPJ é 44.249.819/0001-62"),
            _turn(1, True, "x" * 401),
        ]

        material = compile_material(self._dia(turnos), turnos)

        self.assertNotIn("44.249.819", material)
        self.assertNotIn("44249819", material.replace(".", "").replace("/", "").replace("-", ""))

    def test_numero_do_lead_nao_vai_inteiro(self):
        turnos = [_turn(0, False, "oi"), _turn(1, True, "y" * 401)]

        material = compile_material(self._dia(turnos), turnos)

        self.assertNotIn(CHAT_A, material)
        self.assertNotIn("556299990000", material)
        self.assertIn("…0000", material)

    def test_takeover_do_dono_aparece_no_material(self):
        # O auditor precisa saber que o dono assumiu: turno dele não é falha da
        # AYA, e uma conversa cheia de takeover explica um dia "sem resposta".
        turnos = [
            _turn(0, False, "quanto custa?"),
            _turn(1, True, "manual do dono"),
        ]
        eventos = parse_log_lines([
            f"[whatsapp-manager] [human-send] chat='{CHAT_A}' bubbles=1 sizes=[999]"
        ])

        material = compile_material(self._dia(turnos, eventos), turnos)

        self.assertIn("dono assumiu", material)

    def test_material_diz_se_a_dica_de_idioma_foi_ignorada(self):
        turnos = [
            _turn(0, False, "quanto custa?"),
            _turn(1, True, "Hello, ready to move forward with the payment?"),
        ]
        linhas = [
            f"2026-08-24T10:00:00 [whatsapp-manager] [language-hint] chat='{CHAT_A}' "
            "lead=pt hint=True fonte=mensagem"
        ]

        material = compile_material(self._dia(turnos, parse_log_lines(linhas)), turnos)

        self.assertIn("dica de idioma estava no prompt e foi ignorada: 1", material)

    def test_material_omite_o_placar_de_idioma_quando_nao_ha_o_que_dizer(self):
        turnos = [_turn(0, False, "oi"), _turn(1, True, "oi!")]

        material = compile_material(self._dia(turnos), turnos)

        self.assertNotIn("dica de idioma", material)

    def test_material_traz_a_latencia_pior_e_a_mediana(self):
        from datetime import timedelta

        lead = _turn(0, False, "quanto custa?")
        resposta = _turn(0, True, "te explico")
        resposta.at = lead.at + timedelta(seconds=95)
        dia = build_day_audit(
            date(2026, 8, 24), [], [lead, resposta], _pt,
            gateway_turns=parse_gateway_lines([GATEWAY_LENTO]),
        )

        material = compile_material(dia, [lead, resposta])

        self.assertIn("95", material)
        self.assertIn("15.3", material)
        self.assertIn("api_calls", material)

    def test_traz_o_placar_de_guarda_e_modelo(self):
        turnos = [
            _turn(0, False, "quanto custa?"),
            _turn(1, True, "Em qual país sua empresa atua?"),
        ]

        material = compile_material(self._dia(turnos), turnos)

        self.assertIn("guarda salvou", material)
        self.assertIn("modelo acertou", material)

    def test_traz_o_motivo_do_disparo_da_guarda_vindo_do_log(self):
        turnos = [_turn(0, False, "quero contratar"), _turn(1, True, "ok")]

        material = compile_material(self._dia(turnos, parse_log_lines([GATE_P4])), turnos)

        self.assertIn("payment-gate:unofficial_details", material)

    def test_evidencia_de_credencial_vai_pela_classificacao_do_log(self):
        # `digits=['official:BR:pix cnpj']` prova que a credencial reproduzida era
        # a real — sem que o valor precise viajar.
        turnos = [_turn(0, False, "quero contratar"), _turn(1, True, "ok")]

        material = compile_material(self._dia(turnos, parse_log_lines([GATE_P4])), turnos)

        self.assertIn("official:BR:pix cnpj", material)

    def test_anexa_trecho_da_conversa_para_o_achado(self):
        turnos = [
            _turn(0, False, "quanto custa?"),
            _turn(1, True, "Hello! ready to move forward with the payment?"),
        ]

        material = compile_material(self._dia(turnos), turnos)

        self.assertIn("quanto custa?", material)

    def test_trecho_nao_despeja_a_bolha_inteira(self):
        # Uma bolha de 407 caracteres é o próprio achado; despejá-la inteira só
        # engorda o que vai para o provider externo sem ajudar a julgar.
        turnos = [_turn(0, False, "oi"), _turn(1, True, "z" * 407)]

        material = compile_material(self._dia(turnos), turnos)

        self.assertNotIn("z" * 300, material)
        self.assertIn("…", material)

    def test_o_mesmo_turno_nao_repete_o_trecho_por_achado(self):
        # Quatro bolhas com uma de 407 disparam `bolhas_demais` e `bolha_longa`;
        # sem agrupar, a mesma conversa aparecia duas vezes no material.
        turnos = [_turn(0, False, "oi")] + [
            _turn(i + 1, True, corpo) for i, corpo in enumerate(["z" * 407, "b", "c", "d"])
        ]
        dia = self._dia(turnos)
        material = compile_material(dia, turnos)

        self.assertEqual(len(dia.format_violations), 2)
        self.assertEqual(material.count("> Lead: oi"), 1)

    def test_dia_sem_achado_ainda_produz_material_legivel(self):
        material = compile_material(self._dia([]), [])

        self.assertIn("2026-08-24", material)


class RenderTest(unittest.TestCase):
    def _dia(self):
        turnos = [
            _turn(0, False, "quanto custa?"),
            _turn(1, True, "Em qual país sua empresa atua?"),
            _turn(2, False, "brasil"),
            _turn(3, True, "A AYA responde seus clientes no WhatsApp."),
        ]
        return build_day_audit(date(2026, 8, 24), parse_log_lines([GATE_P4]), turnos, _pt), turnos

    def test_resumo_ao_dono_traz_o_placar_calculado_pelo_codigo(self):
        # O placar nunca vem do modelo: é exatamente o que ele existe para não
        # poder maquiar ("o dia correu bem" quando a guarda segurou cada turno).
        dia, _ = self._dia()

        resumo = render_owner_summary(dia, "1. Preço errado no turno 2.")

        self.assertIn("1 guarda", resumo)
        self.assertIn("1 modelo", resumo)

    def test_resumo_inclui_o_veredito_do_auditor(self):
        dia, _ = self._dia()

        resumo = render_owner_summary(dia, "1. Preço errado no turno 2.")

        self.assertIn("Preço errado no turno 2.", resumo)

    def test_resumo_nao_cita_numero_completo_de_lead(self):
        dia, _ = self._dia()

        resumo = render_owner_summary(dia, "tudo certo")

        self.assertNotIn("556299990000", resumo)

    def test_resumo_avisa_quando_o_auditor_nao_respondeu(self):
        # Sem veredito o relatório ainda vale: o placar é determinístico.
        dia, _ = self._dia()

        resumo = render_owner_summary(dia, "")

        self.assertIn("sem veredito", resumo.lower())
        self.assertIn("1 guarda", resumo)

    def test_relatorio_completo_guarda_o_material_para_conferencia(self):
        dia, turnos = self._dia()
        material = compile_material(dia, turnos)

        completo = render_report(dia, "achado 1", material)

        self.assertIn("achado 1", completo)
        self.assertIn("## Números do dia", completo)
        self.assertIn("2026-08-24", completo)

    def test_relatorio_completo_preserva_a_data_do_dia(self):
        dia, turnos = self._dia()

        completo = render_report(dia, "veredito", compile_material(dia, turnos))

        self.assertIn("# Auditoria do atendimento — 2026-08-24", completo)
        self.assertNotIn("[dígitos:8]", completo)

    def test_veredito_do_modelo_tambem_passa_pela_redacao(self):
        # O veredito vem de fora: se o modelo repetir um documento visto no
        # material, ele não pode chegar ao disco nem ao dono.
        dia, turnos = self._dia()

        completo = render_report(dia, "vazou 44.249.819/0001-62 aqui", compile_material(dia, turnos))

        self.assertNotIn("44.249.819", completo)

    def test_relatorio_completo_tambem_e_redigido(self):
        turnos = [_turn(0, False, "CNPJ 44.249.819/0001-62"), _turn(1, True, "ok")]
        dia = build_day_audit(date(2026, 8, 24), [], turnos, _pt)

        completo = render_report(dia, "veredito", compile_material(dia, turnos))

        self.assertNotIn("44.249.819", completo)


class BusinessTimezoneTest(unittest.TestCase):
    def test_o_fuso_do_relatorio_acompanha_o_tz_do_container(self):
        # O log é escrito em hora local do container e os turnos eram recortados
        # em São Paulo: com TZ diferente, as duas metades do relatório cobriam
        # janelas deslocadas.
        import os
        from unittest.mock import patch

        import daily_audit

        with patch.dict(os.environ, {"TZ": "America/New_York"}):
            self.assertEqual(str(daily_audit.business_tz()), "America/New_York")

    def test_sem_tz_continua_no_fuso_do_negocio(self):
        import os
        from unittest.mock import patch

        import daily_audit

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TZ", None)
            self.assertEqual(str(daily_audit.business_tz()), "America/Sao_Paulo")

    def test_tz_invalido_nao_derruba_o_relatorio(self):
        import os
        from unittest.mock import patch

        import daily_audit

        with patch.dict(os.environ, {"TZ": "Nao/Existe"}):
            self.assertEqual(str(daily_audit.business_tz()), "America/Sao_Paulo")


class ReadDayLogLinesTest(unittest.TestCase):
    """O coletor lê o arquivo espelho do log do plugin, não `docker logs`:
    o cron roda DENTRO do container, onde `docker` não existe."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="whatsaya-log-")
        self.path = Path(self.tmp.name) / "whatsapp_plugin.log"

    def tearDown(self):
        self.tmp.cleanup()

    def _escreve(self, *linhas):
        self.path.write_text("\n".join(linhas) + "\n", encoding="utf-8")

    def test_recorta_so_as_linhas_do_dia(self):
        self._escreve(
            f"2026-08-23T19:00:00 {HUMAN_SEND}",
            f"2026-08-24T09:00:00 {HUMAN_SEND}",
            f"2026-08-25T09:00:00 {HUMAN_SEND}",
        )

        linhas = read_day_log_lines(self.path, date(2026, 8, 24))

        self.assertEqual(len(linhas), 1)
        self.assertIn("2026-08-24T09:00:00", linhas[0])

    def test_linha_sem_data_acompanha_a_anterior(self):
        # Traceback e mensagem multi-linha não têm timestamp próprio; descartá-las
        # cortaria a linha do evento ao meio.
        self._escreve(
            f"2026-08-24T09:00:00 {WATCHDOG_ALERTA}",
            "  continuação da mensagem",
        )

        linhas = read_day_log_lines(self.path, date(2026, 8, 24))

        self.assertEqual(len(linhas), 2)

    def test_arquivo_ausente_devolve_vazio(self):
        self.assertEqual(read_day_log_lines(Path(self.tmp.name) / "nao-existe.log", date(2026, 8, 24)), [])

    def test_le_tambem_os_arquivos_rotacionados(self):
        # A rotação parte o dia em dois arquivos; ler só o atual perderia a manhã.
        rotacionado = Path(str(self.path) + ".1")
        rotacionado.write_text(f"2026-08-24T08:00:00 {HUMAN_SEND}\n", encoding="utf-8")
        self._escreve(f"2026-08-24T19:00:00 {HUMAN_SEND}")

        linhas = read_day_log_lines(self.path, date(2026, 8, 24))

        self.assertEqual(len(linhas), 2)
        self.assertIn("08:00:00", linhas[0])


class SplitOwnerManualTest(unittest.TestCase):
    """`from_me=1` no banco é AYA **ou** o dono digitando na conversa do lead.

    Os dois writers do bridge usam `INSERT OR IGNORE` contra o mesmo índice
    único, então `message_type` é corrida e não serve para separar. O log
    `[human-send]` é a autoridade: ele registra o chat e o tamanho de cada bolha
    que a AYA realmente enviou.
    """

    def test_bolha_da_aya_e_reconhecida_pelo_tamanho_registrado_no_log(self):
        turnos = [
            _turn(0, False, "quanto custa?"),
            _turn(1, True, "x" * 40),
        ]
        eventos = parse_log_lines([
            f"[whatsapp-manager] [human-send] chat='{CHAT_A}' bubbles=1 sizes=[40]"
        ])

        auditaveis, dono = split_owner_manual(turnos, eventos)

        # A mensagem do lead fica: é ela que dá contexto ao turno.
        self.assertEqual([t.body for t in auditaveis], ["quanto custa?", "x" * 40])
        self.assertEqual(dono, [])

    def test_mensagem_manual_do_dono_nao_conta_como_resposta_da_aya(self):
        # Sem isso, o dono assumindo a conversa vira "turno da AYA" e leva
        # violação de formato pelo que ele mesmo digitou.
        turnos = [
            _turn(0, False, "quanto custa?"),
            _turn(1, True, "x" * 40),
            _turn(2, True, "deixa comigo, eu respondo"),
        ]
        eventos = parse_log_lines([
            f"[whatsapp-manager] [human-send] chat='{CHAT_A}' bubbles=1 sizes=[40]"
        ])

        auditaveis, dono = split_owner_manual(turnos, eventos)

        self.assertEqual([t.body for t in auditaveis], ["quanto custa?", "x" * 40])
        self.assertEqual([t.body for t in dono], ["deixa comigo, eu respondo"])

    def test_bolhas_repetidas_do_mesmo_tamanho_sao_consumidas_uma_a_uma(self):
        turnos = [_turn(i, True, "abc") for i in range(3)]
        eventos = parse_log_lines([
            f"[whatsapp-manager] [human-send] chat='{CHAT_A}' bubbles=2 sizes=[3, 3]"
        ])

        bot, dono = split_owner_manual(turnos, eventos)

        self.assertEqual(len(bot), 2)
        self.assertEqual(len(dono), 1)

    def test_mensagem_do_lead_nunca_e_reclassificada(self):
        turnos = [_turn(0, False, "oi")]

        bot, dono = split_owner_manual(turnos, [])

        self.assertEqual([t.body for t in bot], ["oi"])
        self.assertEqual(dono, [])

    def test_conversa_sem_envio_no_log_mantem_os_turnos(self):
        # Sem nenhum envio registrado não há como discriminar. Descartar aqui
        # faria um log rotacionado zerar o dia em silêncio — pior que
        # superestimar.
        turnos = [_turn(0, False, "oi"), _turn(1, True, "resposta sem log")]

        auditaveis, dono = split_owner_manual(turnos, [])

        self.assertEqual([t.body for t in auditaveis], ["oi", "resposta sem log"])
        self.assertEqual(dono, [])

    def test_a_decisao_e_por_conversa(self):
        # CHAT_A tem envio registrado, então lá o não-correspondente é do dono.
        # CHAT_B não tem nenhum, então lá nada é reclassificado.
        turnos = [
            _turn(0, True, "manual do dono no A", chat=CHAT_A),
            _turn(1, True, "abc", chat=CHAT_A),
            _turn(2, True, "sem log no B", chat=CHAT_B),
        ]
        eventos = parse_log_lines([
            f"[whatsapp-manager] [human-send] chat='{CHAT_A}' bubbles=1 sizes=[3]"
        ])

        auditaveis, dono = split_owner_manual(turnos, eventos)

        self.assertEqual([t.body for t in auditaveis], ["abc", "sem log no B"])
        self.assertEqual([t.body for t in dono], ["manual do dono no A"])


class LanguageHintScoreboardTest(unittest.TestCase):
    """Mede se a dica determinística de idioma funciona.

    A regra da sessão é "instruir não funciona, filtrar funciona". Esta é a
    medição que decide: se o balde "dica presente + idioma trocado" dominar, a
    dica é instrução ignorada e o caso vira filtro de saída.
    """

    def _dia(self, turnos, linhas):
        return build_day_audit(date(2026, 8, 24), parse_log_lines(linhas), turnos, _pt)

    def _turnos_com_troca(self, hora):
        # Lead escreve em pt, AYA responde em en.
        lead = _turn(0, False, "quanto custa?")
        resposta = _turn(hora, True, "Hello, ready to move forward with the payment?")
        return [lead, resposta]

    def test_troca_com_dica_presente_conta_como_dica_ignorada(self):
        linhas = [
            f"2026-08-24T10:00:00 [whatsapp-manager] [language-hint] chat='{CHAT_A}' "
            "lead=pt hint=True fonte=mensagem"
        ]

        placar = language_hint_scoreboard(self._dia(self._turnos_com_troca(1), linhas))

        self.assertEqual(placar["dica_ignorada"], 1)
        self.assertEqual(placar["sem_dica"], 0)

    def test_troca_sem_dica_conta_como_falta_de_sinal(self):
        linhas = [
            f"2026-08-24T10:00:00 [whatsapp-manager] [language-hint] chat='{CHAT_A}' "
            "lead=? hint=False fonte=nenhuma"
        ]

        placar = language_hint_scoreboard(self._dia(self._turnos_com_troca(1), linhas))

        self.assertEqual(placar["dica_ignorada"], 0)
        self.assertEqual(placar["sem_dica"], 1)
        self.assertEqual(placar["fontes_sem_dica"]["nenhuma"], 1)

    def test_dica_presente_sem_troca_conta_como_dica_funcionou(self):
        linhas = [
            f"2026-08-24T10:00:00 [whatsapp-manager] [language-hint] chat='{CHAT_A}' "
            "lead=pt hint=True fonte=cadastro"
        ]
        turnos = [_turn(0, False, "quanto custa?"), _turn(1, True, "Te explico o atendimento.")]

        placar = language_hint_scoreboard(self._dia(turnos, linhas))

        self.assertEqual(placar["dica_funcionou"], 1)
        self.assertEqual(placar["dica_ignorada"], 0)

    def test_usa_a_dica_mais_recente_antes_do_turno(self):
        # Duas montagens de prompt no mesmo chat: vale a que precedeu a resposta.
        linhas = [
            f"2026-08-24T10:00:00 [whatsapp-manager] [language-hint] chat='{CHAT_A}' "
            "lead=pt hint=False fonte=nenhuma",
            f"2026-08-24T10:00:30 [whatsapp-manager] [language-hint] chat='{CHAT_A}' "
            "lead=pt hint=True fonte=mensagem",
        ]

        placar = language_hint_scoreboard(self._dia(self._turnos_com_troca(1), linhas))

        self.assertEqual(placar["dica_ignorada"], 1)

    def test_dica_posterior_ao_turno_nao_conta(self):
        # Uma dica emitida DEPOIS da resposta não pode explicá-la.
        linhas = [
            f"2026-08-24T11:00:00 [whatsapp-manager] [language-hint] chat='{CHAT_A}' "
            "lead=pt hint=True fonte=mensagem"
        ]

        placar = language_hint_scoreboard(self._dia(self._turnos_com_troca(1), linhas))

        self.assertEqual(placar["dica_ignorada"], 0)
        self.assertEqual(placar["sem_dica"], 1)

    def test_dica_de_outra_conversa_nao_e_usada(self):
        linhas = [
            f"2026-08-24T10:00:00 [whatsapp-manager] [language-hint] chat='{CHAT_B}' "
            "lead=pt hint=True fonte=mensagem"
        ]

        placar = language_hint_scoreboard(self._dia(self._turnos_com_troca(1), linhas))

        self.assertEqual(placar["dica_ignorada"], 0)
        self.assertEqual(placar["sem_dica"], 1)

    def test_dia_sem_troca_de_idioma_devolve_placar_zerado(self):
        placar = language_hint_scoreboard(self._dia([], []))

        self.assertEqual(placar["dica_ignorada"], 0)
        self.assertEqual(placar["sem_dica"], 0)


# Formato verbatim do gateway.log (hora LOCAL, diferente do log do plugin).
GATEWAY_READY = (
    "2026-08-24 19:29:00,171 INFO gateway.run: response ready: platform=whatsapp "
    f"chat={CHAT_A} time=7.7s api_calls=1 response=1 chars"
)
GATEWAY_LENTO = (
    "2026-08-24 19:34:59,057 INFO gateway.run: response ready: platform=whatsapp "
    f"chat={CHAT_A} time=15.3s api_calls=3 response=1 chars"
)
GATEWAY_OUTRA_PLATAFORMA = (
    "2026-08-24 19:35:10,000 INFO gateway.run: response ready: platform=email "
    "chat=alguem@example.com time=2.0s api_calls=1 response=1 chars"
)


class ParseGatewayLinesTest(unittest.TestCase):
    """O gateway.log é a única fonte de latência do modelo e de `api_calls`.

    Ele NÃO tem o prefixo do plugin, usa hora local (o log do plugin também, mas
    `docker logs -t` é UTC) e o `chat=` vem sem aspas.
    """

    def test_le_latencia_e_chamadas_de_api(self):
        (turno,) = parse_gateway_lines([GATEWAY_READY])

        self.assertEqual(turno.chat_id, CHAT_A)
        self.assertEqual(turno.seconds, 7.7)
        self.assertEqual(turno.api_calls, 1)
        self.assertEqual(turno.at.strftime("%H:%M:%S"), "19:29:00")

    def test_ignora_plataforma_que_nao_e_whatsapp(self):
        self.assertEqual(parse_gateway_lines([GATEWAY_OUTRA_PLATAFORMA]), [])

    def test_ignora_o_resto_do_gateway_log(self):
        ruido = "2026-08-24 19:29:00,171 INFO gateway.run: something else entirely"

        self.assertEqual(parse_gateway_lines([ruido]), [])

    def test_le_varias_linhas(self):
        turnos = parse_gateway_lines([GATEWAY_READY, GATEWAY_LENTO])

        self.assertEqual([t.seconds for t in turnos], [7.7, 15.3])
        self.assertEqual([t.api_calls for t in turnos], [1, 3])


class ReplyLatenciesTest(unittest.TestCase):
    """Latência ponta a ponta: da mensagem do lead até a primeira bolha da AYA.

    Diferente do `time=` do gateway, que mede só o modelo — esta inclui debounce
    e a espera humanizada do `_human_send`, que é o que o lead sente.
    """

    def test_mede_do_lead_ate_a_primeira_bolha(self):
        from datetime import timedelta

        lead = _turn(0, False, "quanto custa?")
        resposta = _turn(0, True, "te explico")
        resposta.at = lead.at + timedelta(seconds=42)

        (segundos,) = reply_latencies(group_replies([lead, resposta]))

        self.assertEqual(segundos, 42.0)

    def test_turno_sem_mensagem_do_lead_nao_tem_latencia(self):
        # Follow-up parte da AYA: não há pergunta cujo tempo medir.
        (resposta,) = group_replies([_turn(0, True, "passou por aqui?")])

        self.assertEqual(reply_latencies([resposta]), [])


def _veredito(*findings):
    import json as _json
    return _json.dumps({"findings": list(findings)}, ensure_ascii=False)


class ParseVerdictTest(unittest.TestCase):
    """Fase 2: o veredito vira propostas tipadas, e o tipo define o portão.

    DADO é aplicável por sim/não no chat do dono; PROMPT e CODIGO nunca são
    automáticos. As duas decisões de segurança do handoff estão codificadas aqui:
    o agente não se automodifica, e nada de escrita de prompt sem suíte cobrindo.
    """

    def test_proposta_de_dado_em_campo_permitido_e_aplicavel(self):
        (p,) = parse_verdict(_veredito({
            "tipo": "DADO", "titulo": "Contato sem contexto",
            "evidencia": "lead repetiu o ramo 2x",
            "proposta": "anotar o ramo do lead",
            "alvo": {"tipo": "contato", "chat": CHAT_A, "campo": "notes", "valor": "clínica odontológica"},
        }))

        self.assertEqual(p.kind, "dado")
        self.assertTrue(p.applicable)

    def test_proposta_de_prompt_nunca_e_aplicavel(self):
        (p,) = parse_verdict(_veredito({
            "tipo": "PROMPT", "titulo": "Regra ignorada",
            "evidencia": "...", "proposta": "encurtar CONSTRAINTS",
            "alvo": {"tipo": "arquivo", "caminho": "support_rules.md"},
        }))

        self.assertEqual(p.kind, "prompt")
        self.assertFalse(p.applicable)

    def test_proposta_de_codigo_nunca_e_aplicavel(self):
        (p,) = parse_verdict(_veredito({
            "tipo": "CODIGO", "titulo": "Falta guarda",
            "evidencia": "...", "proposta": "filtrar na saída", "alvo": {},
        }))

        self.assertEqual(p.kind, "codigo")
        self.assertFalse(p.applicable)

    def test_dado_apontando_para_arquivo_de_prompt_e_recusado(self):
        # O caminho de escape óbvio: rotular de DADO uma edição de prompt.
        (p,) = parse_verdict(_veredito({
            "tipo": "DADO", "titulo": "ajuste",
            "evidencia": "...", "proposta": "mudar a regra",
            "alvo": {"tipo": "arquivo", "caminho": "SOUL_WHATSAPP.md"},
        }))

        self.assertFalse(p.applicable)
        self.assertIn("alvo", p.reason)

    def test_chave_pix_nunca_e_aplicavel_mesmo_como_dado(self):
        # Errar a chave manda o pagamento do cliente para a conta errada, e o
        # modelo deste sistema já reproduziu credencial real por contaminação.
        (p,) = parse_verdict(_veredito({
            "tipo": "DADO", "titulo": "corrigir pix",
            "evidencia": "...", "proposta": "atualizar a chave",
            "alvo": {"tipo": "catalogo", "chave": "plano-x", "campo": "pix_key", "valor": "outra"},
        }))

        self.assertFalse(p.applicable)
        self.assertIn("pix_key", p.reason)

    def test_link_tambem_fica_fora_do_aplicavel(self):
        (p,) = parse_verdict(_veredito({
            "tipo": "DADO", "titulo": "corrigir link",
            "evidencia": "...", "proposta": "novo link",
            "alvo": {"tipo": "catalogo", "chave": "plano-x", "campo": "link", "valor": "http://x"},
        }))

        self.assertFalse(p.applicable)

    def test_campo_auto_gerado_do_contato_e_recusado(self):
        # `summary`/`tone`/`guidelines` são do classificador; update manual já
        # não os sobrescreve, e o auditor não pode furar essa regra.
        (p,) = parse_verdict(_veredito({
            "tipo": "DADO", "titulo": "resumo",
            "evidencia": "...", "proposta": "reescrever o resumo",
            "alvo": {"tipo": "contato", "chat": CHAT_A, "campo": "summary", "valor": "..."},
        }))

        self.assertFalse(p.applicable)

    def test_tipo_desconhecido_vira_nota_sem_portao(self):
        (p,) = parse_verdict(_veredito({
            "tipo": "OUTRO", "titulo": "x", "evidencia": "y", "proposta": "z", "alvo": {},
        }))

        self.assertEqual(p.kind, "nota")
        self.assertFalse(p.applicable)

    def test_veredito_que_nao_e_json_vira_nota_unica(self):
        # Fail-safe: sem estrutura não há portão, e o dono lê o texto cru.
        propostas = parse_verdict("O dia correu bem, nada relevante.")

        self.assertEqual(len(propostas), 1)
        self.assertEqual(propostas[0].kind, "nota")
        self.assertFalse(propostas[0].applicable)
        self.assertIn("O dia correu bem", propostas[0].proposal)

    def test_json_dentro_de_cerca_markdown_e_lido(self):
        cru = "```json\n" + _veredito({
            "tipo": "CODIGO", "titulo": "t", "evidencia": "e", "proposta": "p", "alvo": {},
        }) + "\n```"

        (p,) = parse_verdict(cru)

        self.assertEqual(p.kind, "codigo")

    def test_veredito_vazio_nao_produz_proposta(self):
        self.assertEqual(parse_verdict(""), [])

    def test_valor_da_proposta_e_redigido(self):
        # O valor vem do modelo e pode repetir um documento lido no material.
        (p,) = parse_verdict(_veredito({
            "tipo": "DADO", "titulo": "anotar",
            "evidencia": "CNPJ 44.249.819/0001-62",
            "proposta": "anotar", "alvo": {"tipo": "contato", "chat": CHAT_A,
                                           "campo": "notes", "valor": "cnpj 44.249.819/0001-62"},
        }))

        self.assertNotIn("44.249.819", p.evidence)
        self.assertNotIn("44.249.819", p.target.get("valor", ""))


class RenderCodeTicketTest(unittest.TestCase):
    """Proposta de CODIGO vira corpo de ticket no ciclo que funcionou em 24/08:
    achado com texto cru → teste vermelho com a frase literal → filtro
    determinístico → deploy. Sem isso o ticket chega como opinião."""

    def _proposta(self):
        (p,) = parse_verdict(_veredito({
            "tipo": "CODIGO", "titulo": "Pergunta de implantação antes da venda",
            "evidencia": "AYA: 'Qual a duração de cada serviço?' para lead sem venda",
            "proposta": "barrar pergunta de implantação na saída quando não há venda",
            "alvo": {},
        }))
        return p

    def test_traz_as_quatro_etapas_do_ciclo(self):
        corpo = render_code_ticket(self._proposta(), date(2026, 8, 24))

        for etapa in ("Achado", "Teste vermelho", "Filtro determinístico", "Deploy"):
            self.assertIn(etapa, corpo)

    def test_usa_a_evidencia_como_frase_literal_do_teste(self):
        corpo = render_code_ticket(self._proposta(), date(2026, 8, 24))

        self.assertIn("Qual a duração de cada serviço?", corpo)

    def test_identifica_o_dia_auditado(self):
        self.assertIn("2026-08-24", render_code_ticket(self._proposta(), date(2026, 8, 24)))

    def test_recusa_proposta_que_nao_e_de_codigo(self):
        (dado,) = parse_verdict(_veredito({
            "tipo": "DADO", "titulo": "t", "evidencia": "e", "proposta": "p",
            "alvo": {"tipo": "contato", "chat": CHAT_A, "campo": "notes", "valor": "v"},
        }))

        self.assertEqual(render_code_ticket(dado, date(2026, 8, 24)), "")

    def test_o_corpo_do_ticket_tambem_e_redigido(self):
        (p,) = parse_verdict(_veredito({
            "tipo": "CODIGO", "titulo": "vazou",
            "evidencia": "modelo escreveu CNPJ 44.249.819/0001-62",
            "proposta": "filtrar", "alvo": {},
        }))

        self.assertNotIn("44.249.819", render_code_ticket(p, date(2026, 8, 24)))


class RenderProposalsLabelTest(unittest.TestCase):
    def test_dado_recusado_nao_se_anuncia_como_aplicavel(self):
        # O rótulo contradizia o próprio aviso logo abaixo ("aplicável por
        # sim/não" seguido de "só o dono altera à mão").
        (p,) = parse_verdict(_veredito({
            "tipo": "DADO", "titulo": "Chave Pix errada", "evidencia": "e",
            "proposta": "trocar a chave",
            "alvo": {"tipo": "catalogo", "chave": "p", "campo": "pix_key", "valor": "x"},
        }))

        texto = render_proposals([p])

        self.assertNotIn("aplicável por sim/não", texto)
        self.assertIn("só o dono altera", texto)

    def test_dado_aceito_continua_anunciado_como_aplicavel(self):
        (p,) = parse_verdict(_veredito({
            "tipo": "DADO", "titulo": "anotar", "evidencia": "e", "proposta": "p",
            "alvo": {"tipo": "contato", "chat": CHAT_A, "campo": "notes", "valor": "v"},
        }))

        self.assertIn("aplicável por sim/não", render_proposals([p]))


class NotionTicketPayloadTest(unittest.TestCase):
    """Corpo do ticket para a base "Tickets — Suporte", montado por código puro.

    Os `select` só aceitam opções que existem no schema — inventar valor faz a
    API recusar a página inteira, e o achado do dia se perde em silêncio.
    """

    def _codigo(self, **over):
        base = {"tipo": "CODIGO", "titulo": "Pergunta de implantação antes da venda",
                "evidencia": "AYA: 'Qual a duração de cada serviço?' para lead sem venda",
                "proposta": "barrar na saída quando não há venda", "alvo": {}}
        base.update(over)
        (p,) = parse_verdict(_veredito(base))
        return p

    def test_usa_o_banco_de_dados_pedido_como_pai(self):
        payload = notion_ticket_payload(self._codigo(), date(2026, 8, 24), "db-123")

        self.assertEqual(payload["parent"], {"database_id": "db-123"})

    def test_versao_nova_da_api_usa_data_source_id(self):
        # A partir de 2025-09-03 o Notion tem base com múltiplas data sources, e
        # `database_id` deixa de bastar: a própria API responde
        # "Databases with multiple data sources are not supported in this API
        # version" — 400, não 404, e por isso não se confunde com permissão.
        payload = notion_ticket_payload(
            self._codigo(), date(2026, 8, 24), "ds-9", api_version="2025-09-03")

        self.assertEqual(payload["parent"], {"data_source_id": "ds-9"})

    def test_versao_antiga_continua_com_database_id(self):
        payload = notion_ticket_payload(
            self._codigo(), date(2026, 8, 24), "db-1", api_version="2022-06-28")

        self.assertEqual(payload["parent"], {"database_id": "db-1"})

    def test_titulo_vai_na_propriedade_title(self):
        payload = notion_ticket_payload(self._codigo(), date(2026, 8, 24), "db")

        titulo = payload["properties"]["Ticket"]["title"][0]["text"]["content"]
        self.assertIn("Pergunta de implantação", titulo)

    def test_usa_apenas_opcoes_que_existem_no_schema(self):
        payload = notion_ticket_payload(self._codigo(), date(2026, 8, 24), "db")
        props = payload["properties"]

        self.assertEqual(props["Tipo"]["select"]["name"], "Melhoria")
        self.assertEqual(props["Origem"]["select"]["name"], "Interno")
        # "Triagem" e não "Aberto": ticket criado por máquina ainda não foi aceito
        # por ninguém, e entrar como Aberto mente sobre o estado dele.
        self.assertEqual(props["Status"]["select"]["name"], "Triagem")
        self.assertIn(props["Prioridade"]["select"]["name"], {"Crítica", "Alta", "Média", "Baixa"})

    def test_descricao_resume_sem_repetir_o_corpo(self):
        payload = notion_ticket_payload(self._codigo(), date(2026, 8, 24), "db")

        desc = payload["properties"]["Descrição"]["rich_text"][0]["text"]["content"]
        self.assertIn("2026-08-24", desc)
        self.assertLessEqual(len(desc), 2000)

    def test_corpo_vira_blocos_com_titulo_de_secao(self):
        payload = notion_ticket_payload(self._codigo(), date(2026, 8, 24), "db")
        tipos = [b["type"] for b in payload["children"]]

        self.assertIn("heading_2", tipos)
        self.assertIn("paragraph", tipos)

    def test_nenhum_bloco_passa_do_limite_da_api(self):
        # rich_text corta em 2000 caracteres; bloco maior faz a API recusar tudo.
        payload = notion_ticket_payload(
            self._codigo(evidencia="x" * 5000), date(2026, 8, 24), "db")

        for bloco in payload["children"]:
            conteudo = bloco[bloco["type"]]["rich_text"]
            for parte in conteudo:
                self.assertLessEqual(len(parte["text"]["content"]), 2000)

    def test_proposta_que_nao_e_de_codigo_nao_vira_ticket(self):
        (dado,) = parse_verdict(_veredito({
            "tipo": "DADO", "titulo": "t", "evidencia": "e", "proposta": "p",
            "alvo": {"tipo": "contato", "chat": CHAT_A, "campo": "notes", "valor": "v"},
        }))

        self.assertIsNone(notion_ticket_payload(dado, date(2026, 8, 24), "db"))

    def test_sem_base_alvo_nao_monta_payload(self):
        self.assertIsNone(notion_ticket_payload(self._codigo(), date(2026, 8, 24), ""))

    def test_credencial_nao_vaza_para_o_notion(self):
        # TKT-1 desta mesma base é "credenciais em texto aberto no Notion".
        # O auditor não pode piorar isso.
        p = self._codigo(evidencia="modelo escreveu CNPJ 44.249.819/0001-62")
        payload = notion_ticket_payload(p, date(2026, 8, 24), "db")

        self.assertNotIn("44.249.819", _json_dump(payload))


def _json_dump(obj):
    import json as _j
    return _j.dumps(obj, ensure_ascii=False)
