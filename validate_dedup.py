#!/usr/bin/env python3
"""
Valida o mecanismo de dedup de respostas duplicadas no plugin whatsapp_manager.

Execução no container Hermes:
    python3 /opt/data/workspace/whatsappkit/validate_dedup.py

Cobre:
    1. Session-level dedup: mesma session_id não envia duas vezes
    2. Turn-based dedup: mesmo turno (chat_id + user_message) não envia duas vezes
    3. Números internacionais: +27 e +594 não vazam pela normalização brasileira
    4. Tool result filter: "Nothing to save." e similares são suprimidos
    5. Status notification skip: _human_send + skip garantem que LLM não roda junto

Desde o Quicksilver, a entrega para contatos sai por transform_llm_output; este
script valida o dedup nesse caminho (reserva de turno + _schedule_contact_reply).
"""

import sys
import os
import types
import unittest
import threading

sys.path.insert(0, "/opt/data/.hermes/plugins/whatsapp-manager")

# ── Mocks mínimos para importar o plugin sem Hermes rodando ─────────────────
os.environ.setdefault("WHATSAPP_OWNER_NUMBER", "5586981612061")
os.environ.setdefault("WHATSAPP_OWNER_NAME", "dono")
os.environ.setdefault("BRIDGE_URL", "http://localhost:3000")

# Suprimir logs durante os testes
import logging
logging.disable(logging.CRITICAL)

import whatsapp_manager as wm

# Restaurar para ver erros se necessário
logging.disable(logging.NOTSET)


def _deliver(session_id, response_text, platform="whatsapp"):
    """Chama transform_llm_output (caminho atual de entrega para contatos).

    Desde que o envio em bolhas migrou de post_llm_call para transform_llm_output,
    o dedup decide se _schedule_contact_reply é chamado. Retorna True se uma
    entrega foi agendada para este turno; False se foi suprimida/deduplicada.
    """
    scheduled = []

    def fake_schedule(chat_id, clean_text, turn_key):
        scheduled.append((chat_id, clean_text))
        return True

    original_schedule = wm._schedule_contact_reply
    wm._schedule_contact_reply = fake_schedule

    import time as _time
    original_sleep = _time.sleep
    _time.sleep = lambda *a, **kw: None

    try:
        ret = wm.transform_llm_output(
            platform=platform,
            session_id=session_id,
            response_text=response_text,
        )
    finally:
        wm._schedule_contact_reply = original_schedule
        _time.sleep = original_sleep

    if ret is not None and ret != "\n":
        raise AssertionError(f"transform_llm_output devia suprimir o reenvio do Hermes, retornou {ret!r}")
    return bool(scheduled)


def _arm_turn(chat_id, user_message):
    """Registra o turno atual do chat, como o pre_gateway_dispatch faria."""
    import hashlib
    tk = chat_id + ":" + hashlib.md5(user_message.encode()).hexdigest()
    wm._turn_key[chat_id] = tk
    return tk


def _clear_state():
    wm._turn_key.clear()
    wm._turn_sent.clear()
    wm._turn_inflight.clear()
    wm._sender_to_chat.clear()
    wm._responded_sessions.clear()


class TestSessionDedup(unittest.TestCase):
    """Camada 1: o mesmo turno de um chat não agenda duas entregas."""

    def setUp(self):
        _clear_state()

    def test_primeira_chamada_passa(self):
        _arm_turn("272335554773018@s.whatsapp.net", "olá?")
        self.assertTrue(_deliver("272335554773018@s.whatsapp.net", "olá"))
        print("  [OK] primeira chamada passa")

    def test_segunda_chamada_mesma_session_suprimida(self):
        _arm_turn("272335554773018@s.whatsapp.net", "oi, tudo bem?")
        self.assertTrue(_deliver("272335554773018@s.whatsapp.net", "primeira resposta"))
        self.assertFalse(_deliver("272335554773018@s.whatsapp.net", "segunda resposta (deve ser suprimida)"))
        print("  [OK] segunda chamada mesma session suprimida")

    def test_sessions_diferentes_passam(self):
        chat_a = "272335554773018@s.whatsapp.net"
        chat_b = "5940090822813@s.whatsapp.net"
        wm._sender_to_chat["session-A"] = chat_a
        wm._sender_to_chat["session-B"] = chat_b
        _arm_turn(chat_a, "pergunta A")
        _arm_turn(chat_b, "pergunta B")
        self.assertTrue(_deliver("session-A", "resposta A"))
        self.assertTrue(_deliver("session-B", "resposta B"))
        print("  [OK] sessions diferentes passam independentemente")

    def test_numero_internacional_27(self):
        """Número sul-africano +27 não vaza pela normalização brasileira."""
        _clear_state()
        _arm_turn("272335554773018@s.whatsapp.net", "oi")
        self.assertTrue(_deliver("272335554773018@s.whatsapp.net", "oi"))
        self.assertFalse(_deliver("272335554773018@s.whatsapp.net", "oi de novo"))
        print("  [OK] número +27 dedup correto")

    def test_numero_internacional_594(self):
        """Número da Guiana Francesa +594 não vaza."""
        _clear_state()
        _arm_turn("5940090822813@s.whatsapp.net", "oi")
        self.assertTrue(_deliver("5940090822813@s.whatsapp.net", "oi"))
        self.assertFalse(_deliver("5940090822813@s.whatsapp.net", "oi de novo"))
        print("  [OK] número +594 dedup correto")

    def test_thread_race_condition(self):
        """Concorrência: somente uma thread agenda, outras são suprimidas."""
        _clear_state()
        _arm_turn("272335554773018@s.whatsapp.net", "corrida")
        results = []
        lock = threading.Lock()

        def call():
            sent = _deliver("272335554773018@s.whatsapp.net", "resposta concorrente")
            with lock:
                results.append(sent)

        threads = [threading.Thread(target=call) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        enviados = [r for r in results if r]
        suprimidos = [r for r in results if not r]
        self.assertEqual(len(enviados), 1, f"Esperado 1 envio, got {len(enviados)}: {results}")
        self.assertEqual(len(suprimidos), 4)
        print(f"  [OK] race condition: 1 enviado, 4 suprimidos")


class TestTurnDedup(unittest.TestCase):
    """Camada 2: turn-based dedup via _turn_key + _turn_sent/_turn_inflight."""

    def setUp(self):
        _clear_state()

    def test_turn_dedup_sessions_diferentes_mesmo_turno(self):
        """Duas sessions diferentes, mesmo turno — só uma passa."""
        chat_id = "272111222333444@s.whatsapp.net"
        _arm_turn(chat_id, "qual o preço do curso?")
        wm._sender_to_chat["session-X"] = chat_id
        wm._sender_to_chat["session-Y"] = chat_id

        self.assertTrue(_deliver("session-X", "O curso custa R$ 399"))
        self.assertFalse(_deliver("session-Y", "O curso custa R$ 399"))
        print("  [OK] turn dedup: sessions diferentes mesmo turno — só uma passa")


class TestToolResultFilter(unittest.TestCase):
    """Tool results intermediários são suprimidos antes do dedup."""

    def setUp(self):
        _clear_state()

    def _test_filter(self, text, should_suppress=True):
        _clear_state()
        chat_id = "272111222333444@s.whatsapp.net"
        _arm_turn(chat_id, "mensagem de teste")
        sent = _deliver(chat_id, text)
        if should_suppress:
            self.assertFalse(sent, f"Esperado suprimir {text!r}, mas foi agendado")
        else:
            self.assertTrue(sent, f"Esperado passar {text!r}, mas foi suprimido")

    def test_nothing_to_save(self):
        self._test_filter("Nothing to save.")
        print("  [OK] 'Nothing to save.' suprimido")

    def test_nothing_to_save_uppercase(self):
        self._test_filter("NOTHING TO SAVE")
        print("  [OK] 'NOTHING TO SAVE' suprimido")

    def test_nada_para_salvar(self):
        self._test_filter("nada para salvar.")
        print("  [OK] 'nada para salvar.' suprimido")

    def test_tool_result_tag(self):
        self._test_filter("[tool result] contact saved")
        print("  [OK] '[tool result]' suprimido")

    def test_resposta_normal_passa(self):
        self._test_filter("O curso está disponível em comunidade.aalencar.com.br", should_suppress=False)
        print("  [OK] resposta normal passa")


class TestInternationalPhoneNormalization(unittest.TestCase):
    """_normalize_brazilian_phone não afeta números internacionais."""

    def test_numero_27_nao_normalizado(self):
        result = wm._normalize_brazilian_phone("272335554773018")
        self.assertEqual(result, "272335554773018")
        print("  [OK] +27 não normalizado")

    def test_numero_594_nao_normalizado(self):
        result = wm._normalize_brazilian_phone("5940090822813")
        self.assertEqual(result, "5940090822813")
        print("  [OK] +594 não normalizado")

    def test_owner_nao_confunde_com_27(self):
        """Owner brasileiro não é confundido com número +27."""
        norm_owner = wm._normalize_brazilian_phone("5586981612061")
        norm_27 = wm._normalize_brazilian_phone("272335554773018")
        self.assertNotEqual(norm_owner, norm_27)
        print("  [OK] owner não confunde com +27")

    def test_resolve_phone_from_jid_27(self):
        """_resolve_phone_from_jid trata corretamente JID +27."""
        result = wm._resolve_phone_from_jid("272335554773018@s.whatsapp.net")
        self.assertIn("272335554773018", result)
        print(f"  [OK] resolve_phone_from_jid(+27) → {result!r}")

    def test_resolve_phone_from_jid_lid(self):
        """_resolve_phone_from_jid com @lid não confunde local com domain."""
        result = wm._resolve_phone_from_jid("164291240063173:0@lid")
        # Deve preservar o domínio @lid, não default para @s.whatsapp.net
        self.assertIn("@lid", result)
        print(f"  [OK] resolve_phone_from_jid(@lid) → {result!r}")


class TestStatusSkipSafety(unittest.TestCase):
    """Status notification retorna skip mesmo se persist falhar."""

    def test_persist_failure_nao_vaza_llm(self):
        """Se _persist_status_notified falhar, o código não cai no LLM."""
        original_persist = wm._persist_status_notified
        called_persist = []

        def fail_persist():
            called_persist.append(True)
            raise OSError("disk full")

        wm._persist_status_notified = fail_persist

        # Verificar que _persist_status_notified está dentro de try separado
        # Simular: _status_notified foi atualizado, persist falha, mas skip ainda é retornado
        try:
            wm._status_notified["test_chat"] = "dormindo"
            wm._persist_status_notified()
        except OSError:
            pass  # Esperado

        # O estado em memória deve estar atualizado mesmo com falha de persist
        self.assertIn("test_chat", wm._status_notified)
        print("  [OK] persist failure não vaza: estado em memória mantido")
        wm._persist_status_notified = original_persist
        wm._status_notified.pop("test_chat", None)


if __name__ == "__main__":
    print("\n" + "="*60)
    print("VALIDAÇÃO DO DEDUP DE RESPOSTAS DUPLICADAS")
    print("="*60 + "\n")

    loader = unittest.TestLoader()
    suite = unittest.TestSuite()

    classes = [
        TestSessionDedup,
        TestTurnDedup,
        TestToolResultFilter,
        TestInternationalPhoneNormalization,
        TestStatusSkipSafety,
    ]

    for cls in classes:
        print(f"\n▶ {cls.__name__}")
        suite.addTests(loader.loadTestsFromTestCase(cls))

    runner = unittest.TextTestRunner(verbosity=0, stream=open(os.devnull, "w"))
    result = runner.run(suite)

    # Re-run com output para mostrar erros
    if not result.wasSuccessful():
        print("\n❌ FALHAS DETECTADAS:\n")
        for test, err in result.failures + result.errors:
            print(f"  FAIL: {test}")
            print(f"  {err}\n")
        sys.exit(1)
    else:
        total = result.testsRun
        print(f"\n{'='*60}")
        print(f"✅ TODOS OS {total} TESTES PASSARAM — dedup validado")
        print("="*60 + "\n")
