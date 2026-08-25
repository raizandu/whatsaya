"""Wiring do auditor diário dentro do plugin.

Fica separado de `plugin_test.py` porque só cobre o encanamento do auditor:
o log em arquivo que o cron consegue ler de dentro do container, a resolução do
modelo auditor e o agendamento.
"""
from __future__ import annotations

import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.append(str(Path(__file__).parent.parent))

import whatsapp_manager as wm


class PluginFileLogTest(unittest.TestCase):
    """O plugin loga só em stdout do container, e `docker logs` não existe de
    dentro do container — que é justamente de onde o cron do auditor roda.
    Sem um arquivo, o coletor não tem o que ler."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="whatsaya-log-test-")
        self.path = Path(self.tmp.name) / "whatsapp_plugin.log"

    def tearDown(self):
        for handler in list(wm.logger.handlers):
            if isinstance(handler, logging.FileHandler):
                handler.close()
                wm.logger.removeHandler(handler)
        self.tmp.cleanup()

    def test_linha_do_plugin_fica_legivel_em_arquivo(self):
        wm._attach_plugin_file_log(self.path)

        wm.logger.warning("[payment-gate] resposta comercial substituída reason=teste")

        conteudo = self.path.read_text(encoding="utf-8")
        self.assertIn("[payment-gate] resposta comercial substituída reason=teste", conteudo)

    def test_o_arquivo_carrega_a_hora_para_o_recorte_por_dia(self):
        wm._attach_plugin_file_log(self.path)

        wm.logger.info("[human-send] chat='x@s.whatsapp.net' bubbles=1 sizes=[9]")

        primeira = self.path.read_text(encoding="utf-8").splitlines()[0]
        self.assertRegex(primeira, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")

    def test_chamar_duas_vezes_nao_duplica_a_linha(self):
        wm._attach_plugin_file_log(self.path)
        wm._attach_plugin_file_log(self.path)

        wm.logger.warning("[handoff] dono avisado sobre 'x@s.whatsapp.net' motivo='y' message_id='z'")

        linhas = [l for l in self.path.read_text(encoding="utf-8").splitlines() if "[handoff]" in l]
        self.assertEqual(len(linhas), 1)

    def test_suite_de_teste_nao_escreve_no_log_de_producao(self):
        """Rodar a suíte DENTRO do container reexecuta `register()` a cada
        processo de teste — 690 vezes numa rodada observada — e despejava
        milhares de linhas de fixture no log que o auditor lê. Além de encher a
        janela de rotação (~1 MB em 15 min), fazia chat de teste aparecer no
        relatório como se fosse lead real."""
        antes = list(wm.logger.handlers)
        gravavel = Path(self.tmp.name) / "producao.log"

        # Aponta o caminho "de produção" para um lugar GRAVÁVEL: se a recusa
        # dependesse de /opt/data não existir, este teste passaria no macOS e
        # falharia no container, que é onde o problema acontece de verdade.
        with patch.dict(os.environ, {"WHATSAPP_PLUGIN_LOG": str(gravavel)}):
            attached = wm._attach_plugin_file_log()

        self.assertFalse(attached)
        self.assertEqual(wm.logger.handlers, antes)
        self.assertFalse(gravavel.exists())

    def test_caminho_explicito_continua_anexando(self):
        # Caminho explícito é deliberado (testes, ferramenta, reprocessamento):
        # a guarda vale só para o caminho automático do boot.
        self.assertTrue(wm._attach_plugin_file_log(self.path))
        wm.logger.warning("[handoff] linha de teste")
        self.assertIn("linha de teste", self.path.read_text(encoding="utf-8"))

    def test_caminho_impossivel_nao_derruba_o_plugin(self):
        # Fail-open: perder o log do auditor é aceitável; derrubar o atendimento não.
        wm._attach_plugin_file_log(Path("/nao/existe/de/jeito/nenhum/x.log"))

        wm.logger.info("[contact-reply] segue funcionando")


class AuditModelResolutionTest(unittest.TestCase):
    """Decisão de segurança fixada no handoff: o auditor roda em provider limpo.

    O `WHATSAPP_CLIENT_MODEL` roda no backend da conta ChatGPT do dono e em 24/08
    reproduziu credencial e preço que NÃO estavam no prompt (memória do lado do
    provider). Reusar aquele provider aqui faria o auditor aprender da
    contaminação que ele existe para detectar."""

    def test_o_auditor_tem_env_propria(self):
        with patch.dict(os.environ, {"WHATSAPP_AUDIT_MODEL": "gpt-5.6-sol"}):
            self.assertEqual(wm.config.whatsapp_audit_model, "gpt-5.6-sol")

    def test_o_auditor_nunca_herda_o_modelo_do_cliente(self):
        with patch.dict(os.environ, {"WHATSAPP_CLIENT_MODEL": "gpt-5.6-luna"}, clear=False):
            os.environ.pop("WHATSAPP_AUDIT_MODEL", None)
            self.assertNotEqual(wm.config.whatsapp_audit_model, "gpt-5.6-luna")

    def test_provider_padrao_do_auditor_e_openrouter(self):
        os.environ.pop("WHATSAPP_AUDIT_PROVIDER", None)
        self.assertEqual(wm.config.whatsapp_audit_provider, "openrouter")

    def test_sem_chave_do_provider_limpo_o_auditor_nao_chama_ninguem(self):
        # Cair no ladder Google→OpenAI→OpenRouter levaria a chamada para a conta
        # contaminada. Sem chave do provider escolhido, não há chamada.
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}), \
             patch("whatsapp_manager._call_llm_api") as mock_call:
            resultado = wm._audit_llm_call("material do dia")

        self.assertIsNone(resultado)
        mock_call.assert_not_called()

    def test_chama_o_openrouter_com_o_modelo_do_auditor(self):
        with patch.dict(os.environ, {
            "OPENROUTER_API_KEY": "sk-teste",
            "WHATSAPP_AUDIT_MODEL": "gpt-5.6-sol",
        }), patch("whatsapp_manager._call_llm_api", return_value="veredito") as mock_call:
            resultado = wm._audit_llm_call("material do dia")

        self.assertEqual(resultado, "veredito")
        url = mock_call.call_args.args[0]
        payload = mock_call.call_args.args[2]
        self.assertIn("openrouter.ai", url)
        self.assertEqual(payload["model"], "gpt-5.6-sol")
        self.assertIn("material do dia", str(payload["messages"]))


class RunDailyAuditTest(unittest.TestCase):
    """Ponta a ponta do auditor, sem rede: log em arquivo + banco → relatório."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="whatsaya-audit-run-")
        raiz = Path(self.tmp.name)
        self.log = raiz / "whatsapp_plugin.log"
        self.reports = raiz / "reports"
        self.db = raiz / "whatsapp_messages.db"
        self.log.write_text(
            "2026-08-24T19:42:11 [whatsapp-manager] [payment-gate] resposta comercial "
            "substituída chat='556299990000@s.whatsapp.net' reason=unofficial_details "
            "market='BR' intent=True digits=['official:BR:pix cnpj'] unofficial=['unknown_digits']\n",
            encoding="utf-8",
        )
        self._seed_db()
        self.env = patch.dict(os.environ, {
            "WHATSAPP_PLUGIN_LOG": str(self.log),
            "WHATSAPP_AUDIT_REPORT_DIR": str(self.reports),
            "WHATSAPP_OWNER_NUMBER": "5511999999999",
        })
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def _seed_db(self):
        import sqlite3
        from datetime import datetime
        from zoneinfo import ZoneInfo

        conn = sqlite3.connect(self.db)
        conn.executescript(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT NOT NULL,"
            " sender_id TEXT, sender_name TEXT, message_id TEXT NOT NULL, message_type TEXT,"
            " body TEXT, timestamp REAL, from_me INTEGER NOT NULL DEFAULT 0,"
            " is_historical INTEGER NOT NULL DEFAULT 0, has_media INTEGER NOT NULL DEFAULT 0,"
            " media_type TEXT, sync_type TEXT, inserted_at REAL NOT NULL DEFAULT 0);"
        )
        tz = ZoneInfo("America/Sao_Paulo")
        base = datetime(2026, 8, 24, 19, 40, tzinfo=tz).timestamp()
        for i, (corpo, from_me) in enumerate([
            ("quero contratar, como pago?", 0),
            ("Perfeito — seguem os dados oficiais para o pagamento:", 1),
        ]):
            conn.execute(
                "INSERT INTO messages (chat_id, message_id, body, timestamp, from_me)"
                " VALUES (?,?,?,?,?)",
                ("556299990000@s.whatsapp.net", f"m{i}", corpo, base + i * 60, from_me),
            )
        conn.commit()
        conn.close()

    def _run(self, veredito="1. Preço errado. Tipo: CODIGO."):
        from datetime import date

        with patch("whatsapp_manager._MSG_DB_PATH", self.db), \
             patch("whatsapp_manager._audit_llm_call", return_value=veredito), \
             patch("whatsapp_manager._human_send", return_value="mid") as send:
            caminho = wm._run_daily_audit(date(2026, 8, 24))
        return caminho, send

    def test_grava_o_relatorio_do_dia_em_disco(self):
        caminho, _ = self._run()

        self.assertTrue(Path(caminho).is_file())
        self.assertEqual(Path(caminho).name, "audit-20260824.md")
        self.assertIn("Preço errado", Path(caminho).read_text(encoding="utf-8"))

    def test_latencia_do_gateway_entra_no_relatorio(self):
        from datetime import date

        gateway = Path(self.tmp.name) / "gateway.log"
        gateway.write_text(
            "2026-08-24 19:29:00,171 INFO gateway.run: response ready: platform=whatsapp "
            "chat=556299990000@s.whatsapp.net time=15.3s api_calls=2 response=1 chars\n",
            encoding="utf-8",
        )

        with patch("whatsapp_manager._MSG_DB_PATH", self.db), \
             patch("whatsapp_manager._GATEWAY_LOG_PATH", gateway), \
             patch("whatsapp_manager._audit_llm_call", return_value="v"), \
             patch("whatsapp_manager._human_send", return_value="mid"):
            caminho = wm._run_daily_audit(date(2026, 8, 24))

        texto = Path(caminho).read_text(encoding="utf-8")
        self.assertIn("15.3", texto)
        self.assertIn("api_calls no dia: 2", texto)

    def test_proposta_aplicavel_fica_armada_para_sim_nao(self):
        from datetime import date
        import json as _json

        wm._pending_audit_action.clear()
        veredito = _json.dumps({"resumo": "dia ok", "findings": [{
            "tipo": "DADO", "titulo": "anotar ramo", "evidencia": "repetiu 2x",
            "proposta": "anotar o ramo", "alvo": {"tipo": "contato",
                                                  "chat": "556299990000@s.whatsapp.net",
                                                  "campo": "notes", "valor": "clínica"},
        }]}, ensure_ascii=False)

        with patch("whatsapp_manager._MSG_DB_PATH", self.db), \
             patch("whatsapp_manager._audit_llm_call", return_value=veredito), \
             patch("whatsapp_manager._human_send", return_value="mid") as send:
            wm._run_daily_audit(date(2026, 8, 24))

        self.assertEqual(len(wm._pending_audit_action), 1)
        self.assertIn("sim", send.call_args.args[1].lower())
        wm._pending_audit_action.clear()

    def test_proposta_nao_aplicavel_nao_arma_portao(self):
        from datetime import date
        import json as _json

        wm._pending_audit_action.clear()
        veredito = _json.dumps({"resumo": "x", "findings": [{
            "tipo": "CODIGO", "titulo": "falta guarda", "evidencia": "e",
            "proposta": "filtrar na saída", "alvo": {},
        }]}, ensure_ascii=False)

        with patch("whatsapp_manager._MSG_DB_PATH", self.db), \
             patch("whatsapp_manager._audit_llm_call", return_value=veredito), \
             patch("whatsapp_manager._human_send", return_value="mid"):
            wm._run_daily_audit(date(2026, 8, 24))

        self.assertEqual(wm._pending_audit_action, {})

    def test_avisa_o_dono_no_self_chat(self):
        _, send = self._run()

        destino = send.call_args.args[0]
        self.assertEqual(destino, "5511999999999@s.whatsapp.net")
        self.assertIn("Auditoria do dia", send.call_args.args[1])

    def test_o_placar_do_codigo_chega_ao_dono(self):
        _, send = self._run()

        self.assertIn("guarda salvou", send.call_args.args[1])

    def test_credencial_do_log_nao_vaza_no_aviso_ao_dono(self):
        _, send = self._run()

        self.assertNotIn("556299990000", send.call_args.args[1])

    def test_sem_veredito_do_modelo_o_relatorio_ainda_sai(self):
        caminho, send = self._run(veredito=None)

        self.assertTrue(Path(caminho).is_file())
        self.assertIn("guarda salvou", send.call_args.args[1])

    def test_o_disparo_de_guarda_do_log_chega_ao_relatorio(self):
        # Sem isto o auditor passava sem ler o log e ninguém notava: o relatório
        # saía "sem disparo de guarda" num dia em que a guarda disparou.
        caminho, _ = self._run()

        texto = Path(caminho).read_text(encoding="utf-8")
        self.assertIn("payment-gate:unofficial_details", texto)
        self.assertIn("official:BR:pix cnpj", texto)

    def test_falha_ao_gravar_nao_e_reportada_como_sucesso(self):
        # `caminho` era atribuído antes do write: com OSError, o tick logava
        # "relatório: <arquivo>" e saía 0 apontando para um arquivo inexistente.
        from datetime import date

        with patch("whatsapp_manager._MSG_DB_PATH", self.db), \
             patch("whatsapp_manager._audit_llm_call", return_value="v"), \
             patch("whatsapp_manager._human_send", return_value="mid"), \
             patch("pathlib.Path.write_text", side_effect=OSError("disco cheio")):
            caminho = wm._run_daily_audit(date(2026, 8, 24))

        self.assertIsNone(caminho)

    def test_sem_dono_configurado_grava_mas_nao_envia(self):
        with patch.dict(os.environ, {"WHATSAPP_OWNER_NUMBER": ""}):
            caminho, send = self._run()

        self.assertTrue(Path(caminho).is_file())
        send.assert_not_called()


class AuditProposalGateTest(unittest.TestCase):
    """Portão da fase 2: só DADO se aplica, e só por sim/não explícito do dono."""

    def setUp(self):
        wm._pending_audit_action.clear()

    def tearDown(self):
        wm._pending_audit_action.clear()

    def _proposta(self, **alvo):
        import daily_audit as da

        return da.Proposal(
            kind="dado", title="anotar ramo", evidence="lead repetiu 2x",
            proposal="anotar o ramo do lead", target=alvo, applicable=True,
        )

    def test_aplica_nota_de_contato_apos_sim(self):
        proposta = self._proposta(tipo="contato", chat="556299990000@s.whatsapp.net",
                                  campo="notes", valor="clínica odontológica")

        with patch("whatsapp_manager._update_contact_fields", return_value="ok") as upd:
            resultado = wm._apply_audit_proposal(proposta)

        self.assertTrue(resultado.startswith("✅"))
        upd.assert_called_once()
        self.assertEqual(upd.call_args.args[1], {"notes": "clínica odontológica"})

    def test_aplica_campo_de_catalogo_apos_sim(self):
        proposta = self._proposta(tipo="catalogo", chave="plano-x",
                                  campo="price", valor="R$ 997")

        with patch("whatsapp_manager._load_product_catalog",
                   return_value={"plano-x": {"name": "Plano X", "price": "R$ 1"}}), \
             patch("whatsapp_manager._save_product_catalog") as save:
            resultado = wm._apply_audit_proposal(proposta)

        self.assertTrue(resultado.startswith("✅"))
        self.assertEqual(save.call_args.args[0]["plano-x"]["price"], "R$ 997")

    def test_proposta_nao_aplicavel_e_recusada_no_backend(self):
        # Segunda camada: mesmo que algo marque `applicable`, o aplicador
        # revalida o alvo. O agente não se automodifica por caminho nenhum.
        import daily_audit as da

        proposta = da.Proposal(kind="dado", target={"tipo": "arquivo", "caminho": "SOUL.md"},
                               applicable=True)

        with patch("whatsapp_manager._update_contact_fields") as upd, \
             patch("whatsapp_manager._save_product_catalog") as save:
            resultado = wm._apply_audit_proposal(proposta)

        self.assertTrue(resultado.startswith("❌"))
        upd.assert_not_called()
        save.assert_not_called()

    def test_chave_pix_e_recusada_no_backend(self):
        import daily_audit as da

        proposta = da.Proposal(kind="dado", applicable=True,
                               target={"tipo": "catalogo", "chave": "p", "campo": "pix_key",
                                       "valor": "outra"})

        with patch("whatsapp_manager._save_product_catalog") as save:
            resultado = wm._apply_audit_proposal(proposta)

        self.assertTrue(resultado.startswith("❌"))
        save.assert_not_called()

    def test_item_de_catalogo_inexistente_nao_cria_item(self):
        proposta = self._proposta(tipo="catalogo", chave="nao-existe",
                                  campo="price", valor="R$ 1")

        with patch("whatsapp_manager._load_product_catalog", return_value={}), \
             patch("whatsapp_manager._save_product_catalog") as save:
            resultado = wm._apply_audit_proposal(proposta)

        self.assertTrue(resultado.startswith("❌"))
        save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
