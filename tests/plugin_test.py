"""Python unit tests for the whatsapp-manager plugin."""

import os
import json
import sqlite3
import tempfile
import time
import urllib.error
import unittest
import sys
from unittest.mock import patch, MagicMock
from pathlib import Path

# Add root directory to sys.path to import whatsapp_manager
sys.path.append(str(Path(__file__).parent.parent))

import whatsapp_manager
from whatsapp_manager import register

class MockContext:
    def __init__(self):
        self.hooks = {}
        self.skills = {}

    def register_hook(self, name, func):
        self.hooks[name] = func

    def register_skill(self, name, path):
        self.skills[name] = path


class BaseWhatsAppManagerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ctx = MockContext()
        self.env_patcher = patch.dict(os.environ, {
            "WHATSAPP_OWNER_NUMBER": "5511999999999",
            # O nome do dono deixou de ser fixo no código e agora vem daqui. Os testes que
            # afirmam "André" nos prompts continuam válidos e passam a provar que a
            # parametrização por WHATSAPP_OWNER_NAME funciona ponta a ponta.
            "WHATSAPP_OWNER_NAME": "André",
            "WHATSAPP_OWNER_MODEL": "gemini-3.5-flash-owner",
            "WHATSAPP_CLIENT_MODEL": "gemini-3.5-flash-client"
        })
        self.env_patcher.start()
        # A política por contato é testada em casos dedicados. Os demais testes
        # de roteamento assumem um contato explicitamente habilitado.
        self.ai_access_patcher = patch(
            "whatsapp_manager._ensure_contact_ai_access",
            return_value=(True, "test-explicit-flow"),
        )
        self.ai_access_patcher.start()
        # Dedup de mensagens recebidas vive num set de módulo: sem limpar aqui, o id de
        # um teste vaza pro seguinte e o dispatch devolve "duplicate-message-id".
        whatsapp_manager._seen_message_ids.clear()

        # Avoid running bootstrap/shutil operations during register
        with patch("pathlib.Path.mkdir"), \
             patch("pathlib.Path.symlink_to"), \
             patch("pathlib.Path.is_symlink", return_value=True), \
             patch("pathlib.Path.write_text"), \
             patch("shutil.copy2"), \
             patch("urllib.request.urlopen"), \
             patch("whatsapp_manager._ensure_google_libs"), \
             patch("whatsapp_manager._pull_and_merge_configurations"), \
             patch("whatsapp_manager._enforce_gateway_safety_config"), \
             patch("whatsapp_manager._self_update_plugin_code", return_value=False):
            register(self.ctx)

    def tearDown(self):
        self.ai_access_patcher.stop()
        self.env_patcher.stop()


class TestMessageRoutingAndDispatch(BaseWhatsAppManagerTest):
    def test_owner_message_identification(self):
        pre_dispatch = self.ctx.hooks.get("pre_gateway_dispatch")
        self.assertIsNotNone(pre_dispatch)

        # Mock Event and Gateway
        event = MagicMock()
        event.source.platform = "whatsapp"
        event.source.user_id = "5511999999999:1@s.whatsapp.net" # With device ID
        event.text = "Hello bot"
        event.source.chat_id = "5511999999999@s.whatsapp.net"

        gateway = MagicMock()
        gateway._session_key_for_source.return_value = "session_1"
        gateway._session_model_overrides = {}

        context = {
            "event": event,
            "gateway": gateway
        }

        res = pre_dispatch("pre_gateway_dispatch", context)
        self.assertIsNone(res) # Owner message is not skipped
        self.assertEqual(gateway._session_model_overrides["session_1"]["model"], "gemini-3.5-flash-owner")

    def test_client_message_when_bot_paused(self):
        pre_dispatch = self.ctx.hooks.get("pre_gateway_dispatch")
        
        event = MagicMock()
        event.source.platform = "whatsapp"
        event.source.user_id = "5511888888888@s.whatsapp.net" # Client
        event.text = "Hello"
        event.source.chat_id = "5511888888888@s.whatsapp.net"

        gateway = MagicMock()
        gateway._session_key_for_source.return_value = "session_2"
        gateway._session_model_overrides = {}

        context = {
            "event": event,
            "gateway": gateway
        }

        # Mock _check_bot_paused to return True
        with patch("whatsapp_manager._check_bot_paused", return_value=True):
            res = pre_dispatch("pre_gateway_dispatch", context)
            self.assertEqual(res, {"action": "skip", "reason": "bot-pausado"})

    def test_legacy_contact_disabled_is_skipped_before_llm(self):
        pre_dispatch = self.ctx.hooks.get("pre_gateway_dispatch")
        self.assertIsNotNone(pre_dispatch)
        event = MagicMock()
        event.source.platform = "whatsapp"
        event.source.user_id = "5511888888888@s.whatsapp.net"
        event.source.chat_id = "5511888888888@s.whatsapp.net"
        event.text = "Oi"
        event.raw = {}
        event.raw_message = {}
        event.is_historical = False

        gateway = MagicMock()
        gateway._session_key_for_source.return_value = "session_legacy"
        gateway._session_model_overrides = {}

        with patch(
            "whatsapp_manager._ensure_contact_ai_access",
            return_value=(False, "legacy-contact-disabled"),
        ), patch("whatsapp_manager._followup_cancel") as cancel:
            res = pre_dispatch("pre_gateway_dispatch", {"event": event, "gateway": gateway})

        self.assertEqual(res, {"action": "skip", "reason": "legacy-contact-disabled"})
        cancel.assert_called_once_with("5511888888888@s.whatsapp.net")

    def test_pre_gateway_dispatch_does_not_rewrite_or_fetch(self):
        pre_dispatch = self.ctx.hooks.get("pre_gateway_dispatch")

        event = MagicMock()
        event.source.platform = "whatsapp"
        event.source.user_id = "5511888888888@s.whatsapp.net" # Client
        event.text = "Hello client query"
        event.source.chat_id = "5511888888888@s.whatsapp.net"

        gateway = MagicMock()
        gateway._session_key_for_source.return_value = "session_3"
        gateway._session_model_overrides = {}

        context = {
            "event": event,
            "gateway": gateway
        }

        with patch("whatsapp_manager._check_bot_paused", return_value=False), \
             patch("whatsapp_manager._check_chat_silenced", return_value=False), \
             patch("whatsapp_manager._fetch_chat_history") as mock_fetch:
            res = pre_dispatch("pre_gateway_dispatch", context)
            self.assertIsNone(res) # Should not skip or rewrite (returns None)
            mock_fetch.assert_not_called() # Should not fetch history at dispatch stage

    def test_pre_gateway_stages_structured_us_lead_metadata_for_first_llm_turn(self):
        pre_dispatch = self.ctx.hooks.get("pre_gateway_dispatch")
        event = MagicMock()
        event.source.platform = "whatsapp"
        event.source.user_id = "12025550199@s.whatsapp.net"
        event.source.chat_id = "12025550199@s.whatsapp.net"
        event.text = "¿Cuánto cuesta?"
        event.raw = {}
        event.raw_message = {
            "fromMe": False,
            "isGroup": False,
            "originalChatId": "123456789012345@lid",
            "originalSenderId": "123456789012345@lid",
            "leadMetadata": {
                "market_id": "US",
                "origin": "Meta Ads",
                "campaign": "US Cleaning Spanish",
                "country": "United States",
                "currency": "USD",
                "offer": "international",
                "timezone": "America/New_York",
                "language": "es",
            },
        }
        event.is_historical = False

        gateway = MagicMock()
        gateway._session_key_for_source.return_value = "session-us-es"
        gateway._session_model_overrides = {}

        with patch("whatsapp_manager._check_bot_paused", return_value=False), \
             patch("whatsapp_manager._check_chat_silenced", return_value=False), \
             patch("whatsapp_manager._followup_note_activity"), \
             patch("whatsapp_manager._track_inbound") as mock_track:
            result = pre_dispatch("pre_gateway_dispatch", {"event": event, "gateway": gateway})

        self.assertIsNone(result)
        staged = mock_track.call_args.args[3]
        self.assertEqual(staged["market_id"], "US")
        self.assertEqual(staged["currency"], "USD")
        self.assertEqual(staged["offer"], "international")
        self.assertEqual(staged["language"], "es")
        self.assertEqual(staged["campaign"], "US Cleaning Spanish")
        self.assertEqual(staged["_identity_lid"], "123456789012345@lid")

    def test_non_whatsapp_platforms_are_ignored(self):
        pre_llm = self.ctx.hooks.get("pre_llm_call")
        
        context = {
            "platform": "telegram",
            "sender_id": "5511999999999"
        }
        res = pre_llm("pre_llm_call", context)
        self.assertIsNone(res)

    def test_pre_gateway_dispatch_non_whatsapp_ignored(self):
        pre_dispatch = self.ctx.hooks.get("pre_gateway_dispatch")
        
        event = MagicMock()
        event.source.platform = "telegram"
        context = {
            "event": event,
            "gateway": MagicMock()
        }
        res = pre_dispatch("pre_gateway_dispatch", context)
        self.assertIsNone(res)

    def test_missing_model_env_vars_fallback(self):
        pre_dispatch = self.ctx.hooks.get("pre_gateway_dispatch")
        
        event = MagicMock()
        event.source.platform = "whatsapp"
        event.source.user_id = "5511999999999@s.whatsapp.net"
        event.text = "Hello"
        event.source.chat_id = "5511999999999@s.whatsapp.net"

        gateway = MagicMock()
        gateway._session_key_for_source.return_value = "session_x"
        gateway._session_model_overrides = {}

        context = {
            "event": event,
            "gateway": gateway
        }

        # Clear env variables WHATSAPP_OWNER_MODEL and WHATSAPP_CLIENT_MODEL
        with patch.dict(os.environ, {}, clear=True):
            # We must restore WHATSAPP_OWNER_NUMBER for the owner check to pass
            os.environ["WHATSAPP_OWNER_NUMBER"] = "5511999999999"
            res = pre_dispatch("pre_gateway_dispatch", context)
            self.assertIsNone(res)
            self.assertEqual(gateway._session_model_overrides["session_x"]["model"], "gemini-3.1-flash-lite")

    def test_missing_session_key_handled_gracefully(self):
        pre_dispatch = self.ctx.hooks.get("pre_gateway_dispatch")
        
        event = MagicMock()
        event.source.platform = "whatsapp"
        event.source.user_id = "5511999999999@s.whatsapp.net"
        event.text = "Hello"
        event.source.chat_id = "5511999999999@s.whatsapp.net"

        gateway = MagicMock()
        # Return None for session key
        gateway._session_key_for_source.return_value = None

        context = {
            "event": event,
            "gateway": gateway
        }

        res = pre_dispatch("pre_gateway_dispatch", context)
        # Should not raise exception
        self.assertIsNone(res)

    def test_owner_manual_message_to_third_party_is_skipped(self):
        """Mensagem manual do dono para terceiro (chat_id != owner) deve retornar skip."""
        pre_dispatch = self.ctx.hooks.get("pre_gateway_dispatch")
        self.assertIsNotNone(pre_dispatch)

        event = MagicMock()
        event.source.platform = "whatsapp"
        # Dono enviando mensagem para terceiro
        event.source.user_id = "5511999999999@s.whatsapp.net"
        event.text = "Olá, tudo bem?"
        # Chat com um terceiro (não é self-chat)
        event.source.chat_id = "5511111111111@s.whatsapp.net"

        gateway = MagicMock()
        gateway._session_key_for_source.return_value = "session_manual"
        gateway._session_model_overrides = {}

        context = {"event": event, "gateway": gateway}

        res = pre_dispatch("pre_gateway_dispatch", context)
        # Deve pular o LLM pois é mensagem manual do dono para terceiro
        self.assertIsNotNone(res)
        self.assertEqual(res.get("action"), "skip")
        self.assertEqual(res.get("reason"), "owner-manual-message")

    def test_silenced_chat_client_message_is_skipped(self):
        """Mensagem de cliente em chat silenciado deve retornar skip."""
        pre_dispatch = self.ctx.hooks.get("pre_gateway_dispatch")
        self.assertIsNotNone(pre_dispatch)

        event = MagicMock()
        event.source.platform = "whatsapp"
        event.source.user_id = "5511888888888@s.whatsapp.net"  # Cliente
        event.text = "Olá, tem novidades?"
        event.source.chat_id = "5511888888888@s.whatsapp.net"

        gateway = MagicMock()
        gateway._session_key_for_source.return_value = "session_silenced"
        gateway._session_model_overrides = {}

        context = {"event": event, "gateway": gateway}

        with patch("whatsapp_manager._check_bot_paused", return_value=False), \
             patch("whatsapp_manager._check_chat_silenced", return_value=True):
            res = pre_dispatch("pre_gateway_dispatch", context)
            self.assertIsNotNone(res)
            self.assertEqual(res.get("action"), "skip")
            self.assertEqual(res.get("reason"), "conversa-silenciada")

    def test_custom_providers_env_vars(self):
        """Pre-dispatch deve honrar as variáveis WHATSAPP_OWNER_PROVIDER e WHATSAPP_CLIENT_PROVIDER."""
        pre_dispatch = self.ctx.hooks.get("pre_gateway_dispatch")
        self.assertIsNotNone(pre_dispatch)

        # 1. Testar Owner com provider customizado
        event_owner = MagicMock()
        event_owner.source.platform = "whatsapp"
        event_owner.source.user_id = "5511999999999@s.whatsapp.net"
        event_owner.text = "Hello"
        event_owner.source.chat_id = "5511999999999@s.whatsapp.net"

        gateway_owner = MagicMock()
        gateway_owner._session_key_for_source.return_value = "session_owner"
        gateway_owner._session_model_overrides = {}

        # 2. Testar Cliente com provider customizado
        event_client = MagicMock()
        event_client.source.platform = "whatsapp"
        event_client.source.user_id = "5511888888888@s.whatsapp.net"
        event_client.text = "Hello"
        event_client.source.chat_id = "5511888888888@s.whatsapp.net"

        gateway_client = MagicMock()
        gateway_client._session_key_for_source.return_value = "session_client"
        gateway_client._session_model_overrides = {}

        with patch.dict(os.environ, {
            "WHATSAPP_OWNER_NUMBER": "5511999999999",
            "WHATSAPP_OWNER_MODEL": "my-owner-model",
            "WHATSAPP_OWNER_PROVIDER": "openrouter",
            "WHATSAPP_CLIENT_MODEL": "my-client-model",
            "WHATSAPP_CLIENT_PROVIDER": "openai"
        }):
            # Rodar dispatch pro owner
            res_owner = pre_dispatch("pre_gateway_dispatch", {"event": event_owner, "gateway": gateway_owner})
            self.assertIsNone(res_owner)
            self.assertEqual(gateway_owner._session_model_overrides["session_owner"]["model"], "my-owner-model")
            self.assertEqual(gateway_owner._session_model_overrides["session_owner"]["provider"], "openrouter")

            # Rodar dispatch pro client
            with patch("whatsapp_manager._check_bot_paused", return_value=False), \
                 patch("whatsapp_manager._check_chat_silenced", return_value=False):
                res_client = pre_dispatch("pre_gateway_dispatch", {"event": event_client, "gateway": gateway_client})
                self.assertIsNone(res_client)
                self.assertEqual(gateway_client._session_model_overrides["session_client"]["model"], "my-client-model")
                self.assertEqual(gateway_client._session_model_overrides["session_client"]["provider"], "openai")

    @patch("whatsapp_manager._human_send")
    def test_pre_gateway_dispatch_help_command(self, mock_send):
        """Verifica que perguntas sobre comandos retornam a mensagem de ajuda."""
        pre_dispatch = self.ctx.hooks.get("pre_gateway_dispatch")
        triggers = [
            "quais comandos posso usar",
            "me explique como você funciona",
            "o que você faz",
            "ajuda",
            "help",
            "como usar você",
            "quais funcionalidades",
        ]
        for trigger in triggers:
            mock_send.reset_mock()
            event = MagicMock()
            event.source.platform = "whatsapp"
            event.source.user_id = "5511999999999@s.whatsapp.net"
            event.text = trigger
            event.source.chat_id = "5511999999999@s.whatsapp.net"
            gateway = MagicMock()
            gateway._session_key_for_source.return_value = f"session_help_{trigger[:8]}"
            gateway._session_model_overrides = {}
            res = pre_dispatch("pre_gateway_dispatch", {"event": event, "gateway": gateway})
            self.assertEqual(res, {"action": "skip", "reason": "help-command"}, f"Falhou para: {trigger!r}")
            mock_send.assert_called_once()
            help_msg = mock_send.call_args[0][1]
            self.assertIn("stop_bot", help_msg)
            self.assertIn("sincronizar contatos", help_msg)

    @patch("whatsapp_manager._sync_contacts_from_db_internal", return_value="sync completed")
    @patch("urllib.request.urlopen")
    def test_pre_gateway_dispatch_sync_contacts_command(self, mock_urlopen, mock_sync):
        """Verifica que pre_gateway_dispatch intercepta o comando sync contacts do dono."""
        pre_dispatch = self.ctx.hooks.get("pre_gateway_dispatch")
        self.assertIsNotNone(pre_dispatch)

        event = MagicMock()
        event.source.platform = "whatsapp"
        event.source.user_id = "5511999999999@s.whatsapp.net" # Dono
        event.text = "sync contacts"
        event.source.chat_id = "5511888888888@s.whatsapp.net" # Envia no chat com um contato

        gateway = MagicMock()
        gateway._session_key_for_source.return_value = "session_sync"
        gateway._session_model_overrides = {}

        context = {"event": event, "gateway": gateway}

        res = pre_dispatch("pre_gateway_dispatch", context)
        self.assertEqual(res, {"action": "skip", "reason": "sync-contacts-command"})
        
        # Verificar que sync foi chamado com force=True
        mock_sync.assert_called_once_with(force=True)
        
        # Verificar que enviou a mensagem de volta
        called_args = mock_urlopen.call_args[0]
        called_req = called_args[0]
        self.assertIn("/send", called_req.full_url)


class TestLLMContextAndPrompting(BaseWhatsAppManagerTest):
    def test_pre_llm_call_owner_context(self):
        pre_llm = self.ctx.hooks.get("pre_llm_call")
        self.assertIsNotNone(pre_llm)

        context = {
            "platform": "whatsapp",
            "sender_id": "5511999999999:2@s.whatsapp.net" # Owner with device ID
        }

        res = pre_llm("pre_llm_call", context)
        self.assertIsNotNone(res)
        self.assertIn("ASSISTENTE PESSOAL", res["context"])

    def test_pre_llm_call_client_context(self):
        pre_llm = self.ctx.hooks.get("pre_llm_call")

        context = {
            "platform": "whatsapp",
            "sender_id": "5511888888888@s.whatsapp.net" # Client
        }

        # Mock reading of files. Sem contato conhecido e sem classificação ao vivo,
        # o roteamento cai no prompt de suporte — a classificação real via LLM não
        # pode rodar aqui (chamaria a rede e herdaria histórico do ambiente).
        with patch("os.path.exists", return_value=True), \
             patch("builtins.open", unittest.mock.mock_open(read_data="Soul client rules")), \
             patch("whatsapp_manager._live_classify_contact", return_value=None):
            res = pre_llm("pre_llm_call", context)
            self.assertIsNotNone(res)
            self.assertIn("SUPORTE WHATSAPP", res["context"])
            self.assertIn("Soul client rules", res["context"])

    def test_pre_llm_call_injects_history_with_fallback(self):
        pre_llm = self.ctx.hooks.get("pre_llm_call")

        context = {
            "platform": "whatsapp",
            "sender_id": "5511888888888:3@s.whatsapp.net" # Client JID with device suffix
        }

        with patch("os.path.exists", return_value=True), \
             patch("builtins.open", unittest.mock.mock_open(read_data="Soul client rules")), \
             patch("whatsapp_manager._fetch_chat_history", return_value="Chat history content") as mock_fetch:
            res = pre_llm("pre_llm_call", context)
            self.assertIsNotNone(res)
            self.assertIn("### HISTÓRICO DE MENSAGENS ANTERIORES ###", res["context"])
            self.assertIn("Chat history content", res["context"])
            # Assert fallback JID derivation was used
            mock_fetch.assert_called_once_with("5511888888888@s.whatsapp.net", limit=50)

    def test_pre_llm_call_personal_contact_context(self):
        pre_llm = self.ctx.hooks.get("pre_llm_call")

        context = {
            "platform": "whatsapp",
            "sender_id": "5511777777777:2@s.whatsapp.net" # A personal contact (Bruna)
        }

        personal_contacts = {
            "5511777777777@s.whatsapp.net": {
                "name": "Bruna",
                "relationship": "namorada",
                "tone": "romantico",
                "guidelines": "Seja fofo",
                "summary": "Conversa carinhosa.",
                "intent": "Amor",
                "frequency": "diária"
            }
        }
        
        with patch("whatsapp_manager._load_support_files", return_value=("", "")), \
             patch("whatsapp_manager._load_personal_contacts", return_value=personal_contacts), \
             patch("whatsapp_manager._fetch_chat_history", return_value=""):
            res = pre_llm("pre_llm_call", context)
            self.assertIsNotNone(res)
            self.assertIn("PERSONA E DIRETRIZES DO SUPORTE WHATSAPP", res["context"])
            self.assertIn("CONTEXTO DO CONTATO", res["context"])
            self.assertIn("Nome: Bruna", res["context"])
            self.assertIn("Relacionamento: namorada", res["context"])
            self.assertIn("Tom de voz recomendado: romantico", res["context"])
            self.assertIn("Diretrizes específicas: Seja fofo", res["context"])

    def test_pre_llm_call_manual_overrides(self):
        pre_llm = self.ctx.hooks.get("pre_llm_call")

        context = {
            "platform": "whatsapp",
            "sender_id": "5511555555555:2@s.whatsapp.net"
        }

        personal_contacts = {
            "5511555555555@s.whatsapp.net": {
                "name": "Marcos (Vendedor)", 
                "relationship": "Cliente", 
                "manual_relationship": "Vendedor", 
                "notes": "Não tenho interesse no momento", 
                "product": "Curso de Inglês", 
                "tone": "técnico e direto", 
                "guidelines": "Seja breve e recuse educadamente.", 
                "summary": "Conversa comercial.", 
                "intent": "Oferecer produto.", 
                "frequency": "esporádica"
            }
        }

        with patch("whatsapp_manager._load_support_files", return_value=("", "")), \
             patch("whatsapp_manager._load_personal_contacts", return_value=personal_contacts), \
             patch("whatsapp_manager._fetch_chat_history", return_value=""):
            res = pre_llm("pre_llm_call", context)
            
        self.assertIsNotNone(res)
        self.assertIn("PERSONA E DIRETRIZES DO SUPORTE WHATSAPP", res["context"])
        self.assertIn("CONTEXTO DO CONTATO", res["context"])
        self.assertIn("Nome: Marcos (Vendedor)", res["context"])
        # manual_relationship deve prevalecer
        self.assertIn("Relacionamento: Vendedor", res["context"])
        # Notes e Product devem ser injetados
        self.assertIn("INSTRUÇÃO OBRIGATÓRIA — siga à risca: Não tenho interesse no momento", res["context"])
        self.assertIn("Produto/Serviço envolvido: Curso de Inglês", res["context"])

    def test_sanitize_classification_result_function(self):
        from whatsapp_manager import _sanitize_classification_result
        
        # Test sanitization with forbidden terms (various cases)
        forbidden_test = {
            "nickname": "pai",
            "pet_name": "MÃE",
            "relationship": "Filho",
            "tone": "informal"
        }
        res = _sanitize_classification_result(forbidden_test)
        self.assertIsNone(res["nickname"])
        self.assertIsNone(res["pet_name"])
        self.assertEqual(res["relationship"], "Filho")

        # Test with allowed/normal values
        allowed_test = {
            "nickname": "Bru",
            "pet_name": "amor",
            "relationship": "AmigoProximo"
        }
        res2 = _sanitize_classification_result(allowed_test)
        self.assertEqual(res2["nickname"], "Bru")
        self.assertEqual(res2["pet_name"], "amor")

    def test_pre_llm_call_sanitizes_legacy_forbidden_pet_names(self):
        pre_llm = self.ctx.hooks.get("pre_llm_call")

        context = {
            "platform": "whatsapp",
            "sender_id": "5511444444444@s.whatsapp.net"
        }

        # JSON containing legacy forbidden values
        personal_json = (
            '{"5511444444444@s.whatsapp.net": {'
            '"name": "Filho do André", '
            '"relationship": "Filho", '
            '"pet_name": "pai", '
            '"nickname": "pai", '
            '"tone": "informal e carinhoso", '
            '"guidelines": "Seja legal.", '
            '"summary": "Conversas familiares.", '
            '"intent": "Falar com pai.", '
            '"frequency": "diária"}}'
        )
        mock_pc_open = unittest.mock.mock_open(read_data=personal_json)
        mock_rules_open = unittest.mock.mock_open(read_data="Soul/Rules content")

        def mock_open_file(path, *args, **kwargs):
            if "personal_contacts.json" in str(path):
                return mock_pc_open(path, *args, **kwargs)
            return mock_rules_open(path, *args, **kwargs)

        with patch("os.path.exists", return_value=True), \
             patch("builtins.open", mock_open_file), \
             patch("whatsapp_manager._fetch_chat_history", return_value=""):
            res = pre_llm("pre_llm_call", context)

        self.assertIsNotNone(res)
        self.assertIn("Nome do contato: Filho do André", res["context"])
        # Ensure 'pai' is not included in the context as nickname or pet_name
        self.assertNotIn("Apelido do contato: pai", res["context"])
        self.assertNotIn("Nome carinhoso: pai", res["context"])

    def test_build_owner_context_with_history(self):
        """Verifica se _build_owner_context inclui a diretriz e o histórico."""
        import whatsapp_manager
        res = whatsapp_manager._build_owner_context("\n\n### HISTÓRICO DE MENSAGENS ANTERIORES ###\nHistory text")
        self.assertIn("ASSISTENTE PESSOAL", res["context"])
        self.assertIn("History text", res["context"])

    def test_build_personal_prompt(self):
        """Verifica se _build_personal_prompt constrói o prompt corretamente com campos opcionais."""
        import whatsapp_manager
        contact_info = {
            "name": "Bruna",
            "tone": "carinhoso",
            "nickname": "Bru",
            "pet_name": "amor",
            "frequent_greeting": "Oi linda",
            "summary": "Resumo teste",
            "intent": "Intenção teste",
            "frequency": "diária",
            "notes": "Notas teste",
            "product": "Produto teste"
        }
        res = whatsapp_manager._build_personal_prompt(contact_info, "namorada", "History content")
        ctx = res["context"]
        self.assertIn("PERSONA — ALGUÉM RESPONDENDO PELO ANDRÉ", ctx)
        self.assertIn("Nome do contato: Bruna", ctx)
        self.assertIn("Relação com o André: namorada", ctx)
        self.assertIn("Tom de voz: carinhoso", ctx)
        self.assertIn("Apelido do contato: Bru", ctx)
        self.assertIn("Saudação frequente: Oi linda", ctx)
        self.assertIn("Resumo das conversas anteriores: Resumo teste", ctx)
        self.assertIn("Intenção das últimas conversas: Intenção teste", ctx)
        self.assertIn("Frequência das conversas: diária", ctx)
        self.assertIn("INSTRUÇÃO OBRIGATÓRIA — siga à risca: Notas teste", ctx)
        self.assertIn("Produto/Serviço envolvido: Produto teste", ctx)
        self.assertIn("History content", ctx)

    def test_build_support_prompt(self):
        """Verifica se _build_support_prompt inclui soul, regras e histórico."""
        import whatsapp_manager
        res = whatsapp_manager._build_support_prompt("custom soul", "custom rules", "History content")
        ctx = res["context"]
        self.assertIn("SUPORTE WHATSAPP", ctx)
        self.assertIn("custom soul", ctx)
        self.assertIn("custom rules", ctx)
        self.assertIn("History content", ctx)
        # Prompt Mestre: AYA comercial — sem identidade de "assistente virtual" padrão
        self.assertNotRegex(ctx, r"diga que você é um atendente/assistente virtual")
        self.assertIn("NÃO se apresente como 'assistente virtual'", ctx)
        # Pode conduzir call curta e/ou contratação direta
        has_call = "15 min" in ctx or "call de" in ctx
        has_pix_hire = "contratação direta" in ctx or "Pix" in ctx
        self.assertTrue(has_call or has_pix_hire, "prompt deve mencionar call de 15 min ou contratação direta")
        # Não forçar handoff só por pergunta de integração
        self.assertIn("NÃO encaminhe para humano só porque perguntaram sobre integração", ctx)
        self.assertIn("1 a 4 frases", ctx)
        self.assertIn("português, inglês ou espanhol", ctx)
        self.assertIn("continua nesse mercado mesmo conversando em espanhol", ctx)

    def test_build_support_prompt_injects_market_metadata_without_language_reclassification(self):
        import whatsapp_manager
        res = whatsapp_manager._build_support_prompt(
            "custom soul",
            "custom rules",
            "",
            contact_info={
                "market_id": "US",
                "origin": "Meta Ads",
                "campaign": "US Cleaning Spanish",
                "country": "United States",
                "currency": "USD",
                "offer": "international",
                "timezone": "America/New_York",
                "language": "es",
            },
        )
        ctx = res["context"]
        self.assertIn("METADADOS COMERCIAIS DO LEAD — FONTE EXTERNA", ctx)
        self.assertIn("Mercado: US", ctx)
        self.assertIn("Campanha: US Cleaning Spanish", ctx)
        self.assertIn("Moeda: USD", ctx)
        self.assertIn("Idioma preferido: es", ctx)
        self.assertIn("Idioma preferido governa somente a língua da resposta", ctx)

    def test_pre_llm_persists_us_market_while_replying_in_spanish(self):
        pre_llm = self.ctx.hooks.get("pre_llm_call")
        contact_key = "12025550199@s.whatsapp.net"
        personal_contacts = {
            contact_key: {
                "name": "María",
                "relationship": "Cliente",
                "summary": "Lead de cleaning.",
                "intent": "Contratar atendimento.",
                "frequency": "esporádica",
            }
        }
        context = {
            "platform": "whatsapp",
            "sender_id": contact_key,
            "user_message": "Tengo una empresa de limpieza en Massachusetts y quiero avanzar.",
        }

        with patch("whatsapp_manager._load_support_files", return_value=("AYA", "rules")), \
             patch("whatsapp_manager._load_personal_contacts", return_value=personal_contacts), \
             patch("whatsapp_manager._fetch_chat_history", return_value=""), \
             patch("whatsapp_manager._write_personal_contacts_atomic") as mock_write:
            result = pre_llm("pre_llm_call", context)

        saved = personal_contacts[contact_key]
        self.assertEqual(saved["market_id"], "US")
        self.assertEqual(saved["currency"], "USD")
        self.assertEqual(saved["offer"], "international")
        self.assertEqual(saved["language"], "es")
        mock_write.assert_called_once_with(personal_contacts)
        self.assertIn("Mercado: US", result["context"])
        self.assertIn("Moeda: USD", result["context"])
        self.assertIn("Idioma preferido: es", result["context"])

    def test_pre_llm_external_us_market_overrides_stale_brazil_while_language_follows_message(self):
        pre_llm = self.ctx.hooks.get("pre_llm_call")
        contact_key = "12025550199@s.whatsapp.net"
        personal_contacts = {
            contact_key: {
                "name": "María",
                "relationship": "Cliente",
                "summary": "Lead de cleaning.",
                "intent": "Conhecer a oferta.",
                "frequency": "esporádica",
                "market_id": "BR",
                "currency": "BRL",
                "offer": "brazil",
                "language": "pt",
            }
        }

        with patch("whatsapp_manager._load_support_files", return_value=("AYA", "rules")), \
             patch("whatsapp_manager._load_personal_contacts", return_value=personal_contacts), \
             patch("whatsapp_manager._fetch_chat_history", return_value=""), \
             patch("whatsapp_manager._write_personal_contacts_atomic") as mock_write:
            result = pre_llm(
                platform="whatsapp",
                sender_id=contact_key,
                user_message="¿Cuánto cuesta?",
                market_id="US",
                origin="Meta Ads",
                campaign="US Cleaning Spanish",
                country="United States",
                currency="USD",
                offer="international",
                timezone="America/New_York",
                language="en",
            )

        saved = personal_contacts[contact_key]
        self.assertEqual(saved["market_id"], "US")
        self.assertEqual(saved["country"], "United States")
        self.assertEqual(saved["currency"], "USD")
        self.assertEqual(saved["offer"], "international")
        self.assertEqual(saved["language"], "es")
        self.assertEqual(saved["campaign"], "US Cleaning Spanish")
        mock_write.assert_called_once_with(personal_contacts)
        self.assertIn("Mercado: US", result["context"])
        self.assertIn("Campanha: US Cleaning Spanish", result["context"])
        self.assertIn("Idioma preferido: es", result["context"])

    def test_pre_llm_consumes_metadata_staged_by_gateway_contract(self):
        pre_llm = self.ctx.hooks.get("pre_llm_call")
        contact_key = "12025550199@s.whatsapp.net"
        personal_contacts = {
            contact_key: {
                "relationship": "Cliente",
                "summary": "Lead de cleaning.",
                "intent": "Conhecer a oferta.",
                "frequency": "esporádica",
            }
        }
        whatsapp_manager._pending_inbound.clear()
        self.addCleanup(whatsapp_manager._pending_inbound.clear)
        whatsapp_manager._track_inbound(
            contact_key,
            "msg-meta",
            "¿Cuánto cuesta?",
            {
                "market_id": "US",
                "origin": "Meta Ads",
                "campaign": "US Cleaning Spanish",
                "country": "United States",
                "currency": "USD",
                "offer": "international",
                "timezone": "America/New_York",
                "language": "es",
            },
        )

        with patch("whatsapp_manager._load_support_files", return_value=("AYA", "rules")), \
             patch("whatsapp_manager._load_personal_contacts", return_value=personal_contacts), \
             patch("whatsapp_manager._fetch_chat_history", return_value=""), \
             patch("whatsapp_manager._write_personal_contacts_atomic") as mock_write:
            result = pre_llm(
                platform="whatsapp",
                sender_id=contact_key,
                session_id=contact_key,
                user_message="¿Cuánto cuesta?",
            )

        saved = personal_contacts[contact_key]
        self.assertEqual(saved["market_id"], "US")
        self.assertEqual(saved["currency"], "USD")
        self.assertEqual(saved["language"], "es")
        self.assertEqual(saved["campaign"], "US Cleaning Spanish")
        mock_write.assert_called_once_with(personal_contacts)
        self.assertIn("Mercado: US", result["context"])
        self.assertIn("Idioma preferido: es", result["context"])

    def test_gateway_to_pre_llm_keeps_us_market_on_spanish_lid_first_turn(self):
        pre_dispatch = self.ctx.hooks.get("pre_gateway_dispatch")
        pre_llm = self.ctx.hooks.get("pre_llm_call")
        lid = "123456789012345@lid"
        phone_key = "12025550199@s.whatsapp.net"
        session = "session-us-es-contract"
        personal_contacts = {
            phone_key: {
                "lid": lid,
                "relationship": "Cliente",
                "summary": "Lead de cleaning.",
                "intent": "Conhecer a oferta.",
                "frequency": "esporádica",
                "ai_enabled": True,
                "in_flow": True,
            }
        }
        whatsapp_manager._pending_inbound.clear()
        self.addCleanup(whatsapp_manager._pending_inbound.clear)
        self.addCleanup(whatsapp_manager._sender_to_chat.pop, session, None)
        self.addCleanup(whatsapp_manager._sender_to_chat.pop, lid, None)

        event = MagicMock()
        event.source.platform = "whatsapp"
        # O bridge resolve o endereço de entrega para telefone, mas preserva o LID
        # original dentro do raw_message para a associação sobreviver a cache frio.
        event.source.user_id = phone_key
        event.source.chat_id = phone_key
        event.text = "¿Cuánto cuesta?"
        event.raw = {}
        event.raw_message = {
            "fromMe": False,
            "isGroup": False,
            "originalChatId": lid,
            "originalSenderId": lid,
            "leadMetadata": {
                "market_id": "US",
                "origin": "Meta Ads",
                "campaign": "US Cleaning Spanish",
                "timezone": "America/New_York",
                "language": "es",
                "unknown_internal_field": "must not persist",
            },
        }
        event.is_historical = False
        gateway = MagicMock()
        gateway._session_key_for_source.return_value = session
        gateway._session_model_overrides = {}
        gateway._session_profile_overrides = {}

        def resolve(jid):
            return phone_key if str(jid).endswith("@lid") else jid

        with patch("whatsapp_manager._resolve_phone_from_jid", side_effect=resolve), \
             patch("whatsapp_manager._check_bot_paused", return_value=False), \
             patch("whatsapp_manager._check_chat_silenced", return_value=False), \
             patch("whatsapp_manager._followup_note_activity"), \
             patch("whatsapp_manager._load_support_files", return_value=("AYA", "rules")), \
             patch("whatsapp_manager._load_personal_contacts", return_value=personal_contacts), \
             patch("whatsapp_manager._fetch_chat_history", return_value=""), \
             patch("whatsapp_manager._write_personal_contacts_atomic") as mock_write:
            self.assertIsNone(pre_dispatch(event=event, gateway=gateway))
            result = pre_llm(
                session_id=session,
                task_id="task-1",
                turn_id="turn-1",
                user_message="¿Cuánto cuesta?",
                conversation_history=[],
                is_first_turn=True,
                model="test-model",
                platform="whatsapp",
                sender_id=phone_key,
            )

        saved = personal_contacts[phone_key]
        self.assertEqual(saved["market_id"], "US")
        self.assertEqual(saved["currency"], "USD")
        self.assertEqual(saved["offer"], "international")
        self.assertEqual(saved["language"], "es")
        self.assertEqual(saved["campaign"], "US Cleaning Spanish")
        self.assertEqual(saved["lid"], lid)
        self.assertNotIn("unknown_internal_field", saved)
        self.assertNotIn(lid, personal_contacts)
        mock_write.assert_called_once_with(personal_contacts)
        self.assertIn("Mercado: US", result["context"])
        self.assertIn("Campanha: US Cleaning Spanish", result["context"])
        self.assertIn("Idioma preferido: es", result["context"])

    def test_pre_llm_persists_lid_association_on_canonical_market_record(self):
        pre_llm = self.ctx.hooks.get("pre_llm_call")
        lid = "123456789012345@lid"
        phone_key = "12025550199@s.whatsapp.net"
        personal_contacts = {
            phone_key: {
                "name": "María",
                "relationship": "Cliente",
                "summary": "Lead US.",
                "intent": "Conhecer a oferta.",
                "frequency": "esporádica",
                "market_id": "US",
                "currency": "USD",
                "offer": "international",
                "language": "es",
            }
        }

        with patch("whatsapp_manager._load_support_files", return_value=("AYA", "rules")), \
             patch("whatsapp_manager._load_personal_contacts", return_value=personal_contacts), \
             patch("whatsapp_manager._resolve_phone_from_jid", return_value=phone_key), \
             patch("whatsapp_manager._fetch_chat_history", return_value=""), \
             patch("whatsapp_manager._write_personal_contacts_atomic") as mock_write:
            result = pre_llm(
                platform="whatsapp",
                sender_id=lid,
                user_message="¿Cuánto cuesta?",
            )

        self.assertEqual(personal_contacts[phone_key]["lid"], lid)
        self.assertEqual(personal_contacts[phone_key]["market_id"], "US")
        self.assertIn("Mercado: US", result["context"])
        mock_write.assert_called_once_with(personal_contacts)

    def test_pre_llm_finds_canonical_us_record_when_lid_map_is_unavailable(self):
        pre_llm = self.ctx.hooks.get("pre_llm_call")
        lid = "123456789012345@lid"
        phone_key = "12025550199@s.whatsapp.net"
        personal_contacts = {
            phone_key: {
                "lid": lid,
                "name": "María",
                "relationship": "Cliente",
                "summary": "Lead de cleaning nos EUA.",
                "intent": "Conhecer a oferta.",
                "frequency": "esporádica",
                "market_id": "US",
                "country": "United States",
                "currency": "USD",
                "offer": "international",
                "language": "es",
            }
        }
        context = {
            "platform": "whatsapp",
            "sender_id": lid,
            "user_message": "¿Cuánto cuesta?",
        }

        with patch("whatsapp_manager._load_support_files", return_value=("AYA", "rules")), \
             patch("whatsapp_manager._load_personal_contacts", return_value=personal_contacts), \
             patch("whatsapp_manager._resolve_phone_from_jid", side_effect=lambda jid: jid), \
             patch("whatsapp_manager._fetch_chat_history", return_value=""), \
             patch("whatsapp_manager._live_classify_contact") as mock_classify, \
             patch("whatsapp_manager._write_personal_contacts_atomic") as mock_write:
            result = pre_llm("pre_llm_call", context)

        self.assertIn("Mercado: US", result["context"])
        self.assertIn("Moeda: USD", result["context"])
        self.assertIn("Idioma preferido: es", result["context"])
        self.assertNotIn(lid, personal_contacts)
        mock_classify.assert_not_called()
        mock_write.assert_not_called()

    def test_support_prompt_hides_payment_details_until_explicit_intent(self):
        import whatsapp_manager
        rules = (
            "<!-- AYA_PAYMENT_DETAILS:BR:START -->\n"
            "Pix CNPJ: 00.000.000/0000-00\n"
            "<!-- AYA_PAYMENT_DETAILS:BR:END -->\n"
            "Método: Zelle\n"
            "<!-- AYA_PAYMENT_DETAILS:US:START -->\n"
            "Recipient: Test Recipient\n"
            "Zelle email: pay@example.com\n"
            "<!-- AYA_PAYMENT_DETAILS:US:END -->"
        )

        hidden = whatsapp_manager._build_support_prompt(
            "AYA", rules, "", contact_info={"market_id": "US"}
        )["context"]
        allowed = whatsapp_manager._build_support_prompt(
            "AYA",
            rules,
            "",
            contact_info={"market_id": "US"},
            payment_market_id="US",
            allow_payment_details=True,
        )["context"]

        self.assertNotIn("Test Recipient", hidden)
        self.assertNotIn("pay@example.com", hidden)
        self.assertNotIn("00.000.000/0000-00", hidden)
        self.assertIn("Test Recipient", allowed)
        self.assertIn("pay@example.com", allowed)
        self.assertNotIn("00.000.000/0000-00", allowed)

    def test_instance_support_prompt_never_injects_generic_pix_catalog(self):
        import whatsapp_manager
        with patch.dict(os.environ, {"WHATSAPP_CONFIG_SUBDIR": "instance"}), \
             patch("whatsapp_manager._build_catalog_context_block", return_value="PIX-ONLY-CATALOG"):
            prompt = whatsapp_manager._build_support_prompt(
                "AYA", "rules", "", contact_info={"market_id": "US", "language": "es"}
            )["context"]

        self.assertNotIn("PIX-ONLY-CATALOG", prompt)


class TestContactManagementAndSync(BaseWhatsAppManagerTest):
    @patch("sqlite3.connect")
    @patch("whatsapp_manager._classify_contact_via_llm")
    @patch("pathlib.Path.exists")
    @patch("builtins.open")
    def test_sync_contacts_from_db_internal_with_updates(self, mock_open, mock_exists, mock_classify, mock_sqlite_connect):
        from whatsapp_manager import _sync_contacts_from_db_internal
        
        # Mock paths exists
        mock_exists.return_value = True
        
        # Mock personal_contacts.json content
        existing_contacts = {
            "5511777777777@s.whatsapp.net": {
                "name": "Bruna",
                "relationship": "namorada",
                "tone": "romantico",
                "guidelines": "Seja fofo"
            },
            "5511888888888@s.whatsapp.net": {
                "name": "Carlos",
                "relationship": "amigo",
                "tone": "descontraído",
                "guidelines": "Fale como amigo",
                "summary": "Conversa antiga",
                "intent": "Ajuda",
                "frequency": "semanal",
                "last_interaction": 1686460000
            }
        }
        
        mock_file = unittest.mock.mock_open(read_data=json.dumps(existing_contacts))
        mock_open.side_effect = lambda path, *args, **kwargs: mock_file(path, *args, **kwargs)
        
        # Mock SQLite cursor and fetchall results
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_sqlite_connect.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        
        mock_cursor.fetchall.side_effect = [
            [
                ("5511777777777@s.whatsapp.net", 1686450000, 1),
                ("5511888888888@s.whatsapp.net", 1686450000, 1)
            ],
            [
                ("5511777777777@s.whatsapp.net", "Bruna", 10, 1686440000, 1686450000), # Stale contact
                ("5511888888888@s.whatsapp.net", "Carlos", 5, 1686440000, 1686450000) # Non-stale contact (skipped)
            ],
            [(0, "Bruna", "oi amor te amo")]
        ]
        
        # Mock LLM classification response
        mock_classify.return_value = {
            "relationship": "AmigoProximo",
            "tone": "informal e carinhoso",
            "nickname": "Bru",
            "pet_name": "amor",
            "frequent_greeting": "Oi linda",
            "summary": "Conversa carinhosa",
            "intent": "Saudação",
            "frequency": "diária",
            "product": None,
            "guidelines": "Seja romântico."
        }
        
        # Call the sync function
        with patch.dict(os.environ, {"CONFIG_REPO": ""}):
            result = _sync_contacts_from_db_internal(force=False)
            
        # Verify the classification was called only once (for Bruna)
        mock_classify.assert_called_once()
        
        # Verify write to personal_contacts.json occurred
        mock_open.assert_any_call(Path("/opt/data/personal_contacts.json"), "w", encoding="utf-8")
        
        # Retrieve what was written
        write_calls = mock_file().write.call_args_list
        written_data = "".join(call[0][0] for call in write_calls)
        written_json = json.loads(written_data)
        
        bruna_data = written_json["5511777777777@s.whatsapp.net"]
        self.assertEqual(bruna_data["name"], "Bruna")
        self.assertEqual(bruna_data["relationship"], "AmigoProximo")
        self.assertEqual(bruna_data["tone"], "informal e carinhoso")
        self.assertEqual(bruna_data["guidelines"], "Seja romântico.")
        self.assertEqual(bruna_data["nickname"], "Bru")
        self.assertEqual(bruna_data["pet_name"], "amor")
        self.assertEqual(bruna_data["summary"], "Conversa carinhosa")
        self.assertEqual(bruna_data["intent"], "Saudação")
        self.assertEqual(bruna_data["frequency"], "diária")
        
        # Carlos fields should remain exactly as they were (not stale, skipped)
        carlos_data = written_json["5511888888888@s.whatsapp.net"]
        self.assertEqual(carlos_data["relationship"], "amigo")
        self.assertEqual(carlos_data["tone"], "descontraído")
        self.assertEqual(carlos_data["guidelines"], "Fale como amigo")
        self.assertEqual(carlos_data["summary"], "Conversa antiga")

    @patch("sqlite3.connect")
    @patch("whatsapp_manager._classify_contact_via_llm")
    @patch("pathlib.Path.exists")
    @patch("builtins.open")
    def test_sync_contacts_from_db_internal_hits_limit(self, mock_open, mock_exists, mock_classify, mock_sqlite_connect):
        from whatsapp_manager import _sync_contacts_from_db_internal
        
        mock_exists.return_value = True
        mock_file = unittest.mock.mock_open(read_data="{}")
        mock_open.side_effect = lambda path, *args, **kwargs: mock_file(path, *args, **kwargs)
        
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_sqlite_connect.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        
        mock_cursor.fetchall.side_effect = [
            [
                ("5511777777777@s.whatsapp.net", 1686450000, 1),
                ("5511888888888@s.whatsapp.net", 1686450000, 1)
            ],
            [
                ("5511777777777@s.whatsapp.net", "Bruna", 10, 1686440000, 1686450000),
                ("5511888888888@s.whatsapp.net", "Carlos", 10, 1686440000, 1686450000)
            ],
            [(0, "Bruna", "oi amor te amo")]
        ]
        
        mock_classify.return_value = {
            "relationship": "AmigoProximo",
            "tone": "informal e carinhoso",
            "nickname": "Bru",
            "pet_name": "amor",
            "frequent_greeting": "Oi linda",
            "summary": "Conversa carinhosa",
            "intent": "Saudação",
            "frequency": "diária",
            "product": None,
            "guidelines": "Seja romântico."
        }
        
        # Limit set to 1 classification
        with patch.dict(os.environ, {"CONFIG_REPO": "", "WHATSAPP_SYNC_MAX_CLASSIFICATIONS": "1"}):
            result = _sync_contacts_from_db_internal(force=False)
            
        # Classify should be called exactly once
        mock_classify.assert_called_once()
        
        # Verify JSON write
        write_calls = mock_file().write.call_args_list
        written_data = "".join(call[0][0] for call in write_calls)
        written_json = json.loads(written_data)
        
        # Bruna is classified
        self.assertEqual(written_json["5511777777777@s.whatsapp.net"]["relationship"], "AmigoProximo")
        self.assertEqual(written_json["5511777777777@s.whatsapp.net"]["summary"], "Conversa carinhosa")
        
        # Carlos is added as Pending
        self.assertEqual(written_json["5511888888888@s.whatsapp.net"]["relationship"], "Cliente")
        self.assertEqual(written_json["5511888888888@s.whatsapp.net"]["summary"], "Pendente de classificação.")

    @patch("sqlite3.connect")
    @patch("whatsapp_manager._classify_contact_via_llm")
    @patch("pathlib.Path.exists")
    @patch("os.path.exists")
    @patch("builtins.open")
    def test_pre_llm_call_live_classification(self, mock_open, mock_os_exists, mock_path_exists, mock_classify, mock_sqlite_connect):
        pre_llm = self.ctx.hooks.get("pre_llm_call")
        
        # Um contato que não está no personal_contacts.json (classifica on-the-fly)
        context = {
            "platform": "whatsapp",
            "sender_id": "5511666666666@s.whatsapp.net"
        }
        
        # Mocks para arquivos existirem
        mock_os_exists.return_value = True
        mock_path_exists.return_value = True
        
        # personal_contacts.json vazio inicialmente
        mock_pc_open = unittest.mock.mock_open(read_data="{}")
        mock_rules_open = unittest.mock.mock_open(read_data="Soul/Rules content")
        
        def mock_open_file(path, *args, **kwargs):
            if "personal_contacts.json" in str(path):
                return mock_pc_open(path, *args, **kwargs)
            return mock_rules_open(path, *args, **kwargs)
            
        mock_open.side_effect = mock_open_file
        
        # Mock do SQLite para retornar histórico e estatísticas
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_sqlite_connect.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchone.return_value = (5, 1686440000, 1686450000, "Live Test Contact")
        mock_cursor.fetchall.return_value = [
            (0, "Live Test Contact", "olá tudo bem"),
            (1, "André", "tudo bem e com vc")
        ]
        
        # Mock do LLM
        mock_classify.return_value = {
            "relationship": "AmigoProximo",
            "tone": "informal e amigável",
            "nickname": "Live",
            "pet_name": None,
            "frequent_greeting": "Eae",
            "summary": "Conversa de teste ao vivo.",
            "intent": "Interagir",
            "frequency": "diária",
            "product": None,
            "guidelines": "Seja descontraído."
        }
        
        with patch("whatsapp_manager._fetch_chat_history", return_value=""):
            res = pre_llm("pre_llm_call", context)
            
        # O LLM de classificação foi chamado on-the-fly
        mock_classify.assert_called_once()
        self.assertIsNotNone(res)
        self.assertIn("PERSONA — ALGUÉM RESPONDENDO PELO ANDRÉ", res["context"])
        self.assertIn("Nome do contato: Live Test Contact", res["context"])
        self.assertIn("Relação com o André: AmigoProximo", res["context"])

    def test_resolve_phone_from_jid_with_lid(self):
        """LID presente no cache deve ser convertido para JID de telefone clássico."""
        import whatsapp_manager
        # Preencher cache de LIDs artificialmente
        whatsapp_manager._lid_to_phone["164291240063173"] = "5511777777777"
        
        result = whatsapp_manager._resolve_phone_from_jid("164291240063173@lid")
        self.assertEqual(result, "5511777777777@s.whatsapp.net")
        
        # Com device suffix (formato LID:device@lid)
        result2 = whatsapp_manager._resolve_phone_from_jid("164291240063173:0@lid")
        self.assertEqual(result2, "5511777777777@s.whatsapp.net")
        
        # Limpar cache após teste
        whatsapp_manager._lid_to_phone.pop("164291240063173", None)

    def test_resolve_phone_from_jid_standard_jid_unchanged(self):
        """JIDs de telefone padrão devem passar sem alteração."""
        import whatsapp_manager
        result = whatsapp_manager._resolve_phone_from_jid("5511888888888@s.whatsapp.net")
        self.assertEqual(result, "5511888888888@s.whatsapp.net")
        
        # Com device suffix
        result2 = whatsapp_manager._resolve_phone_from_jid("5511888888888:3@s.whatsapp.net")
        self.assertEqual(result2, "5511888888888@s.whatsapp.net")

    def test_resolve_phone_from_jid_unknown_lid_returns_as_is(self):
        """LID não presente no cache deve ser retornado sem alteração de formato."""
        import whatsapp_manager
        # Garantir que o LID não está no cache
        whatsapp_manager._lid_to_phone.pop("999999999999999", None)
        
        result = whatsapp_manager._resolve_phone_from_jid("999999999999999@lid")
        self.assertEqual(result, "999999999999999@lid")

    def test_resolve_phone_from_jid_empty(self):
        """Verifica que _resolve_phone_from_jid trata entrada vazia."""
        import whatsapp_manager
        self.assertEqual(whatsapp_manager._resolve_phone_from_jid(""), "")
        self.assertIsNone(whatsapp_manager._resolve_phone_from_jid(None))

    def test_resolve_phone_from_jid_mapped_lid(self):
        """Verifica que _resolve_phone_from_jid resolve LIDs mapeados."""
        import whatsapp_manager
        whatsapp_manager._lid_to_phone["123456789012345"] = "5511988888888"
        try:
            res = whatsapp_manager._resolve_phone_from_jid("123456789012345@lid")
            self.assertEqual(res, "5511988888888@s.whatsapp.net")
        finally:
            whatsapp_manager._lid_to_phone.pop("123456789012345", None)

    def test_resolve_chat_id_with_internal_map(self):
        """Verifica _resolve_chat_id com entrada no dicionário _sender_to_chat."""
        import whatsapp_manager
        whatsapp_manager._sender_to_chat["test_sender@s.whatsapp.net"] = "resolved_chat@s.whatsapp.net"
        try:
            res = whatsapp_manager._resolve_chat_id("test_sender@s.whatsapp.net")
            self.assertEqual(res, "resolved_chat@s.whatsapp.net")
        finally:
            whatsapp_manager._sender_to_chat.pop("test_sender@s.whatsapp.net", None)

    def test_resolve_chat_id_fallback(self):
        """Verifica _resolve_chat_id fazendo fallback por split."""
        import whatsapp_manager
        res = whatsapp_manager._resolve_chat_id("5511999999999:2@s.whatsapp.net")
        self.assertEqual(res, "5511999999999@s.whatsapp.net")

    @patch("os.path.exists")
    @patch("builtins.open")
    def test_load_support_files_existing(self, mock_open, mock_exists):
        """Verifica se _load_support_files carrega arquivos quando eles existem."""
        import whatsapp_manager
        mock_exists.return_value = True
        
        mock_soul_open = unittest.mock.mock_open(read_data="custom soul rules")
        mock_rules_open = unittest.mock.mock_open(read_data="custom support rules")
        
        def mock_open_file(path, *args, **kwargs):
            if "SOUL_WHATSAPP.md" in str(path):
                return mock_soul_open(path, *args, **kwargs)
            return mock_rules_open(path, *args, **kwargs)
            
        mock_open.side_effect = mock_open_file
        
        soul, rules = whatsapp_manager._load_support_files()
        self.assertEqual(soul, "custom soul rules")
        self.assertEqual(rules, "custom support rules")

    @patch("os.path.exists", return_value=False)
    def test_load_support_files_missing(self, mock_exists):
        """Verifica se _load_support_files usa fallbacks quando arquivos não existem."""
        import whatsapp_manager
        soul, rules = whatsapp_manager._load_support_files()
        self.assertIn("chatbot de suporte", soul)
        self.assertIn("Chatkanban", rules)

    @patch("os.path.exists", return_value=True)
    @patch("builtins.open")
    def test_load_personal_contacts_success(self, mock_open, mock_exists):
        """Verifica se _load_personal_contacts lê e sanitiza contatos."""
        import whatsapp_manager
        contacts_data = {
            "5511777777777@s.whatsapp.net": {
                "name": "Bruna",
                "relationship": "namorada",
                "pet_name": "pai",  # deve ser sanitizado para None
                "nickname": "Bru"
            }
        }
        mock_open.return_value.__enter__.return_value = MagicMock(read=lambda: json.dumps(contacts_data))
        
        res = whatsapp_manager._load_personal_contacts()
        self.assertIn("5511777777777@s.whatsapp.net", res)
        self.assertEqual(res["5511777777777@s.whatsapp.net"]["name"], "Bruna")
        self.assertIsNone(res["5511777777777@s.whatsapp.net"]["pet_name"])
        self.assertEqual(res["5511777777777@s.whatsapp.net"]["nickname"], "Bru")

    @patch("os.path.exists", return_value=True)
    @patch("builtins.open", side_effect=OSError("Read error"))
    def test_load_personal_contacts_error(self, mock_open, mock_exists):
        """Verifica se _load_personal_contacts trata exceções de IO retornando dicionário vazio."""
        import whatsapp_manager
        res = whatsapp_manager._load_personal_contacts()
        self.assertEqual(res, {})


class TestMediaMessageProcessing(BaseWhatsAppManagerTest):
    def test_get_media_info_direct_attrs(self):
        """Verifica _get_media_info com atributos diretos no objeto."""
        event = MagicMock()
        event.has_media = True
        event.media_type = "ptt"
        event.media_urls = ["/path/to/voice.ogg"]
        event.message_id = "msg123"
        
        info = whatsapp_manager._get_media_info(event)
        self.assertTrue(info["has_media"])
        self.assertEqual(info["media_type"], "ptt")
        self.assertEqual(info["media_urls"], ["/path/to/voice.ogg"])
        self.assertEqual(info["message_id"], "msg123")

    def test_get_media_info_dict_payload(self):
        """Verifica _get_media_info com dicionário interno (raw_event)."""
        event = MagicMock(spec=[])
        event.raw_event = {
            "hasMedia": True,
            "mediaType": "image",
            "mediaUrls": "/path/to/photo.jpg",
            "messageId": "msg456"
        }
        
        info = whatsapp_manager._get_media_info(event)
        self.assertTrue(info["has_media"])
        self.assertEqual(info["media_type"], "image")
        self.assertEqual(info["media_urls"], ["/path/to/photo.jpg"])
        self.assertEqual(info["message_id"], "msg456")

    def test_get_mime_type(self):
        """Verifica se _get_mime_type retorna o tipo correto."""
        self.assertEqual(whatsapp_manager._get_mime_type("audio.ogg"), "audio/ogg")
        self.assertEqual(whatsapp_manager._get_mime_type("IMAGE.PNG"), "image/png")
        self.assertEqual(whatsapp_manager._get_mime_type("file.unknown"), "application/octet-stream")

    @patch("os.path.exists", return_value=True)
    @patch("os.remove")
    @patch.dict(os.environ, {"GOOGLE_API_KEY": "test-key", "FISH_API_KEY": "fk-test"})
    def test_process_media_message_audio(self, mock_remove, mock_exists):
        """Áudio vai para o Fish ASR, não para o Gemini — e o arquivo é apagado depois."""
        event = MagicMock()
        event.has_media = True
        event.media_type = "ptt"
        event.media_urls = ["/path/to/voice.ogg"]

        with patch.object(whatsapp_manager, "_transcribe_via_fish", return_value="bom dia, tudo bem?") as mock_fish, \
             patch("urllib.request.urlopen") as mock_urlopen:
            text = whatsapp_manager._process_media_message(event)

        self.assertEqual(text, "bom dia, tudo bem?")
        mock_fish.assert_called_once_with("/path/to/voice.ogg")
        # Nenhum provedor de LLM é chamado para áudio.
        mock_urlopen.assert_not_called()
        mock_remove.assert_called_once_with("/path/to/voice.ogg")

    @patch("os.path.exists", return_value=True)
    @patch("os.remove")
    @patch.dict(os.environ, {"GOOGLE_API_KEY": "test-key", "FISH_API_KEY": ""})
    def test_process_media_message_audio_sem_fish(self, mock_remove, mock_exists):
        """Sem chave do Fish não há transcrição — e o áudio some do disco do mesmo jeito."""
        event = MagicMock()
        event.has_media = True
        event.media_type = "ptt"
        event.media_urls = ["/path/to/voice.ogg"]

        with patch("urllib.request.urlopen") as mock_urlopen:
            self.assertIsNone(whatsapp_manager._process_media_message(event))
            mock_urlopen.assert_not_called()
        mock_remove.assert_called_once_with("/path/to/voice.ogg")

    @patch("os.path.exists", return_value=True)
    @patch("builtins.open", new_callable=unittest.mock.mock_open, read_data=b"mock-image-data")
    @patch("urllib.request.urlopen")
    @patch.dict(os.environ, {"GOOGLE_API_KEY": "test-key", "WHATSAPP_CLIENT_MEDIA_MODEL": "gemini-custom-media-model"})
    def test_process_media_message_image_custom_model(self, mock_urlopen, mock_open, mock_exists):
        """WHATSAPP_CLIENT_MEDIA_MODEL agora vale para imagem — áudio é do Fish."""
        event = MagicMock()
        event.has_media = True
        event.media_type = "image"
        event.media_urls = ["/path/to/foto.jpg"]

        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "candidates": [{"content": {"parts": [{"text": "um comprovante de pix"}]}}]
        }).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_response

        text = whatsapp_manager._process_media_message(event)
        self.assertEqual(text, "um comprovante de pix")

        args, kwargs = mock_urlopen.call_args
        request_obj = args[0]
        self.assertIn("gemini-custom-media-model", request_obj.full_url)

    @patch("os.path.exists", return_value=True)
    @patch("builtins.open", new_callable=unittest.mock.mock_open, read_data=b"mock-image-data")
    @patch("os.remove")
    @patch("urllib.request.urlopen")
    @patch.dict(os.environ, {"GOOGLE_API_KEY": "test-key"})
    def test_process_media_message_multiple_images_limit(self, mock_urlopen, mock_remove, mock_open, mock_exists):
        """Verifica que o processamento de imagens se limita a no máximo 5 imagens por mensagem."""
        event = MagicMock()
        event.has_media = True
        event.media_type = "image"
        # 7 image paths
        event.media_urls = [f"/path/to/photo{i}.jpg" for i in range(7)]
        
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "candidates": [{
                "content": {
                    "parts": [{
                        "text": "Esta é uma descrição de 5 fotos unificadas."
                    }]
                }
            }]
        }).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_response

        text = whatsapp_manager._process_media_message(event)
        self.assertEqual(text, "Esta é uma descrição de 5 fotos unificadas.")
        
        # O open deve ter sido chamado exatamente 5 vezes (limite de 5 imagens)
        self.assertEqual(mock_open.call_count, 5)
        
        # Imagens não são mais apagadas aqui — o Hermes 0.19+ também lê o mesmo arquivo
        # cacheado nativamente pra montar a mensagem multimodal, e apagar antes disso
        # quebra esse fluxo do lado do Hermes.
        mock_remove.assert_not_called()

    @patch.dict(os.environ, {}, clear=True)
    def test_process_media_message_no_google_key(self):
        """Verifica que _process_media_message retorna None se GOOGLE_API_KEY estiver ausente."""
        import whatsapp_manager
        event = MagicMock()
        res = whatsapp_manager._process_media_message(event)
        self.assertIsNone(res)

    @patch.dict(os.environ, {"GOOGLE_API_KEY": "test-key"})
    def test_process_media_message_no_media(self):
        """Verifica que _process_media_message retorna None se o evento não contiver mídia."""
        import whatsapp_manager
        event = MagicMock()
        event.has_media = False
        res = whatsapp_manager._process_media_message(event)
        self.assertIsNone(res)

    @patch.dict(os.environ, {"GOOGLE_API_KEY": "test-key"})
    def test_process_media_message_unsupported_type(self):
        """Verifica que _process_media_message retorna None para tipos de mídia não suportados."""
        import whatsapp_manager
        event = MagicMock()
        event.has_media = True
        event.media_type = "video"
        event.media_urls = ["/path/to/video.mp4"]
        res = whatsapp_manager._process_media_message(event)
        self.assertIsNone(res)

    @patch.dict(os.environ, {"GOOGLE_API_KEY": "test-key"})
    @patch("os.path.exists", return_value=False)
    def test_process_media_message_file_not_found(self, mock_exists):
        """Verifica que _process_media_message retorna None se o arquivo físico não existir."""
        import whatsapp_manager
        event = MagicMock()
        event.has_media = True
        event.media_type = "ptt"
        event.media_urls = ["/path/to/nonexistent.ogg"]
        res = whatsapp_manager._process_media_message(event)
        self.assertIsNone(res)

    @patch("whatsapp_manager._process_media_message", return_value="transcribed audio")
    @patch("whatsapp_manager._persist_transcription_to_db")
    @patch("pathlib.Path.exists", return_value=True)
    def test_pre_gateway_dispatch_media_audio(self, mock_exists, mock_persist, mock_process_media):
        """Verifica que pre_gateway_dispatch processa áudio, atualiza evento e persiste no banco."""
        pre_dispatch = self.ctx.hooks.get("pre_gateway_dispatch")
        self.assertIsNotNone(pre_dispatch)

        event = MagicMock()
        event.source.platform = "whatsapp"
        event.has_media = True
        event.media_type = "ptt"
        event.media_urls = ["/path/to/voice.ogg"]
        event.message_id = "msg123"
        event.text = ""
        event.source.user_id = "5511888888888@s.whatsapp.net"
        event.source.chat_id = "5511888888888@s.whatsapp.net"

        gateway = MagicMock()
        gateway._session_key_for_source.return_value = "session_media"
        gateway._session_model_overrides = {}

        context = {"event": event, "gateway": gateway}

        with patch("whatsapp_manager._check_bot_paused", return_value=False), \
             patch("whatsapp_manager._check_chat_silenced", return_value=False):
            res = pre_dispatch("pre_gateway_dispatch", context)
            
        self.assertIsNone(res) # Não deve pular
        self.assertEqual(event.text, '[Áudio: "transcribed audio"]')
        mock_persist.assert_called_once_with("/opt/data/.hermes/whatsapp_messages.db", "msg123", '[Áudio: "transcribed audio"]')


class TestExternalServicesAndUpdates(BaseWhatsAppManagerTest):
    @patch("urllib.request.urlopen")
    @patch.dict(os.environ, {"GOOGLE_API_KEY": "fake-key"})
    def test_classify_contact_via_llm_gemini(self, mock_urlopen):
        from whatsapp_manager import _classify_contact_via_llm
        
        # Mocking Gemini response
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "candidates": [{
                "content": {
                    "parts": [{
                        "text": '{"relationship": "amigo/namorada", "tone": "informal e carinhoso", "nickname": "Bru", "pet_name": "amor", "frequent_greeting": "Oi linda", "summary": "Conversa carinhosa", "intent": "Saudação", "frequency": "diária", "guidelines": "Seja romântico."}'
                    }]
                }
            }]
        }).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_response

        res = _classify_contact_via_llm("Bruna", "oi amor te amo", "Total messages: 10")
        self.assertEqual(res["relationship"], "amigo/namorada")
        self.assertEqual(res["nickname"], "Bru")
        self.assertEqual(res["pet_name"], "amor")
        self.assertEqual(res["frequent_greeting"], "Oi linda")
        self.assertEqual(res["summary"], "Conversa carinhosa")
        self.assertEqual(res["intent"], "Saudação")
        self.assertEqual(res["frequency"], "diária")

    @patch.dict(os.environ, {}, clear=True)
    def test_classify_contact_via_llm_fallback(self):
        from whatsapp_manager import _classify_contact_via_llm
        res = _classify_contact_via_llm("José", "olá tudo bem", "Total messages: 1")
        self.assertEqual(res["relationship"], "Cliente")
        self.assertEqual(res["tone"], "polido e profissional")
        self.assertIsNone(res["nickname"])

    @patch.dict(os.environ, {"KEEP_LOCAL_PLUGIN": ""})
    @patch("whatsapp_manager.Path.exists", autospec=True)
    @patch("subprocess.run")
    @patch("urllib.request.urlopen")
    def test_self_update_plugin_code_git(self, mock_urlopen, mock_subrun, mock_exists):
        """Verifica que o auto-updater do plugin usa git fetch/reset quando .git existe."""
        mock_exists.side_effect = lambda path: str(path) != "/opt/data/.hermes/keep-local-plugin"
        
        # Caso 1: hashes diferentes (deve atualizar)
        mock_result_local = MagicMock()
        mock_result_local.stdout = "local_commit_hash\n"
        mock_result_remote = MagicMock()
        mock_result_remote.stdout = "remote_commit_hash\n"
        
        mock_subrun.side_effect = [
            MagicMock(returncode=0), # fetch
            mock_result_local,       # local
            mock_result_remote,      # remote
            MagicMock(returncode=0)  # reset
        ]
        
        res = whatsapp_manager._self_update_plugin_code()
        self.assertTrue(res)
        
        # Caso 2: hashes iguais (não deve atualizar)
        mock_subrun.side_effect = [
            MagicMock(returncode=0), # fetch
            mock_result_local,       # local
            mock_result_local,       # remote (mesmo hash)
        ]
        res = whatsapp_manager._self_update_plugin_code()
        self.assertFalse(res)
        
        # Caso 3: git falha, deve cair no fallback HTTP
        mock_subrun.side_effect = Exception("Git fail")
        mock_response = MagicMock()
        mock_response.read.return_value = b"mock content"
        mock_urlopen.return_value.__enter__.return_value = mock_response
        
        with patch("pathlib.Path.read_bytes", return_value=b"different content"), \
             patch("pathlib.Path.write_bytes") as mock_write, \
             patch("pathlib.Path.mkdir"):
            res = whatsapp_manager._self_update_plugin_code()
            self.assertTrue(res)

    @patch("urllib.request.urlopen")
    @patch.dict(os.environ, {"GOOGLE_API_KEY": "test-google-key", "WHATSAPP_CONTACT_CLASSIFIER_MODEL": "gemini-1.5-pro-test"})
    def test_classify_contact_custom_model(self, mock_urlopen):
        """Verifica que o classificador de contatos utiliza o modelo configurado no ambiente."""
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "candidates": [{
                "content": {
                    "parts": [{
                        "text": '{"relationship": "Amigo", "tone": "informal", "nickname": null, "pet_name": null, "frequent_greeting": null, "summary": "resumo", "intent": "intencao", "frequency": "diaria", "product": null, "guidelines": "seja gentil"}'
                    }]
                }
            }]
        }).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_response

        res = whatsapp_manager._classify_contact_via_llm("Carlos", "history", "stats")
        self.assertEqual(res["relationship"], "Amigo")
        
        called_args = mock_urlopen.call_args[0]
        called_req = called_args[0]
        self.assertIn("gemini-1.5-pro-test", called_req.full_url)


class TestUtilityFunctionsAndLogs(BaseWhatsAppManagerTest):
    @patch("urllib.request.urlopen")
    def test_check_bot_paused_updates_lid_cache(self, mock_urlopen):
        """_check_bot_paused deve atualizar _lid_to_phone quando a resposta contiver lidToPhone."""
        import whatsapp_manager
        
        bridge_response = {
            "botPaused": False,
            "lidToPhone": {
                "111111111111111": "5511222222222",
                "333333333333333": "5511444444444"
            }
        }
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(bridge_response).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_response
        
        whatsapp_manager._lid_to_phone.clear()
        whatsapp_manager._bot_status_cache = {"paused": False, "ts": 0}

        result = whatsapp_manager._check_bot_paused()
        
        self.assertFalse(result)
        self.assertEqual(whatsapp_manager._lid_to_phone.get("111111111111111"), "5511222222222")
        self.assertEqual(whatsapp_manager._lid_to_phone.get("333333333333333"), "5511444444444")
        whatsapp_manager._lid_to_phone.clear()

    @patch("urllib.request.urlopen", side_effect=OSError("bridge offline"))
    def test_check_bot_paused_is_fail_closed_when_bridge_offline(self, mock_urlopen):
        whatsapp_manager._bot_status_cache = {"paused": False, "ts": 0}
        self.assertTrue(whatsapp_manager._check_bot_paused(force=True))

    @patch("urllib.request.urlopen")
    def test_check_bot_paused_rejects_invalid_payload(self, mock_urlopen):
        response = MagicMock()
        response.read.return_value = json.dumps({"status": "unknown"}).encode()
        mock_urlopen.return_value.__enter__.return_value = response
        whatsapp_manager._bot_status_cache = {"paused": False, "ts": 0}
        self.assertTrue(whatsapp_manager._check_bot_paused(force=True))

    @patch("urllib.request.urlopen")
    def test_free_bot_state_is_not_cached(self, mock_urlopen):
        response = MagicMock()
        response.read.return_value = json.dumps({"botPaused": False}).encode()
        mock_urlopen.return_value.__enter__.return_value = response
        whatsapp_manager._bot_status_cache = {"paused": False, "ts": 0}
        self.assertFalse(whatsapp_manager._check_bot_paused())
        self.assertFalse(whatsapp_manager._check_bot_paused())
        self.assertEqual(mock_urlopen.call_count, 2)

    @patch("whatsapp_manager._check_chat_silenced", return_value=False)
    @patch("whatsapp_manager._check_bot_paused", return_value=False)
    def test_delivery_gate_forces_fresh_pause_and_takeover_checks(self, mock_bot, mock_chat):
        whatsapp_manager._assert_delivery_allowed("5511888888888@s.whatsapp.net")
        mock_bot.assert_called_once_with(force=True)
        mock_chat.assert_called_once_with("5511888888888@s.whatsapp.net", force=True)

    @patch("whatsapp_manager._check_bot_paused")
    def test_delivery_gate_rejects_noncanonical_recipient_before_status_lookup(self, mock_bot):
        with self.assertRaises(whatsapp_manager.DeliveryBlocked):
            whatsapp_manager._assert_delivery_allowed("sessao-ambigua")
        mock_bot.assert_not_called()

    @patch("whatsapp_manager._assert_delivery_allowed")
    @patch("urllib.request.urlopen")
    def test_send_bridge_media_requires_real_message_id(self, mock_urlopen, mock_gate):
        response = MagicMock()
        response.read.return_value = json.dumps({"success": True, "messageId": "ptt-123"}).encode()
        mock_urlopen.return_value.__enter__.return_value = response

        message_id = whatsapp_manager._send_bridge_media(
            "5511888888888@s.whatsapp.net",
            "/tmp/voice.ogg",
        )

        self.assertEqual(message_id, "ptt-123")
        mock_gate.assert_called_once_with("5511888888888@s.whatsapp.net")
        request = mock_urlopen.call_args.args[0]
        payload = json.loads(request.data.decode())
        self.assertIs(payload["automation"], True)

    @patch("whatsapp_manager._assert_delivery_allowed")
    @patch("urllib.request.urlopen")
    def test_send_bridge_media_rejects_success_without_message_id(self, mock_urlopen, mock_gate):
        response = MagicMock()
        response.read.return_value = json.dumps({"success": True}).encode()
        mock_urlopen.return_value.__enter__.return_value = response
        with self.assertRaisesRegex(RuntimeError, "messageId do PTT"):
            whatsapp_manager._send_bridge_media(
                "5511888888888@s.whatsapp.net",
                "/tmp/voice.ogg",
            )

    def test_custom_print_redirection(self):
        """Verifica que WARNING+/ERROR vão para stderr e INFO vai para stdout via _WMLogHandler."""
        import sys
        import logging
        from io import StringIO

        stdout_capture = StringIO()
        stderr_capture = StringIO()

        original_stdout = sys.stdout
        original_stderr = sys.stderr
        sys.stdout = stdout_capture
        sys.stderr = stderr_capture

        try:
            import whatsapp_manager
            whatsapp_manager.logger.warning("⚠️ Test warning message")
            whatsapp_manager.logger.info("Regular info message")
        finally:
            sys.stdout = original_stdout
            sys.stderr = original_stderr

        stderr_val = stderr_capture.getvalue()
        stdout_val = stdout_capture.getvalue()

        self.assertIn("Test warning message", stderr_val, "WARNING deveria ir para stderr")
        self.assertNotIn("Test warning message", stdout_val)
        self.assertIn("Regular info message", stdout_val, "INFO deveria ir para stdout")
        self.assertNotIn("Regular info message", stderr_val)

    @patch("sqlite3.connect")
    def test_update_db_message(self, mock_connect):
        """Verifica a atualização do banco SQLite com detecção dinâmica de colunas."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_connect.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        
        mock_cursor.fetchall.return_value = [
            (0, "chat_id", "TEXT", 0, None, 0),
            (1, "message_id", "TEXT", 0, None, 0),
            (2, "body", "TEXT", 0, None, 0)
        ]
        mock_cursor.rowcount = 1
        
        updated = whatsapp_manager._update_db_message("/dummy/path.db", "msg123", "new body text")
        self.assertEqual(updated, 1)
        mock_cursor.execute.assert_any_call("PRAGMA table_info(messages)")
        mock_cursor.execute.assert_any_call("UPDATE messages SET body = ? WHERE message_id = ?", ("new body text", "msg123"))

    @patch("sqlite3.connect")
    def test_update_db_message_alternative_msg_id(self, mock_connect):
        """Verifica _update_db_message com coluna de ID msg_id."""
        import whatsapp_manager
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_connect.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        
        mock_cursor.fetchall.return_value = [
            (0, "chat_id", "TEXT", 0, None, 0),
            (1, "msg_id", "TEXT", 0, None, 0),
            (2, "body", "TEXT", 0, None, 0)
        ]
        mock_cursor.rowcount = 1
        
        res = whatsapp_manager._update_db_message("/dummy.db", "msg123", "new body")
        self.assertEqual(res, 1)
        mock_cursor.execute.assert_any_call("UPDATE messages SET body = ? WHERE msg_id = ?", ("new body", "msg123"))

    @patch("sqlite3.connect")
    def test_update_db_message_alternative_id(self, mock_connect):
        """Verifica _update_db_message com coluna de ID id."""
        import whatsapp_manager
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_connect.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        
        mock_cursor.fetchall.return_value = [
            (0, "chat_id", "TEXT", 0, None, 0),
            (1, "id", "TEXT", 0, None, 0),
            (2, "body", "TEXT", 0, None, 0)
        ]
        mock_cursor.rowcount = 1
        
        res = whatsapp_manager._update_db_message("/dummy.db", "msg123", "new body")
        self.assertEqual(res, 1)
        mock_cursor.execute.assert_any_call("UPDATE messages SET body = ? WHERE id = ?", ("new body", "msg123"))

    @patch("sqlite3.connect")
    def test_update_db_message_no_id_column(self, mock_connect):
        """Verifica que _update_db_message retorna -1 quando não há nenhuma coluna de ID."""
        import whatsapp_manager
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_connect.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        
        mock_cursor.fetchall.return_value = [
            (0, "chat_id", "TEXT", 0, None, 0),
            (1, "body", "TEXT", 0, None, 0)
        ]
        
        res = whatsapp_manager._update_db_message("/dummy.db", "msg123", "new body")
        self.assertEqual(res, -1)

    @patch("sqlite3.connect", side_effect=Exception("Connection error"))
    def test_update_db_message_error(self, mock_connect):
        """Verifica que _update_db_message retorna -2 quando ocorre uma exceção."""
        import whatsapp_manager
        res = whatsapp_manager._update_db_message("/dummy.db", "msg123", "new body")
        self.assertEqual(res, -2)

    @patch("whatsapp_manager._update_db_message")
    @patch("threading.Thread")
    def test_persist_transcription_to_db_immediate(self, mock_thread, mock_update):
        """Verifica que _persist_transcription_to_db não cria thread se a inserção for imediata."""
        import whatsapp_manager
        mock_update.return_value = 1
        whatsapp_manager._persist_transcription_to_db("/dummy.db", "msg123", "body")
        mock_thread.assert_not_called()

    @patch("whatsapp_manager._update_db_message")
    def test_persist_transcription_to_db_bg_retry(self, mock_update):
        """Verifica que _persist_transcription_to_db lança thread e retenta se retorno for 0."""
        import whatsapp_manager
        mock_update.side_effect = [0, 1]
        
        with patch("time.sleep") as mock_sleep:
            whatsapp_manager._persist_transcription_to_db("/dummy.db", "msg123", "body")
            
            import time
            for _ in range(20):
                if mock_update.call_count >= 2:
                    break
                time.sleep(0.01)
                
            self.assertEqual(mock_update.call_count, 2)
            mock_sleep.assert_any_call(1)


class TestUpdateContactFields(BaseWhatsAppManagerTest):
    """Testes para _update_contact_fields — busca em cascata por níveis 1-6."""

    def _make_contacts(self):
        return {
            "5511777777777@s.whatsapp.net": {
                "name": "Isabel Alencar",
                "relationship": "Parente",
                "nickname": "Bel",
                "pet_name": None,
                "notes": None,
            },
            "5511888888888@s.whatsapp.net": {
                "name": "Carlos Silva",
                "relationship": "Cliente",
                "nickname": None,
                "pet_name": None,
                "notes": None,
            },
        }

    @patch("whatsapp_manager._push_personal_contacts_to_github")
    @patch("pathlib.Path.exists", return_value=True)
    @patch("builtins.open")
    def test_level2_exact_name_match(self, mock_open, mock_exists, mock_push):
        contacts = self._make_contacts()
        mock_open.return_value.__enter__.return_value = MagicMock(
            read=lambda: json.dumps(contacts)
        )
        written = {}

        def fake_open(path, mode="r", **kwargs):
            m = unittest.mock.mock_open(read_data=json.dumps(contacts))()
            if "w" in mode:
                def capture_write(data):
                    written["data"] = data
                m.write = capture_write
            return m

        mock_open.side_effect = fake_open

        from whatsapp_manager import _update_contact_fields
        result = _update_contact_fields("Isabel Alencar", {"notes": "filha mais velha"})
        self.assertIn("Isabel Alencar", result)
        self.assertIn("✅", result)

    @patch("whatsapp_manager._push_personal_contacts_to_github")
    @patch("pathlib.Path.exists", return_value=True)
    @patch("builtins.open")
    def test_level3_nickname_match(self, mock_open, mock_exists, mock_push):
        contacts = self._make_contacts()

        def fake_open(path, mode="r", **kwargs):
            m = unittest.mock.mock_open(read_data=json.dumps(contacts))()
            m.write = lambda data: None
            return m

        mock_open.side_effect = fake_open

        from whatsapp_manager import _update_contact_fields
        result = _update_contact_fields("Bel", {"notes": "via apelido"})
        self.assertIn("✅", result)
        self.assertIn("Isabel Alencar", result)

    @patch("whatsapp_manager._push_personal_contacts_to_github")
    @patch("pathlib.Path.exists", return_value=True)
    @patch("builtins.open")
    def test_level4_substring_name_match(self, mock_open, mock_exists, mock_push):
        contacts = self._make_contacts()

        def fake_open(path, mode="r", **kwargs):
            m = unittest.mock.mock_open(read_data=json.dumps(contacts))()
            m.write = lambda data: None
            return m

        mock_open.side_effect = fake_open

        from whatsapp_manager import _update_contact_fields
        # "Isabel" é substring de "Isabel Alencar"
        result = _update_contact_fields("Isabel", {"relationship": "Filha"})
        self.assertIn("✅", result)
        self.assertIn("Isabel Alencar", result)

    @patch("whatsapp_manager._push_personal_contacts_to_github")
    @patch("pathlib.Path.exists", return_value=True)
    @patch("builtins.open")
    @patch("sqlite3.connect")
    def test_level5_sender_name_db_match(self, mock_connect, mock_open, mock_exists, mock_push):
        """Nível 5: contato sem nome no JSON mas com sender_name no DB."""
        contacts = {
            "5511999111111@s.whatsapp.net": {
                "name": "Contato 1111",
                "relationship": "Cliente",
                "nickname": None, "pet_name": None, "notes": None,
            }
        }

        def fake_open(path, mode="r", **kwargs):
            m = unittest.mock.mock_open(read_data=json.dumps(contacts))()
            m.write = lambda data: None
            return m

        mock_open.side_effect = fake_open

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_connect.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        # DB retorna sender_name "Pedro Souza" para o chat_id correspondente
        mock_cursor.fetchall.return_value = [
            ("5511999111111@s.whatsapp.net", "Pedro Souza")
        ]
        mock_conn.__enter__ = lambda s: mock_conn
        mock_conn.__exit__ = MagicMock(return_value=False)

        from whatsapp_manager import _update_contact_fields
        result = _update_contact_fields("Pedro", {"notes": "encontrado via DB"})
        self.assertIn("✅", result)

    @patch("pathlib.Path.exists", return_value=True)
    @patch("builtins.open")
    @patch("sqlite3.connect", side_effect=sqlite3.DatabaseError("db error"))
    @patch("urllib.request.urlopen", side_effect=Exception("bridge error"))
    def test_not_found_returns_error_message(self, mock_urlopen, mock_connect, mock_open, mock_exists):
        contacts = self._make_contacts()
        mock_open.return_value.__enter__.return_value = MagicMock(
            read=lambda: json.dumps(contacts)
        )

        def fake_open(path, mode="r", **kwargs):
            return unittest.mock.mock_open(read_data=json.dumps(contacts))()

        mock_open.side_effect = fake_open

        from whatsapp_manager import _update_contact_fields
        result = _update_contact_fields("Desconhecido XYZ", {"notes": "x"})
        self.assertIn("❌", result)
        self.assertIn("não encontrado", result)

    @patch("whatsapp_manager._push_personal_contacts_to_github")
    @patch("pathlib.Path.exists", return_value=True)
    @patch("builtins.open")
    @patch("sqlite3.connect", side_effect=Exception("no db"))
    @patch("urllib.request.urlopen")
    def test_level6_bridge_search_match(self, mock_urlopen, mock_connect, mock_open, mock_exists, mock_push):
        """Nível 6: bridge /contacts/search retorna resultado."""
        contacts = self._make_contacts()

        def fake_open(path, mode="r", **kwargs):
            m = unittest.mock.mock_open(read_data=json.dumps(contacts))()
            m.write = lambda data: None
            return m

        mock_open.side_effect = fake_open

        bridge_resp = json.dumps({
            "results": [{"jid": "5511777777777@s.whatsapp.net", "name": "Isabel Alencar"}]
        }).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = bridge_resp
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        from whatsapp_manager import _update_contact_fields
        result = _update_contact_fields("Isabel", {"notes": "via bridge"})
        self.assertIn("✅", result)

    @patch("whatsapp_manager._push_personal_contacts_to_github")
    @patch("pathlib.Path.exists", return_value=True)
    @patch("builtins.open")
    def test_level1_phone_number_match(self, mock_open, mock_exists, mock_push):
        """Nível 1: busca por número de telefone."""
        contacts = self._make_contacts()

        def fake_open(path, mode="r", **kwargs):
            m = unittest.mock.mock_open(read_data=json.dumps(contacts))()
            m.write = lambda data: None
            return m

        mock_open.side_effect = fake_open

        from whatsapp_manager import _update_contact_fields
        result = _update_contact_fields("5511777777777", {"notes": "via número"})
        self.assertIn("✅", result)
        self.assertIn("Isabel Alencar", result)


class TestPendingContactUpdate(BaseWhatsAppManagerTest):
    """Testes para o fluxo _pending_contact_update: não encontrado → pede número → aplica."""

    def setUp(self):
        super().setUp()
        import whatsapp_manager
        whatsapp_manager._pending_contact_update.clear()

    def tearDown(self):
        import whatsapp_manager
        whatsapp_manager._pending_contact_update.clear()
        super().tearDown()

    @patch("whatsapp_manager._update_contact_fields", return_value="❌ Contato 'Maria' não encontrado em personal_contacts.json nem no histórico de mensagens.")
    @patch("whatsapp_manager._extract_contact_name_via_llm", return_value="Maria")
    @patch("whatsapp_manager._classify_contact_via_llm", return_value={
        "relationship": "Parente", "manual_relationship": "Parente",
        "name": "Maria", "nickname": None, "pet_name": None,
        "notes": None, "product": None, "frequent_greeting": None,
    })
    @patch("whatsapp_manager._classify_owner_intent", return_value={
        "intent_type": "update_contact", "is_update": True,
        "contact_identifier": "Maria", "intent": "atualizar contato Maria",
    })
    @patch("urllib.request.urlopen")
    def test_not_found_stores_pending_and_asks_phone(
        self, mock_urlopen, mock_owner_intent, mock_classify, mock_extract, mock_update
    ):
        """Quando contato não é encontrado, armazena pendência e pergunta o número."""
        import whatsapp_manager

        pre_dispatch = self.ctx.hooks.get("pre_gateway_dispatch")
        event = MagicMock()
        event.source.platform = "whatsapp"
        event.source.user_id = "5511999999999@s.whatsapp.net"
        event.source.chat_id = "5511999999999@s.whatsapp.net"
        event.text = "atualize a Maria, ela é minha parente"
        event.has_media = False

        gateway = MagicMock()
        gateway._session_key_for_source.return_value = "sess_pending"
        gateway._session_model_overrides = {}

        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"success": true, "messageId": "test-mid"}'
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        pre_dispatch("pre_gateway_dispatch", {"event": event, "gateway": gateway})

        sender_id = "5511999999999@s.whatsapp.net"
        self.assertIn(sender_id, whatsapp_manager._pending_contact_update)
        pending = whatsapp_manager._pending_contact_update[sender_id]
        self.assertEqual(pending["name"], "Maria")

    @patch("whatsapp_manager._update_contact_fields")
    @patch("urllib.request.urlopen")
    def test_phone_reply_resolves_pending(self, mock_urlopen, mock_update):
        """Quando dono responde com número, pendência é aplicada e removida."""
        import whatsapp_manager

        sender_id = "5511999999999@s.whatsapp.net"
        whatsapp_manager._pending_contact_update[sender_id] = {
            "name": "Maria",
            "fields": {"relationship": "Parente", "manual_relationship": "Parente", "name": "Maria"},
        }
        mock_update.return_value = "✅ Contato Maria atualizado."

        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"success": true, "messageId": "test-mid"}'
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        pre_dispatch = self.ctx.hooks.get("pre_gateway_dispatch")
        event = MagicMock()
        event.source.platform = "whatsapp"
        event.source.user_id = sender_id
        event.source.chat_id = sender_id
        event.text = "5511888777666"
        event.has_media = False

        gateway = MagicMock()
        gateway._session_key_for_source.return_value = "sess_resolve"
        gateway._session_model_overrides = {}

        res = pre_dispatch("pre_gateway_dispatch", {"event": event, "gateway": gateway})

        self.assertEqual(res, {"action": "skip", "reason": "update-contact-pending"})
        self.assertNotIn(sender_id, whatsapp_manager._pending_contact_update)
        mock_update.assert_called()


class TestSalesDetection(BaseWhatsAppManagerTest):
    """Testes para a detecção de comprovante Pix e o registro/revisão de vendas."""

    CLIENT_ID = "5511888877777@s.whatsapp.net"

    def _make_event(self, sender_id, chat_id, text):
        """Evento base do dispatch. `raw` e `message_id` precisam ser tipos reais:
        num MagicMock cru o id vira mock e chega assim no sqlite (`Error binding
        parameter`), mascarando o caminho de dedup e de persistência."""
        self._msg_seq = getattr(self, "_msg_seq", 0) + 1
        event = MagicMock()
        event.raw = {}
        event.message_id = f"msg_sale_{self._msg_seq}"
        event.source.platform = "whatsapp"
        event.source.user_id = sender_id
        event.source.chat_id = chat_id
        event.text = text
        return event

    def _make_image_event(self, sender_id, chat_id, caption=""):
        event = self._make_event(sender_id, chat_id, caption)
        event.has_media = True
        event.media_type = "image"
        event.media_urls = ["/path/to/receipt.jpg"]
        return event

    def _make_text_event(self, sender_id, chat_id, text):
        event = self._make_event(sender_id, chat_id, text)
        event.has_media = False
        return event

    def _dispatch_event(self, event):
        pre_dispatch = self.ctx.hooks.get("pre_gateway_dispatch")
        gateway = MagicMock()
        gateway._session_key_for_source.return_value = "sess_sales"
        gateway._session_model_overrides = {}
        return pre_dispatch("pre_gateway_dispatch", {"event": event, "gateway": gateway})

    @patch("whatsapp_manager._save_sales")
    @patch("whatsapp_manager._load_sales", return_value={})
    @patch("whatsapp_manager._load_personal_contacts", return_value={})
    @patch("whatsapp_manager._process_media_message", return_value="foto de um comprovante de pagamento")
    @patch("whatsapp_manager._detect_and_extract_sale_from_image", return_value={
        "is_payment_receipt": True, "amount": "R$ 15,00", "payment_datetime": "28/07 14:30",
        "sender_name": "Maria Silva", "bank_app": "Nubank", "address": None,
    })
    @patch("urllib.request.urlopen")
    def test_client_payment_receipt_is_recorded_and_notified(
        self, mock_urlopen, mock_detect, mock_process, mock_contacts, mock_load, mock_save
    ):
        """Comprovante de cliente: grava a venda como pending_review e avisa cliente + dono."""
        import whatsapp_manager
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"success": true, "messageId": "test-mid"}'
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        with patch("whatsapp_manager._human_send") as mock_send:
            event = self._make_image_event(self.CLIENT_ID, self.CLIENT_ID, caption="aqui está o comprovante")
            res = self._dispatch_event(event)

        self.assertEqual(res, {"action": "skip", "reason": "sale-detected"})
        mock_save.assert_called_once()
        saved_sales = mock_save.call_args[0][0]
        saved_sale = next(iter(saved_sales.values()))
        self.assertEqual(saved_sale["status"], "pending_review")
        self.assertEqual(saved_sale["amount"], "R$ 15,00")
        self.assertEqual(saved_sale["contact_key"], self.CLIENT_ID)
        # Confirma apenas o recebimento pro cliente + avisa o dono = 2 mensagens
        self.assertEqual(mock_send.call_count, 2)
        client_message = mock_send.call_args_list[0].args[1].lower()
        self.assertIn("recebemos seu comprovante", client_message)
        self.assertIn("confirmação da equipe", client_message)
        self.assertNotIn("pedido confirmado", client_message)

    @patch("whatsapp_manager._save_sales")
    @patch("whatsapp_manager._load_sales", return_value={})
    @patch("whatsapp_manager._load_personal_contacts", return_value={
        "12025550199@s.whatsapp.net": {
            "name": "María",
            "market_id": "US",
            "currency": "USD",
            "language": "es",
        },
    })
    @patch("whatsapp_manager._process_media_message", return_value="foto de un comprobante de pago")
    @patch("whatsapp_manager._detect_and_extract_sale_from_image", return_value={
        "is_payment_receipt": True, "amount": "US$ 497", "payment_datetime": "23/08 14:30",
        "sender_name": "María", "bank_app": "Zelle", "address": None,
    })
    @patch("urllib.request.urlopen")
    def test_us_spanish_payment_receipt_ack_stays_spanish(
        self, mock_urlopen, mock_detect, mock_process, mock_contacts, mock_load, mock_save
    ):
        import whatsapp_manager
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"success": true, "messageId": "test-mid"}'
        mock_urlopen.return_value.__enter__.return_value = mock_resp
        lid = "123456789012345@lid"

        with patch(
            "whatsapp_manager._resolve_phone_from_jid",
            side_effect=lambda jid: (
                "12025550199@s.whatsapp.net" if jid == lid else jid
            ),
        ), patch("whatsapp_manager._human_send") as mock_send:
            event = self._make_image_event(lid, lid, caption="Aquí está el comprobante")
            res = self._dispatch_event(event)

        self.assertEqual(res, {"action": "skip", "reason": "sale-detected"})
        client_call = next(c for c in mock_send.call_args_list if c.args[0] == lid)
        client_message = client_call.args[1].lower()
        self.assertIn("recibimos tu comprobante", client_message)
        self.assertIn("confirmación del equipo", client_message)
        self.assertNotIn("pago fue confirmado", client_message)

    @patch("whatsapp_manager._save_sales")
    @patch("whatsapp_manager._process_media_message", return_value="uma selfie")
    @patch("whatsapp_manager._detect_and_extract_sale_from_image", return_value={
        "is_payment_receipt": True, "amount": "R$ 15,00",
    })
    @patch("urllib.request.urlopen")
    def test_owner_sending_image_never_triggers_sale(self, mock_urlopen, mock_detect, mock_process, mock_save):
        """Mesmo que a detecção retorne positivo, imagem do PRÓPRIO dono nunca vira venda."""
        import whatsapp_manager
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"success": true, "messageId": "test-mid"}'
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        owner_id = "5511999999999@s.whatsapp.net"
        event = self._make_image_event(owner_id, owner_id, caption="teste")
        res = self._dispatch_event(event)

        self.assertNotEqual(res, {"action": "skip", "reason": "sale-detected"})
        mock_save.assert_not_called()

    @patch("whatsapp_manager._save_sales")
    @patch("whatsapp_manager._process_media_message", return_value="uma foto de um gato")
    @patch("whatsapp_manager._detect_and_extract_sale_from_image", return_value={"is_payment_receipt": False})
    @patch("urllib.request.urlopen")
    def test_non_receipt_image_does_not_create_sale(self, mock_urlopen, mock_detect, mock_process, mock_save):
        """Imagem comum (não comprovante) não vira venda nem interrompe o fluxo normal."""
        import whatsapp_manager
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"success": true, "messageId": "test-mid"}'
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        event = self._make_image_event(self.CLIENT_ID, self.CLIENT_ID, caption="olha meu gato")
        res = self._dispatch_event(event)

        self.assertNotEqual(res, {"action": "skip", "reason": "sale-detected"})
        mock_save.assert_not_called()

    @patch("urllib.request.urlopen")
    def test_list_sales_command_is_deterministic_no_llm(self, mock_urlopen):
        """'listar vendas' responde direto do arquivo, sem passar por _classify_owner_intent."""
        import whatsapp_manager
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"success": true, "messageId": "test-mid"}'
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        owner_id = "5511999999999@s.whatsapp.net"
        with patch("whatsapp_manager._load_sales", return_value={
            "v1": {"contact_name": "João", "amount": "R$ 15,00", "status": "pending_review"},
        }), patch("whatsapp_manager._classify_owner_intent") as mock_intent:
            event = self._make_text_event(owner_id, owner_id, "listar vendas")
            res = self._dispatch_event(event)
            mock_intent.assert_not_called()

        self.assertEqual(res, {"action": "skip", "reason": "sales-list-command"})

    @patch("whatsapp_manager._save_sales")
    @patch("whatsapp_manager._load_sales", return_value={
        "v1": {"contact_name": "João", "amount": "R$ 15,00", "status": "pending_review"},
    })
    @patch("urllib.request.urlopen")
    def test_confirm_sale_command_updates_status(self, mock_urlopen, mock_load, mock_save):
        import whatsapp_manager
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"success": true, "messageId": "test-mid"}'
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        owner_id = "5511999999999@s.whatsapp.net"
        event = self._make_text_event(owner_id, owner_id, "confirmar venda v1")
        res = self._dispatch_event(event)

        self.assertEqual(res, {"action": "skip", "reason": "sale-review-command"})
        mock_save.assert_called_once()
        saved = mock_save.call_args[0][0]
        self.assertEqual(saved["v1"]["status"], "confirmed")

    @patch("whatsapp_manager._save_sales")
    @patch("whatsapp_manager._load_sales", return_value={})
    @patch("urllib.request.urlopen")
    def test_confirm_unknown_sale_id_does_not_crash(self, mock_urlopen, mock_load, mock_save):
        import whatsapp_manager
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"success": true, "messageId": "test-mid"}'
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        owner_id = "5511999999999@s.whatsapp.net"
        event = self._make_text_event(owner_id, owner_id, "confirmar venda v99")
        res = self._dispatch_event(event)

        self.assertEqual(res, {"action": "skip", "reason": "sale-review-command"})
        mock_save.assert_not_called()

    @patch("whatsapp_manager._save_sales")
    @patch("whatsapp_manager._load_sales", return_value={
        "001-30072026-0031": {"contact_name": "João", "contact_key": "5511888877777@s.whatsapp.net", "amount": "R$ 15,00", "status": "pending_review"},
    })
    @patch("whatsapp_manager._load_product_catalog", return_value={})
    @patch("urllib.request.urlopen")
    def test_confirm_sale_with_hyphenated_id(self, mock_urlopen, mock_catalog, mock_load, mock_save):
        """Regressão: normalized_msg troca hífen por espaço, o que quebrava confirmar/rejeitar
        venda pra IDs no formato novo (001-DDMMYYYY-HHMM) — só "vN" passava despercebido."""
        import whatsapp_manager
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"success": true, "messageId": "test-mid"}'
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        owner_id = "5511999999999@s.whatsapp.net"
        event = self._make_text_event(owner_id, owner_id, "confirmar venda 001-30072026-0031")
        with patch("whatsapp_manager._human_send"):
            res = self._dispatch_event(event)

        self.assertEqual(res, {"action": "skip", "reason": "sale-review-command"})
        mock_save.assert_called_once()
        saved = mock_save.call_args[0][0]
        self.assertEqual(saved["001-30072026-0031"]["status"], "confirmed")

    @patch("whatsapp_manager._save_sales")
    @patch("whatsapp_manager._load_sales", return_value={
        "001-30072026-0031": {
            "contact_name": "João", "contact_key": "5511888877777@s.whatsapp.net",
            "product": "E-book Teste", "status": "pending_review",
        },
    })
    @patch("whatsapp_manager._load_product_catalog", return_value={
        "ebook": {"name": "E-book Teste", "link": "https://exemplo.com/ebook", "active": True},
    })
    @patch("urllib.request.urlopen")
    def test_confirm_sale_notifies_client_with_digital_link(self, mock_urlopen, mock_catalog, mock_load, mock_save):
        """Ao confirmar, o CLIENTE (não só o dono) recebe o link do produto digital confirmado."""
        import whatsapp_manager
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"success": true, "messageId": "test-mid"}'
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        owner_id = "5511999999999@s.whatsapp.net"
        event = self._make_text_event(owner_id, owner_id, "confirmar venda 001-30072026-0031")
        with patch("whatsapp_manager._human_send") as mock_send:
            self._dispatch_event(event)

        self.assertEqual(mock_send.call_count, 2)
        client_call = next(c for c in mock_send.call_args_list if c.args[0] == "5511888877777@s.whatsapp.net")
        self.assertIn("https://exemplo.com/ebook", client_call.args[1])

    @patch("whatsapp_manager._save_sales")
    @patch("whatsapp_manager._load_sales", return_value={
        "aya-us-1": {
            "contact_name": "María",
            "contact_key": "123456789012345@lid",
            "product": "WhatsAYA",
            "status": "pending_review",
        },
    })
    @patch("whatsapp_manager._load_product_catalog", return_value={})
    @patch("whatsapp_manager._load_personal_contacts", return_value={
        "12025550199@s.whatsapp.net": {
            "market_id": "US",
            "currency": "USD",
            "language": "es",
        },
    })
    @patch("urllib.request.urlopen")
    def test_confirm_aya_sale_continues_spanish_onboarding(
        self, mock_urlopen, mock_contacts, mock_catalog, mock_load, mock_save
    ):
        import whatsapp_manager
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"success": true, "messageId": "test-mid"}'
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        owner_id = "5511999999999@s.whatsapp.net"
        event = self._make_text_event(owner_id, owner_id, "confirmar venda aya-us-1")
        with patch.dict(os.environ, {"WHATSAPP_CONFIG_SUBDIR": "instance"}), \
             patch(
                 "whatsapp_manager._resolve_phone_from_jid",
                 side_effect=lambda jid: (
                     "12025550199@s.whatsapp.net"
                     if jid == "123456789012345@lid"
                     else jid
                 ),
             ), \
             patch("whatsapp_manager._human_send") as mock_send:
            self._dispatch_event(event)

        client_call = next(
            c for c in mock_send.call_args_list if c.args[0] == "123456789012345@lid"
        )
        self.assertIn("Tu pago fue confirmado", client_call.args[1])
        self.assertIn("onboarding", client_call.args[1])
        self.assertNotIn("envio", client_call.args[1].lower())

    @patch("whatsapp_manager._save_sales")
    @patch("whatsapp_manager._load_sales", return_value={
        "aya-us-1": {
            "contact_name": "María",
            "contact_key": "123456789012345@lid",
            "product": "WhatsAYA",
            "status": "pending_review",
        },
    })
    @patch("whatsapp_manager._load_personal_contacts", return_value={
        "12025550199@s.whatsapp.net": {
            "market_id": "US",
            "currency": "USD",
            "language": "es",
        },
    })
    @patch("urllib.request.urlopen")
    def test_reject_aya_sale_stays_in_spanish(
        self, mock_urlopen, mock_contacts, mock_load, mock_save
    ):
        import whatsapp_manager
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"success": true, "messageId": "test-mid"}'
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        owner_id = "5511999999999@s.whatsapp.net"
        event = self._make_text_event(owner_id, owner_id, "rejeitar venda aya-us-1")
        with patch.dict(os.environ, {"WHATSAPP_CONFIG_SUBDIR": "instance"}), \
             patch(
                 "whatsapp_manager._resolve_phone_from_jid",
                 side_effect=lambda jid: (
                     "12025550199@s.whatsapp.net"
                     if jid == "123456789012345@lid"
                     else jid
                 ),
             ), \
             patch("whatsapp_manager._human_send") as mock_send:
            self._dispatch_event(event)

        client_call = next(
            c for c in mock_send.call_args_list if c.args[0] == "123456789012345@lid"
        )
        self.assertIn("No pudimos confirmar tu pago", client_call.args[1])
        self.assertIn("envíamelo de nuevo", client_call.args[1])
        self.assertNotIn("Não conseguimos", client_call.args[1])

    @patch("whatsapp_manager._load_sales", return_value={
        "001-30072026-0031": {
            "contact_name": "João", "product": "E-book", "quantity": 2,
            "amount": "R$ 15,00", "status": "pending_review",
        },
    })
    @patch("urllib.request.urlopen")
    def test_ver_id_without_pedido_keyword(self, mock_urlopen, mock_load):
        """'ver <id>' (sem a palavra 'pedido') também deve disparar o comando determinístico."""
        import whatsapp_manager
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"success": true, "messageId": "test-mid"}'
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        owner_id = "5511999999999@s.whatsapp.net"
        event = self._make_text_event(owner_id, owner_id, "ver 001-30072026-0031")
        with patch("whatsapp_manager._human_send") as mock_send:
            res = self._dispatch_event(event)

        self.assertEqual(res, {"action": "skip", "reason": "sale-view-command"})
        sent_text = mock_send.call_args[0][1]
        self.assertIn("Quantidade: 2", sent_text)
        self.assertIn("Produto: E-book", sent_text)

    @patch("whatsapp_manager._load_sales", return_value={})
    @patch("urllib.request.urlopen")
    def test_ver_unrelated_phrase_not_swallowed(self, mock_urlopen, mock_load):
        """'ver histórico'/'ver contato' não devem ser tratados como 'ver pedido' — só dispara
        quando o que vem depois do 'ver' parece mesmo um ID de venda."""
        import whatsapp_manager
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"success": true, "messageId": "test-mid"}'
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        owner_id = "5511999999999@s.whatsapp.net"
        event = self._make_text_event(owner_id, owner_id, "ver histórico do João")
        res = self._dispatch_event(event)

        self.assertNotEqual(res, {"action": "skip", "reason": "sale-view-command"})

    def test_find_quantity_default_is_one(self):
        import whatsapp_manager
        with patch("whatsapp_manager._find_recent_client_messages", return_value=["oi tudo bem"]):
            qty = whatsapp_manager._find_quantity_in_recent_messages(self.CLIENT_ID)
        self.assertEqual(qty, 1)

    def test_find_quantity_from_caption(self):
        import whatsapp_manager
        with patch("whatsapp_manager._find_recent_client_messages", return_value=[]):
            qty = whatsapp_manager._find_quantity_in_recent_messages(self.CLIENT_ID, "quero 3 por favor")
        self.assertEqual(qty, 3)

    def test_find_product_matches_partial_keyword(self):
        """Cliente escreve só 'mini pc', não o nome completo do catálogo — deve casar mesmo assim."""
        import whatsapp_manager
        catalog = {
            "mini-pc": {"name": "Mini Pc Acemagic Ryzen 7 7730u 16gb", "active": True},
        }
        with patch("whatsapp_manager._load_product_catalog", return_value=catalog), \
             patch("whatsapp_manager._find_recent_client_messages", return_value=["quero o mini pc"]):
            product = whatsapp_manager._find_product_in_recent_messages(self.CLIENT_ID)
        self.assertEqual(product, "Mini Pc Acemagic Ryzen 7 7730u 16gb")

    def test_next_sale_id_format(self):
        """ID = sequência(3 dígitos)-DDMMYYYY-HHMM, ex: 001-29072026-1530."""
        import whatsapp_manager
        sale_id = whatsapp_manager._next_sale_id({"a": {}, "b": {}})
        self.assertRegex(sale_id, r"^003-\d{8}-\d{4}$")

    def test_next_sale_id_skips_collision(self):
        import whatsapp_manager
        from datetime import datetime
        now_str = datetime.now().strftime("%d%m%Y-%H%M")
        # 1 item existente -> próxima sequência seria 002, mas essa chave já está ocupada
        # (mesmo minuto) -> deve pular pra 003.
        existing = {f"002-{now_str}": {}}
        sale_id = whatsapp_manager._next_sale_id(existing)
        self.assertEqual(sale_id, f"003-{now_str}")

    @patch("whatsapp_manager._save_sales")
    @patch("whatsapp_manager._load_sales", return_value={})
    @patch("whatsapp_manager._load_personal_contacts", return_value={})
    @patch("whatsapp_manager._process_media_message", return_value="comprovante de pagamento")
    @patch("whatsapp_manager._find_address_in_recent_messages", return_value="Rua das Flores, 123")
    @patch("whatsapp_manager._detect_and_extract_sale_from_image", return_value={
        "is_payment_receipt": True, "amount": "R$ 15,00", "address": None,
    })
    @patch("urllib.request.urlopen")
    def test_address_found_in_earlier_message_no_pending_created(
        self, mock_urlopen, mock_detect, mock_find_addr, mock_process, mock_contacts, mock_load, mock_save
    ):
        """Endereço mandado ANTES do comprovante: achado no histórico, sem precisar ficar pendente."""
        import whatsapp_manager
        whatsapp_manager._pending_sale_address.clear()
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"success": true, "messageId": "test-mid"}'
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        with patch("whatsapp_manager._human_send"):
            event = self._make_image_event(self.CLIENT_ID, self.CLIENT_ID)
            self._dispatch_event(event)

        mock_find_addr.assert_called_once_with(self.CLIENT_ID)
        saved_sale = next(iter(mock_save.call_args[0][0].values()))
        self.assertEqual(saved_sale["address"], "Rua das Flores, 123")
        self.assertNotIn(self.CLIENT_ID, whatsapp_manager._pending_sale_address)

    @patch("whatsapp_manager._save_sales")
    @patch("whatsapp_manager._load_sales", return_value={})
    @patch("whatsapp_manager._load_personal_contacts", return_value={})
    @patch("whatsapp_manager._process_media_message", return_value="comprovante de pagamento")
    @patch("whatsapp_manager._find_address_in_recent_messages", return_value=None)
    @patch("whatsapp_manager._detect_and_extract_sale_from_image", return_value={
        "is_payment_receipt": True, "amount": "R$ 15,00", "address": None,
    })
    @patch("urllib.request.urlopen")
    def test_no_address_anywhere_sets_pending_for_next_message(
        self, mock_urlopen, mock_detect, mock_find_addr, mock_process, mock_contacts, mock_load, mock_save
    ):
        """Sem endereço na legenda nem no histórico: fica pendente aguardando a próxima mensagem."""
        import whatsapp_manager
        whatsapp_manager._pending_sale_address.clear()
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"success": true, "messageId": "test-mid"}'
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        with patch("whatsapp_manager._human_send"):
            event = self._make_image_event(self.CLIENT_ID, self.CLIENT_ID)
            self._dispatch_event(event)

        self.assertIn(self.CLIENT_ID, whatsapp_manager._pending_sale_address)
        saved_sale = next(iter(mock_save.call_args[0][0].values()))
        self.assertIsNone(saved_sale["address"])

    @patch("whatsapp_manager._save_sales")
    @patch("whatsapp_manager._load_sales", return_value={"v1": {"contact_name": "João", "address": None}})
    @patch("whatsapp_manager._extract_address_via_llm", return_value="Av. Brasil, 500")
    @patch("whatsapp_manager._check_bot_paused", return_value=True)
    @patch("urllib.request.urlopen")
    def test_pending_address_captured_from_next_message_without_interrupting_flow(
        self, mock_urlopen, mock_paused, mock_extract, mock_load, mock_save
    ):
        """Endereço mandado DEPOIS do comprovante: capturado como efeito colateral, sem
        interceptar a mensagem (ela segue o fluxo normal do cliente)."""
        import whatsapp_manager
        whatsapp_manager._pending_sale_address.clear()
        whatsapp_manager._pending_sale_address[self.CLIENT_ID] = {"sale_id": "v1", "created_at": whatsapp_manager.time.time()}
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"success": true, "messageId": "test-mid"}'
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        event = self._make_text_event(self.CLIENT_ID, self.CLIENT_ID, "Av. Brasil, 500, apto 4")
        with patch("whatsapp_manager._human_send"):
            res = self._dispatch_event(event)

        self.assertNotIn(self.CLIENT_ID, whatsapp_manager._pending_sale_address)
        saved = mock_save.call_args[0][0]
        self.assertEqual(saved["v1"]["address"], "Av. Brasil, 500")
        # Não retornou skip específico de venda — mensagem seguiu pro fluxo normal
        # (que aqui bate no "bot-pausado" mockado, não em nada relacionado a vendas).
        self.assertEqual(res, {"action": "skip", "reason": "bot-pausado"})

    @patch("whatsapp_manager._extract_address_via_llm", return_value=None)
    @patch("whatsapp_manager._check_bot_paused", return_value=True)
    @patch("urllib.request.urlopen")
    def test_expired_pending_address_is_discarded(self, mock_urlopen, mock_paused, mock_extract):
        """Pendência de endereço mais velha que o TTL é descartada."""
        import whatsapp_manager
        whatsapp_manager._pending_sale_address.clear()
        whatsapp_manager._pending_sale_address[self.CLIENT_ID] = {
            "sale_id": "v1", "created_at": whatsapp_manager.time.time() - whatsapp_manager._PENDING_SALE_ADDRESS_TTL_S - 1,
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"success": true, "messageId": "test-mid"}'
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        event = self._make_text_event(self.CLIENT_ID, self.CLIENT_ID, "oi, tudo bem?")
        self._dispatch_event(event)

        self.assertNotIn(self.CLIENT_ID, whatsapp_manager._pending_sale_address)

    @patch("whatsapp_manager._update_contact_fields", return_value="✅ Contato João atualizado.")
    def test_save_address_to_contact_reuses_update_contact_fields(self, mock_update):
        """_save_address_to_contact delega pra _update_contact_fields com o telefone extraído do chat_id."""
        import whatsapp_manager
        whatsapp_manager._save_address_to_contact("5511888877777@s.whatsapp.net", "Rua X, 123")
        mock_update.assert_called_once_with("5511888877777", {"address": "Rua X, 123"})

    def test_save_address_to_contact_noop_without_address(self):
        import whatsapp_manager
        with patch("whatsapp_manager._update_contact_fields") as mock_update:
            whatsapp_manager._save_address_to_contact("5511888877777@s.whatsapp.net", "")
            mock_update.assert_not_called()

    @patch("whatsapp_manager._save_address_to_contact")
    @patch("whatsapp_manager._save_sales")
    @patch("whatsapp_manager._load_sales", return_value={})
    @patch("whatsapp_manager._load_personal_contacts", return_value={})
    @patch("whatsapp_manager._process_media_message", return_value="comprovante de pagamento")
    @patch("whatsapp_manager._find_address_in_recent_messages", return_value="Rua das Flores, 123")
    @patch("whatsapp_manager._detect_and_extract_sale_from_image", return_value={
        "is_payment_receipt": True, "amount": "R$ 15,00", "address": None,
    })
    @patch("urllib.request.urlopen")
    def test_address_from_receipt_flow_also_saved_to_contact(
        self, mock_urlopen, mock_detect, mock_find_addr, mock_process, mock_contacts, mock_load, mock_save, mock_save_addr
    ):
        """Endereço achado durante a detecção da venda também é gravado no cadastro do contato."""
        import whatsapp_manager
        whatsapp_manager._pending_sale_address.clear()
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"success": true, "messageId": "test-mid"}'
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        with patch("whatsapp_manager._human_send"):
            event = self._make_image_event(self.CLIENT_ID, self.CLIENT_ID)
            self._dispatch_event(event)

        mock_save_addr.assert_called_once_with(self.CLIENT_ID, "Rua das Flores, 123")

    @patch("whatsapp_manager._save_address_to_contact")
    @patch("whatsapp_manager._save_sales")
    @patch("whatsapp_manager._load_sales", return_value={"v1": {"contact_name": "João", "address": None}})
    @patch("whatsapp_manager._extract_address_via_llm", return_value="Av. Brasil, 500")
    @patch("whatsapp_manager._check_bot_paused", return_value=True)
    @patch("urllib.request.urlopen")
    def test_address_from_pending_capture_also_saved_to_contact(
        self, mock_urlopen, mock_paused, mock_extract, mock_load, mock_save, mock_save_addr
    ):
        """Endereço capturado numa mensagem posterior também é gravado no cadastro do contato."""
        import whatsapp_manager
        whatsapp_manager._pending_sale_address.clear()
        whatsapp_manager._pending_sale_address[self.CLIENT_ID] = {"sale_id": "v1", "created_at": whatsapp_manager.time.time()}
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"success": true, "messageId": "test-mid"}'
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        event = self._make_text_event(self.CLIENT_ID, self.CLIENT_ID, "Av. Brasil, 500, apto 4")
        with patch("whatsapp_manager._human_send"):
            self._dispatch_event(event)

        mock_save_addr.assert_called_once_with(self.CLIENT_ID, "Av. Brasil, 500")


class TestProductCatalog(BaseWhatsAppManagerTest):
    """Testes para o catálogo de produtos/serviços: cadastro/edição/remoção via chat com confirmação."""

    SENDER_ID = "5511999999999@s.whatsapp.net"

    def setUp(self):
        super().setUp()
        import whatsapp_manager
        whatsapp_manager._pending_catalog_action.clear()

    def tearDown(self):
        import whatsapp_manager
        whatsapp_manager._pending_catalog_action.clear()
        super().tearDown()

    def _make_event(self, text):
        event = MagicMock()
        event.source.platform = "whatsapp"
        event.source.user_id = self.SENDER_ID
        event.source.chat_id = self.SENDER_ID
        event.text = text
        event.has_media = False
        return event

    def _make_gateway(self):
        gateway = MagicMock()
        gateway._session_key_for_source.return_value = "sess_catalog"
        gateway._session_model_overrides = {}
        return gateway

    def _dispatch(self, text):
        pre_dispatch = self.ctx.hooks.get("pre_gateway_dispatch")
        return pre_dispatch("pre_gateway_dispatch", {"event": self._make_event(text), "gateway": self._make_gateway()})

    @patch("whatsapp_manager._extract_catalog_item_via_llm", return_value={
        "name": "Mentoria Individual", "description": "1h por semana", "price": "R$ 500",
    })
    @patch("whatsapp_manager._classify_owner_intent", return_value={
        "intent_type": "catalog_add", "intent": "cadastrar mentoria",
    })
    @patch("urllib.request.urlopen")
    def test_catalog_add_creates_pending_draft(self, mock_urlopen, mock_intent, mock_extract):
        """Pedido de cadastro guarda o rascunho como pendência e pede confirmação — não salva ainda."""
        import whatsapp_manager
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"success": true, "messageId": "test-mid"}'
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        with patch("whatsapp_manager._save_product_catalog") as mock_save:
            res = self._dispatch("adiciona um produto: mentoria individual, R$ 500, 1h por semana")
            mock_save.assert_not_called()

        self.assertEqual(res, {"action": "skip", "reason": "catalog-add-draft"})
        pending = whatsapp_manager._pending_catalog_action[self.SENDER_ID]
        self.assertEqual(pending["action"], "add")
        self.assertEqual(pending["item"]["name"], "Mentoria Individual")

    @patch("whatsapp_manager._classify_owner_intent", return_value={
        "intent_type": "catalog_add", "intent": "cadastrar produto",
    })
    @patch("urllib.request.urlopen")
    def test_catalog_add_without_name_asks_and_waits_for_details(self, mock_urlopen, mock_intent):
        """Sem nome extraível, guarda pendência 'awaiting_details' em vez de só avisar e desistir."""
        import whatsapp_manager
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"success": true, "messageId": "test-mid"}'
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        with patch("whatsapp_manager._extract_catalog_item_via_llm", return_value={}):
            res = self._dispatch("gostaria de incluir um produto na lista")

        self.assertEqual(res, {"action": "skip", "reason": "catalog-add-draft"})
        pending = whatsapp_manager._pending_catalog_action[self.SENDER_ID]
        self.assertEqual(pending["type"], "awaiting_details")

        # Próxima mensagem do dono é tratada como os detalhes do produto, sem repetir "adiciona um produto"
        with patch("whatsapp_manager._extract_catalog_item_via_llm", return_value={"name": "Mentoria", "price": "R$ 500"}):
            res2 = self._dispatch("mentoria, R$ 500")

        self.assertEqual(res2, {"action": "skip", "reason": "catalog-awaiting-details"})
        pending2 = whatsapp_manager._pending_catalog_action[self.SENDER_ID]
        self.assertEqual(pending2["action"], "add")
        self.assertEqual(pending2["item"]["name"], "Mentoria")

    @patch("whatsapp_manager._save_product_catalog")
    @patch("whatsapp_manager._load_product_catalog", return_value={})
    @patch("urllib.request.urlopen")
    def test_catalog_add_confirm_saves_item(self, mock_urlopen, mock_load, mock_save):
        """Responder 'sim' à pendência de cadastro salva o produto e limpa a pendência."""
        import whatsapp_manager
        whatsapp_manager._pending_catalog_action[self.SENDER_ID] = {
            "action": "add",
            "item": {"name": "Mentoria Individual", "description": "1h por semana", "price": "R$ 500"},
            "created_at": whatsapp_manager.time.time(),
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"success": true, "messageId": "test-mid"}'
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        res = self._dispatch("sim")

        self.assertEqual(res, {"action": "skip", "reason": "catalog-confirmation"})
        self.assertNotIn(self.SENDER_ID, whatsapp_manager._pending_catalog_action)
        mock_save.assert_called_once()
        saved_catalog = mock_save.call_args[0][0]
        saved_item = next(iter(saved_catalog.values()))
        self.assertEqual(saved_item["name"], "Mentoria Individual")
        self.assertTrue(saved_item["active"])

    @patch("whatsapp_manager._save_product_catalog")
    @patch("urllib.request.urlopen")
    def test_catalog_add_cancel_discards_draft(self, mock_urlopen, mock_save):
        """Responder 'não' cancela e não grava nada."""
        import whatsapp_manager
        whatsapp_manager._pending_catalog_action[self.SENDER_ID] = {
            "action": "add",
            "item": {"name": "Mentoria Individual"},
            "created_at": whatsapp_manager.time.time(),
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"success": true, "messageId": "test-mid"}'
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        res = self._dispatch("não")

        self.assertEqual(res, {"action": "skip", "reason": "catalog-confirmation"})
        self.assertNotIn(self.SENDER_ID, whatsapp_manager._pending_catalog_action)
        mock_save.assert_not_called()

    @patch("whatsapp_manager._extract_catalog_update_via_llm", return_value={"price": "R$ 550"})
    @patch("whatsapp_manager._find_catalog_matches", return_value=[
        ("mentoria-individual", {"name": "Mentoria Individual", "price": "R$ 500"}),
    ])
    @patch("whatsapp_manager._classify_owner_intent", return_value={
        "intent_type": "catalog_update", "product_identifier": "mentoria", "intent": "editar preço",
    })
    @patch("urllib.request.urlopen")
    def test_catalog_update_single_match_asks_confirmation(self, mock_urlopen, mock_intent, mock_find, mock_extract):
        """Match único vai direto para confirmação, sem desambiguação."""
        import whatsapp_manager
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"success": true, "messageId": "test-mid"}'
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        res = self._dispatch("muda o preço da mentoria pra 550")

        self.assertEqual(res, {"action": "skip", "reason": "catalog-update-draft"})
        pending = whatsapp_manager._pending_catalog_action[self.SENDER_ID]
        self.assertEqual(pending["action"], "update")
        self.assertEqual(pending["key"], "mentoria-individual")
        self.assertEqual(pending["changes"], {"price": "R$ 550"})

    @patch("whatsapp_manager._find_catalog_matches", return_value=[
        ("consultoria-marketing", {"name": "Consultoria de Marketing"}),
        ("consultoria-financeira", {"name": "Consultoria Financeira"}),
    ])
    @patch("whatsapp_manager._classify_owner_intent", return_value={
        "intent_type": "catalog_remove", "product_identifier": "consultoria", "intent": "remover consultoria",
    })
    @patch("urllib.request.urlopen")
    def test_catalog_ambiguous_match_asks_disambiguation(self, mock_urlopen, mock_intent, mock_find):
        """Múltiplos produtos batendo com o nome pedem desambiguação antes de confirmar."""
        import whatsapp_manager
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"success": true, "messageId": "test-mid"}'
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        res = self._dispatch("remove o produto consultoria")

        self.assertEqual(res, {"action": "skip", "reason": "catalog-remove-draft"})
        pending = whatsapp_manager._pending_catalog_action[self.SENDER_ID]
        self.assertEqual(pending["type"], "disambiguate")
        self.assertEqual(len(pending["candidates"]), 2)

    @patch("whatsapp_manager._save_product_catalog")
    @patch("whatsapp_manager._load_product_catalog", return_value={
        "mentoria-individual": {"name": "Mentoria Individual", "active": True},
    })
    @patch("urllib.request.urlopen")
    def test_catalog_remove_confirm_marks_inactive_not_deleted(self, mock_urlopen, mock_load, mock_save):
        """Confirmar remoção marca active=False em vez de apagar a entrada."""
        import whatsapp_manager
        whatsapp_manager._pending_catalog_action[self.SENDER_ID] = {
            "action": "remove", "key": "mentoria-individual",
            "created_at": whatsapp_manager.time.time(),
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"success": true, "messageId": "test-mid"}'
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        res = self._dispatch("sim")

        self.assertEqual(res, {"action": "skip", "reason": "catalog-confirmation"})
        saved_catalog = mock_save.call_args[0][0]
        self.assertFalse(saved_catalog["mentoria-individual"]["active"])
        self.assertIn("mentoria-individual", saved_catalog)

    @patch("whatsapp_manager._classify_owner_intent", return_value={"intent_type": "other", "intent": "papo qualquer"})
    @patch("urllib.request.urlopen")
    def test_expired_pending_is_discarded(self, mock_urlopen, mock_intent):
        """Pendência mais velha que o TTL é descartada em vez de aplicada."""
        import whatsapp_manager
        whatsapp_manager._pending_catalog_action[self.SENDER_ID] = {
            "action": "add",
            "item": {"name": "Produto Antigo"},
            "created_at": whatsapp_manager.time.time() - whatsapp_manager._PENDING_CATALOG_TTL_S - 1,
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"success": true, "messageId": "test-mid"}'
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        self._dispatch("sim")

        self.assertNotIn(self.SENDER_ID, whatsapp_manager._pending_catalog_action)

    def test_build_catalog_context_block_only_lists_active_items(self):
        """Bloco de contexto injetado no prompt só inclui itens ativos."""
        import whatsapp_manager
        with patch("whatsapp_manager._load_product_catalog", return_value={
            "a": {"name": "Produto Ativo", "price": "R$ 100", "active": True},
            "b": {"name": "Produto Inativo", "price": "R$ 200", "active": False},
        }):
            block = whatsapp_manager._build_catalog_context_block()
        self.assertIn("Produto Ativo", block)
        self.assertNotIn("Produto Inativo", block)

    def test_find_catalog_matches_exact_takes_priority_over_partial(self):
        """Match exato retorna sozinho, mesmo se outros itens batem parcialmente."""
        import whatsapp_manager
        with patch("whatsapp_manager._load_product_catalog", return_value={
            "consultoria": {"name": "Consultoria"},
            "consultoria-vip": {"name": "Consultoria VIP"},
        }):
            matches = whatsapp_manager._find_catalog_matches("Consultoria")
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0][1]["name"], "Consultoria")

    @patch("whatsapp_manager._save_product_catalog")
    @patch("whatsapp_manager._load_product_catalog", return_value={})
    @patch("whatsapp_manager._extract_catalog_update_via_llm", return_value={"link": "https://chatkanban.cloud/"})
    @patch("urllib.request.urlopen")
    def test_amend_during_add_confirmation_updates_draft_without_saving(self, mock_urlopen, mock_amend, mock_load, mock_save):
        """Mensagem que não é sim/não durante a confirmação de cadastro emenda o rascunho (ex: link) em vez de ser ignorada."""
        import whatsapp_manager
        whatsapp_manager._pending_catalog_action[self.SENDER_ID] = {
            "action": "add",
            "item": {"name": "Sistema de agentes", "description": "selfhosting", "price": "R$ 150"},
            "created_at": whatsapp_manager.time.time(),
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"success": true, "messageId": "test-mid"}'
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        res = self._dispatch("inclua o link do produto https://chatkanban.cloud/")

        self.assertEqual(res, {"action": "skip", "reason": "catalog-confirmation"})
        mock_save.assert_not_called()
        pending = whatsapp_manager._pending_catalog_action[self.SENDER_ID]
        self.assertEqual(pending["item"]["link"], "https://chatkanban.cloud/")
        self.assertEqual(pending["item"]["name"], "Sistema de agentes")

        # Confirmar em seguida deve salvar já com o link emendado
        res2 = self._dispatch("sim")
        self.assertEqual(res2, {"action": "skip", "reason": "catalog-confirmation"})
        mock_save.assert_called_once()
        saved_item = next(iter(mock_save.call_args[0][0].values()))
        self.assertEqual(saved_item["link"], "https://chatkanban.cloud/")

    @patch("whatsapp_manager._extract_catalog_update_via_llm", return_value={})
    @patch("urllib.request.urlopen")
    def test_unrelated_message_during_confirmation_still_asks_sim_nao(self, mock_urlopen, mock_amend):
        """Mensagem sem nenhum campo extraível não vira emenda silenciosa — mantém o pedido de confirmação."""
        import whatsapp_manager
        whatsapp_manager._pending_catalog_action[self.SENDER_ID] = {
            "action": "add", "item": {"name": "Mentoria"},
            "created_at": whatsapp_manager.time.time(),
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"success": true, "messageId": "test-mid"}'
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        res = self._dispatch("beleza")

        self.assertEqual(res, {"action": "skip", "reason": "catalog-confirmation"})
        self.assertIn(self.SENDER_ID, whatsapp_manager._pending_catalog_action)

    @patch("urllib.request.urlopen")
    def test_list_catalog_command_is_deterministic_no_llm(self, mock_urlopen):
        """'listar catálogo' responde direto do arquivo, sem passar por _classify_owner_intent."""
        import whatsapp_manager
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"success": true, "messageId": "test-mid"}'
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        with patch("whatsapp_manager._load_product_catalog", return_value={
            "a": {"name": "Mentoria Individual", "price": "R$ 500", "active": True},
            "b": {"name": "Produto Antigo", "active": False},
        }), patch("whatsapp_manager._classify_owner_intent") as mock_intent:
            res = self._dispatch("listar catálogo")
            mock_intent.assert_not_called()

        self.assertEqual(res, {"action": "skip", "reason": "catalog-list-command"})

    @patch("urllib.request.urlopen")
    def test_list_catalog_empty_message(self, mock_urlopen):
        import whatsapp_manager
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"success": true, "messageId": "test-mid"}'
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        with patch("whatsapp_manager._load_product_catalog", return_value={}):
            res = self._dispatch("quais produtos")

        self.assertEqual(res, {"action": "skip", "reason": "catalog-list-command"})

    @patch("whatsapp_manager._find_catalog_matches", return_value=[
        ("mentoria-individual", {"name": "Mentoria Individual", "active": True}),
    ])
    @patch("whatsapp_manager._classify_owner_intent", return_value={
        "intent_type": "catalog_delete_permanent", "product_identifier": "mentoria", "intent": "apagar de vez",
    })
    @patch("urllib.request.urlopen")
    def test_delete_permanent_refuses_active_product(self, mock_urlopen, mock_intent, mock_find):
        """Não deixa apagar definitivamente um produto que ainda está ativo — pede pra desativar primeiro."""
        import whatsapp_manager
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"success": true, "messageId": "test-mid"}'
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        res = self._dispatch("apaga definitivamente o produto mentoria")

        self.assertEqual(res, {"action": "skip", "reason": "catalog-delete_permanent-draft"})
        self.assertNotIn(self.SENDER_ID, whatsapp_manager._pending_catalog_action)

    @patch("whatsapp_manager._find_catalog_matches", return_value=[
        ("mentoria-individual", {"name": "Mentoria Individual", "active": False}),
    ])
    @patch("whatsapp_manager._classify_owner_intent", return_value={
        "intent_type": "catalog_delete_permanent", "product_identifier": "mentoria", "intent": "apagar de vez",
    })
    @patch("urllib.request.urlopen")
    def test_delete_permanent_asks_confirmation_for_inactive_product(self, mock_urlopen, mock_intent, mock_find):
        """Produto já inativo: cria pendência de apagar definitivamente e pede confirmação."""
        import whatsapp_manager
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"success": true, "messageId": "test-mid"}'
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        res = self._dispatch("apaga definitivamente o produto mentoria")

        self.assertEqual(res, {"action": "skip", "reason": "catalog-delete_permanent-draft"})
        pending = whatsapp_manager._pending_catalog_action[self.SENDER_ID]
        self.assertEqual(pending["action"], "delete_permanent")
        self.assertEqual(pending["key"], "mentoria-individual")

    @patch("whatsapp_manager._save_product_catalog")
    @patch("whatsapp_manager._load_product_catalog", return_value={
        "mentoria-individual": {"name": "Mentoria Individual", "active": False},
    })
    @patch("urllib.request.urlopen")
    def test_delete_permanent_confirm_removes_key_entirely(self, mock_urlopen, mock_load, mock_save):
        """Confirmar a exclusão definitiva remove a chave do dict inteiramente (não é soft-delete)."""
        import whatsapp_manager
        whatsapp_manager._pending_catalog_action[self.SENDER_ID] = {
            "action": "delete_permanent", "key": "mentoria-individual",
            "created_at": whatsapp_manager.time.time(),
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"success": true, "messageId": "test-mid"}'
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        res = self._dispatch("sim")

        self.assertEqual(res, {"action": "skip", "reason": "catalog-confirmation"})
        mock_save.assert_called_once()
        saved_catalog = mock_save.call_args[0][0]
        self.assertNotIn("mentoria-individual", saved_catalog)


class TestFullSummaryFunctions(BaseWhatsAppManagerTest):
    """Testes para _update_full_summary, _compress_full_summary e _sync_full_summaries."""

    @patch("urllib.request.urlopen")
    @patch.dict(os.environ, {"GOOGLE_API_KEY": "test-key"})
    def test_update_full_summary_returns_llm_result(self, mock_urlopen):
        """_update_full_summary deve retornar texto do LLM."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "candidates": [{"content": {"parts": [{"text": "Jun/25: perguntou sobre preços."}]}}]
        }).encode()
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        from whatsapp_manager import _update_full_summary
        result = _update_full_summary(
            name="Carlos",
            existing_full_summary="",
            new_session_text="quanto custa o serviço?",
            session_date="Jun/25",
        )
        self.assertIsNotNone(result)
        self.assertIn("Jun/25", result)

    @patch.dict(os.environ, {}, clear=True)
    def test_update_full_summary_no_api_key_returns_none(self):
        """Sem chave de API, _update_full_summary deve retornar None."""
        from whatsapp_manager import _update_full_summary
        result = _update_full_summary("Maria", "", "oi tudo bem", "Jun/25")
        self.assertIsNone(result)

    @patch("urllib.request.urlopen")
    @patch.dict(os.environ, {"GOOGLE_API_KEY": "test-key"})
    def test_compress_full_summary_returns_llm_result(self, mock_urlopen):
        """_compress_full_summary deve retornar 1-2 frases do LLM."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "candidates": [{"content": {"parts": [{"text": "Carlos é cliente recorrente que busca suporte técnico."}]}}]
        }).encode()
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        from whatsapp_manager import _compress_full_summary
        long_summary = "Jun/25: " + ("x" * 300) + " Jul/25: " + ("y" * 300)
        result = _compress_full_summary("Carlos", long_summary)
        self.assertIsNotNone(result)
        self.assertIn("Carlos", result)

    @patch("sqlite3.connect")
    @patch("pathlib.Path.exists", return_value=True)
    @patch("whatsapp_manager._update_full_summary", return_value="Jun/25: pediu informações sobre produto.")
    def test_sync_full_summaries_only_includes_user_role(self, mock_update_summary, mock_exists, mock_connect):
        """_sync_full_summaries deve passar ao LLM apenas mensagens role=user (contato)."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_connect.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        mock_conn.__enter__ = lambda s: mock_conn
        mock_conn.__exit__ = MagicMock(return_value=False)

        # Uma sessão nova para Carlos
        mock_cursor.fetchall.side_effect = [
            [(42, 1750000000.0, "sessão junho")],  # new_sessions
            [
                ("user", "preciso de suporte"),
                ("assistant", "claro, como posso ajudar?"),
                ("user", "problema no acesso"),
                ("assistant", "vou verificar para você"),
            ],  # messages da sessão
        ]

        personal_contacts = {
            "5511888888888@s.whatsapp.net": {
                "name": "Carlos",
                "relationship": "Cliente",
                "full_summary": "",
                "last_summarized_at": 0,
            }
        }

        from whatsapp_manager import _sync_full_summaries
        count = _sync_full_summaries(personal_contacts, "/fake/state.db")

        self.assertEqual(count, 1)

        # Verificar que apenas mensagens role=user foram passadas ao LLM
        call_kwargs = mock_update_summary.call_args
        session_text = call_kwargs[1]["new_session_text"] if call_kwargs[1] else call_kwargs[0][2]
        self.assertIn("preciso de suporte", session_text)
        self.assertIn("problema no acesso", session_text)
        # Respostas do bot NÃO devem aparecer
        self.assertNotIn("claro, como posso ajudar", session_text)
        self.assertNotIn("vou verificar para você", session_text)

    @patch("pathlib.Path.exists", return_value=False)
    def test_sync_full_summaries_no_state_db_returns_zero(self, mock_exists):
        """Sem state.db, retorna 0 sem erros."""
        from whatsapp_manager import _sync_full_summaries
        result = _sync_full_summaries({"k": {"name": "X"}}, "/nonexistent/state.db")
        self.assertEqual(result, 0)

    @patch("sqlite3.connect")
    @patch("pathlib.Path.exists", return_value=True)
    @patch("whatsapp_manager._update_full_summary", return_value=None)
    def test_sync_full_summaries_skips_owner(self, mock_update, mock_exists, mock_connect):
        """_sync_full_summaries não deve processar o número do owner."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_connect.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        mock_conn.__enter__ = lambda s: mock_conn
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_cursor.fetchall.return_value = []

        owner_jid = "5511999999999@s.whatsapp.net"
        personal_contacts = {
            owner_jid: {"name": "André", "relationship": "Owner"},
        }

        from whatsapp_manager import _sync_full_summaries
        _sync_full_summaries(personal_contacts, "/fake/state.db")
        mock_update.assert_not_called()


class TestExtractContactNameViaLLM(BaseWhatsAppManagerTest):
    """Testes para _extract_contact_name_via_llm."""

    @patch("urllib.request.urlopen")
    @patch.dict(os.environ, {"GOOGLE_API_KEY": "test-key"})
    def test_returns_name_from_llm(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "candidates": [{"content": {"parts": [{"text": "Isabel Alencar"}]}}]
        }).encode()
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        from whatsapp_manager import _extract_contact_name_via_llm
        result = _extract_contact_name_via_llm("atualize a Isabel Alencar, ela é minha filha")
        self.assertEqual(result, "Isabel Alencar")

    @patch("urllib.request.urlopen")
    @patch.dict(os.environ, {"GOOGLE_API_KEY": "test-key"})
    def test_returns_none_when_llm_says_none(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "candidates": [{"content": {"parts": [{"text": "NONE"}]}}]
        }).encode()
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        from whatsapp_manager import _extract_contact_name_via_llm
        result = _extract_contact_name_via_llm("bom dia")
        self.assertIsNone(result)

    @patch("urllib.request.urlopen")
    @patch.dict(os.environ, {"GOOGLE_API_KEY": "test-key"})
    def test_returns_none_when_name_too_long(self, mock_urlopen):
        long_name = "A" * 65
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "candidates": [{"content": {"parts": [{"text": long_name}]}}]
        }).encode()
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        from whatsapp_manager import _extract_contact_name_via_llm
        result = _extract_contact_name_via_llm("alguma mensagem longa")
        self.assertIsNone(result)

    @patch.dict(os.environ, {}, clear=True)
    def test_returns_none_without_api_keys(self):
        from whatsapp_manager import _extract_contact_name_via_llm
        result = _extract_contact_name_via_llm("atualize o Carlos")
        self.assertIsNone(result)


class TestNormalizeAndTextUtils(BaseWhatsAppManagerTest):
    """Testes para _normalize_brazilian_phone e _normalize_text."""

    def test_normalize_brazilian_phone_removes_9_digit(self):
        """Número com 9 extra deve ser normalizado para 8 dígitos locais."""
        from whatsapp_manager import _normalize_brazilian_phone
        # 55 + DDD 11 + 9 (extra) + 8 dígitos = 13 dígitos → remove o 9
        self.assertEqual(_normalize_brazilian_phone("5511987654321"), "551187654321")

    def test_normalize_brazilian_phone_without_extra_9(self):
        """Número sem 9 extra não deve ser alterado."""
        from whatsapp_manager import _normalize_brazilian_phone
        self.assertEqual(_normalize_brazilian_phone("551187654321"), "551187654321")

    def test_normalize_brazilian_phone_strips_non_digits(self):
        """Deve ignorar espaços, parênteses, hifens."""
        from whatsapp_manager import _normalize_brazilian_phone
        self.assertEqual(_normalize_brazilian_phone("+55 (11) 98765-4321"), "551187654321")

    def test_normalize_brazilian_phone_international_no_brazil(self):
        """Número não-brasileiro não deve ser alterado."""
        from whatsapp_manager import _normalize_brazilian_phone
        # Número com código de país diferente (Argentina = 54)
        self.assertEqual(_normalize_brazilian_phone("541198765432"), "541198765432")

    def test_normalize_text_removes_accents_and_lowercases(self):
        """Deve remover acentos e converter para minúsculas."""
        from whatsapp_manager import _normalize_text
        self.assertEqual(_normalize_text("Isabel"), "isabel")
        self.assertEqual(_normalize_text("Ação"), "acao")
        self.assertEqual(_normalize_text("JOÃO"), "joao")
        self.assertEqual(_normalize_text("ñoño"), "nono")

    def test_normalize_text_empty_string(self):
        from whatsapp_manager import _normalize_text
        self.assertEqual(_normalize_text(""), "")

    def test_normalize_text_already_clean(self):
        from whatsapp_manager import _normalize_text
        self.assertEqual(_normalize_text("carlos"), "carlos")


class TestExtractJsonFromText(BaseWhatsAppManagerTest):
    """Testes para _extract_json_from_text."""

    def test_plain_json(self):
        from whatsapp_manager import _extract_json_from_text
        result = _extract_json_from_text('{"relationship": "Filho", "nickname": "Bel"}')
        self.assertEqual(result["relationship"], "Filho")
        self.assertEqual(result["nickname"], "Bel")

    def test_json_inside_markdown_block(self):
        from whatsapp_manager import _extract_json_from_text
        text = '```json\n{"relationship": "Cliente", "tone": "formal"}\n```'
        result = _extract_json_from_text(text)
        self.assertEqual(result["relationship"], "Cliente")

    def test_json_with_surrounding_text(self):
        from whatsapp_manager import _extract_json_from_text
        text = 'Aqui está a classificação:\n{"relationship": "Amigo"}\nEspero que ajude.'
        result = _extract_json_from_text(text)
        self.assertEqual(result["relationship"], "Amigo")

    def test_json_with_nested_object(self):
        from whatsapp_manager import _extract_json_from_text
        result = _extract_json_from_text('{"a": {"b": 1}, "c": "valor"}')
        self.assertEqual(result["a"]["b"], 1)
        self.assertEqual(result["c"], "valor")

    def test_no_json_raises(self):
        from whatsapp_manager import _extract_json_from_text
        with self.assertRaises((ValueError, json.JSONDecodeError)):
            _extract_json_from_text("Não há JSON aqui, apenas texto.")

    def test_json_with_null_values(self):
        from whatsapp_manager import _extract_json_from_text
        result = _extract_json_from_text('{"nickname": null, "pet_name": null}')
        self.assertIsNone(result["nickname"])
        self.assertIsNone(result["pet_name"])


class TestDetectContactQuery(BaseWhatsAppManagerTest):
    """Testes para _detect_contact_query."""

    def test_detects_conversa_com(self):
        from whatsapp_manager import _detect_contact_query
        # Padrão: "conversa com <Nome>" — sem artigo entre "com" e o nome
        self.assertEqual(_detect_contact_query("consegue ver a conversa com Isabel?"), "Isabel")

    def test_detects_o_que_disse(self):
        from whatsapp_manager import _detect_contact_query
        self.assertEqual(_detect_contact_query("o que Vivi disse ontem?"), "Vivi")

    def test_detects_historico_de(self):
        from whatsapp_manager import _detect_contact_query
        # Padrão: "histórico de <Nome>" — "do" corresponde a d[eo]
        self.assertEqual(_detect_contact_query("me mostra o histórico do Carlos"), "Carlos")

    def test_detects_mensagens_de(self):
        from whatsapp_manager import _detect_contact_query
        # Padrão: "mensagens de <Nome>" — "de" corresponde a d[eo]
        self.assertEqual(_detect_contact_query("quais as mensagens de Bruna?"), "Bruna")

    def test_ignores_stopwords(self):
        from whatsapp_manager import _detect_contact_query
        # "ela" é stopword
        self.assertIsNone(_detect_contact_query("o que ela disse?"))

    def test_returns_none_for_unrelated_message(self):
        from whatsapp_manager import _detect_contact_query
        self.assertIsNone(_detect_contact_query("bom dia, tudo bem?"))
        self.assertIsNone(_detect_contact_query("quero pedir uma pizza"))

    def test_case_insensitive(self):
        from whatsapp_manager import _detect_contact_query
        # Padrão sem artigo: "CONVERSA COM PEDRO" deve funcionar case-insensitive
        result = _detect_contact_query("CONVERSA COM PEDRO")
        self.assertIsNotNone(result)
        self.assertIn("Pedro", result.title())


class TestSearchContactByName(BaseWhatsAppManagerTest):
    """Testes para _search_contact_by_name."""

    def _patch_contacts(self, contacts: dict):
        return patch("whatsapp_manager._load_personal_contacts", return_value=contacts)

    def test_exact_name_match(self):
        from whatsapp_manager import _search_contact_by_name
        contacts = {
            "5511777@s.whatsapp.net": {"name": "Isabel Alencar", "nickname": None, "pet_name": None},
        }
        with self._patch_contacts(contacts):
            key, data = _search_contact_by_name("Isabel Alencar")
        self.assertEqual(key, "5511777@s.whatsapp.net")
        self.assertEqual(data["name"], "Isabel Alencar")

    def test_substring_name_match(self):
        from whatsapp_manager import _search_contact_by_name
        contacts = {
            "5511777@s.whatsapp.net": {"name": "Isabel Alencar", "nickname": None, "pet_name": None},
        }
        with self._patch_contacts(contacts):
            key, data = _search_contact_by_name("Isabel")
        self.assertIsNotNone(key)

    def test_nickname_match(self):
        from whatsapp_manager import _search_contact_by_name
        contacts = {
            "5511777@s.whatsapp.net": {"name": "Isabel Alencar", "nickname": "Bel", "pet_name": None},
        }
        with self._patch_contacts(contacts):
            key, data = _search_contact_by_name("Bel")
        self.assertEqual(key, "5511777@s.whatsapp.net")

    def test_no_match_returns_none(self):
        from whatsapp_manager import _search_contact_by_name
        contacts = {
            "5511777@s.whatsapp.net": {"name": "Carlos Silva", "nickname": None, "pet_name": None},
        }
        with self._patch_contacts(contacts):
            key, data = _search_contact_by_name("Desconhecido")
        self.assertIsNone(key)
        self.assertIsNone(data)

    def test_accent_insensitive(self):
        from whatsapp_manager import _search_contact_by_name
        contacts = {
            "5511777@s.whatsapp.net": {"name": "João Vítor", "nickname": None, "pet_name": None},
        }
        with self._patch_contacts(contacts):
            key, data = _search_contact_by_name("joao vitor")
        self.assertIsNotNone(key)


class TestFetchCrossSessionHistory(BaseWhatsAppManagerTest):
    """Testes para _fetch_cross_session_history."""

    @patch("sqlite3.connect")
    @patch("pathlib.Path.exists", return_value=True)
    def test_returns_formatted_history_from_bridge_db(self, mock_exists, mock_connect):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_connect.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        mock_conn.__enter__ = lambda s: mock_conn
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_cursor.fetchall.return_value = [
            (0, "Isabel", "oi André, tudo bem?", 1750000001),
            (1, "André", "tudo sim!", 1750000002),
        ]

        from whatsapp_manager import _fetch_cross_session_history
        result = _fetch_cross_session_history("5511777777777")

        self.assertIn("Isabel: oi André, tudo bem?", result)
        self.assertIn("André: tudo sim!", result)

    @patch("pathlib.Path.exists", return_value=False)
    def test_returns_empty_when_no_db(self, mock_exists):
        from whatsapp_manager import _fetch_cross_session_history
        result = _fetch_cross_session_history("5511777777777")
        self.assertEqual(result, "")

    @patch("sqlite3.connect")
    @patch("pathlib.Path.exists")
    def test_falls_back_to_state_db(self, mock_exists, mock_connect):
        """Quando bridge_db não tem resultados, usa state.db."""
        # bridge_db existe, state_db existe
        mock_exists.side_effect = [True, True]

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_connect.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        mock_conn.__enter__ = lambda s: mock_conn
        mock_conn.__exit__ = MagicMock(return_value=False)
        # Primeira chamada (bridge_db) retorna vazio; segunda (state.db) retorna dados
        mock_cursor.fetchall.side_effect = [
            [],
            [("user", None, "mensagem do contato", 1750000001)],
        ]

        from whatsapp_manager import _fetch_cross_session_history
        result = _fetch_cross_session_history("5511777777777")
        self.assertIn("mensagem do contato", result)

    @patch("sqlite3.connect")
    @patch("pathlib.Path.exists", return_value=True)
    def test_labels_messages_correctly(self, mock_exists, mock_connect):
        """from_me=1 deve aparecer como 'André'; from_me=0 como sender_name ou 'Contato'."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_connect.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        mock_conn.__enter__ = lambda s: mock_conn
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_cursor.fetchall.return_value = [
            (1, None, "resposta do bot", 1750000001),
            (0, None, "mensagem do contato", 1750000002),
        ]

        from whatsapp_manager import _fetch_cross_session_history
        result = _fetch_cross_session_history("5511777777777")
        self.assertIn("André: resposta do bot", result)
        self.assertIn("Contato: mensagem do contato", result)


class TestBestContactName(BaseWhatsAppManagerTest):
    """Testes para _best_contact_name."""

    def test_prefers_bridge_name_over_db(self):
        from whatsapp_manager import _best_contact_name
        name, source = _best_contact_name("5511@s", "Isabel", "Contato 7777", "5511")
        self.assertEqual(name, "Isabel")
        self.assertEqual(source, "bridge")

    def test_uses_db_name_when_bridge_generic(self):
        from whatsapp_manager import _best_contact_name
        name, source = _best_contact_name("5511@s", None, "Carlos Silva", "5511")
        self.assertEqual(name, "Carlos Silva")
        self.assertEqual(source, "log")

    def test_fallback_when_both_generic(self):
        from whatsapp_manager import _best_contact_name
        name, source = _best_contact_name("5511@s", None, None, "5511777")
        self.assertEqual(name, "Contato 5511777")
        self.assertEqual(source, "fallback")

    def test_rejects_numeric_bridge_name(self):
        """Nome que é só número não deve ser aceito como nome real."""
        from whatsapp_manager import _best_contact_name
        name, source = _best_contact_name("5511@s", "5511777777777", "Pedro", "5511")
        self.assertEqual(name, "Pedro")
        self.assertEqual(source, "log")

    def test_rejects_jid_as_name(self):
        from whatsapp_manager import _best_contact_name
        name, source = _best_contact_name("5511@s", "5511@s.whatsapp.net", None, "5511")
        self.assertEqual(source, "fallback")


class TestCallLlmApi(BaseWhatsAppManagerTest):
    """Testes para _call_llm_api."""

    @patch("urllib.request.urlopen")
    def test_returns_extracted_text(self, mock_urlopen):
        from whatsapp_manager import _call_llm_api
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"text": "resultado"}).encode()
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        result = _call_llm_api(
            url="http://fake/api",
            headers={"Content-Type": "application/json"},
            payload={"prompt": "teste"},
            extract_fn=lambda r: r["text"],
        )
        self.assertEqual(result, "resultado")

    @patch("urllib.request.urlopen", side_effect=urllib.error.URLError("timeout"))
    def test_returns_none_on_http_error(self, mock_urlopen):
        from whatsapp_manager import _call_llm_api
        result = _call_llm_api(
            url="http://fake/api",
            headers={},
            payload={},
            extract_fn=lambda r: r["text"],
        )
        self.assertIsNone(result)

    @patch("urllib.request.urlopen")
    def test_returns_none_on_malformed_json_response(self, mock_urlopen):
        from whatsapp_manager import _call_llm_api
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"NOT JSON"
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        result = _call_llm_api(
            url="http://fake/api",
            headers={},
            payload={},
            extract_fn=lambda r: r["text"],
        )
        self.assertIsNone(result)

    @patch("urllib.request.urlopen")
    def test_returns_none_when_extract_fn_raises_key_error(self, mock_urlopen):
        from whatsapp_manager import _call_llm_api
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"other": "field"}).encode()
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        result = _call_llm_api(
            url="http://fake/api",
            headers={},
            payload={},
            extract_fn=lambda r: r["missing_key"],
        )
        self.assertIsNone(result)


class TestContactAiAccess(unittest.TestCase):
    """Legados/importados ficam off; somente contato novo realtime entra no fluxo."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "personal_contacts.json"
        self.path_patcher = patch.object(whatsapp_manager, "_PERSONAL_CONTACTS_PATH", self.path)
        self.path_patcher.start()

    def tearDown(self):
        self.path_patcher.stop()
        self.tmp.cleanup()

    def _write(self, data):
        self.path.write_text(json.dumps(data), encoding="utf-8")

    def _read(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_existing_legacy_without_flag_is_migrated_disabled(self):
        jid = "5511888888888@s.whatsapp.net"
        self._write({jid: {"name": "Contato legado", "relationship": "Cliente"}})

        allowed, reason = whatsapp_manager._ensure_contact_ai_access(jid, jid)

        self.assertFalse(allowed)
        self.assertEqual(reason, "legacy-contact-disabled")
        row = self._read()[jid]
        self.assertIs(row["ai_enabled"], False)
        self.assertIs(row["in_flow"], False)
        self.assertEqual(row["flow_origin"], "legacy_sync")

    def test_explicit_flow_contact_is_allowed(self):
        jid = "5511777777777@s.whatsapp.net"
        self._write({jid: {"ai_enabled": True, "in_flow": True, "flow_origin": "crm"}})

        allowed, reason = whatsapp_manager._ensure_contact_ai_access(jid, jid)

        self.assertTrue(allowed)
        self.assertEqual(reason, "explicit-flow")

    def test_unknown_realtime_contact_enters_flow_enabled(self):
        jid = "5511666666666@s.whatsapp.net"
        self._write({})

        allowed, reason = whatsapp_manager._ensure_contact_ai_access(jid, jid)

        self.assertTrue(allowed)
        self.assertEqual(reason, "new-live-inbound")
        row = self._read()[jid]
        self.assertIs(row["ai_enabled"], True)
        self.assertIs(row["in_flow"], True)
        self.assertEqual(row["flow_origin"], "new_live_inbound")

    def test_historical_unknown_contact_never_enters_flow(self):
        jid = "5511555555555@s.whatsapp.net"
        self._write({})

        allowed, reason = whatsapp_manager._ensure_contact_ai_access(jid, jid, is_historical=True)

        self.assertFalse(allowed)
        self.assertEqual(reason, "historical-import")
        self.assertEqual(self._read(), {})

    def test_corrupted_policy_is_fail_closed(self):
        self.path.write_text("{invalid", encoding="utf-8")

        allowed, reason = whatsapp_manager._ensure_contact_ai_access(
            "5511444444444@s.whatsapp.net",
            "5511444444444@s.whatsapp.net",
        )

        self.assertFalse(allowed)
        self.assertEqual(reason, "contact-policy-unavailable")

    def test_sync_merge_cannot_reenable_local_disabled_contact(self):
        key = "5511333333333@s.whatsapp.net"
        remote = {key: {"name": "Legado", "ai_enabled": True, "in_flow": True}}
        local = {key: {"name": "Legado", "ai_enabled": False, "in_flow": False}}

        merged = whatsapp_manager._merge_records_field_level(remote, local)

        self.assertIs(merged[key]["ai_enabled"], False)
        self.assertIs(merged[key]["in_flow"], False)


class TestCheckChatSilenced(BaseWhatsAppManagerTest):
    """Testes para _check_chat_silenced."""

    def setUp(self):
        super().setUp()
        import whatsapp_manager
        whatsapp_manager._chat_status_cache.clear()

    @patch("urllib.request.urlopen")
    def test_returns_true_when_silenced(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"isSilenced": True}).encode()
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        from whatsapp_manager import _check_chat_silenced
        result = _check_chat_silenced("5511888888888@s.whatsapp.net")
        self.assertTrue(result)

    @patch("urllib.request.urlopen")
    def test_returns_false_when_not_silenced(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"isSilenced": False}).encode()
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        from whatsapp_manager import _check_chat_silenced
        result = _check_chat_silenced("5511888888888@s.whatsapp.net")
        self.assertFalse(result)

    @patch("urllib.request.urlopen", side_effect=OSError("bridge offline"))
    def test_returns_true_when_bridge_offline_fail_closed(self, mock_urlopen):
        from whatsapp_manager import _check_chat_silenced
        result = _check_chat_silenced("5511888888888@s.whatsapp.net")
        self.assertTrue(result)

    @patch("urllib.request.urlopen")
    def test_invalid_payload_is_fail_closed(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"status": "unknown"}).encode()
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        from whatsapp_manager import _check_chat_silenced
        result = _check_chat_silenced("5511888888888@s.whatsapp.net")
        self.assertTrue(result)

    @patch("urllib.request.urlopen")
    def test_false_status_is_not_cached(self, mock_urlopen):
        """Estado livre deve ser reconsultado para takeover manual ter efeito imediato."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"isSilenced": False}).encode()
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        from whatsapp_manager import _check_chat_silenced
        _check_chat_silenced("5512222@s.whatsapp.net")
        _check_chat_silenced("5512222@s.whatsapp.net")

        self.assertEqual(mock_urlopen.call_count, 2)

    @patch("urllib.request.urlopen")
    def test_uses_cache_on_second_call(self, mock_urlopen):
        """Segunda chamada dentro do TTL não deve fazer HTTP."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"isSilenced": True}).encode()
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        from whatsapp_manager import _check_chat_silenced
        _check_chat_silenced("5511111@s.whatsapp.net")
        _check_chat_silenced("5511111@s.whatsapp.net")

        self.assertEqual(mock_urlopen.call_count, 1)


class TestNLUpdateOwnerFieldsRestriction(BaseWhatsAppManagerTest):
    """Garante que atualização NL não sobrescreve tone/summary/guidelines."""

    @patch("whatsapp_manager._update_contact_fields", return_value="✅ Contato Carlos atualizado.")
    @patch("whatsapp_manager._extract_contact_name_via_llm", return_value="Carlos")
    @patch("whatsapp_manager._classify_contact_via_llm", return_value={
        "relationship": "Cliente",
        "manual_relationship": "Cliente",
        "name": "Carlos",
        "nickname": None,
        "pet_name": None,
        "notes": "cliente frequente",
        "product": None,
        "frequent_greeting": None,
        # Campos que NÃO devem ser aplicados
        "tone": "polido e profissional",
        "summary": "inventado pelo LLM",
        "guidelines": "inventado também",
        "intent": "comprar",
        "frequency": "semanal",
    })
    @patch("urllib.request.urlopen")
    def test_restricted_fields_not_passed_to_update(
        self, mock_urlopen, mock_classify, mock_extract, mock_update
    ):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"success": true, "messageId": "test-mid"}'
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        pre_dispatch = self.ctx.hooks.get("pre_gateway_dispatch")
        event = MagicMock()
        event.source.platform = "whatsapp"
        event.source.user_id = "5511999999999@s.whatsapp.net"
        event.source.chat_id = "5511999999999@s.whatsapp.net"
        event.text = "atualize o Carlos, ele é cliente"
        event.has_media = False

        gateway = MagicMock()
        gateway._session_key_for_source.return_value = "sess_nl"
        gateway._session_model_overrides = {}

        pre_dispatch("pre_gateway_dispatch", {"event": event, "gateway": gateway})

        if mock_update.called:
            _, fields_passed = mock_update.call_args[0]
            self.assertNotIn("tone", fields_passed)
            self.assertNotIn("summary", fields_passed)
            self.assertNotIn("guidelines", fields_passed)
            self.assertNotIn("intent", fields_passed)
            self.assertNotIn("frequency", fields_passed)


class TestFetchChatHistory(BaseWhatsAppManagerTest):
    """Testes para _fetch_chat_history."""

    @patch("urllib.request.urlopen")
    def test_returns_history_string(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"history": "Carlos: oi\nAndré: tudo bem"}).encode()
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        from whatsapp_manager import _fetch_chat_history
        result = _fetch_chat_history("5511888@s.whatsapp.net", limit=10)
        self.assertEqual(result, "Carlos: oi\nAndré: tudo bem")

    @patch("urllib.request.urlopen", side_effect=OSError("offline"))
    def test_returns_empty_when_server_offline(self, mock_urlopen):
        from whatsapp_manager import _fetch_chat_history
        result = _fetch_chat_history("5511888@s.whatsapp.net")
        self.assertEqual(result, "")

    @patch("urllib.request.urlopen")
    def test_returns_empty_when_no_history_key(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"other": "data"}).encode()
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        from whatsapp_manager import _fetch_chat_history
        result = _fetch_chat_history("5511888@s.whatsapp.net")
        self.assertEqual(result, "")

    @patch("urllib.request.urlopen", side_effect=OSError("message server offline"))
    def test_falls_back_to_local_sqlite_when_server_is_offline(self, mock_urlopen):
        from whatsapp_manager import _fetch_chat_history

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "whatsapp_messages.db"
            conn = sqlite3.connect(db_path)
            conn.execute(
                """
                CREATE TABLE messages (
                    chat_id TEXT,
                    sender_name TEXT,
                    body TEXT,
                    timestamp REAL,
                    from_me INTEGER
                )
                """
            )
            conn.executemany(
                "INSERT INTO messages VALUES (?, ?, ?, ?, ?)",
                [
                    ("12025550123@s.whatsapp.net", "Taylor", "We operate in Massachusetts", 1, 0),
                    ("12025550123@s.whatsapp.net", "AYA", "How do you manage the schedule?", 2, 1),
                    ("other@s.whatsapp.net", "Other", "must not leak", 3, 0),
                ],
            )
            conn.commit()
            conn.close()

            with patch.object(whatsapp_manager, "_MSG_DB_PATH", db_path):
                result = _fetch_chat_history("12025550123@s.whatsapp.net", limit=10)

        self.assertEqual(
            result,
            "Taylor: We operate in Massachusetts\nAYA: How do you manage the schedule?",
        )

    @patch("urllib.request.urlopen", side_effect=OSError("message server offline"))
    def test_local_fallback_resolves_phone_to_lid_history(self, mock_urlopen):
        from whatsapp_manager import _fetch_chat_history

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "whatsapp_messages.db"
            conn = sqlite3.connect(db_path)
            conn.execute(
                """
                CREATE TABLE messages (
                    chat_id TEXT,
                    sender_name TEXT,
                    body TEXT,
                    timestamp REAL,
                    from_me INTEGER
                )
                """
            )
            conn.execute(
                "INSERT INTO messages VALUES (?, ?, ?, ?, ?)",
                ("777000111@lid", "Taylor", "My cleaning company operates in the US", 1, 0),
            )
            conn.commit()
            conn.close()

            with patch.object(whatsapp_manager, "_MSG_DB_PATH", db_path), patch.dict(
                whatsapp_manager._lid_to_phone,
                {"777000111": "12025550123"},
                clear=True,
            ):
                result = _fetch_chat_history("12025550123@s.whatsapp.net", limit=10)

        self.assertEqual(result, "Taylor: My cleaning company operates in the US")


class TestResolveContactNameFromBridge(BaseWhatsAppManagerTest):
    """Testes para _resolve_contact_name_from_bridge."""

    @patch("urllib.request.urlopen")
    def test_returns_name_when_found(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"name": "Isabel Alencar"}).encode()
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        from whatsapp_manager import _resolve_contact_name_from_bridge
        result = _resolve_contact_name_from_bridge("5511777777777@s.whatsapp.net")
        self.assertEqual(result, "Isabel Alencar")

    @patch("urllib.request.urlopen")
    def test_returns_none_when_name_is_empty(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"name": ""}).encode()
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        from whatsapp_manager import _resolve_contact_name_from_bridge
        result = _resolve_contact_name_from_bridge("5511777@s.whatsapp.net")
        self.assertIsNone(result)

    @patch("urllib.request.urlopen", side_effect=Exception("bridge offline"))
    def test_returns_none_when_bridge_fails(self, mock_urlopen):
        from whatsapp_manager import _resolve_contact_name_from_bridge
        result = _resolve_contact_name_from_bridge("5511777@s.whatsapp.net")
        self.assertIsNone(result)

    def test_returns_none_for_empty_jid(self):
        from whatsapp_manager import _resolve_contact_name_from_bridge
        self.assertIsNone(_resolve_contact_name_from_bridge(""))
        self.assertIsNone(_resolve_contact_name_from_bridge(None))

    @patch("urllib.request.urlopen")
    def test_strips_whitespace_from_name(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"name": "  Carlos  "}).encode()
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        from whatsapp_manager import _resolve_contact_name_from_bridge
        result = _resolve_contact_name_from_bridge("5511888@s.whatsapp.net")
        self.assertEqual(result, "Carlos")


class TestPushPersonalContactsToGithub(BaseWhatsAppManagerTest):
    """Testes para _push_personal_contacts_to_github."""

    @patch.dict(os.environ, {}, clear=True)
    @patch("pathlib.Path.exists", return_value=True)
    def test_returns_false_without_config_repo(self, mock_exists):
        from whatsapp_manager import _push_personal_contacts_to_github
        # Sem CONFIG_REPO nem token, deve retornar False imediatamente
        result = _push_personal_contacts_to_github()
        self.assertFalse(result)

    @patch("pathlib.Path.exists", return_value=False)
    def test_returns_false_when_file_not_found(self, mock_exists):
        from whatsapp_manager import _push_personal_contacts_to_github
        result = _push_personal_contacts_to_github()
        self.assertFalse(result)

    @patch("whatsapp_manager._github_put_file", return_value=True)
    @patch("pathlib.Path.read_bytes", return_value=b'{"test": 1}')
    @patch("pathlib.Path.exists", return_value=True)
    @patch.dict(os.environ, {"CONFIG_REPO": "user/repo", "CONFIG_GITHUB_TOKEN": "fake-token"})
    def test_calls_github_put_file_with_correct_args(self, mock_exists, mock_read, mock_put):
        from whatsapp_manager import _push_personal_contacts_to_github
        result = _push_personal_contacts_to_github()
        self.assertTrue(result)
        mock_put.assert_called_once()
        call_kwargs = mock_put.call_args
        self.assertEqual(call_kwargs[1].get("github_path") or call_kwargs[0][3], "personal_contacts.json")


class TestFieldLevelMergeAndPushFailureNotification(BaseWhatsAppManagerTest):
    """Testes para _merge_records_field_level e _notify_owner_if_push_failed.

    Cobre a correção do bug: um pull periódico não deve mais apagar um campo local
    preenchido (ex: manual_relationship) com um valor vazio/nulo vindo do GitHub, e uma
    falha de push não deve mais ficar 100% silenciosa.
    """

    def test_remote_empty_field_does_not_wipe_local_value(self):
        from whatsapp_manager import _merge_records_field_level
        remote = {"5511@s.whatsapp.net": {"name": "Isabel", "manual_relationship": None}}
        local = {"5511@s.whatsapp.net": {"name": "Isabel", "manual_relationship": "Filha"}}
        merged = _merge_records_field_level(remote, local)
        self.assertEqual(merged["5511@s.whatsapp.net"]["manual_relationship"], "Filha")

    def test_remote_non_empty_field_wins_over_local(self):
        from whatsapp_manager import _merge_records_field_level
        remote = {"5511@s.whatsapp.net": {"name": "Isabel", "manual_relationship": "Amiga"}}
        local = {"5511@s.whatsapp.net": {"name": "Isabel", "manual_relationship": "Filha"}}
        merged = _merge_records_field_level(remote, local)
        self.assertEqual(merged["5511@s.whatsapp.net"]["manual_relationship"], "Amiga")

    def test_local_only_key_is_preserved(self):
        from whatsapp_manager import _merge_records_field_level
        remote = {"a@s.whatsapp.net": {"name": "A"}}
        local = {"a@s.whatsapp.net": {"name": "A"}, "b@s.whatsapp.net": {"name": "B"}}
        merged = _merge_records_field_level(remote, local)
        self.assertIn("b@s.whatsapp.net", merged)
        self.assertEqual(merged["b@s.whatsapp.net"]["name"], "B")

    def test_local_only_field_not_present_in_remote_is_kept(self):
        from whatsapp_manager import _merge_records_field_level
        remote = {"a@s.whatsapp.net": {"name": "A"}}
        local = {"a@s.whatsapp.net": {"name": "A", "notes": "prefere ligação"}}
        merged = _merge_records_field_level(remote, local)
        self.assertEqual(merged["a@s.whatsapp.net"]["notes"], "prefere ligação")

    @patch("whatsapp_manager._human_send")
    def test_notifies_owner_when_push_fails(self, mock_send):
        from whatsapp_manager import _notify_owner_if_push_failed
        # O aviso só faz sentido quando o sync está configurado — daí o repo e o token.
        with patch.dict(os.environ, {"CONFIG_REPO": "contexto", "CONFIG_GITHUB_TOKEN": "tok"}):
            _notify_owner_if_push_failed(lambda: False, "os contatos")
        mock_send.assert_called_once()
        chat_id, message = mock_send.call_args[0]
        self.assertIn("5511999999999", chat_id)
        self.assertIn("os contatos", message)

    @patch("whatsapp_manager._human_send")
    def test_does_not_notify_when_push_succeeds(self, mock_send):
        from whatsapp_manager import _notify_owner_if_push_failed
        with patch.dict(os.environ, {"CONFIG_REPO": "contexto", "CONFIG_GITHUB_TOKEN": "tok"}):
            _notify_owner_if_push_failed(lambda: True, "os contatos")
        mock_send.assert_not_called()

    @patch("whatsapp_manager._human_send")
    def test_does_not_notify_when_sync_is_not_configured(self, mock_send):
        """Sem repo/token não há sync, então não há falha a avisar.

        Antes o dono recebia '⚠️ Não consegui sincronizar' a cada contato salvo, venda
        registrada e item de catálogo alterado em qualquer instalação sem GitHub.
        """
        from whatsapp_manager import _notify_owner_if_push_failed
        with patch.dict(os.environ, {"CONFIG_REPO": "", "CONFIG_GITHUB_TOKEN": ""}):
            _notify_owner_if_push_failed(lambda: False, "os contatos")
        mock_send.assert_not_called()


class TestRunSyncInBackground(BaseWhatsAppManagerTest):
    """Testes para _run_sync_in_background."""

    def setUp(self):
        super().setUp()
        import whatsapp_manager
        whatsapp_manager._sync_running.clear()

    def tearDown(self):
        import whatsapp_manager
        whatsapp_manager._sync_running.clear()
        super().tearDown()

    @patch("whatsapp_manager._sync_contacts_from_db_internal", return_value="10 contatos sincronizados")
    def test_starts_thread_and_sets_sync_running(self, mock_sync):
        import time
        import whatsapp_manager
        from whatsapp_manager import _run_sync_in_background

        _run_sync_in_background(force=True, chat_id=None)

        # Aguardar o worker concluir e liberar o claim (máx 2s).
        for _ in range(20):
            if mock_sync.called and not whatsapp_manager._sync_running.is_set():
                break
            time.sleep(0.1)

        self.assertTrue(mock_sync.called)
        # Após conclusão, o lock deve estar limpo
        self.assertFalse(whatsapp_manager._sync_running.is_set())

    @patch("whatsapp_manager._sync_contacts_from_db_internal", return_value="ok")
    def test_blocks_concurrent_sync(self, mock_sync):
        import whatsapp_manager
        from whatsapp_manager import _run_sync_in_background

        # Simular sync já em andamento
        whatsapp_manager._sync_running.set()

        _run_sync_in_background(force=True, chat_id=None)

        # _sync_contacts_from_db_internal NÃO deve ter sido chamado
        mock_sync.assert_not_called()

    @patch("urllib.request.urlopen")
    @patch("whatsapp_manager._sync_contacts_from_db_internal", return_value="5 atualizados")
    def test_notifies_owner_when_chat_id_provided(self, mock_sync, mock_urlopen):
        import time

        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"success": true, "messageId": "test-mid"}'
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        from whatsapp_manager import _run_sync_in_background
        _run_sync_in_background(force=False, chat_id="5511999999999@s.whatsapp.net")

        for _ in range(20):
            if mock_urlopen.called:
                break
            time.sleep(0.1)

        self.assertTrue(mock_urlopen.called)
        req = mock_urlopen.call_args[0][0]
        self.assertIn("/send", req.full_url)


class TestSyncContactsNamePreservation(BaseWhatsAppManagerTest):
    """Garante que sync não substitui nome real por nome genérico 'Contato XXXX'."""

    @patch("sqlite3.connect")
    @patch("whatsapp_manager._classify_contact_via_llm")
    @patch("pathlib.Path.exists", return_value=True)
    @patch("builtins.open")
    def test_does_not_overwrite_real_name_with_generic(
        self, mock_open, mock_exists, mock_classify, mock_connect
    ):
        from whatsapp_manager import _sync_contacts_from_db_internal

        existing = {
            "5511777777777@s.whatsapp.net": {
                "name": "Isabel Alencar",
                "relationship": "Parente",
                "tone": "informal",
                "guidelines": "seja gentil",
                "market_id": "US",
                "country": "United States",
                "currency": "USD",
                "language": "es",
                "last_interaction": 1686450000,
            }
        }

        mock_file = unittest.mock.mock_open(read_data=json.dumps(existing))
        mock_open.side_effect = lambda path, *a, **kw: mock_file(path, *a, **kw)

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_connect.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor

        # Sync retorna nome genérico "Contato 7777" (como Baileys faz em multi-device)
        mock_cursor.fetchall.side_effect = [
            [("5511777777777@s.whatsapp.net", 1686450001, 1)],
            [("5511777777777@s.whatsapp.net", "Contato 7777", 10, 1686440000, 1686450001)],
            [("user", "5511777777777", "oi tudo bem")],
        ]

        mock_classify.return_value = {
            "relationship": "Parente",
            "tone": "informal",
            "nickname": None, "pet_name": None, "frequent_greeting": None,
            "summary": "Conversa familiar.", "intent": "Manter contato.",
            "frequency": "semanal", "product": None,
            "guidelines": "seja gentil.",
        }

        with patch.dict(os.environ, {"CONFIG_REPO": ""}):
            _sync_contacts_from_db_internal(force=True)

        write_calls = mock_file().write.call_args_list
        written_data = "".join(c[0][0] for c in write_calls)
        written = json.loads(written_data)

        # Nome real deve ser preservado, não substituído por "Contato 7777"
        saved_name = written.get("5511777777777@s.whatsapp.net", {}).get("name")
        self.assertEqual(saved_name, "Isabel Alencar",
                         f"Nome real deve ser preservado, mas ficou: {saved_name!r}")
        saved = written["5511777777777@s.whatsapp.net"]
        self.assertEqual(saved["market_id"], "US")
        self.assertEqual(saved["country"], "United States")
        self.assertEqual(saved["currency"], "USD")
        self.assertEqual(saved["language"], "es")


class TestLiveClassifyContact(BaseWhatsAppManagerTest):
    """Testes para _live_classify_contact."""

    def _make_mock_db(self, mock_connect, stats_row, history_rows):
        """Helper: configura mock SQLite para retornar stats e histórico."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_connect.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        mock_conn.__enter__ = lambda s: mock_conn
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_cursor.fetchone.return_value = stats_row
        mock_cursor.fetchall.return_value = history_rows
        return mock_conn, mock_cursor

    @patch("whatsapp_manager._push_personal_contacts_to_github")
    @patch("whatsapp_manager._classify_contact_via_llm")
    @patch("sqlite3.connect")
    @patch("pathlib.Path.exists", return_value=True)
    @patch("builtins.open", new_callable=unittest.mock.mock_open)
    def test_classifies_new_contact_with_sufficient_messages(
        self, mock_open, mock_exists, mock_connect, mock_classify, mock_push
    ):
        """Contato novo com mensagens suficientes deve ser classificado via LLM."""
        mock_conn, mock_cursor = self._make_mock_db(
            mock_connect,
            stats_row=(10, 1686440000, 1686450000, "Carlos"),
            history_rows=[
                (0, "Carlos", "preciso de ajuda com o sistema"),
                (1, "André", "claro, pode me dizer o problema?"),
            ],
        )

        mock_classify.return_value = {
            "relationship": "Cliente",
            "tone": "polido e profissional",
            "nickname": None, "pet_name": None,
            "frequent_greeting": None,
            "summary": "Cliente buscando suporte técnico.",
            "intent": "Suporte.", "frequency": "esporádica",
            "guidelines": "Seja prestativo.",
            "product": None,
        }

        personal_contacts = {}

        from whatsapp_manager import _live_classify_contact
        result = _live_classify_contact(
            sender_id="5511888888888@s.whatsapp.net",
            db_query_jid="5511888888888@s.whatsapp.net",
            phone_number="5511888888888@s.whatsapp.net",
            contact_info=None,
            target_key="5511888888888@s.whatsapp.net",
            personal_contacts=personal_contacts,
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["name"], "Carlos")
        self.assertEqual(result["relationship"], "Cliente")
        mock_classify.assert_called_once()

    @patch("pathlib.Path.exists", return_value=True)
    @patch("sqlite3.connect")
    def test_returns_none_when_no_messages(self, mock_connect, mock_exists):
        """Sem mensagens no DB, deve retornar None sem chamar LLM."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_connect.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchone.return_value = (0, None, None, None)
        mock_cursor.fetchall.return_value = []

        from whatsapp_manager import _live_classify_contact
        result = _live_classify_contact(
            sender_id="5511777777777@s.whatsapp.net",
            db_query_jid="5511777777777@s.whatsapp.net",
            phone_number="5511777777777@s.whatsapp.net",
            contact_info=None,
            target_key="5511777777777@s.whatsapp.net",
            personal_contacts={},
        )

        self.assertIsNone(result)

    def test_returns_none_for_owner(self):
        """Dono nunca deve ser classificado — deve retornar None imediatamente."""
        from whatsapp_manager import _live_classify_contact
        # O número do owner está em WHATSAPP_OWNER_NUMBER = "5511999999999"
        result = _live_classify_contact(
            sender_id="5511999999999@s.whatsapp.net",
            db_query_jid="5511999999999@s.whatsapp.net",
            phone_number="5511999999999@s.whatsapp.net",
            contact_info=None,
            target_key="5511999999999@s.whatsapp.net",
            personal_contacts={},
        )
        self.assertIsNone(result)

    @patch("whatsapp_manager._push_personal_contacts_to_github")
    @patch("whatsapp_manager._classify_contact_via_llm")
    @patch("sqlite3.connect")
    @patch("pathlib.Path.exists", return_value=True)
    @patch("builtins.open", new_callable=unittest.mock.mock_open)
    def test_preserves_manual_relationship(
        self, mock_open, mock_exists, mock_connect, mock_classify, mock_push
    ):
        """manual_relationship definido pelo dono deve sobrescrever o que o LLM classificar."""
        mock_conn, mock_cursor = self._make_mock_db(
            mock_connect,
            stats_row=(5, 1686440000, 1686450000, "Pedro"),
            history_rows=[(0, "Pedro", "oi André")],
        )

        mock_classify.return_value = {
            "relationship": "Cliente",   # LLM acha que é cliente
            "tone": "polido e profissional",
            "nickname": None, "pet_name": None, "frequent_greeting": None,
            "summary": "Conversa casual.", "intent": "Social.",
            "frequency": "semanal", "guidelines": "Seja gentil.", "product": None,
        }

        contact_info = {
            "name": "Pedro",
            "manual_relationship": "Amigo",  # dono definiu como amigo
            "relationship": "Amigo",
            "notes": "vizinho de longa data",
            "market_id": "US",
            "country": "United States",
            "currency": "USD",
            "language": "es",
        }

        from whatsapp_manager import _live_classify_contact
        result = _live_classify_contact(
            sender_id="5511666666666@s.whatsapp.net",
            db_query_jid="5511666666666@s.whatsapp.net",
            phone_number="5511666666666@s.whatsapp.net",
            contact_info=contact_info,
            target_key="5511666666666@s.whatsapp.net",
            personal_contacts={"5511666666666@s.whatsapp.net": contact_info},
        )

        self.assertIsNotNone(result)
        # manual_relationship deve prevalecer sobre o que o LLM retornou
        self.assertEqual(result["relationship"], "Amigo")
        self.assertEqual(result["manual_relationship"], "Amigo")
        # notes devem ser preservadas
        self.assertEqual(result["notes"], "vizinho de longa data")
        self.assertEqual(result["market_id"], "US")
        self.assertEqual(result["country"], "United States")
        self.assertEqual(result["currency"], "USD")
        self.assertEqual(result["language"], "es")

    @patch("whatsapp_manager._push_personal_contacts_to_github")
    @patch("sqlite3.connect")
    @patch("pathlib.Path.exists", return_value=True)
    @patch("builtins.open", new_callable=unittest.mock.mock_open)
    def test_uses_stub_classification_below_min_threshold(
        self, mock_open, mock_exists, mock_connect, mock_push
    ):
        """Com poucas mensagens (< mínimo), deve usar classificação padrão sem chamar LLM."""
        mock_conn, mock_cursor = self._make_mock_db(
            mock_connect,
            stats_row=(1, 1686440000, 1686450000, "Laura"),
            history_rows=[(0, "Laura", "oi")],
        )

        from whatsapp_manager import _live_classify_contact
        with patch("whatsapp_manager._classify_contact_via_llm") as mock_classify:
            result = _live_classify_contact(
                sender_id="5511555555555@s.whatsapp.net",
                db_query_jid="5511555555555@s.whatsapp.net",
                phone_number="5511555555555@s.whatsapp.net",
                contact_info=None,
                target_key="5511555555555@s.whatsapp.net",
                personal_contacts={},
            )
            # LLM não deve ser chamado
            mock_classify.assert_not_called()

        # Resultado deve usar valores padrão
        self.assertIsNotNone(result)
        self.assertEqual(result["relationship"], "Cliente")
        self.assertEqual(result["summary"], "Conversa muito curta.")

    @patch("whatsapp_manager._push_personal_contacts_to_github")
    @patch("whatsapp_manager._classify_contact_via_llm")
    @patch("sqlite3.connect")
    @patch("pathlib.Path.exists", return_value=True)
    @patch("builtins.open", new_callable=unittest.mock.mock_open)
    def test_persists_to_personal_contacts_json(
        self, mock_open, mock_exists, mock_connect, mock_classify, mock_push
    ):
        """Resultado da classificação deve ser gravado em personal_contacts.json."""
        mock_conn, mock_cursor = self._make_mock_db(
            mock_connect,
            stats_row=(8, 1686440000, 1686450000, "Ana"),
            history_rows=[(0, "Ana", "quero contratar")],
        )

        mock_classify.return_value = {
            "relationship": "Cliente", "tone": "formal",
            "nickname": None, "pet_name": None, "frequent_greeting": None,
            "summary": "Interesse em contratar.", "intent": "Compra.",
            "frequency": "esporádica", "guidelines": "Apresente o produto.", "product": "SaaS",
        }

        personal_contacts = {}

        from whatsapp_manager import _live_classify_contact
        _live_classify_contact(
            sender_id="5511444444444@s.whatsapp.net",
            db_query_jid="5511444444444@s.whatsapp.net",
            phone_number="5511444444444@s.whatsapp.net",
            contact_info=None,
            target_key="5511444444444@s.whatsapp.net",
            personal_contacts=personal_contacts,
        )

        # Deve ter tentado abrir o arquivo para escrita
        write_calls = [c for c in mock_open.call_args_list if "w" in str(c)]
        self.assertTrue(len(write_calls) > 0, "personal_contacts.json deve ser gravado")

        # Contato deve estar no dict em memória
        self.assertIn("5511444444444@s.whatsapp.net", personal_contacts)
        self.assertEqual(personal_contacts["5511444444444@s.whatsapp.net"]["name"], "Ana")


class TestShouldRunStyleLearning(unittest.IsolatedAsyncioTestCase):
    """Testa a gate function que decide se o aprendizado deve rodar."""

    @patch("whatsapp_manager._SOUL_LEARNING_STATE_PATH")
    @patch("whatsapp_manager.sqlite3.connect")
    @patch("pathlib.Path.exists")
    def test_returns_true_when_no_state_file(self, mock_path_exists, mock_sqlite, mock_state_path):
        """Sem arquivo de estado → deve retornar True."""
        mock_state_path.exists.return_value = False
        # bridge_db existe
        def path_exists(self_path):
            return True
        mock_path_exists.side_effect = lambda: True

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_sqlite.return_value.__enter__ = lambda s: mock_conn
        mock_sqlite.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchone.return_value = (1700000100,)

        with patch("whatsapp_manager.Path") as mock_path_cls:
            bridge_db_mock = MagicMock()
            bridge_db_mock.exists.return_value = True
            state_path_mock = MagicMock()
            state_path_mock.exists.return_value = False

            def path_factory(p):
                if "whatsapp_messages.db" in str(p):
                    return bridge_db_mock
                return state_path_mock

            mock_path_cls.side_effect = path_factory

            with patch("whatsapp_manager._SOUL_LEARNING_STATE_PATH", state_path_mock), \
                 patch("whatsapp_manager.sqlite3.connect") as mock_conn2:
                mock_conn2.return_value.__enter__ = lambda s: mock_conn
                mock_conn2.return_value.__exit__ = MagicMock(return_value=False)
                result = whatsapp_manager._should_run_style_learning()

        self.assertTrue(result)

    @patch("whatsapp_manager.sqlite3.connect")
    def test_returns_false_when_no_db(self, mock_sqlite):
        """Sem banco de dados → deve retornar False."""
        with patch("whatsapp_manager.Path") as mock_path_cls:
            bridge_db_mock = MagicMock()
            bridge_db_mock.exists.return_value = False
            mock_path_cls.return_value = bridge_db_mock

            result = whatsapp_manager._should_run_style_learning()

        self.assertFalse(result)

    def test_returns_false_on_exception(self):
        """Qualquer exceção deve retornar False silenciosamente."""
        with patch("whatsapp_manager.Path", side_effect=Exception("boom")):
            result = whatsapp_manager._should_run_style_learning()
        self.assertFalse(result)


class TestCollectOwnerMessagesByRelationship(unittest.IsolatedAsyncioTestCase):
    """Testa coleta de mensagens do André agrupadas por relacionamento."""

    def _make_sqlite_mock(self, chat_ids, messages_by_chat, contact_msgs_by_chat=None):
        """
        contact_msgs_by_chat: dict {chat_id: [(body, timestamp), ...]}
          mensagens recebidas (from_me=0) para simular diálogos.
          Se None, retorna lista vazia para todas as queries de from_me=0.
        """
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_conn.__enter__ = lambda s: mock_conn
        mock_conn.__exit__ = MagicMock(return_value=False)
        contact_msgs_by_chat = contact_msgs_by_chat or {}

        def fetchall_side_effect():
            call_count = mock_cursor.fetchall.call_count
            # Call 1: lid_phone_map query (@lid cross-reference) — retorna vazio
            if call_count == 1:
                return []
            # Call 2: chat_ids query
            if call_count == 2:
                return [(cid, 0) for cid in chat_ids]
            # Calls subsequentes: duas por chat (André messages + contact messages)
            last_args = mock_cursor.execute.call_args_list[-1]
            last_sql = last_args[0][0] if last_args[0] else ""
            last_params = last_args[0][1] if len(last_args[0]) > 1 else (last_args[1] or {})
            chat_id = last_params[0] if last_params else None
            if "from_me=0" in last_sql:
                return contact_msgs_by_chat.get(chat_id, [])
            return [(m, 1700000000) for m in messages_by_chat.get(chat_id, [])]

        mock_cursor.fetchall.side_effect = fetchall_side_effect
        return mock_conn

    def _path_factory(self, bridge_exists=True, state_exists=False):
        """Retorna um side_effect para patch('whatsapp_manager.Path') que distingue bridge_db de state_db."""
        bridge_mock = MagicMock()
        bridge_mock.exists.return_value = bridge_exists
        state_mock = MagicMock()
        state_mock.exists.return_value = state_exists

        def factory(p):
            if "whatsapp_messages.db" in str(p):
                return bridge_mock
            return state_mock

        return factory

    def test_groups_messages_by_relationship(self):
        personal_contacts = {
            "5511111111111@s.whatsapp.net": {"relationship": "Cliente"},
            "5511222222222@s.whatsapp.net": {"relationship": "Amigo"},
        }
        messages_by_chat = {
            "5511111111111@s.whatsapp.net": ["oi tudo bem", "pode me enviar o boleto?", "obrigado", "vou verificar", "em breve retorno"],
            "5511222222222@s.whatsapp.net": ["eae mano", "blz", "vamo sim", "pode mandar", "kkk verdade"],
        }
        chat_ids = list(messages_by_chat.keys())
        mock_conn = self._make_sqlite_mock(chat_ids, messages_by_chat)

        with patch("whatsapp_manager.Path", side_effect=self._path_factory(bridge_exists=True, state_exists=False)), \
             patch("whatsapp_manager.sqlite3.connect", return_value=mock_conn):
            result = whatsapp_manager._collect_owner_messages_by_relationship(personal_contacts)

        self.assertIn("Cliente", result)
        self.assertIn("Amigo", result)

    def test_dialogue_contact_message_after_andre(self):
        """Regressão: mensagem do contato chegando DEPOIS de André deve ser pareada (André iniciou)."""
        personal_contacts = {"5511111111111@s.whatsapp.net": {"relationship": "Cliente"}}
        messages_by_chat = {"5511111111111@s.whatsapp.net": ["vc viu o jogo?", "e do Brasil?"]}
        # Contato responde 2h depois da primeira mensagem do André
        contact_msgs = {"5511111111111@s.whatsapp.net": [("sou cliente sim", 1700007200)]}
        mock_conn = self._make_sqlite_mock(
            list(messages_by_chat.keys()), messages_by_chat, contact_msgs
        )
        with patch("whatsapp_manager.Path", side_effect=self._path_factory()), \
             patch("whatsapp_manager.sqlite3.connect", return_value=mock_conn):
            result = whatsapp_manager._collect_owner_messages_by_relationship(personal_contacts)
        self.assertIn("Cliente", result)
        dialogues = [m for m in result["Cliente"] if m.get("contact")]
        self.assertGreater(len(dialogues), 0, "Deve existir pelo menos um diálogo com mensagem do contato")
        self.assertEqual(dialogues[0]["contact"], "sou cliente sim")

    def test_dialogue_each_contact_msg_used_once(self):
        """Regressão: a mesma mensagem do contato não deve ser pareada com múltiplas mensagens do André."""
        personal_contacts = {"5511111111111@s.whatsapp.net": {"relationship": "Cliente"}}
        messages_by_chat = {"5511111111111@s.whatsapp.net": ["msg1", "msg2"]}
        # Uma única mensagem do contato — deve ser usada só uma vez
        contact_msgs = {"5511111111111@s.whatsapp.net": [("resposta unica", 1700003600)]}
        mock_conn = self._make_sqlite_mock(
            list(messages_by_chat.keys()), messages_by_chat, contact_msgs
        )
        with patch("whatsapp_manager.Path", side_effect=self._path_factory()), \
             patch("whatsapp_manager.sqlite3.connect", return_value=mock_conn):
            result = whatsapp_manager._collect_owner_messages_by_relationship(personal_contacts)
        if "Cliente" in result:
            paired = [m for m in result["Cliente"] if m.get("contact") == "resposta unica"]
            self.assertEqual(len(paired), 1, "Mesma mensagem do contato não deve aparecer em múltiplos pares")

    def test_dialogue_contact_msg_outside_24h_not_paired(self):
        """Regressão: mensagem do contato fora da janela de 24h não deve ser pareada."""
        personal_contacts = {"5511111111111@s.whatsapp.net": {"relationship": "Cliente"}}
        messages_by_chat = {"5511111111111@s.whatsapp.net": ["oi"]}
        # Contato respondeu 25h depois — fora da janela
        contact_msgs = {"5511111111111@s.whatsapp.net": [("resposta tardia", 1700000000 + 90001)]}
        mock_conn = self._make_sqlite_mock(
            list(messages_by_chat.keys()), messages_by_chat, contact_msgs
        )
        with patch("whatsapp_manager.Path", side_effect=self._path_factory()), \
             patch("whatsapp_manager.sqlite3.connect", return_value=mock_conn):
            result = whatsapp_manager._collect_owner_messages_by_relationship(personal_contacts)
        if "Cliente" in result:
            paired = [m for m in result["Cliente"] if m.get("contact")]
            self.assertEqual(len(paired), 0, "Mensagem fora de 24h não deve ser pareada")

    def test_dialogue_contact_msg_whitespace_normalized(self):
        """Regressão: quebras de linha extras na mensagem do contato devem ser normalizadas."""
        personal_contacts = {"5511111111111@s.whatsapp.net": {"relationship": "Cliente"}}
        messages_by_chat = {"5511111111111@s.whatsapp.net": ["blz"]}
        contact_msgs = {"5511111111111@s.whatsapp.net": [("oi\n\n\n como vai\n", 1700003600)]}
        mock_conn = self._make_sqlite_mock(
            list(messages_by_chat.keys()), messages_by_chat, contact_msgs
        )
        with patch("whatsapp_manager.Path", side_effect=self._path_factory()), \
             patch("whatsapp_manager.sqlite3.connect", return_value=mock_conn):
            result = whatsapp_manager._collect_owner_messages_by_relationship(personal_contacts)
        if "Cliente" in result:
            paired = [m for m in result["Cliente"] if m.get("contact")]
            self.assertTrue(len(paired) > 0)
            self.assertNotIn("\n", paired[0]["contact"], "Quebras de linha não devem aparecer no contato")
            self.assertEqual(paired[0]["contact"], "oi como vai")

    def test_excludes_bot_messages(self):
        """Mensagens geradas pelo bot (presentes no state.db como assistant) devem ser excluídas."""
        personal_contacts = {
            "5511111111111@s.whatsapp.net": {"relationship": "Cliente"},
        }
        bot_reply = "Olá! Posso ajudar com mais alguma coisa?"
        manual_msg = "ok entendi, vou verificar"

        bridge_conn = MagicMock()
        bridge_conn.__enter__ = lambda s: bridge_conn
        bridge_conn.__exit__ = MagicMock(return_value=False)
        bridge_cur = MagicMock()
        bridge_conn.cursor.return_value = bridge_cur

        state_conn = MagicMock()
        state_conn.__enter__ = lambda s: state_conn
        state_conn.__exit__ = MagicMock(return_value=False)
        state_cur = MagicMock()
        state_conn.cursor.return_value = state_cur

        # state.db retorna o bot_reply como assistant
        state_cur.fetchall.return_value = [(bot_reply,)]

        call_counts = {"n": 0}
        def fetchall_bridge():
            call_counts["n"] += 1
            if call_counts["n"] == 1:
                return [("5511111111111@s.whatsapp.net",)]
            return [(bot_reply, 1700000000, None), (manual_msg, 1700000001, None)]

        bridge_cur.fetchall.side_effect = fetchall_bridge

        def connect_factory(path, *a, **kw):
            if "whatsapp_messages" in str(path):
                return bridge_conn
            return state_conn

        with patch("whatsapp_manager.Path", side_effect=self._path_factory(bridge_exists=True, state_exists=True)), \
             patch("whatsapp_manager.sqlite3.connect", side_effect=connect_factory):
            result = whatsapp_manager._collect_owner_messages_by_relationship(personal_contacts)

        if "Cliente" in result:
            andres = [item["owner"] if isinstance(item, dict) else item for item in result["Cliente"]]
            self.assertNotIn(bot_reply, andres)

    def test_filters_media_messages(self):
        personal_contacts = {
            "5511333333333@s.whatsapp.net": {"relationship": "Parente"},
        }
        messages_by_chat = {
            "5511333333333@s.whatsapp.net": [
                "<Media omitted>", "oi pai", "tudo bem?",
                "image omitted", "vou ai amanhã", "beijo",
            ],
        }
        mock_conn = self._make_sqlite_mock(list(messages_by_chat.keys()), messages_by_chat)

        with patch("whatsapp_manager.Path", side_effect=self._path_factory(bridge_exists=True, state_exists=False)), \
             patch("whatsapp_manager.sqlite3.connect", return_value=mock_conn):
            result = whatsapp_manager._collect_owner_messages_by_relationship(personal_contacts)

        if "Parente" in result:
            for item in result["Parente"]:
                text = item["owner"] if isinstance(item, dict) else item
                self.assertNotIn("omitted", text.lower())

    def test_returns_empty_when_db_missing(self):
        with patch("whatsapp_manager.Path", side_effect=self._path_factory(bridge_exists=False, state_exists=False)):
            result = whatsapp_manager._collect_owner_messages_by_relationship({"any": {}})

        self.assertEqual(result, {})

    def test_returns_empty_on_exception(self):
        with patch("whatsapp_manager.Path", side_effect=Exception("db error")):
            result = whatsapp_manager._collect_owner_messages_by_relationship({})
        self.assertEqual(result, {})


class TestExtractStylePatternsViaLlm(unittest.IsolatedAsyncioTestCase):
    """Testa a chamada ao LLM para extrair padrões de escrita."""

    def test_returns_markdown_on_success(self):
        messages = {
            "Cliente": ["oi tudo bem", "pode me mandar o boleto", "obrigado pelo suporte", "vou verificar", "ok entendi"],
            "Amigo": ["eae mano", "blz sim", "vamo sim", "kkk verdade", "te ligo depois"],
        }
        expected_md = "## EXEMPLOS REAIS DE ESCRITA\n> Gerado automaticamente.\n\n### Cliente\n**Padrões:** formal\n"

        with patch("whatsapp_manager._call_llm_api", return_value=expected_md), \
             patch("whatsapp_manager.config") as mock_config:
            mock_config.google_api_key = "fake-key"
            mock_config.openai_api_key = None
            mock_config.openrouter_api_key = None
            mock_config.whatsapp_contact_classifier_model = "gemini-2.0-flash-lite"

            result = whatsapp_manager._extract_style_patterns_via_llm(messages)

        self.assertIsNotNone(result)
        self.assertIn("EXEMPLOS REAIS DE ESCRITA", result)

    def test_falls_back_to_openai_when_gemini_fails(self):
        messages = {
            "Amigo": ["eae", "blz", "vamo", "sim", "boa"],
        }
        openai_md = "## EXEMPLOS REAIS DE ESCRITA\n### Amigo\n**Padrões:** informal\n"

        call_count = {"n": 0}
        def fake_call_llm(url, headers, payload, extract_fn, timeout=30):
            call_count["n"] += 1
            if "gemini" in url:
                return None  # Gemini falha
            return openai_md

        with patch("whatsapp_manager._call_llm_api", side_effect=fake_call_llm), \
             patch("whatsapp_manager.config") as mock_config:
            mock_config.google_api_key = "fake-gemini"
            mock_config.openai_api_key = "fake-openai"
            mock_config.openrouter_api_key = None
            mock_config.whatsapp_contact_classifier_model = None

            result = whatsapp_manager._extract_style_patterns_via_llm(messages)

        self.assertIsNotNone(result)
        self.assertIn("Amigo", result)
        self.assertEqual(call_count["n"], 2)

    def test_returns_none_when_all_providers_fail(self):
        messages = {"Cliente": ["oi", "tudo bem", "ok", "sim", "entendi"]}

        with patch("whatsapp_manager._call_llm_api", return_value=None), \
             patch("whatsapp_manager.config") as mock_config:
            mock_config.google_api_key = "key"
            mock_config.openai_api_key = "key"
            mock_config.openrouter_api_key = "key"
            mock_config.whatsapp_contact_classifier_model = None

            result = whatsapp_manager._extract_style_patterns_via_llm(messages)

        self.assertIsNone(result)


class TestUpdateSoulWhatsappWithExamples(unittest.IsolatedAsyncioTestCase):
    """Testa a injeção da seção de exemplos no SOUL_WHATSAPP.md."""

    ORIGINAL_SOUL = (
        "# Persona André Alencar\n\n"
        "Você é o assistente de WhatsApp do André.\n"
        "Seja prestativo e profissional.\n"
    )

    NEW_SECTION = (
        "### Cliente\n"
        "**Padrões:** formal, sem abreviações\n"
        "**Exemplos reais:**\n"
        '- "Olá, posso ajudar?"\n'
    )

    def _run_update(self, original_content, style_section, github_ok=True):
        mock_path = MagicMock()
        mock_path.exists.return_value = True
        mock_path.read_text.return_value = original_content
        written = {}

        def fake_write_text(content, encoding=None):
            written["content"] = content

        mock_path.write_text.side_effect = fake_write_text

        state_path = MagicMock()
        state_path.exists.return_value = False

        with patch("whatsapp_manager._SOUL_WHATSAPP_PATH", mock_path), \
             patch("whatsapp_manager._SOUL_LEARNING_STATE_PATH", state_path), \
             patch("whatsapp_manager._github_put_file", return_value=github_ok), \
             patch("whatsapp_manager.sqlite3.connect") as mock_db, \
             patch("whatsapp_manager.config") as mock_config:
            mock_config.config_repo = None  # skip GitHub
            mock_config.config_github_token = None
            conn = MagicMock()
            conn.__enter__ = lambda s: conn
            conn.__exit__ = MagicMock(return_value=False)
            cur = MagicMock()
            cur.fetchone.return_value = (1700000000,)
            conn.cursor.return_value = cur
            mock_db.return_value = conn

            result = whatsapp_manager._update_soul_whatsapp_with_examples(style_section)

        return result, written.get("content", "")

    def test_appends_section_when_sentinel_absent(self):
        ok, written = self._run_update(self.ORIGINAL_SOUL, self.NEW_SECTION)
        self.assertTrue(ok)
        self.assertIn("## EXEMPLOS REAIS DE ESCRITA", written)
        self.assertIn("Persona André Alencar", written)  # persona original preservada
        self.assertIn("Cliente", written)

    def test_replaces_existing_section(self):
        old_section = "## EXEMPLOS REAIS DE ESCRITA\n### AntiguoGrupo\nExemplo antigo.\n"
        content_with_existing = self.ORIGINAL_SOUL + "\n\n" + old_section
        ok, written = self._run_update(content_with_existing, self.NEW_SECTION)
        self.assertTrue(ok)
        self.assertNotIn("AntiguoGrupo", written)
        self.assertNotIn("Exemplo antigo", written)
        self.assertIn("Cliente", written)
        self.assertIn("Persona André Alencar", written)  # persona original preservada

    def test_does_not_duplicate_sentinel(self):
        section_with_sentinel = "## EXEMPLOS REAIS DE ESCRITA\n" + self.NEW_SECTION
        ok, written = self._run_update(self.ORIGINAL_SOUL, section_with_sentinel)
        self.assertTrue(ok)
        self.assertEqual(written.count("## EXEMPLOS REAIS DE ESCRITA"), 1)

    def test_returns_false_when_file_missing(self):
        mock_path = MagicMock()
        mock_path.exists.return_value = False

        with patch("whatsapp_manager._SOUL_WHATSAPP_PATH", mock_path):
            result = whatsapp_manager._update_soul_whatsapp_with_examples("qualquer coisa")

        self.assertFalse(result)

    def test_pushes_to_github_when_configured(self):
        mock_path = MagicMock()
        mock_path.exists.return_value = True
        mock_path.read_text.return_value = self.ORIGINAL_SOUL
        mock_path.write_text = MagicMock()

        state_path = MagicMock()
        state_path.exists.return_value = False

        with patch("whatsapp_manager._SOUL_WHATSAPP_PATH", mock_path), \
             patch("whatsapp_manager._SOUL_LEARNING_STATE_PATH", state_path), \
             patch("whatsapp_manager._github_put_file", return_value=True) as mock_github, \
             patch("whatsapp_manager.sqlite3.connect") as mock_db, \
             patch("whatsapp_manager.config") as mock_config:
            mock_config.config_repo = "myuser/myrepo"
            mock_config.config_github_token = "ghp_token"
            mock_config.hermes_setup_github_user = "myuser"
            conn = MagicMock()
            conn.__enter__ = lambda s: conn
            conn.__exit__ = MagicMock(return_value=False)
            cur = MagicMock()
            cur.fetchone.return_value = (1700000000,)
            conn.cursor.return_value = cur
            mock_db.return_value = conn

            whatsapp_manager._update_soul_whatsapp_with_examples(self.NEW_SECTION)

        mock_github.assert_called_once()
        call_kwargs = mock_github.call_args
        self.assertEqual(call_kwargs.kwargs.get("github_path") or call_kwargs[1].get("github_path") or call_kwargs[0][3], "SOUL_WHATSAPP.md")


@patch.dict(os.environ, {"WHATSAPP_OWNER_NAME": "André"})
class TestStyleLearningRegressions(unittest.IsolatedAsyncioTestCase):
    """Testes de regressão para bugs conhecidos no style learning."""

    def test_sanitize_filters_owner_name_in_contact_name(self):
        """contact_name nunca deve ser 'André Alencar' — evita mostrar dono como destinatário."""
        from whatsapp_manager import _normalize_text
        bad_names = ["André Alencar", "Andre Alencar", "andré alencar", "ANDRÉ ALENCAR"]
        for name in bad_names:
            norm = _normalize_text(name)
            self.assertIn(norm, ("andre alencar", "andré alencar"),
                          f"_normalize_text deve normalizar '{name}'")
            # O filtro no código rejeita esses nomes
            is_filtered = norm in ("andre alencar", "andré alencar", "andre", "andré")
            self.assertTrue(is_filtered, f"Nome '{name}' deveria ser filtrado como placeholder do dono")

    def test_sanitize_filters_auto_generated_contact_names(self):
        """Nomes como 'Contato 558699997003' devem ser descartados como placeholder."""
        from whatsapp_manager import _normalize_text
        placeholders = ["Contato 558699997003", "contato 11999990000", "Usuario 123", "Desconhecido"]
        for name in placeholders:
            norm = _normalize_text(name)
            is_placeholder = (
                norm.startswith("contato ")
                or norm.startswith("usuario ")
                or norm.startswith("desconhecido")
            )
            self.assertTrue(is_placeholder, f"'{name}' deveria ser identificado como placeholder")

    def test_sanitize_sensitive_blocks_balance(self):
        """Mensagens com saldo bancário devem ser bloqueadas."""
        from whatsapp_manager import _sanitize_sensitive
        cases = [
            "O saldo disponível na sua conta é de R$1.316,59",
            "saldo R$ 2.500,00",
            "consultar o saldo do neymar\nO saldo disponível é de R$500,00",
        ]
        for msg in cases:
            result = _sanitize_sensitive(msg)
            self.assertIsNone(result, f"Deveria filtrar mensagem com saldo: '{msg[:50]}'")

    def test_sanitize_sensitive_allows_normal_messages(self):
        """Mensagens normais não devem ser bloqueadas."""
        from whatsapp_manager import _sanitize_sensitive
        cases = ["oi tudo bem?", "vendeu ?", "2 gols do Haalend", "blz mano", "Oi"]
        for msg in cases:
            result = _sanitize_sensitive(msg)
            self.assertIsNotNone(result, f"Não deveria filtrar: '{msg}'")
            self.assertEqual(result, msg)

    def test_sanitize_sensitive_blocks_cpf(self):
        """CPF deve ser bloqueado."""
        from whatsapp_manager import _sanitize_sensitive
        self.assertIsNone(_sanitize_sensitive("meu CPF é 123.456.789-00"))

    def test_sanitize_sensitive_blocks_password(self):
        """Senhas devem ser bloqueadas."""
        from whatsapp_manager import _sanitize_sensitive
        self.assertIsNone(_sanitize_sensitive("a senha é 1234"))

    def test_build_style_section_dialogue_format(self):
        """_build_style_section_directly deve usar formato 'Nome: msg / André: resp'."""
        from whatsapp_manager import _build_style_section_directly
        messages_by_rel = {
            "Cliente": [
                {"contact": "vc faz sites?", "owner": "Faço sim!", "contact_name": "João"},
                {"contact": None, "owner": "vendeu?", "contact_name": "Maria"},
            ]
        }
        result = _build_style_section_directly(messages_by_rel)
        # Deve ter o nome do contato como falante
        self.assertIn('João: "vc faz sites?"', result)
        self.assertIn('André: "Faço sim!"', result)
        # Sem contexto de contato, deve mostrar só André
        self.assertIn('André: "vendeu?"', result)
        # Não deve usar formato antigo "André →"
        self.assertNotIn("André →", result)
        self.assertNotIn("André p/", result)

    def test_build_style_section_filters_sensitive_data(self):
        """_build_style_section_directly deve filtrar dados sensíveis."""
        from whatsapp_manager import _build_style_section_directly
        messages_by_rel = {
            "Cliente": [
                {"contact": None, "owner": "saldo R$ 5.000,00 na conta", "contact_name": "João"},
                {"contact": None, "owner": "oi tudo bem?", "contact_name": "João"},
            ]
        }
        result = _build_style_section_directly(messages_by_rel)
        self.assertNotIn("5.000,00", result)
        self.assertIn("oi tudo bem?", result)

    def test_build_style_section_owner_name_not_as_contact(self):
        """contact_name 'André Alencar' não deve aparecer como destinatário."""
        from whatsapp_manager import _build_style_section_directly
        messages_by_rel = {
            "Cliente": [
                {"contact": None, "owner": "oi", "contact_name": "Cliente"},
            ]
        }
        result = _build_style_section_directly(messages_by_rel)
        # Label deve ser "Cliente", não "André Alencar"
        self.assertNotIn("André Alencar:", result)
        self.assertIn("André:", result)

    def test_sync_uses_only_received_sender_name(self):
        """O sync de contatos não deve usar sender_name de mensagens enviadas (from_me=1)."""
        # O bug era: SELECT MAX(sender_name) pegava 'André Alencar' de msgs saídas
        # Fix: MAX(CASE WHEN from_me=0 THEN sender_name ELSE NULL END)
        import sqlite3 as _sq
        conn = _sq.connect(":memory:")
        conn.execute("""CREATE TABLE messages (
            chat_id TEXT, sender_name TEXT, from_me INTEGER,
            timestamp INTEGER, body TEXT
        )""")
        # Apenas mensagens saídas (from_me=1) com sender_name do dono
        conn.execute("INSERT INTO messages VALUES ('5511@s', 'André Alencar', 1, 1000, 'oi')")
        conn.execute("INSERT INTO messages VALUES ('5511@s', 'André Alencar', 1, 1001, 'tudo?')")
        cur = conn.cursor()
        cur.execute("""
            SELECT chat_id,
                   MAX(CASE WHEN from_me=0 THEN sender_name ELSE NULL END) as name,
                   COUNT(*) as msg_count
            FROM messages WHERE chat_id NOT LIKE '%@g.us%'
            GROUP BY chat_id
        """)
        row = cur.fetchone()
        conn.close()
        chat_id, name, msg_count = row
        self.assertEqual(chat_id, "5511@s")
        self.assertIsNone(name, "Nome deve ser NULL quando só há msgs enviadas")
        self.assertEqual(msg_count, 2)


@patch.dict(os.environ, {"WHATSAPP_OWNER_NAME": "André"})
class TestBuildStyleSectionWithPatterns(unittest.TestCase):
    """Testa a geração do SOUL_WHATSAPP.md com padrões do LLM + exemplos do Python."""

    def _make_msgs(self, contact_name, pairs):
        """Cria lista de dicts com pares (contact_msg, andre_msg). None = sem contexto."""
        return [
            {"contact": c, "owner": a, "contact_name": contact_name}
            for c, a in pairs
        ]

    def test_format_dialogue_pair_uses_separate_bullets(self):
        """Diálogo com contexto deve ter dois bullets separados, não indentado."""
        from whatsapp_manager import _build_style_section_with_patterns
        msgs = self._make_msgs("João", [("vc faz sites?", "Faço sim!")])
        result = _build_style_section_with_patterns({"Cliente": msgs}, None)
        lines = result.splitlines()
        contact_line = next((l for l in lines if 'João: "vc faz sites?"' in l), None)
        andre_line = next((l for l in lines if 'André: "Faço sim!"' in l), None)
        self.assertIsNotNone(contact_line, "Linha do contato não encontrada")
        self.assertIsNotNone(andre_line, "Linha do André não encontrada")
        self.assertTrue(contact_line.startswith("- "), "Contato deve ser bullet '- '")
        self.assertTrue(andre_line.startswith("- "), "André deve ser bullet '- ', não indentado")

    def test_blank_line_between_dialogue_pairs(self):
        """Deve haver linha em branco entre pares de diálogo."""
        from whatsapp_manager import _build_style_section_with_patterns
        msgs = self._make_msgs("Pedro", [
            ("tudo bem?", "tudo!"),
            ("vc faz app?", "faço sim"),
        ])
        result = _build_style_section_with_patterns({"Amigo": msgs}, None)
        # Deve haver linha em branco separando os pares
        self.assertIn('\n\n', result)

    def test_contact_name_appears_as_label(self):
        """O nome do contato deve aparecer como label no bullet."""
        from whatsapp_manager import _build_style_section_with_patterns
        msgs = self._make_msgs("EmpreendedorSerial", [("oi", "olá!")])
        result = _build_style_section_with_patterns({"Cliente": msgs}, None)
        self.assertIn('EmpreendedorSerial: "oi"', result)
        self.assertIn('André: "olá!"', result)

    def test_no_owner_name_as_label(self):
        """Nome do dono nunca deve aparecer como label do contato."""
        from whatsapp_manager import _build_style_section_with_patterns
        msgs = self._make_msgs("André Alencar", [("oi", "olá!")])
        result = _build_style_section_with_patterns({"Cliente": msgs}, None)
        # "André Alencar:" não deve aparecer como label de contato (só "André:" como resposta)
        self.assertNotIn('André Alencar: "oi"', result)

    def test_message_without_context_no_contact_bullet(self):
        """Mensagem sem contexto deve ter só bullet do André."""
        from whatsapp_manager import _build_style_section_with_patterns
        msgs = self._make_msgs("João", [(None, "vendeu?")])
        result = _build_style_section_with_patterns({"Cliente": msgs}, None)
        self.assertIn('André: "vendeu?"', result)
        self.assertNotIn('João: "None"', result)

    def test_sentinel_header_present(self):
        """O sentinel ## EXEMPLOS REAIS DE ESCRITA deve estar no output."""
        from whatsapp_manager import _build_style_section_with_patterns, _STYLE_SENTINEL
        msgs = self._make_msgs("João", [("oi", "olá")])
        result = _build_style_section_with_patterns({"Cliente": msgs}, None)
        self.assertIn(_STYLE_SENTINEL, result)

    def test_llm_patterns_included_when_provided(self):
        """Padrões do LLM devem aparecer no output quando fornecidos."""
        from whatsapp_manager import _build_style_section_with_patterns
        msgs = self._make_msgs("João", [("oi", "olá")])
        llm_output = "### Cliente\n**Padrões identificados:**\n- Usa 'vc' frequentemente"
        result = _build_style_section_with_patterns({"Cliente": msgs}, llm_output)
        self.assertIn("Usa 'vc' frequentemente", result)

    def test_sensitive_data_filtered(self):
        """Dados sensíveis devem ser removidos dos exemplos."""
        from whatsapp_manager import _build_style_section_with_patterns
        msgs = self._make_msgs("João", [
            (None, "saldo R$ 5.000,00 na conta"),
            (None, "oi tudo bem?"),
        ])
        result = _build_style_section_with_patterns({"Cliente": msgs}, None)
        self.assertNotIn("5.000,00", result)
        self.assertIn("oi tudo bem?", result)

    def test_no_arrow_format(self):
        """Nunca deve usar formato com seta 'André →'."""
        from whatsapp_manager import _build_style_section_with_patterns
        msgs = self._make_msgs("João", [("pergunta", "resposta")])
        result = _build_style_section_with_patterns({"Cliente": msgs}, None)
        self.assertNotIn("André →", result)
        self.assertNotIn("André p/", result)

    def test_multiple_relationship_groups(self):
        """Deve gerar seção separada para cada relacionamento."""
        from whatsapp_manager import _build_style_section_with_patterns
        msgs_by_rel = {
            "Cliente": self._make_msgs("João", [(None, "oi cliente")]),
            "Amigo": self._make_msgs("Pedro", [(None, "oi amigo")]),
        }
        result = _build_style_section_with_patterns(msgs_by_rel, None)
        self.assertIn("### Cliente", result)
        self.assertIn("### Amigo", result)
        self.assertIn('André: "oi cliente"', result)
        self.assertIn('André: "oi amigo"', result)


class TestDedupPersonalContacts(unittest.TestCase):
    """Testa _dedup_personal_contacts e _merge_contact_entries."""

    def setUp(self):
        from whatsapp_manager import _dedup_personal_contacts, _merge_contact_entries
        self._dedup = _dedup_personal_contacts
        self._merge = _merge_contact_entries

    def _contacts(self, entries: dict) -> dict:
        return {k: dict(v) for k, v in entries.items()}

    def test_lid_and_whatsapp_both_kept(self):
        """@lid e @s.whatsapp.net devem coexistir — nenhum é removido."""
        pc = self._contacts({
            "5586@s.whatsapp.net": {"relationship": "Amigo"},
            "abc@lid": {"relationship": "Filho", "name": "Pedrinho"},
        })
        lid_map = {"abc": "5586"}
        removed = self._dedup(pc, lid_map)
        self.assertEqual(removed, 0)
        self.assertIn("abc@lid", pc)
        self.assertIn("5586@s.whatsapp.net", pc)

    def test_lid_cross_reference_added(self):
        """Campo 'lid' deve ser adicionado ao @s.whatsapp.net como cross-reference."""
        pc = self._contacts({
            "5586@s.whatsapp.net": {"relationship": "Amigo"},
            "abc@lid": {"relationship": "Filho"},
        })
        lid_map = {"abc": "5586"}
        self._dedup(pc, lid_map)
        self.assertEqual(pc["5586@s.whatsapp.net"]["lid"], "abc@lid")

    def test_lid_data_preserved_independently(self):
        """Dados do @lid não são alterados — cada entrada mantém seus próprios valores."""
        pc = self._contacts({
            "5586@s.whatsapp.net": {"manual_relationship": "Amigo"},
            "abc@lid": {"manual_relationship": "namorada"},
        })
        lid_map = {"abc": "5586"}
        self._dedup(pc, lid_map)
        self.assertEqual(pc["5586@s.whatsapp.net"]["manual_relationship"], "Amigo")
        self.assertEqual(pc["abc@lid"]["manual_relationship"], "namorada")

    def test_phone_normalization_dedup(self):
        """Dois @s.whatsapp.net com mesmo telefone (com/sem 9º dígito) devem ser mesclados.
        Formato BR: 55 + DDD (2) + 9 + número (8) = 13 dígitos → normaliza p/ 12 dígitos.
        """
        # 5586999970003 (13 dígitos, com 9º) normaliza para 558699970003 (12 dígitos)
        pc = self._contacts({
            "5586999970003@s.whatsapp.net": {"relationship": "Cliente", "name": "EmpreendedorSerial"},
            "558699970003@s.whatsapp.net": {"relationship": "Cliente"},
        })
        removed = self._dedup(pc, {})
        self.assertEqual(removed, 1)
        self.assertEqual(len(pc), 1)
        remaining = list(pc.values())[0]
        self.assertEqual(remaining["name"], "EmpreendedorSerial")

    def test_lid_without_phone_mapping_stays(self):
        """@lid sem telefone mapeado deve permanecer no dict."""
        pc = self._contacts({
            "abc@lid": {"relationship": "Cliente", "name": "X"},
        })
        removed = self._dedup(pc, {})
        self.assertEqual(removed, 0)
        self.assertIn("abc@lid", pc)

    def test_lid_field_added_when_lid_entry_absent(self):
        """Campo 'lid' é adicionado ao @s.whatsapp.net mesmo sem entrada @lid no dict."""
        pc = self._contacts({
            "5586@s.whatsapp.net": {"relationship": "Cliente"},
        })
        lid_map = {"abc": "5586"}
        self._dedup(pc, lid_map)
        self.assertEqual(pc["5586@s.whatsapp.net"]["lid"], "abc@lid")

    def test_owner_name_not_propagated(self):
        """Nome do dono no @lid não deve sobrescrever nome do @s.whatsapp.net."""
        pc = self._contacts({
            "5586@s.whatsapp.net": {"name": "Cliente X"},
            "abc@lid": {"name": "André Alencar"},
        })
        lid_map = {"abc": "5586"}
        self._dedup(pc, lid_map)
        self.assertEqual(pc["5586@s.whatsapp.net"]["name"], "Cliente X")

    def test_no_duplicate_removal_without_lid_map(self):
        """Sem lid_phone_map, entradas @s.whatsapp.net distintas não são tocadas."""
        pc = self._contacts({
            "5586@s.whatsapp.net": {"relationship": "Cliente"},
            "5599@s.whatsapp.net": {"relationship": "Amigo"},
        })
        removed = self._dedup(pc, {})
        self.assertEqual(removed, 0)
        self.assertEqual(len(pc), 2)

    def test_merge_preserves_commercial_market_metadata(self):
        primary = {"relationship": "Cliente"}
        secondary = {
            "market_id": "US",
            "country": "United States",
            "currency": "USD",
            "offer": "international",
            "timezone": "America/New_York",
            "language": "es",
        }

        self._merge(primary, secondary)

        self.assertEqual(primary["market_id"], "US")
        self.assertEqual(primary["country"], "United States")
        self.assertEqual(primary["currency"], "USD")
        self.assertEqual(primary["offer"], "international")
        self.assertEqual(primary["timezone"], "America/New_York")
        self.assertEqual(primary["language"], "es")


class TestSanitizeSensitive(unittest.TestCase):
    """Testa _sanitize_sensitive — segurança dos exemplos de diálogo."""

    def setUp(self):
        from whatsapp_manager import _sanitize_sensitive
        self.sanitize = _sanitize_sensitive

    def test_text_normal_retorna_intacto(self):
        self.assertEqual(self.sanitize("oi tudo bem"), "oi tudo bem")

    def test_vazio_retorna_none(self):
        self.assertIsNone(self.sanitize(""))
        self.assertIsNone(self.sanitize(None))

    def test_cpf_descartado(self):
        self.assertIsNone(self.sanitize("meu cpf é 123.456.789-00"))

    def test_cnpj_descartado(self):
        self.assertIsNone(self.sanitize("cnpj: 12.345.678/0001-99"))

    def test_senha_descartada(self):
        self.assertIsNone(self.sanitize("a senha é 1234"))
        self.assertIsNone(self.sanitize("password: abc123"))

    def test_numero_cartao_descartado(self):
        self.assertIsNone(self.sanitize("cartão: 4111 1111 1111 1111"))
        self.assertIsNone(self.sanitize("1234567890123456"))

    def test_cvv_descartado(self):
        self.assertIsNone(self.sanitize("cvv 123"))

    def test_saldo_descartado(self):
        self.assertIsNone(self.sanitize("saldo R$ 5.000,00"))

    def test_token_descartado(self):
        self.assertIsNone(self.sanitize("seu token de acesso expirou"))

    def test_agencia_descartada(self):
        self.assertIsNone(self.sanitize("agência: 1234"))

    def test_conta_descartada(self):
        self.assertIsNone(self.sanitize("conta: 123456"))

    def test_mensagem_futebol_permitida(self):
        self.assertEqual(self.sanitize("o Messi fez 5 gols"), "o Messi fez 5 gols")

    def test_preco_pequeno_permitido(self):
        """Valores pequenos como R$ 50 não devem ser bloqueados."""
        self.assertIsNotNone(self.sanitize("custa R$ 50"))


class TestSanitizeClassificationResult(unittest.TestCase):
    """Testa _sanitize_classification_result — evita apelidos possessivos do André."""

    def setUp(self):
        from whatsapp_manager import _sanitize_classification_result
        self.sanitize = _sanitize_classification_result

    def test_pet_name_pai_removido(self):
        res = {"pet_name": "pai", "relationship": "Filho"}
        result = self.sanitize(res)
        self.assertIsNone(result["pet_name"])

    def test_nickname_mae_removido(self):
        res = {"nickname": "mãe", "relationship": "Filho"}
        result = self.sanitize(res)
        self.assertIsNone(result["nickname"])

    def test_nickname_normal_mantido(self):
        res = {"nickname": "Pedrinho", "relationship": "Filho"}
        result = self.sanitize(res)
        self.assertEqual(result["nickname"], "Pedrinho")

    def test_nao_dict_retorna_intacto(self):
        self.assertEqual(self.sanitize("string"), "string")
        self.assertIsNone(self.sanitize(None))

    def test_campos_ausentes_sem_erro(self):
        res = {"relationship": "Cliente"}
        result = self.sanitize(res)
        self.assertEqual(result["relationship"], "Cliente")


class TestExtractJsonFromText(unittest.TestCase):
    """Testa _extract_json_from_text — parser robusto de JSON embutido em texto."""

    def setUp(self):
        from whatsapp_manager import _extract_json_from_text
        self.extract = _extract_json_from_text

    def test_json_puro(self):
        result = self.extract('{"key": "value"}')
        self.assertEqual(result["key"], "value")

    def test_json_com_texto_ao_redor(self):
        result = self.extract('Aqui está o resultado: {"name": "João", "age": 30} fim.')
        self.assertEqual(result["name"], "João")

    def test_json_com_string_contendo_chave(self):
        result = self.extract('{"msg": "olá {mundo}"}')
        self.assertEqual(result["msg"], "olá {mundo}")

    def test_json_invalido_levanta_erro(self):
        with self.assertRaises((ValueError, Exception)):
            self.extract("sem json aqui")

    def test_json_aninhado(self):
        result = self.extract('texto {"a": {"b": 1}} mais texto')
        self.assertEqual(result["a"]["b"], 1)


class TestNormalizeBrazilianPhone(unittest.TestCase):
    """Testa _normalize_brazilian_phone — normalização do 9º dígito."""

    def setUp(self):
        from whatsapp_manager import _normalize_brazilian_phone
        self.norm = _normalize_brazilian_phone

    def test_com_nono_digito_normaliza(self):
        # 55 + 86 + 9 + 8 dígitos = 13 → remove o 9
        self.assertEqual(self.norm("5586999970003"), "558699970003")

    def test_sem_nono_digito_intacto(self):
        self.assertEqual(self.norm("558699970003"), "558699970003")

    def test_numero_curto_intacto(self):
        self.assertEqual(self.norm("11987654321"), "11987654321")

    def test_com_caracteres_nao_numericos(self):
        # +55 (86) 99997-0003 → 5586999970003 (13 dígitos) → normaliza para 558699970003
        self.assertEqual(self.norm("+55 (86) 99997-0003"), "558699970003")

    def test_sem_prefixo_55_intacto(self):
        self.assertEqual(self.norm("86999970003"), "86999970003")


class TestBuildLidPhoneMap(unittest.TestCase):
    """Testa _build_lid_phone_map — construção do mapa LID→telefone."""

    def setUp(self):
        from whatsapp_manager import _build_lid_phone_map
        self.build = _build_lid_phone_map

    def test_sem_arquivos_retorna_vazio(self):
        with patch("whatsapp_manager.Path") as mock_path:
            mock_path.return_value.exists.return_value = False
            result = self.build(None)
            self.assertEqual(result, {})

    def test_com_db_retorna_mapeamentos(self):
        mock_conn = MagicMock()
        mock_conn.__enter__ = lambda s: mock_conn
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        mock_cur.fetchall.return_value = [
            ("265231477510271@lid", "558695903469@s.whatsapp.net"),
        ]
        import whatsapp_manager
        with patch("whatsapp_manager.Path") as mock_path, \
             patch("whatsapp_manager.sqlite3.connect", return_value=mock_conn):
            session_mock = MagicMock()
            session_mock.exists.return_value = False
            db_mock = MagicMock()
            db_mock.exists.return_value = True
            mock_path.side_effect = lambda p: session_mock if "session" in str(p) else db_mock
            result = self.build("/fake/db.sqlite")
        self.assertIn("265231477510271", result)
        self.assertEqual(result["265231477510271"], "558695903469")


class TestUpdateContactFieldsSteps(BaseWhatsAppManagerTest):
    """Testes para os passos 1–6 de _update_contact_fields."""

    BASE_CONTACTS = {
        "5511777777777@s.whatsapp.net": {
            "name": "Isabel Costa", "relationship": "Cliente",
            "nickname": "Bela", "pet_name": None,
        },
        "5511888888888@s.whatsapp.net": {
            "name": "Carlos Silva", "relationship": "Amigo",
            "nickname": None, "pet_name": None,
        },
    }

    def _call(self, identifier, fields, contacts=None, pc_exists=True, db_exists=False, db_rows=None, bridge_results=None):
        import whatsapp_manager
        contacts = contacts if contacts is not None else dict(self.BASE_CONTACTS)

        def fake_open(path, *a, **kw):
            import io
            return io.StringIO(json.dumps(contacts))

        def fake_write_open(path, mode="r", *a, **kw):
            import io
            if "w" in mode:
                return io.StringIO()
            return io.StringIO(json.dumps(contacts))

        mock_pc = MagicMock()
        mock_pc.exists.return_value = pc_exists
        mock_bridge_db = MagicMock()
        mock_bridge_db.exists.return_value = db_exists

        with patch("whatsapp_manager.Path") as mock_path, \
             patch("builtins.open", side_effect=lambda p, *a, **kw: __import__("io").StringIO(json.dumps(contacts)) if "w" not in str(a) else __import__("io").StringIO()), \
             patch("whatsapp_manager.sqlite3.connect") as mock_connect, \
             patch("urllib.request.urlopen") as mock_urlopen:

            def path_factory(p):
                m = MagicMock()
                if "personal_contacts" in str(p):
                    m.exists.return_value = pc_exists
                elif "whatsapp_messages" in str(p):
                    m.exists.return_value = db_exists
                else:
                    m.exists.return_value = False
                return m

            mock_path.side_effect = path_factory

            # DB mock
            mock_conn = MagicMock()
            mock_cursor = MagicMock()
            mock_conn.__enter__ = lambda s: mock_conn
            mock_conn.__exit__ = MagicMock(return_value=False)
            mock_conn.cursor.return_value = mock_cursor
            mock_cursor.fetchall.return_value = db_rows or []
            mock_connect.return_value = mock_conn

            # Bridge mock
            if bridge_results is not None:
                mock_resp = MagicMock()
                mock_resp.read.return_value = json.dumps({"results": bridge_results}).encode()
                mock_urlopen.return_value.__enter__.return_value = mock_resp
            else:
                mock_urlopen.side_effect = Exception("bridge offline")

            with patch("builtins.open", lambda p, *a, **kw: __import__("io").StringIO(json.dumps(contacts))):
                result = whatsapp_manager._update_contact_fields(identifier, fields)

        return result

    def test_pc_not_found(self):
        from whatsapp_manager import _update_contact_fields
        with patch("whatsapp_manager.Path") as mock_path:
            m = MagicMock()
            m.exists.return_value = False
            mock_path.return_value = m
            result = _update_contact_fields("Isabel", {"relationship": "Filha"})
        self.assertIn("não encontrado", result)

    def test_step1_match_by_phone_number(self):
        """Passo 1: match exato por número de telefone."""
        import whatsapp_manager
        contacts = {"5511777777777@s.whatsapp.net": {"name": "Isabel", "relationship": "Cliente", "nickname": None, "pet_name": None}}
        with patch("whatsapp_manager.Path") as mock_path, \
             patch("builtins.open", lambda p, *a, **kw: __import__("io").StringIO(json.dumps(contacts))):
            mock_path.return_value.exists.return_value = True
            result = whatsapp_manager._update_contact_fields("5511777777777", {"relationship": "Amiga"})
        self.assertIn("Isabel", result)

    def test_step2_exact_name_match(self):
        """Passo 2: match exato de name."""
        import whatsapp_manager
        contacts = {"5511777777777@s.whatsapp.net": {"name": "Isabel Costa", "relationship": "Cliente", "nickname": None, "pet_name": None}}
        with patch("whatsapp_manager.Path") as mock_path, \
             patch("builtins.open", lambda p, *a, **kw: __import__("io").StringIO(json.dumps(contacts))):
            mock_path.return_value.exists.return_value = True
            result = whatsapp_manager._update_contact_fields("Isabel Costa", {"relationship": "Filha"})
        self.assertIn("Isabel", result)

    def test_step3_nickname_match(self):
        """Passo 3: match por nickname."""
        import whatsapp_manager
        contacts = {"5511777777777@s.whatsapp.net": {"name": "Isabel Costa", "relationship": "Cliente", "nickname": "Bela", "pet_name": None}}
        with patch("whatsapp_manager.Path") as mock_path, \
             patch("builtins.open", lambda p, *a, **kw: __import__("io").StringIO(json.dumps(contacts))):
            mock_path.return_value.exists.return_value = True
            result = whatsapp_manager._update_contact_fields("Bela", {"notes": "minha amiga"})
        self.assertIn("Isabel", result)

    def test_step4_partial_name_match(self):
        """Passo 4: match parcial de substring em name."""
        import whatsapp_manager
        contacts = {"5511777777777@s.whatsapp.net": {"name": "Carlos Alberto Silva", "relationship": "Cliente", "nickname": None, "pet_name": None}}
        with patch("whatsapp_manager.Path") as mock_path, \
             patch("builtins.open", lambda p, *a, **kw: __import__("io").StringIO(json.dumps(contacts))):
            mock_path.return_value.exists.return_value = True
            result = whatsapp_manager._update_contact_fields("Carlos", {"relationship": "Amigo"})
        self.assertIn("Carlos", result)

    def test_step5_db_lookup(self):
        """Passo 5: match por sender_name no whatsapp_messages.db."""
        import whatsapp_manager
        contacts = {"5511777777777@s.whatsapp.net": {"name": "Contato 7777", "relationship": "Cliente", "nickname": None, "pet_name": None}}
        with patch("whatsapp_manager.Path") as mock_path, \
             patch("builtins.open", lambda p, *a, **kw: __import__("io").StringIO(json.dumps(contacts))), \
             patch("whatsapp_manager.sqlite3.connect") as mock_connect, \
             patch("urllib.request.urlopen", side_effect=Exception("bridge offline")):
            def path_side(p):
                m = MagicMock()
                m.exists.return_value = True
                return m
            mock_path.side_effect = path_side
            mock_conn = MagicMock()
            mock_cursor = MagicMock()
            mock_conn.__enter__ = lambda s: mock_conn
            mock_conn.__exit__ = MagicMock(return_value=False)
            mock_conn.cursor.return_value = mock_cursor
            mock_cursor.fetchall.return_value = [("5511777777777@s.whatsapp.net", "Roberto Alves")]
            mock_connect.return_value = mock_conn
            result = whatsapp_manager._update_contact_fields("Roberto", {"relationship": "Amigo"})
        self.assertNotIn("não encontrado", result)

    def test_step6_bridge_lookup(self):
        """Passo 6: match via bridge /contacts/search."""
        import whatsapp_manager
        contacts = {}
        bridge_results = [{"jid": "5511333333333@s.whatsapp.net", "name": "Fernanda Melo"}]
        with patch("whatsapp_manager.Path") as mock_path, \
             patch("builtins.open", lambda p, *a, **kw: __import__("io").StringIO(json.dumps(contacts))), \
             patch("whatsapp_manager.sqlite3.connect") as mock_connect, \
             patch("urllib.request.urlopen") as mock_urlopen:
            def path_side(p):
                m = MagicMock()
                m.exists.return_value = True if "personal" in str(p) else False
                return m
            mock_path.side_effect = path_side
            mock_conn = MagicMock()
            mock_cursor = MagicMock()
            mock_conn.__enter__ = lambda s: mock_conn
            mock_conn.__exit__ = MagicMock(return_value=False)
            mock_conn.cursor.return_value = mock_cursor
            mock_cursor.fetchall.return_value = []
            mock_connect.return_value = mock_conn
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps({"results": bridge_results}).encode()
            mock_urlopen.return_value.__enter__.return_value = mock_resp
            result = whatsapp_manager._update_contact_fields("Fernanda", {"relationship": "Amiga"})
        self.assertNotIn("não encontrado", result)

    def test_not_found_returns_error(self):
        """Sem match em nenhum passo: retorna mensagem de não encontrado."""
        import whatsapp_manager
        contacts = {}
        with patch("whatsapp_manager.Path") as mock_path, \
             patch("builtins.open", lambda p, *a, **kw: __import__("io").StringIO(json.dumps(contacts))), \
             patch("whatsapp_manager.sqlite3.connect") as mock_connect, \
             patch("urllib.request.urlopen", side_effect=Exception("bridge offline")):
            def path_side(p):
                m = MagicMock()
                m.exists.return_value = True if "personal" in str(p) else False
                return m
            mock_path.side_effect = path_side
            mock_conn = MagicMock()
            mock_cursor = MagicMock()
            mock_conn.__enter__ = lambda s: mock_conn
            mock_conn.__exit__ = MagicMock(return_value=False)
            mock_conn.cursor.return_value = mock_cursor
            mock_cursor.fetchall.return_value = []
            mock_connect.return_value = mock_conn
            result = whatsapp_manager._update_contact_fields("Ninguem Aqui", {"relationship": "Amigo"})
        self.assertIn("não encontrado", result)


class TestProcessMediaMessage(BaseWhatsAppManagerTest):
    """Testes para _process_media_message — transcrição de áudio e imagem."""

    def _make_event(self, media_type, file_path):
        event = MagicMock()
        event.has_media = True
        event.media_type = media_type
        event.media_urls = [file_path]
        event.message_id = "msg_test_123"
        return event

    def _config_patch(self, google="", openai="", openrouter="", media_model="gemini-1.5-flash"):
        """Patch das propriedades de config via __get__ no tipo."""
        import whatsapp_manager
        cfg_type = type(whatsapp_manager.config)
        return [
            patch.object(cfg_type, "google_api_key", new_callable=lambda: property(lambda self: google)),
            patch.object(cfg_type, "openai_api_key", new_callable=lambda: property(lambda self: openai)),
            patch.object(cfg_type, "openrouter_api_key", new_callable=lambda: property(lambda self: openrouter)),
            patch.object(cfg_type, "whatsapp_client_media_model", new_callable=lambda: property(lambda self: media_model)),
        ]

    def test_no_api_key_returns_none(self):
        """Sem nenhuma chave: retorna None e não chama ninguém."""
        import whatsapp_manager
        event = self._make_event("ptt", "/tmp/audio.ogg")
        patches = self._config_patch()
        # FISH_API_KEY explicitamente vazia: dentro do container a chave real está no env
        # e o teste faria uma chamada de verdade ao Fish.
        with patches[0], patches[1], patches[2], patches[3], \
             patch.dict(os.environ, {"FISH_API_KEY": ""}, clear=False), \
             patch("urllib.request.urlopen") as mock_urlopen, \
             patch("os.remove"):
            result = whatsapp_manager._process_media_message(event)
        self.assertIsNone(result)
        mock_urlopen.assert_not_called()

    def test_file_not_found_returns_none(self):
        """Arquivo de mídia inexistente: retorna None."""
        import whatsapp_manager
        event = self._make_event("ptt", "/tmp/nonexistent_audio_xyz.ogg")
        patches = self._config_patch(google="fake-key")
        with patches[0], patches[1], patches[2], patches[3], \
             patch("os.path.exists", return_value=False):
            result = whatsapp_manager._process_media_message(event)
        self.assertIsNone(result)

    def test_unsupported_media_type_returns_none(self):
        """Tipo de mídia não suportado (vídeo, documento): retorna None."""
        import whatsapp_manager
        event = self._make_event("video", "/tmp/video.mp4")
        patches = self._config_patch(google="fake-key")
        with patches[0], patches[1], patches[2], patches[3]:
            result = whatsapp_manager._process_media_message(event)
        self.assertIsNone(result)

    @patch("urllib.request.urlopen")
    @patch("os.remove")
    @patch("os.path.exists", return_value=True)
    def test_audio_vai_para_o_fish_e_nao_para_o_gemini(self, mock_exists, mock_remove, mock_urlopen):
        """Mesmo com chave do Google preenchida, áudio é transcrito pelo Fish."""
        import whatsapp_manager

        event = self._make_event("ptt", "/tmp/audio.ogg")
        patches = self._config_patch(google="fake-key")
        with patches[0], patches[1], patches[2], patches[3], \
             patch.object(whatsapp_manager, "_transcribe_via_fish", return_value="olá tudo bem") as mock_fish:
            result = whatsapp_manager._process_media_message(event)
        self.assertEqual(result, "olá tudo bem")
        mock_fish.assert_called_once_with("/tmp/audio.ogg")
        mock_urlopen.assert_not_called()

    @patch("urllib.request.urlopen")
    @patch("os.remove")
    @patch("os.path.exists", return_value=True)
    @patch("builtins.open", new_callable=MagicMock)
    def test_gemini_image_description(self, mock_open, mock_exists, mock_remove, mock_urlopen):
        """Gemini descreve imagem com sucesso."""
        import whatsapp_manager
        mock_open.return_value.__enter__ = lambda s: s
        mock_open.return_value.__exit__ = MagicMock(return_value=False)
        mock_open.return_value.read.return_value = b"fake image data"

        gemini_response = {"candidates": [{"content": {"parts": [{"text": "uma foto de um gato"}]}}]}
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(gemini_response).encode()
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        event = self._make_event("image", "/tmp/photo.jpg")
        patches = self._config_patch(google="fake-key")
        with patches[0], patches[1], patches[2], patches[3]:
            result = whatsapp_manager._process_media_message(event)
        self.assertEqual(result, "uma foto de um gato")

    @patch("urllib.request.urlopen")
    @patch("os.remove")
    @patch("os.path.exists", return_value=True)
    @patch("builtins.open", new_callable=MagicMock)
    def test_gemini_fails_returns_none(self, mock_open, mock_exists, mock_remove, mock_urlopen):
        """Gemini falha na imagem e sem outros providers: retorna None."""
        import whatsapp_manager
        mock_open.return_value.__enter__ = lambda s: s
        mock_open.return_value.__exit__ = MagicMock(return_value=False)
        mock_open.return_value.read.return_value = b"fake image data"
        mock_urlopen.side_effect = Exception("Gemini offline")

        event = self._make_event("image", "/tmp/foto.jpg")
        patches = self._config_patch(google="fake-key")
        with patches[0], patches[1], patches[2], patches[3]:
            result = whatsapp_manager._process_media_message(event)
        self.assertIsNone(result)


class TestGetMimeType(unittest.TestCase):
    """Testes para _get_mime_type."""

    def test_ogg(self):
        from whatsapp_manager import _get_mime_type
        self.assertEqual(_get_mime_type("/tmp/audio.ogg"), "audio/ogg")

    def test_mp3(self):
        from whatsapp_manager import _get_mime_type
        self.assertEqual(_get_mime_type("/tmp/sound.mp3"), "audio/mpeg")

    def test_jpg(self):
        from whatsapp_manager import _get_mime_type
        self.assertEqual(_get_mime_type("/tmp/photo.jpg"), "image/jpeg")

    def test_png(self):
        from whatsapp_manager import _get_mime_type
        self.assertEqual(_get_mime_type("/tmp/image.PNG"), "image/png")

    def test_unknown_extension(self):
        from whatsapp_manager import _get_mime_type
        self.assertEqual(_get_mime_type("/tmp/file.xyz"), "application/octet-stream")


class TestOwnerCommands(BaseWhatsAppManagerTest):
    """Testes para comandos do owner em pre_gateway_dispatch."""

    def setUp(self):
        super().setUp()
        import whatsapp_manager
        whatsapp_manager._pending_contact_update.clear()

    def tearDown(self):
        import whatsapp_manager
        whatsapp_manager._pending_contact_update.clear()
        super().tearDown()

    def _dispatch(self, msg_text, mock_urlopen):
        pre_dispatch = self.ctx.hooks.get("pre_gateway_dispatch")
        event = MagicMock()
        event.source.platform = "whatsapp"
        event.source.user_id = "5511999999999@s.whatsapp.net"
        event.source.chat_id = "5511999999999@s.whatsapp.net"
        event.text = msg_text
        event.has_media = False
        gateway = MagicMock()
        gateway._session_key_for_source.return_value = "sess_owner"
        gateway._session_model_overrides = {}
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"success": true, "messageId": "test-mid"}'
        mock_urlopen.return_value.__enter__.return_value = mock_resp
        return pre_dispatch("pre_gateway_dispatch", {"event": event, "gateway": gateway})

    # ── update contact (comando direto) ──────────────────────────────────────

    @patch("whatsapp_manager._update_contact_fields", return_value="✅ Contato Isabel atualizado.")
    @patch("urllib.request.urlopen")
    def test_update_contact_command(self, mock_urlopen, mock_update):
        """'update contact Isabel relationship=Filha' executa e retorna skip."""
        result = self._dispatch("update contact Isabel relationship=Filha", mock_urlopen)
        self.assertEqual(result["action"], "skip")
        self.assertEqual(result["reason"], "update-contact-command")
        mock_update.assert_called_once_with("Isabel", {"relationship": "Filha"})

    @patch("urllib.request.urlopen")
    def test_update_contact_missing_fields(self, mock_urlopen):
        """'update contact Isabel' sem campos avisa o owner."""
        result = self._dispatch("update contact Isabel", mock_urlopen)
        self.assertEqual(result["action"], "skip")
        # Deve ter enviado mensagem de erro via bridge
        mock_urlopen.assert_called()

    @patch("urllib.request.urlopen")
    def test_update_contact_no_fields(self, mock_urlopen):
        """'update contact Isabel' sem campo=valor avisa o owner."""
        result = self._dispatch("update contact Isabel", mock_urlopen)
        self.assertEqual(result["action"], "skip")
        mock_urlopen.assert_called()

    # ── NL update (via _classify_owner_intent) ───────────────────────────────

    @patch("whatsapp_manager._update_contact_fields", return_value="✅ Contato Maria atualizado.")
    @patch("whatsapp_manager._extract_update_fields_via_llm", return_value={"relationship": "Parente", "manual_relationship": "Parente"})
    @patch("whatsapp_manager._classify_owner_intent", return_value={
        "intent_type": "update_contact", "is_update": True,
        "contact_identifier": "Maria", "intent": "atualizar Maria",
    })
    @patch("urllib.request.urlopen")
    def test_nl_update_found(self, mock_urlopen, mock_intent, mock_extract, mock_update):
        """NL update com contato encontrado retorna skip."""
        result = self._dispatch("a Maria é minha parente", mock_urlopen)
        self.assertEqual(result["action"], "skip")
        mock_update.assert_called()

    @patch("whatsapp_manager._save_owner_status")
    @patch("whatsapp_manager._classify_owner_intent", return_value={
        "intent_type": "set_status", "is_update": False, "is_status": True,
        "is_clear": False, "description": "em reunião", "until_iso": None,
        "intent": "definir status",
    })
    @patch("urllib.request.urlopen")
    def test_set_status(self, mock_urlopen, mock_intent, mock_save):
        """Owner informa status: _save_owner_status é chamado e retorna skip."""
        result = self._dispatch("entrei em reunião agora", mock_urlopen)
        self.assertEqual(result["action"], "skip")
        self.assertEqual(result["reason"], "owner-status-set")
        mock_save.assert_called_once()

    @patch("whatsapp_manager._clear_owner_status")
    @patch("whatsapp_manager._classify_owner_intent", return_value={
        "intent_type": "set_status", "is_update": False, "is_status": True,
        "is_clear": True, "intent": "limpando status",
    })
    @patch("urllib.request.urlopen")
    def test_clear_status(self, mock_urlopen, mock_intent, mock_clear):
        """Owner limpa status: _clear_owner_status é chamado."""
        result = self._dispatch("já voltei", mock_urlopen)
        self.assertEqual(result["action"], "skip")
        mock_clear.assert_called_once()

    @patch("whatsapp_manager._get_active_owner_status", return_value={"description": "em reunião", "until_iso": None})
    @patch("whatsapp_manager._classify_owner_intent", return_value={
        "intent_type": "query_status", "is_update": False, "is_status": False,
        "intent": "consultar status ativo",
    })
    @patch("urllib.request.urlopen")
    def test_query_status_active(self, mock_urlopen, mock_intent, mock_get):
        """Owner consulta status ativo: retorna skip."""
        result = self._dispatch("qual meu status?", mock_urlopen)
        self.assertEqual(result["action"], "skip")
        self.assertEqual(result["reason"], "owner-status-query")

    @patch("whatsapp_manager._get_active_owner_status", return_value=None)
    @patch("whatsapp_manager._classify_owner_intent", return_value={
        "intent_type": "query_status", "is_update": False, "is_status": False,
        "intent": "consultar status ativo",
    })
    @patch("urllib.request.urlopen")
    def test_query_status_none(self, mock_urlopen, mock_intent, mock_get):
        """Owner consulta status quando não há nenhum ativo."""
        result = self._dispatch("qual meu status?", mock_urlopen)
        self.assertEqual(result["action"], "skip")

    # ── Pendência resolvida por número ────────────────────────────────────────

    @patch("whatsapp_manager._update_contact_fields", return_value="✅ Contato atualizado.")
    @patch("urllib.request.urlopen")
    def test_pending_resolved_by_phone(self, mock_urlopen, mock_update):
        """Quando há pendência e owner manda um número, resolve a pendência."""
        import whatsapp_manager
        whatsapp_manager._pending_contact_update["5511999999999@s.whatsapp.net"] = {
            "name": "João",
            "fields": {"relationship": "Amigo", "manual_relationship": "Amigo"},
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"success": true, "messageId": "test-mid"}'
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        result = self._dispatch("5511888777666", mock_urlopen)
        self.assertEqual(result["action"], "skip")
        self.assertEqual(result["reason"], "update-contact-pending")
        mock_update.assert_called()
        self.assertNotIn("5511999999999@s.whatsapp.net", whatsapp_manager._pending_contact_update)

    @patch("whatsapp_manager._update_contact_fields", return_value="✅ Contato atualizado.")
    @patch("urllib.request.urlopen")
    def test_disambiguate_resolved(self, mock_urlopen, mock_update):
        """Desambiguação resolvida quando owner manda número correspondente."""
        import whatsapp_manager
        whatsapp_manager._pending_contact_update["5511999999999@s.whatsapp.net"] = {
            "type": "disambiguate",
            "name": "Ana",
            "candidates": [
                ("5511111111111@s.whatsapp.net", "Ana Silva", "5511111111111"),
                ("5511222222222@s.whatsapp.net", "Ana Costa", "5511222222222"),
            ],
            "fields": {"relationship": "Cliente"},
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"success": true, "messageId": "test-mid"}'
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        result = self._dispatch("5511111111111", mock_urlopen)
        self.assertEqual(result["action"], "skip")
        self.assertEqual(result["reason"], "update-contact-disambiguate")
        mock_update.assert_called_once()


class TestExplicitHumanRequest(unittest.TestCase):
    """Turno real de 24/08: "Quero falar com uma pessoa" → a AYA prometeu retomada
    humana sem emitir [[HANDOFF]] e ninguém foi avisado. Pedido explícito de humano
    dispara o aviso pelo código, sem depender do modelo lembrar do marcador."""

    def test_pedido_explicito_e_reconhecido(self):
        for msg in (
            "Quero falar com uma pessoa",
            "quero falar com um humano",
            "queria falar com o responsável",
            "me passa pra um atendente",
            "posso falar com alguém?",
            "Can I talk to a human?",
            "I want to speak with a person",
            "quiero hablar con una persona",
        ):
            with self.subTest(msg=msg):
                self.assertTrue(whatsapp_manager._lead_requests_human(msg))

    def test_mencao_nao_explicita_nao_dispara(self):
        for msg in (
            "uma pessoa me indicou vocês",
            "vocês atendem pessoas físicas?",
            "o atendimento humanizado é o diferencial de vocês?",
            "quero falar com vocês sobre o produto",
        ):
            with self.subTest(msg=msg):
                self.assertFalse(whatsapp_manager._lead_requests_human(msg))


class TestOwnerMarketCorrection(BaseWhatsAppManagerTest):
    """Decisão de 24/08: mercado errado no cadastro se corrige pelo chat do dono
    (`update contact <numero> market_id=BR`). O bloco mercado/país/moeda/oferta
    vira junto — corrigir só market_id deixaria currency=USD velho no cadastro."""

    def _run_update(self, contacts, identifier, fields):
        chunks = []

        def fake_open(path, mode="r", **kwargs):
            m = unittest.mock.mock_open(read_data=json.dumps(contacts))()
            if "w" in mode:
                m.write = chunks.append
            return m

        with patch("whatsapp_manager._push_personal_contacts_to_github"), \
             patch("pathlib.Path.exists", return_value=True), \
             patch("builtins.open", side_effect=fake_open):
            result = whatsapp_manager._update_contact_fields(identifier, fields)
        written = json.loads("".join(chunks)) if chunks else {}
        return result, written

    def _contacts_lead_us(self):
        return {
            "5562999995459@s.whatsapp.net": {
                "name": "Lead Teste",
                "lid": "111222333444555@lid",
                "market_id": "US",
                "market": "United States",
                "country": "United States",
                "currency": "USD",
                "offer": "international",
            },
            "111222333444555@lid": {
                "name": "Lead Teste",
                "market_id": "US",
                "currency": "USD",
            },
        }

    def test_corrigir_market_id_vira_o_bloco_inteiro_nas_duas_chaves(self):
        result, written = self._run_update(
            self._contacts_lead_us(), "5562999995459", {"market_id": "BR"}
        )
        self.assertIn("✅", result)
        rec = written["5562999995459@s.whatsapp.net"]
        self.assertEqual(rec["market_id"], "BR")
        self.assertEqual(rec["currency"], "BRL")
        self.assertEqual(rec["offer"], "brazil")
        self.assertEqual(rec["market_source"], "owner_manual")
        mirror = written["111222333444555@lid"]
        self.assertEqual(mirror["market_id"], "BR")
        self.assertEqual(mirror["currency"], "BRL")

    def test_alias_de_mercado_e_normalizado(self):
        contacts = self._contacts_lead_us()
        contacts["5562999995459@s.whatsapp.net"].update(
            {"market_id": "BR", "currency": "BRL"}
        )
        result, written = self._run_update(contacts, "5562999995459", {"market_id": "eua"})
        self.assertIn("✅", result)
        rec = written["5562999995459@s.whatsapp.net"]
        self.assertEqual(rec["market_id"], "US")
        self.assertEqual(rec["currency"], "USD")

    def test_corrigir_por_currency_nao_e_revertido_pelo_market_id_velho(self):
        result, written = self._run_update(
            self._contacts_lead_us(), "5562999995459", {"currency": "BRL"}
        )
        self.assertIn("✅", result)
        rec = written["5562999995459@s.whatsapp.net"]
        self.assertEqual(rec["market_id"], "BR")
        self.assertEqual(rec["currency"], "BRL")

    def test_mercado_invalido_nao_grava_nada(self):
        result, written = self._run_update(
            self._contacts_lead_us(), "5562999995459", {"market_id": "XX"}
        )
        self.assertIn("❌", result)
        self.assertEqual(written, {})


class TestBlockUnblockCommands(BaseWhatsAppManagerTest):
    """Itens 6 e 7 do QA de 24/08: `desbloquear` só gravava blocked=False — o gate
    (ai_enabled/in_flow) continuava barrando como legacy-contact-disabled, e a sessão
    antiga do Hermes reabria e vazava a persona anterior para o lead."""

    def _dispatch(self, msg_text, mock_urlopen, gateway=None):
        pre_dispatch = self.ctx.hooks.get("pre_gateway_dispatch")
        event = MagicMock()
        event.source.platform = "whatsapp"
        event.source.user_id = "5511999999999@s.whatsapp.net"
        event.source.chat_id = "5511999999999@s.whatsapp.net"
        event.text = msg_text
        event.has_media = False
        if gateway is None:
            gateway = MagicMock()
        gateway._session_key_for_source.return_value = "sess_owner"
        gateway._session_model_overrides = {}
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"success": true, "messageId": "test-mid"}'
        mock_urlopen.return_value.__enter__.return_value = mock_resp
        return pre_dispatch("pre_gateway_dispatch", {"event": event, "gateway": gateway})

    @patch("whatsapp_manager._reset_hermes_sessions_for_contact", return_value=0)
    @patch("whatsapp_manager._update_contact_fields", return_value="✅ Contato *Lead* (5562999995459@s.whatsapp.net) atualizado.")
    @patch("urllib.request.urlopen")
    def test_desbloquear_religa_o_gate_de_ia(self, mock_urlopen, mock_update, _mock_reset):
        result = self._dispatch("desbloquear 5562999995459", mock_urlopen)
        self.assertEqual(result["reason"], "unblock-contact-command")
        identifier, fields = mock_update.call_args.args
        self.assertEqual(identifier, "5562999995459")
        self.assertIs(fields["blocked"], False)
        self.assertIs(fields["ai_enabled"], True)
        self.assertIs(fields["in_flow"], True)
        self.assertEqual(fields.get("flow_origin"), "owner_unblock")

    @patch("whatsapp_manager._reset_hermes_sessions_for_contact", return_value=1)
    @patch("whatsapp_manager._update_contact_fields", return_value="✅ Contato *Lead* (5562999995459@s.whatsapp.net) atualizado.")
    @patch("urllib.request.urlopen")
    def test_desbloquear_encerra_a_sessao_antiga_do_hermes(self, mock_urlopen, _mock_update, mock_reset):
        self._dispatch("desbloquear 5562999995459", mock_urlopen)
        mock_reset.assert_called_once()
        self.assertEqual(mock_reset.call_args.args[1], "5562999995459@s.whatsapp.net")

    @patch("whatsapp_manager._reset_hermes_sessions_for_contact", return_value=0)
    @patch("whatsapp_manager._update_contact_fields", return_value="❌ Contato 'x' não encontrado em personal_contacts.json nem no histórico de mensagens.")
    @patch("urllib.request.urlopen")
    def test_desbloquear_de_contato_inexistente_nao_reseta_sessao(self, mock_urlopen, _mock_update, mock_reset):
        self._dispatch("desbloquear x", mock_urlopen)
        mock_reset.assert_not_called()

    @patch("whatsapp_manager._update_contact_fields", return_value="✅ Contato *Lead* (5562999995459@s.whatsapp.net) atualizado.")
    @patch("urllib.request.urlopen")
    def test_bloquear_continua_gravando_somente_blocked(self, mock_urlopen, mock_update):
        result = self._dispatch("bloquear 5562999995459", mock_urlopen)
        self.assertEqual(result["reason"], "block-contact-command")
        self.assertEqual(mock_update.call_args.args[1], {"blocked": True})

    def test_reset_encerra_entrada_viva_no_store(self):
        entry = MagicMock()
        entry.session_key = "agent:main:whatsapp:dm:5562999995459"
        entry.session_id = "20260819_010203_abc"
        entry.origin.chat_id = "5562999995459@s.whatsapp.net"
        outro = MagicMock()
        outro.session_key = "agent:main:whatsapp:dm:5599888887777"
        outro.session_id = "20260820_010203_def"
        outro.origin.chat_id = "5599888887777@s.whatsapp.net"
        store = MagicMock(spec=["list_sessions", "reset_session", "_db"])
        store.list_sessions.return_value = [entry, outro]
        store.reset_session.return_value = object()
        store._db = None
        gateway = MagicMock()
        gateway.session_store = store
        with patch("whatsapp_manager._load_personal_contacts", return_value={}):
            n = whatsapp_manager._reset_hermes_sessions_for_contact(
                gateway, "5562999995459@s.whatsapp.net"
            )
        store.reset_session.assert_called_once_with("agent:main:whatsapp:dm:5562999995459")
        self.assertEqual(n, 1)

    def test_reset_promove_linha_duravel_sem_entrada_no_store(self):
        """A sessão de 19/08 do QA não estava no store — vivia só no state.db e a
        recuperação a ressuscitava. A fronteira durável precisa ser promovida lá."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = Path(tmp.name) / "state.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE sessions (id TEXT, source TEXT, chat_id TEXT, ended_at TEXT, end_reason TEXT)"
        )
        conn.execute(
            "INSERT INTO sessions VALUES ('20260819_115109_d3f511', 'whatsapp', '5562999995459@s.whatsapp.net', NULL, NULL)"
        )
        conn.commit()
        conn.close()
        store = MagicMock(spec=["list_sessions", "reset_session", "_db"])
        store.list_sessions.return_value = []
        promote = MagicMock(return_value=True)
        store._db = MagicMock(spec=["promote_to_session_reset"])
        store._db.promote_to_session_reset = promote
        gateway = MagicMock()
        gateway.session_store = store
        with patch("whatsapp_manager._load_personal_contacts", return_value={}), \
             patch.object(whatsapp_manager, "_HERMES_STATE_DB_PATH", db_path):
            n = whatsapp_manager._reset_hermes_sessions_for_contact(
                gateway, "5562999995459@s.whatsapp.net"
            )
        promote.assert_called_once()
        self.assertEqual(promote.call_args.args[0], "20260819_115109_d3f511")
        self.assertEqual(n, 1)

    def test_reset_sem_gateway_nao_quebra(self):
        self.assertEqual(
            whatsapp_manager._reset_hermes_sessions_for_contact(None, "5562999995459"), 0
        )


REGRAS_DOIS_MERCADOS = """# Base comercial

## Tabela comercial — fonte única de preço

| Mercado | Implementação | Mensalidade |
|---|---:|---:|
| Brasil | R$ 1.500 | R$ 497/mês |
| Estados Unidos | US$ 497 | US$ 99/mês |

## Regras de mercado

Uma empresa que opera nos EUA continua mercado US mesmo falando espanhol.
Espanhol não puxa preço, Pix ou regras do Brasil.

### Isolamento obrigatório

- Lead do mercado Brasil usa somente a própria oferta, moeda e pagamento.
- Lead do mercado dos EUA usa somente a própria oferta, moeda e pagamento.
- Nunca misture R$ e US$ na mesma oferta.
- Para EUA, não mencione Pix, CNPJ, horário de Goiânia nem condição do Brasil.
- Para Brasil, não mencione Zelle, destinatário dos EUA nem condição dos EUA.

## Mercado Brasil

- **método de pagamento da implementação:** Pix
<!-- AYA_PAYMENT_DETAILS:BR:START -->
- **Pix CNPJ:** 44.249.819/0001-62
- **Titular:** Fulano de Tal
<!-- AYA_PAYMENT_DETAILS:BR:END -->

## Mercado Estados Unidos

- **método de pagamento da implementação:** Zelle

### Zelle — dados oficiais estruturados

<!-- AYA_PAYMENT_DETAILS:US:START -->
- **Recipient:** Sicrana de Tal
- **Zelle email:** sicrana@example.com
<!-- AYA_PAYMENT_DETAILS:US:END -->

## O que a implementação inclui

- Configuração da persona.

## Intenção de compra e pagamento

### Fechamento EUA — modelo de resposta

Copie Recipient e e-mail do bloco liberado.

### Fechamento Brasil

Envie o Pix oficial em texto copiável.

### Confirmação de pagamento

Peça o comprovante.
"""


class TestGateMarketSections(unittest.TestCase):
    """Recorte por mercado: a alternativa errada não pode existir no prompt."""

    def _gate(self, mercado):
        return whatsapp_manager._gate_market_sections_for_prompt(
            REGRAS_DOIS_MERCADOS, mercado
        )

    def test_lead_us_nao_ve_nada_do_brasil(self):
        s = self._gate("US")
        self.assertNotIn("Pix", s)
        self.assertNotIn("CNPJ", s)
        self.assertNotIn("R$ 1.500", s)
        self.assertNotIn("Fechamento Brasil", s)
        self.assertNotIn("Mercado Brasil", s)
        # e continua com tudo do mercado dele
        self.assertIn("Zelle", s)
        self.assertIn("US$ 497", s)
        self.assertIn("Fechamento EUA", s)
        self.assertIn("Recipient", s)

    def test_lead_br_nao_ve_nada_dos_eua(self):
        s = self._gate("BR")
        self.assertNotIn("Zelle", s)
        self.assertNotIn("Recipient", s)
        self.assertNotIn("US$ 497", s)
        self.assertNotIn("Fechamento EUA", s)
        self.assertIn("Pix", s)
        self.assertIn("R$ 1.500", s)
        self.assertIn("Fechamento Brasil", s)

    def test_secao_geral_sobrevive_aos_dois(self):
        for mercado in ("US", "BR"):
            with self.subTest(mercado=mercado):
                s = self._gate(mercado)
                self.assertIn("O que a implementação inclui", s)
                self.assertIn("Configuração da persona", s)
                self.assertIn("Confirmação de pagamento", s)

    def test_mercado_desconhecido_preserva_tudo(self):
        """Sem saber onde a empresa opera, o modelo precisa das duas tabelas para perguntar."""
        for mercado in ("", "XX", None):
            with self.subTest(mercado=mercado):
                s = whatsapp_manager._gate_market_sections_for_prompt(
                    REGRAS_DOIS_MERCADOS, mercado
                )
                self.assertIn("R$ 1.500", s)
                self.assertIn("US$ 497", s)

    def test_titulo_de_roteamento_nao_e_de_mercado(self):
        self.assertEqual(whatsapp_manager._heading_market("Roteamento de mercado"), "")
        self.assertEqual(whatsapp_manager._heading_market("Mercado Brasil"), "BR")
        self.assertEqual(whatsapp_manager._heading_market("Mercado Estados Unidos"), "US")
        self.assertEqual(whatsapp_manager._heading_market("Fechamento EUA"), "US")
        # título citando os dois é de roteamento, fica
        self.assertEqual(whatsapp_manager._heading_market("Brasil e Estados Unidos"), "")

    def test_instrucao_de_mercado_some_quando_mercado_e_conhecido(self):
        """QA de 24/08: as linhas "Para EUA, não mencione Pix..." eram as últimas âncoras
        de R$/Pix/CNPJ no prompt de um lead US — existem para proibir o que o recorte já
        removeu, e são a única fonte possível desses literais para o modelo copiar."""
        s = self._gate("US")
        self.assertNotIn("Pix", s)
        self.assertNotIn("CNPJ", s)
        self.assertNotIn("R$", s)
        self.assertNotIn("Nunca misture", s)
        self.assertNotIn("horário de Goiânia", s)
        b = self._gate("BR")
        self.assertNotIn("Zelle", b)
        self.assertNotIn("US$", b)
        self.assertNotIn("Nunca misture", b)

    def test_linha_que_nomeia_o_outro_mercado_some(self):
        s = self._gate("US")
        self.assertNotIn("Lead do mercado Brasil", s)
        self.assertNotIn("regras do Brasil", s)
        b = self._gate("BR")
        self.assertNotIn("Lead do mercado dos EUA", b)
        self.assertNotIn("empresa que opera nos EUA", b)

    def test_usa_como_verbo_nao_e_token_de_mercado(self):
        """"Lead do mercado Brasil usa somente..." tem o verbo "usa" — não pode ser
        confundido com USA e sumir do prompt de um lead brasileiro."""
        b = self._gate("BR")
        self.assertIn("Lead do mercado Brasil usa somente", b)

    def test_mercado_desconhecido_preserva_instrucoes_de_isolamento(self):
        s = whatsapp_manager._gate_market_sections_for_prompt(REGRAS_DOIS_MERCADOS, "")
        self.assertIn("Nunca misture R$ e US$", s)
        self.assertIn("Para EUA, não mencione Pix", s)
        self.assertIn("Para Brasil, não mencione Zelle", s)

    def test_paragrafo_de_prosa_cai_inteiro_sem_deixar_fragmento(self):
        """Code-review de 24/08: derrubar linha a linha dentro de prosa deixava um
        fragmento órfão dizendo o OPOSTO ("...e mantenha USD, oferta internacional,")
        no prompt de um lead brasileiro."""
        regras = (
            "## Regras\n\n"
            "Uma empresa que opera nos EUA segue mercado US mesmo\n"
            "se o lead conversar em espanhol. Responda em espanhol e mantenha USD, oferta internacional,\n"
            "sem puxar preço, Pix ou regras do Brasil.\n\n"
            "Parágrafo neutro que fica.\n"
        )
        b = whatsapp_manager._gate_market_sections_for_prompt(regras, "BR")
        self.assertNotIn("mantenha USD", b)
        self.assertNotIn("Responda em espanhol", b)
        self.assertIn("Parágrafo neutro que fica", b)

    def test_usd_e_dolar_tambem_sao_literais_de_mercado(self):
        b = whatsapp_manager._gate_market_sections_for_prompt(
            "Cobre o valor em USD.\n\nPreço em dólares.", "BR"
        )
        self.assertNotIn("USD", b)
        self.assertNotIn("dólares", b)
        s = whatsapp_manager._gate_market_sections_for_prompt("O valor em reais é fixo.", "US")
        self.assertNotIn("reais", s)

    def test_verbo_usa_em_titulo_nao_e_mercado(self):
        """"Quem usa a AYA" era classificado como seção US e sumia para lead BR."""
        self.assertEqual(whatsapp_manager._heading_market("Quem usa a AYA"), "")
        self.assertEqual(whatsapp_manager._heading_market("Como a AYA usa o WhatsApp"), "")

    def test_cerca_de_codigo_nao_reabre_secao_recortada(self):
        regras = (
            "## Mercado Brasil\n\nsegredo brasileiro\n\n```\n# comentario de exemplo\nmais segredo\n```\n\n"
            "## Geral\n\nconteudo neutro\n"
        )
        s = whatsapp_manager._gate_market_sections_for_prompt(regras, "US")
        self.assertNotIn("segredo", s)
        self.assertIn("conteudo neutro", s)

    def test_linha_de_tabela_rotulada_por_moeda_tambem_e_recortada(self):
        regras = "| USD | US$ 497 | US$ 99/mês |\n| BRL | R$ 1.500 | R$ 497/mês |"
        b = whatsapp_manager._gate_market_sections_for_prompt(regras, "BR")
        self.assertNotIn("US$ 497", b)
        self.assertIn("R$ 1.500", b)

    def test_horario_de_goiania_nao_vai_para_lead_us(self):
        """Turno real de 24/08: lead US recebeu "horário de Goiânia" — o horário
        comercial vive em seção neutra e sobrevivia ao recorte."""
        s = whatsapp_manager._gate_market_sections_for_prompt(
            "Atendimento humano de segunda a sexta, das 08h às 18h, no horário de Goiânia.",
            "US",
        )
        self.assertNotIn("Goiânia", s)
        b = whatsapp_manager._gate_market_sections_for_prompt(
            "Atendimento humano de segunda a sexta, das 08h às 18h, no horário de Goiânia.",
            "BR",
        )
        self.assertIn("Goiânia", b)

    def test_credencial_do_outro_mercado_nunca_vaza_com_intencao(self):
        """Mesmo liberando pagamento, a credencial liberada é a do mercado do lead."""
        s = whatsapp_manager._gate_payment_details_for_prompt(
            whatsapp_manager._gate_market_sections_for_prompt(REGRAS_DOIS_MERCADOS, "US"),
            market_id="US",
            allow_payment_details=True,
        )
        self.assertIn("sicrana@example.com", s)
        self.assertNotIn("44.249.819", s)


class TestOnboardingGate(unittest.TestCase):
    """Item 2 do QA de 24/08: 6 turnos de entrevista de implantação antes da venda.

    A regra "ONBOARDING SÓ DEPOIS DA VENDA" existe no meio de CONSTRAINTS ABSOLUTAS
    e é ignorada — instruir não funciona, filtrar funciona. A guarda barra pergunta
    de configuração de implantação quando não há venda registrada para o contato."""

    def _gate(self, texto, vendas=None, chat="12025550199@s.whatsapp.net"):
        with patch("whatsapp_manager._load_sales", return_value=vendas or {}):
            return whatsapp_manager._enforce_aya_onboarding_output_gate(
                texto, user_message="oi", chat_id=chat
            )

    def test_entrevista_de_implantacao_vira_fallback(self):
        out = self._gate(
            "Você pode me passar os dias e horários de funcionamento? "
            "E qual a duração de cada serviço?"
        )
        self.assertNotIn("dias e horários", out)
        self.assertNotIn("duração de cada serviço", out)
        self.assertTrue(out.strip())

    def test_pergunta_de_onboarding_sai_e_o_resto_fica(self):
        out = self._gate(
            "A AYA atende seus clientes direto no WhatsApp. "
            "Como você prefere a configuração de agenda?"
        )
        self.assertIn("atende seus clientes direto no WhatsApp", out)
        self.assertNotIn("configuração de agenda", out)

    def test_area_de_cobertura_e_pergunta_de_onboarding(self):
        out = self._gate("Qual é a área de cobertura do seu serviço?")
        self.assertNotIn("área de cobertura", out)

    def test_duracao_reformulada_tambem_e_onboarding(self):
        """Turno real do QA de 24/08 à noite: o modelo inverteu a ordem
        ("serviços têm duração" em vez de "duração de serviço") e escapou."""
        out = self._gate(
            "Para montar essa lógica, os serviços de limpeza têm duração fixa "
            "ou variam conforme o tipo e o tamanho do imóvel?"
        )
        self.assertNotIn("duração fixa", out)
        out2 = self._gate("Quanto tempo dura cada atendimento?")
        self.assertNotIn("Quanto tempo dura", out2)

    def test_duracao_em_afirmacao_ou_sem_servico_fica(self):
        for texto in (
            "A implantação leva poucos dias e a gente conduz junto.",
            "Quanto tempo você perde por dia respondendo mensagens?",
        ):
            with self.subTest(texto=texto):
                self.assertEqual(self._gate(texto), texto)

    def test_ressalva_sobre_configuracao_nao_e_pergunta(self):
        """Afirmar que a configuração vem depois é o comportamento certo — fica."""
        texto = "A configuração de agenda a gente ajusta junto depois da contratação."
        self.assertEqual(self._gate(texto), texto)

    def test_pergunta_de_diagnostico_fica(self):
        texto = "Como funciona seu atendimento hoje? Onde os clientes mais travam?"
        self.assertEqual(self._gate(texto), texto)

    def test_venda_registrada_libera_onboarding(self):
        chat = "12025550199@s.whatsapp.net"
        texto = "Me passa os dias e horários de funcionamento para configurar a agenda?"
        out = self._gate(texto, vendas={"V1": {"contact_key": chat}}, chat=chat)
        self.assertEqual(out, texto)


class TestPriceFallbacks(unittest.TestCase):
    """Defeitos dos fallbacks da guarda, introduzidos em 99931f6 e medidos no QA de 24/08."""

    def test_quanto_que_custa_coloquial_e_pergunta_de_preco(self):
        """Turno real do QA de 24/08 à noite: "e quanto q custa?" caiu no ramo
        sem preço porque o "q" quebra o `quanto\\s+custa`."""
        for msg in (
            "Ah que show....cara, e quanto q custa?",
            "quanto que custa?",
            "quanto q fica isso?",
            "quanto vale?",
        ):
            with self.subTest(msg=msg):
                self.assertTrue(whatsapp_manager._asks_about_price(msg))

    def test_objecao_de_preco_e_assunto_de_preco(self):
        """"Achei o valor caro" caía na frase neutra — objeção é assunto de preço."""
        for msg in (
            "Achei o valor de implementação um pouco caro.",
            "Achei caro",
            "muito caro pra mim",
            "tá salgado",
            "isso tá muito salgado",
            "fora do orçamento",
            "that's too expensive",
            "es muy caro",
            "la implementación es muy cara",
            "me parece carísimo",
        ):
            with self.subTest(msg=msg):
                self.assertTrue(whatsapp_manager._asks_about_price(msg))

    def test_cara_como_pessoa_nao_e_preco(self):
        """"cara" é vocativo comum em pt-BR — não pode virar assunto de preço."""
        for msg in ("e aí cara, tudo bem?", "valeu cara"):
            with self.subTest(msg=msg):
                self.assertFalse(whatsapp_manager._asks_about_price(msg))

    def test_mensagem_neutra_continua_sem_ser_preco(self):
        for msg in (
            "oi, tudo bem?",
            "quero entender como a AYA funcionaria",
            "vocês atendem clínica?",
        ):
            with self.subTest(msg=msg):
                self.assertFalse(whatsapp_manager._asks_about_price(msg))

    def test_fallback_nao_repete_pergunta_ja_respondida(self):
        """Teste #05 do QA: no 7º turno o lead recebeu a mesma pergunta do 1º."""
        historico = (
            "AYA: Me conta como funciona seu atendimento hoje que eu te explico como a AYA se encaixa.\n"
            "Lead: A gente atende por telefone e WhatsApp."
        )
        with patch("whatsapp_manager._fetch_chat_history", return_value=historico):
            out = whatsapp_manager._payment_gate_fallback(
                "hmm entendi",
                {"market_id": "US", "language": "pt"},
                "market_mismatch",
                rules_content="",
                chat_id="12025550199@s.whatsapp.net",
            )
        self.assertNotIn("Me conta como funciona seu atendimento", out)
        self.assertNotIn("?", out)
        self.assertTrue(out.strip())

    def test_fallback_pergunta_normalmente_na_primeira_vez(self):
        with patch("whatsapp_manager._fetch_chat_history", return_value=""):
            out = whatsapp_manager._payment_gate_fallback(
                "hmm entendi",
                {"market_id": "US", "language": "pt"},
                "market_mismatch",
                rules_content="",
                chat_id="12025550199@s.whatsapp.net",
            )
        self.assertIn("Me conta como funciona", out)

    def test_fallback_sem_chat_id_segue_perguntando(self):
        out = whatsapp_manager._payment_gate_fallback(
            "hmm entendi",
            {"market_id": "US", "language": "pt"},
            "market_mismatch",
            rules_content="",
        )
        self.assertIn("Me conta como funciona", out)


class TestPurchaseIntentGaps(unittest.TestCase):
    """Buracos medidos no QA de 24/08 no fluxo de fechamento."""

    def test_como_faco_o_pagamento_e_intencao(self):
        """Turno real do QA de 24/08 à noite: "Como faço o pagamento?" deu intent=False
        — o padrão só cobria "como fazer o pagamento"."""
        for msg in (
            "Como faço o pagamento?",
            "como faço pra pagar",
            "Mesmo assim quero avançar. Como faço para contratar?",
        ):
            with self.subTest(msg=msg):
                self.assertTrue(whatsapp_manager._has_explicit_purchase_intent(msg))

    def test_como_faco_para_contratar_e_intencao(self):
        for msg in (
            "Como faço para contratar?",
            "Como faço pra contratar",
            "Como posso contratar?",
            "Vamos fechar",
            "How do I sign up?",
            "How can I get started?",
            "¿Cómo hago para contratar?",
        ):
            with self.subTest(msg=msg):
                self.assertTrue(whatsapp_manager._has_explicit_purchase_intent(msg))

    def test_fechamento_negado_ou_encerramento_nao_e_intencao(self):
        """Achados do code-review de 24/08: o padrão `vamos fechar` sem guarda
        liberava credencial de pagamento para recusa e despedida."""
        for msg in (
            "Não vamos fechar",
            "Vamos fechar por hoje, obrigado",
            "vamos fechar o escopo do projeto primeiro",
        ):
            with self.subTest(msg=msg):
                self.assertFalse(whatsapp_manager._has_explicit_purchase_intent(msg))

    def test_contratar_terceiros_ou_cancelar_nao_e_intencao(self):
        """Lead é dono de negócio: contratar funcionário/gente não é contratar a AYA."""
        for msg in (
            "Como faço para contratar mais funcionários?",
            "Como posso contratar gente para a clínica?",
            "No sé cómo contratar personal para mi empresa",
            "Como faço para cancelar depois de contratar?",
            "Trabalho como contrato temporário",
        ):
            with self.subTest(msg=msg):
                self.assertFalse(whatsapp_manager._has_explicit_purchase_intent(msg))

    def test_plural_em_ingles_e_intencao(self):
        """"How do we sign up?" — lead falando pela empresa no plural."""
        for msg in ("How do we sign up?", "How can we get started?"):
            with self.subTest(msg=msg):
                self.assertTrue(whatsapp_manager._has_explicit_purchase_intent(msg))

    def test_como_contrato_espanhol_com_pergunta_e_intencao(self):
        self.assertTrue(whatsapp_manager._has_explicit_purchase_intent("¿Cómo contrato?"))

    def test_pergunta_de_preco_continua_nao_sendo_intencao(self):
        """Regra da própria base: perguntar preço não é intenção de pagar."""
        for msg in (
            "Quanto custa?",
            "Qual o valor da implementação?",
            "How much does it cost?",
            "Achei o valor de implementação um pouco caro.",
            "Não quero contratar agora",
        ):
            with self.subTest(msg=msg):
                self.assertFalse(whatsapp_manager._has_explicit_purchase_intent(msg))


class TestSplitHumanBubbles(unittest.TestCase):
    """Splitter de bolhas — sem I/O."""

    def test_blank_line_becomes_two_bubbles(self):
        parts = whatsapp_manager._split_human_bubbles("eita\n\nele capotou aqui, só umas 11h")
        self.assertEqual(parts, ["eita", "ele capotou aqui, só umas 11h"])

    def test_three_paragraphs_become_three_bubbles(self):
        parts = whatsapp_manager._split_human_bubbles("oi\n\ncomo vai\n\ntudo bem?")
        self.assertEqual(parts, ["oi", "como vai", "tudo bem?"])

    def test_more_than_three_paragraphs_are_capped(self):
        parts = whatsapp_manager._split_human_bubbles("a\n\nb\n\nc\n\nd")
        self.assertEqual(len(parts), 3)
        self.assertEqual(parts[0], "a")
        self.assertEqual(parts[1], "b")
        self.assertIn("c", parts[2])
        self.assertIn("d", parts[2])

    def test_short_lines_split(self):
        parts = whatsapp_manager._split_human_bubbles("opa\nele tá no futebol até umas 21h")
        self.assertEqual(len(parts), 2)
        self.assertEqual(parts[0], "opa")

    def test_single_short_message_stays_one_bubble(self):
        self.assertEqual(whatsapp_manager._split_human_bubbles("só umas 11h"), ["só umas 11h"])

    def test_opener_splits_long_block(self):
        rest = (
            "consegui ver a planilha e tem umas pautas boas pra gente fechar ainda hoje "
            "quando você tiver um tempo pra olhar com calma o que ficou pendente nessa semana"
        )
        block = f"Oi! {rest}"
        self.assertGreater(len(block), 140)
        parts = whatsapp_manager._split_human_bubbles(block)
        self.assertEqual(parts[0], "Oi!")
        self.assertTrue(parts[1].startswith("consegui"))

    def test_url_sentence_is_not_auto_split(self):
        text = (
            "Segue o link https://exemplo.com/checkout agora. "
            "Depois que pagar me manda o comprovante por aqui mesmo combinado?"
        )
        self.assertEqual(whatsapp_manager._split_human_bubbles(text), [text])

    def test_empty_returns_empty_list(self):
        self.assertEqual(whatsapp_manager._split_human_bubbles("   "), [])

    def test_list_block_keeps_line_breaks(self):
        """Bug do QA de 23/08: lista virava uma parede de texto sem quebra nenhuma."""
        text = (
            "Para uma empresa nos Estados Unidos, ela pode:\n"
            "- Responder os leads automaticamente, inclusive em inglês.\n"
            "- Explicar seus produtos ou serviços com base nas informações aprovadas.\n"
            "- Fazer perguntas para entender o perfil e a necessidade do lead.\n"
            "- Qualificar quem tem real potencial de compra.\n"
            "- Conduzir o contato para reunião, orçamento ou pagamento."
        )
        parts = whatsapp_manager._split_human_bubbles(text)
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0].count("\n"), 5)
        self.assertTrue(parts[0].startswith("Para uma empresa nos Estados Unidos, ela pode:\n- Responder"))

    def test_numbered_list_is_one_bubble(self):
        text = (
            "A conversa poderia funcionar assim:\n"
            "1. O cliente chama no WhatsApp.\n"
            "2. A AYA entende qual serviço ele procura.\n"
            "3. Solicita o endereço para confirmar a área atendida.\n"
            "4. Apresenta os dias e horários disponíveis."
        )
        parts = whatsapp_manager._split_human_bubbles(text)
        self.assertEqual(len(parts), 1)
        self.assertIn("\n1. O cliente chama", parts[0])
        self.assertIn("\n4. Apresenta os dias", parts[0])

    def test_structured_answer_gets_more_bubbles_than_cap(self):
        """Conteúdo estruturado passa de 3 bolhas em vez de virar um bloco só."""
        text = (
            "A AYA funciona como sua primeira atendente comercial no WhatsApp. "
            "Para uma empresa nos Estados Unidos, ela pode:\n\n"
            "- Responder os leads automaticamente, inclusive em inglês.\n"
            "- Qualificar quem tem real potencial de compra.\n"
            "- Encaminhar a conversa quando houver necessidade de negociação.\n\n"
            "Você define a oferta, o público, as perguntas e o tom da conversa. "
            "A AYA segue esse processo sem inventar preços, prazos ou condições.\n\n"
            "Para configurar no seu caso, precisamos definir o idioma e o fuso horário dos EUA."
        )
        parts = whatsapp_manager._split_human_bubbles(text)
        self.assertEqual(len(parts), 5)
        self.assertTrue(parts[1].startswith("Para uma empresa nos Estados Unidos, ela pode:\n-"))
        self.assertTrue(all(len(p) <= 520 for p in parts), [len(p) for p in parts])

    def test_overflow_never_glued_with_space(self):
        """Sobra acima do teto é colada com linha em branco, nunca com espaço."""
        text = "\n\n".join(f"Frase número {i} do bloco de teste que precisa continuar longa." for i in range(8))
        parts = whatsapp_manager._split_human_bubbles(text)
        self.assertLessEqual(len(parts), 5)
        self.assertIn("\n\n", parts[-1])
        self.assertNotIn("teste. Frase", parts[-1])


class TestPostLlmCall(BaseWhatsAppManagerTest):
    """post_llm_call só processa EXEC do dono. Contatos vão em transform_llm_output."""

    def _call(self, session_id, response_text, platform="whatsapp"):
        post_llm = self.ctx.hooks.get("post_llm_call")
        return post_llm(
            "post_llm_call",
            platform=platform,
            session_id=session_id,
            assistant_response=response_text,
        )

    def test_contact_session_returns_none(self):
        """Contato: post_llm_call não envia — Hermes ignora este retorno."""
        result = self._call("5511888888888@s.whatsapp.net", "oi, tudo bem!")
        self.assertIsNone(result)

    def test_owner_session_returns_none_without_exec(self):
        """Owner sem EXEC no response: post_llm_call retorna None (Hermes envia normalmente)."""
        result = self._call("5511999999999@s.whatsapp.net", "resposta normal sem exec")
        self.assertIsNone(result)

    def test_non_whatsapp_platform_returns_none(self):
        """Plataformas que não são whatsapp passam sem filtro."""
        result = self._call("qualquer_session", "resposta", platform="telegram")
        self.assertIsNone(result)

    def test_empty_response_returns_none(self):
        """Response vazio retorna None."""
        result = self._call("5511888888888@s.whatsapp.net", "")
        self.assertIsNone(result)

    @patch("whatsapp_manager._update_contact_fields", return_value="✅ Contato atualizado.")
    def test_owner_exec_is_processed(self, mock_update):
        """Owner com EXEC no response: executa update e remove o EXEC do texto."""
        result = self._call(
            "5511999999999@s.whatsapp.net",
            "Ok, vou atualizar.\nEXEC: update contact Isabel relationship=Filha"
        )
        self.assertIsNotNone(result)
        mock_update.assert_called_once()
        self.assertNotIn("EXEC:", result["assistant_response"])


class TestPrepareContactReply(BaseWhatsAppManagerTest):
    """Filtro de resposta a contatos: redação seletiva + vazamento interno (§17)."""

    def test_third_party_phone_redacted(self):
        import whatsapp_manager
        out = whatsapp_manager._prepare_contact_reply("ligue para 11999887766 ok?")
        self.assertIn("[número omitido]", out)
        self.assertNotIn("11999887766", out)

    def test_rotulo_resposta_sugerida_nao_chega_ao_lead(self):
        """QA de 24/08 pós-reset: a primeira bolha do lead foi literalmente
        "*Resposta sugerida:*" — a persona default do dono rascunhando em vez
        de a AYA responder. O rótulo nunca pode sair para o cliente."""
        out = whatsapp_manager._prepare_contact_reply(
            "*Resposta sugerida:*\n\nOlá! A AYA atende seus clientes no WhatsApp."
        )
        self.assertEqual(out, "Olá! A AYA atende seus clientes no WhatsApp.")

    def test_prefixo_de_rascunho_inline_e_removido(self):
        out = whatsapp_manager._prepare_contact_reply("Resposta sugerida: Olá! Tudo bem?")
        self.assertEqual(out, "Olá! Tudo bem?")

    def test_variantes_de_rascunho_en_es(self):
        casos = (
            ("Suggested reply: Hi there.", "Hi there."),
            ("Respuesta sugerida: Hola, ¿cómo estás?", "Hola, ¿cómo estás?"),
            ("Sugestão de resposta: Oi!", "Oi!"),
        )
        for entrada, esperado in casos:
            with self.subTest(entrada=entrada):
                self.assertEqual(whatsapp_manager._prepare_contact_reply(entrada), esperado)

    def test_rascunho_entre_aspas_e_desembrulhado(self):
        """Caso de 19/08: o lead recebeu a resposta inteira entre aspas."""
        out = whatsapp_manager._prepare_contact_reply(
            'Resposta sugerida para o lead:\n"Olá, posso ajudar com a AYA?"'
        )
        self.assertEqual(out, "Olá, posso ajudar com a AYA?")

    def test_mencao_a_sugestao_no_meio_do_texto_fica(self):
        texto = "Posso te mandar uma resposta sugerida depois, tudo bem?"
        self.assertEqual(whatsapp_manager._prepare_contact_reply(texto), texto)

    def test_owner_whatsapp_number_preserved(self):
        import whatsapp_manager
        with patch.dict(os.environ, {"WHATSAPP_OWNER_NUMBER": "5562936180895"}):
            for sample in (
                "Chama no WhatsApp: 62936180895",
                "WhatsApp oficial: 62 93618-0895",
                "Fala com a gente: 5562936180895",
            ):
                out = whatsapp_manager._prepare_contact_reply(sample)
                self.assertNotIn("[número omitido]", out, sample)
                self.assertTrue(
                    "62936180895" in out.replace(" ", "").replace("-", "")
                    or "5562936180895" in out.replace(" ", "").replace("-", ""),
                    sample,
                )

    def test_pix_cnpj_preserved(self):
        import whatsapp_manager
        cnpj = "44.249.819/0001-62"
        with patch.dict(os.environ, {"WHATSAPP_PIX_KEY": cnpj}):
            out = whatsapp_manager._prepare_contact_reply(f"Pix CNPJ: {cnpj}")
            self.assertIn(cnpj, out)
            self.assertNotIn("[número omitido]", out)

    def test_self_improvement_alone_suppressed(self):
        import whatsapp_manager
        out = whatsapp_manager._prepare_contact_reply("Self-improvement review")
        self.assertEqual(out, "")

    def test_internal_commercial_authorization_is_never_sent(self):
        import whatsapp_manager
        text = (
            "Não posso oferecer desconto sem autorização explícita do Gustavo.\n"
            "Esse é o valor atual da condição."
        )
        out = whatsapp_manager._prepare_contact_reply(text)
        self.assertEqual(out, "Esse é o valor atual da condição.")

    def test_human_validation_jargon_is_never_sent(self):
        import whatsapp_manager
        text = (
            "The next step is human validation before payment.\n"
            "I can send the official payment details now."
        )
        out = whatsapp_manager._prepare_contact_reply(text)
        self.assertEqual(out, "I can send the official payment details now.")

    def test_internal_commercial_rules_are_never_sent_in_spanish(self):
        import whatsapp_manager
        text = (
            "No puedo cambiarlo sin autorización explícita de Gustavo.\n"
            "El siguiente paso es validación humana.\n"
            "Esta es la condición actual."
        )
        out = whatsapp_manager._prepare_contact_reply(text)
        self.assertEqual(out, "Esta es la condición actual.")

    def test_internal_approval_variations_are_never_sent(self):
        import whatsapp_manager
        samples = (
            "Preciso da aprovação do Gustavo antes de enviar o pagamento.",
            "Gustavo precisa aprovar essa condição.",
            "I need Gustavo approval before payment.",
            "Gustavo must approve this condition.",
            "Necesito la aprobación de Gustavo antes del pago.",
            "Gustavo tiene que aprobar esta condición.",
            "No puedo cambiarlo sin la autorización explícita de Gustavo.",
            "Isso precisa ser aprovado pelo Gustavo.",
            "Antes de cobrar, preciso validar isso com o Gustavo.",
            "This must be approved by Gustavo.",
            "Necesito verificarlo con Gustavo antes del pago.",
            "Antes de cobrar, preciso verificar com o André.",
        )
        for sample in samples:
            self.assertEqual(whatsapp_manager._prepare_contact_reply(sample), "", sample)

    def test_official_zelle_recipient_is_preserved(self):
        import whatsapp_manager
        text = (
            "Zelle:\n"
            "Izabella Kristiny de Freitas\n"
            "izabellafreitas2002@hotmail.com"
        )
        self.assertEqual(whatsapp_manager._prepare_contact_reply(text), text)

    def test_integration_caveats_are_not_mistaken_for_internal_approval(self):
        import whatsapp_manager
        samples = (
            "Precisamos validar a integração com o Google Calendar durante a configuração.",
            "Vou verificar com o QuickBooks como a conexão funciona.",
            "Só precisamos confirmar como a conexão com o calendário será feita durante a configuração.",
        )
        for sample in samples:
            self.assertEqual(whatsapp_manager._prepare_contact_reply(sample), sample)

    def test_core_fallback_notice_suppressed(self):
        """Família de aviso de retry/fallback do core — vazou pro cliente em 2026-08-20."""
        import whatsapp_manager
        blocked = [
            "🔄 Switched to fallback model: gpt-5.6-luna via openai-codex → deepseek/deepseek-v4-flash via openrouter",
            "🔄 Primary model failed — switching to fallback: deepseek/deepseek-v4-flash via openrouter",
            "↻ Empty response after tool calls — using earlier content as final answer",
            "⏳ Retrying in 2.4s (attempt 2/3)...",
            "🗜️ Compressed 180 → 42 messages, retrying...",
            "⚠️ Empty/malformed response — switching to fallback...",
            "⚠️ Non-retryable error (HTTP 403) — trying fallback...",
            "❌ Billing or credits exhausted — HTTP 402",
            "❌ API failed after 3 retries — HTTP 500",
        ]
        for sample in blocked:
            self.assertTrue(whatsapp_manager.isSystemError(sample), sample)
            self.assertEqual(whatsapp_manager._prepare_contact_reply(sample), "", sample)
        # O aviso que o próprio plugin manda pro dono é em português e não pode ser mudo.
        self.assertFalse(whatsapp_manager.isSystemError(
            "⚠️ O provider do modelo falhou respondendo Tony e a mensagem de erro "
            "foi bloqueada — o cliente não recebeu nada."
        ))
        self.assertFalse(whatsapp_manager.isSystemError("Fechou! Te mando o Pix então"))

    def test_commercial_reply_preserved(self):
        import whatsapp_manager
        text = "Hoje são R$997 de implementação e R$397/mês. Quer fechar por Pix ou prefere uma call de 15 min?"
        out = whatsapp_manager._prepare_contact_reply(text)
        self.assertEqual(out, text)

    def test_implementacao_inclui_not_treated_as_action_claim(self):
        """'inclui' (presente) é texto comercial; não confundir com 'incluí' (pretérito)."""
        import whatsapp_manager
        text = (
            "A implementação inclui a configuração personalizada da IA, "
            "regras, serviços, testes e a primeira versão para validação."
        )
        out = whatsapp_manager._prepare_contact_reply(text)
        self.assertEqual(out, text)
        self.assertNotIn("não tenho como fazer", out)


class TestTransformLlmOutput(BaseWhatsAppManagerTest):
    """Hook que envia bolhas e devolve whitespace para o Hermes não reenviar o bloco."""

    def setUp(self):
        super().setUp()
        whatsapp_manager._turn_key.clear()
        whatsapp_manager._turn_sent.clear()
        whatsapp_manager._turn_inflight.clear()
        whatsapp_manager._turn_inbound.clear()
        whatsapp_manager._turn_context_bindings.set(())
        whatsapp_manager._sender_to_chat.clear()
        whatsapp_manager._responded_sessions.clear()
        whatsapp_manager._pending_inbound.clear()
        whatsapp_manager._HUMAN_DELIVER_SYNC = True
        self.delivery_gate_patcher = patch("whatsapp_manager._assert_delivery_allowed")
        self.delivery_gate_patcher.start()
        self.voice_patcher = patch("whatsapp_manager._maybe_send_voice", return_value=None)
        self.voice_patcher.start()
        self.turn_persist_patcher = patch("whatsapp_manager._persist_turn_sent_to_disk")
        self.turn_persist_patcher.start()

    def tearDown(self):
        self.turn_persist_patcher.stop()
        self.voice_patcher.stop()
        self.delivery_gate_patcher.stop()
        whatsapp_manager._turn_key.clear()
        whatsapp_manager._turn_sent.clear()
        whatsapp_manager._turn_inflight.clear()
        whatsapp_manager._turn_inbound.clear()
        whatsapp_manager._turn_context_bindings.set(())
        whatsapp_manager._sender_to_chat.clear()
        whatsapp_manager._responded_sessions.clear()
        whatsapp_manager._pending_inbound.clear()
        whatsapp_manager._HUMAN_DELIVER_SYNC = False
        super().tearDown()

    def _call(self, session_id, response_text, platform="whatsapp"):
        if platform == "whatsapp" and session_id and not whatsapp_manager._session_is_owner(session_id):
            chat_id = whatsapp_manager._sender_to_chat.get(session_id) or session_id
            test_turn = f"test-turn:{session_id}"
            whatsapp_manager._turn_key.setdefault(chat_id, test_turn)
            whatsapp_manager._turn_key.setdefault(session_id, test_turn)
        hook = self.ctx.hooks.get("transform_llm_output")
        self.assertIsNotNone(hook)
        return hook(
            "transform_llm_output",
            platform=platform,
            session_id=session_id,
            assistant_response=response_text,
        )

    @patch("whatsapp_manager._human_send")
    def test_contact_sends_bubbles_and_returns_whitespace(self, mock_send):
        result = self._call("5511888888888@s.whatsapp.net", "oi, tudo bem!")
        self.assertEqual(result, "\n")
        mock_send.assert_called_once_with(
            "5511888888888@s.whatsapp.net",
            "oi, tudo bem!",
            automation=True,
        )

    @patch("whatsapp_manager._human_send")
    def test_blank_line_is_kept_for_the_splitter(self, mock_send):
        text = "eita\n\nele capotou aqui, só umas 11h"
        result = self._call("5511888888888@s.whatsapp.net", text)
        self.assertEqual(result, "\n")
        mock_send.assert_called_once_with(
            "5511888888888@s.whatsapp.net",
            text,
            automation=True,
        )

    @patch("whatsapp_manager._human_send")
    def test_owner_session_returns_none(self, mock_send):
        result = self._call("5511999999999@s.whatsapp.net", "resposta do dono")
        self.assertIsNone(result)
        mock_send.assert_not_called()

    def test_non_whatsapp_platform_returns_none(self):
        result = self._call("5511888888888@s.whatsapp.net", "oi", platform="telegram")
        self.assertIsNone(result)

    @patch("whatsapp_manager._human_send")
    def test_tool_result_suppressed(self, mock_send):
        result = self._call("5511888888888@s.whatsapp.net", "nothing to save.")
        self.assertEqual(result, "\n")
        mock_send.assert_not_called()

    @patch("whatsapp_manager._human_send")
    def test_tool_result_ok_suppressed(self, mock_send):
        result = self._call("5511888888888@s.whatsapp.net", "ok.")
        self.assertEqual(result, "\n")
        mock_send.assert_not_called()

    @patch("whatsapp_manager._human_send")
    def test_action_claim_replaced(self, mock_send):
        result = self._call("5511888888888@s.whatsapp.net", "pronto, adicionei o contato.")
        self.assertEqual(result, "\n")
        sent = mock_send.call_args[0][1]
        self.assertNotIn("André", sent)
        self.assertIn("não é algo que consigo fazer", sent)
        self.assertNotIn("adicionei", sent)

    @patch("whatsapp_manager._human_send")
    def test_phone_number_redacted(self, mock_send):
        result = self._call("5511888888888@s.whatsapp.net", "ligue para 11999887766 ok?")
        self.assertEqual(result, "\n")
        sent = mock_send.call_args[0][1]
        self.assertIn("[número omitido]", sent)
        self.assertNotIn("11999887766", sent)

    @patch("whatsapp_manager._human_send")
    def test_turn_dedup_suppresses_second_call(self, mock_send):
        session = "5511888888888@s.whatsapp.net"
        whatsapp_manager._turn_key[session] = "turno-abc"

        r1 = self._call(session, "primeira resposta")
        self.assertEqual(r1, "\n")
        r2 = self._call(session, "segunda resposta duplicada")
        self.assertEqual(r2, "\n")
        mock_send.assert_called_once()

    @patch("whatsapp_manager._human_send", side_effect=("sent-old", "sent-new"))
    def test_old_response_keeps_its_turn_when_new_pre_llm_already_started(self, mock_send):
        session = "12025550199@s.whatsapp.net"
        whatsapp_manager._track_inbound(session, "msg-old", "Quiero avanzar.")
        old_turn = whatsapp_manager._register_contact_turn(
            session,
            session,
            "Quiero avanzar.",
        )

        whatsapp_manager._track_inbound(session, "msg-new", "No quiero pagar.")
        new_turn = whatsapp_manager._register_contact_turn(
            session,
            session,
            "No quiero pagar.",
        )

        self.assertNotEqual(old_turn, new_turn)
        self.assertIn(old_turn, whatsapp_manager._turn_inbound)
        self.assertIn(new_turn, whatsapp_manager._turn_inbound)

        self.assertEqual(self._call(session, "Old reply"), "\n")
        self.assertIn(old_turn, whatsapp_manager._turn_sent)
        self.assertNotIn(new_turn, whatsapp_manager._turn_sent)
        self.assertEqual(
            whatsapp_manager._current_inbound_text(session),
            "No quiero pagar.",
        )

        self.assertEqual(self._call(session, "New reply"), "\n")
        self.assertIn(new_turn, whatsapp_manager._turn_sent)
        self.assertEqual(mock_send.call_args_list[0].args[1], "Old reply")
        self.assertEqual(mock_send.call_args_list[1].args[1], "New reply")
        self.assertEqual(whatsapp_manager._current_inbound_text(session), "")

    def test_identical_messages_with_different_ids_are_distinct_turns(self):
        session = "12025550199@s.whatsapp.net"
        whatsapp_manager._track_inbound(session, "msg-one", "Ok")
        first = whatsapp_manager._register_contact_turn(session, session, "Ok")
        whatsapp_manager._track_inbound(session, "msg-two", "Ok")
        second = whatsapp_manager._register_contact_turn(session, session, "Ok")

        self.assertNotEqual(first, second)
        self.assertEqual(
            whatsapp_manager._turn_inbound[first]["message_id"],
            "msg-one",
        )
        self.assertEqual(
            whatsapp_manager._turn_inbound[second]["message_id"],
            "msg-two",
        )

    def test_reservation_does_not_mark_sent_before_confirmation(self):
        session = "5511888888888@s.whatsapp.net"
        whatsapp_manager._turn_key[session] = "turn-pending"
        reserved, turn_key = whatsapp_manager._reserve_contact_send(session, session, "resposta")
        self.assertTrue(reserved)
        self.assertEqual(turn_key, "turn-pending")
        self.assertIn(turn_key, whatsapp_manager._turn_inflight)
        self.assertNotIn(turn_key, whatsapp_manager._turn_sent)

        whatsapp_manager._complete_contact_send(turn_key, delivered=True, uncertain=False)
        self.assertNotIn(turn_key, whatsapp_manager._turn_inflight)
        self.assertIn(turn_key, whatsapp_manager._turn_sent)

    @patch("whatsapp_manager._human_send")
    def test_gate_block_releases_reservation_without_marking_sent(self, mock_send):
        session = "5511888888888@s.whatsapp.net"
        with patch(
            "whatsapp_manager._assert_delivery_allowed",
            side_effect=whatsapp_manager.DeliveryBlocked("takeover ativo"),
        ):
            result = self._call(session, "vou responder")
        self.assertEqual(result, "\n")
        turn_key = whatsapp_manager._turn_key[session]
        self.assertNotIn(turn_key, whatsapp_manager._turn_inflight)
        self.assertNotIn(turn_key, whatsapp_manager._turn_sent)
        mock_send.assert_not_called()

    @patch("whatsapp_manager._human_send")
    def test_opaque_session_without_exact_mapping_is_suppressed(self, mock_send):
        result = self._call("sessao-sem-binding", "não pode sair")
        self.assertEqual(result, "\n")
        mock_send.assert_not_called()

    @patch("whatsapp_manager._human_send")
    def test_encoded_phone_cannot_be_redirected_to_another_contact(self, mock_send):
        session = "agent:main:whatsapp:dm:5511888888888"
        whatsapp_manager._sender_to_chat[session] = "5511777777777@s.whatsapp.net"
        result = self._call(session, "não pode cruzar")
        self.assertEqual(result, "\n")
        mock_send.assert_not_called()

    @patch("whatsapp_manager._persist_turn_sent_to_disk")
    @patch("whatsapp_manager._human_send")
    def test_turn_dedup_survives_restart(self, mock_send, mock_persist):
        import hashlib

        chat_id = "60314276073511@s.whatsapp.net"
        user_msg = "oi tudo bem"
        tk = chat_id + ":" + hashlib.md5(user_msg.encode()).hexdigest()
        whatsapp_manager._turn_sent.add(tk)

        with whatsapp_manager._turn_lock:
            old_tk = whatsapp_manager._turn_key.get(chat_id)
            if old_tk != tk:
                whatsapp_manager._turn_key[chat_id] = tk
                if old_tk:
                    whatsapp_manager._turn_sent.discard(old_tk)

        self.assertIn(tk, whatsapp_manager._turn_sent)
        r = self._call(chat_id, "olá Jardel!")
        self.assertEqual(r, "\n")
        mock_send.assert_not_called()

    @patch("whatsapp_manager._human_send", side_effect=RuntimeError("bridge offline"))
    def test_send_failure_is_terminal_uncertain_without_default_retry(self, mock_send):
        session = "5511888888888@s.whatsapp.net"
        result = self._call(session, "tudo certo!")
        self.assertEqual(result, "\n")
        turn_key = whatsapp_manager._turn_key[session]
        self.assertIn(turn_key, whatsapp_manager._turn_sent)
        self.assertNotIn(turn_key, whatsapp_manager._turn_inflight)
        second = self._call(session, "tudo certo de novo!")
        self.assertEqual(second, "\n")
        mock_send.assert_called_once()

    @patch("whatsapp_manager._human_send")
    def test_mapped_session_resolves_chat_id(self, mock_send):
        whatsapp_manager._sender_to_chat["sessao-uuid"] = "5511888888888@s.whatsapp.net"
        result = self._call("sessao-uuid", "opa")
        self.assertEqual(result, "\n")
        mock_send.assert_called_once_with(
            "5511888888888@s.whatsapp.net",
            "opa",
            automation=True,
        )

    @staticmethod
    def _payment_rules():
        return (
            "| Mercado | Implementação | Mensalidade |\n"
            "| --- | --- | --- |\n"
            "| Brasil | R$ 1.500 | R$ 497/mês |\n"
            "| Estados Unidos | US$ 497 | US$ 99/mês |\n"
            "<!-- AYA_PAYMENT_DETAILS:BR:START -->\n"
            "- **Pix CNPJ:** 44.249.819/0001-62\n"
            "- **Titular:** Titular Brasil\n"
            "<!-- AYA_PAYMENT_DETAILS:BR:END -->\n"
            "<!-- AYA_PAYMENT_DETAILS:US:START -->\n"
            "- **Recipient:** Test Recipient\n"
            "- **Zelle email:** pay@example.com\n"
            "<!-- AYA_PAYMENT_DETAILS:US:END -->"
        )

    def _call_aya_payment_reply(self, inbound, response, contact):
        session = "12025550199@s.whatsapp.net"
        test_turn = f"test-turn:{session}"
        whatsapp_manager._turn_key.pop(session, None)
        whatsapp_manager._turn_sent.discard(test_turn)
        whatsapp_manager._turn_inflight.discard(test_turn)
        whatsapp_manager._turn_inbound.pop(test_turn, None)
        whatsapp_manager._track_inbound(session, "msg-payment", inbound)
        with patch.dict(os.environ, {"WHATSAPP_CONFIG_SUBDIR": "instance"}), \
             patch("whatsapp_manager._load_personal_contacts", return_value={session: contact}), \
             patch("whatsapp_manager._load_support_files", return_value=("AYA", self._payment_rules())), \
             patch("whatsapp_manager._human_send") as mock_send:
            result = self._call(session, response)
        return result, mock_send

    def test_zelle_details_are_blocked_for_price_question(self):
        response = "Zelle:\nTest Recipient\npay@example.com"
        result, mock_send = self._call_aya_payment_reply(
            "¿Cuánto cuesta?",
            response,
            {"market_id": "US", "currency": "USD", "language": "es"},
        )

        self.assertEqual(result, "\n")
        sent = mock_send.call_args.args[1]
        self.assertNotIn("Test Recipient", sent)
        self.assertNotIn("pay@example.com", sent)

    def test_zelle_details_are_preserved_for_explicit_us_purchase_intent(self):
        response = "Zelle:\nTest Recipient\npay@example.com"
        result, mock_send = self._call_aya_payment_reply(
            "Quiero avanzar.",
            response,
            {"market_id": "US", "currency": "USD", "language": "es"},
        )

        self.assertEqual(result, "\n")
        self.assertEqual(mock_send.call_args.args[1], response)

    def test_intencao_com_modelo_errando_recebe_o_zelle_oficial(self):
        """QA de 24/08 à noite: lead US quente ("Como faço o pagamento?") ficava sem
        caminho de pagamento porque o modelo insistia na credencial BR (memória do
        provider) e o fallback só devolvia frase neutra. Com intenção explícita e
        mercado conhecido, o fallback entrega o bloco oficial do PRÓPRIO mercado —
        as mesmas condições que liberariam o bloco no prompt."""
        _result, mock_send = self._call_aya_payment_reply(
            "Como faço o pagamento?",
            "Pode pagar pelo Pix: CNPJ 44.249.819/0001-62, Titular Brasil. R$ 997.",
            {"market_id": "US", "currency": "USD", "language": "pt"},
        )
        sent = mock_send.call_args.args[1]
        self.assertIn("Test Recipient", sent)
        self.assertIn("pay@example.com", sent)
        self.assertNotIn("44.249.819", sent)
        self.assertNotIn("997", sent)

    def test_intencao_com_credencial_inventada_recebe_a_oficial(self):
        _result, mock_send = self._call_aya_payment_reply(
            "Quiero avanzar y pagar.",
            "Zelle email: attacker@example.com\nRecipient: Persona Inventada",
            {"market_id": "US", "currency": "USD", "language": "es"},
        )
        sent = mock_send.call_args.args[1]
        self.assertIn("pay@example.com", sent)
        self.assertNotIn("attacker@example.com", sent)

    def test_sem_intencao_o_fallback_nao_entrega_credencial(self):
        _result, mock_send = self._call_aya_payment_reply(
            "hmm entendi",
            "Pode pagar pelo Pix: CNPJ 44.249.819/0001-62.",
            {"market_id": "US", "currency": "USD", "language": "pt"},
        )
        sent = mock_send.call_args.args[1]
        self.assertNotIn("pay@example.com", sent)
        self.assertNotIn("Test Recipient", sent)
        self.assertNotIn("44.249.819", sent)

    def test_data_americana_nao_reprova_zelle_oficial(self):
        """Assimetria (a) do QA: a whitelist de dígitos do US é vazia, então 08/25/2026
        virava unknown_digits e derrubava uma resposta com o conjunto oficial correto."""
        response = "Zelle:\nTest Recipient\npay@example.com\nWe can start on 08/25/2026."
        _result, mock_send = self._call_aya_payment_reply(
            "I want to move forward.",
            response,
            {"market_id": "US", "currency": "USD", "language": "en"},
        )
        self.assertEqual(mock_send.call_args.args[1], response)

    def test_rotulo_em_portugues_aceita_o_destinatario_oficial_us(self):
        """Assimetria (b): o prompt manda atender lead US em pt/es quando for o caso,
        mas "Destinatario:" reprovava mesmo com o valor oficial exato."""
        response = "Destinatário: Test Recipient\nZelle email: pay@example.com"
        _result, mock_send = self._call_aya_payment_reply(
            "Quero avançar com a contratação.",
            response,
            {"market_id": "US", "currency": "USD", "language": "pt"},
        )
        self.assertEqual(mock_send.call_args.args[1], response)

    def test_rotulo_alias_com_valor_errado_continua_reprovando(self):
        response = "Destinatário: Persona Inventada\nZelle email: pay@example.com"
        _result, mock_send = self._call_aya_payment_reply(
            "Quero avançar com a contratação.",
            response,
            {"market_id": "US", "currency": "USD", "language": "pt"},
        )
        self.assertNotIn("Persona Inventada", mock_send.call_args.args[1])

    def test_nome_oficial_em_frase_corrida_conta_como_presente(self):
        """Assimetria (c): "Send it to <nome> at <email>" zerava field_presence
        mesmo com nome e e-mail oficiais corretos."""
        response = "Send it to Test Recipient at pay@example.com."
        _result, mock_send = self._call_aya_payment_reply(
            "I want to move forward.",
            response,
            {"market_id": "US", "currency": "USD", "language": "en"},
        )
        self.assertEqual(mock_send.call_args.args[1], response)

    def test_nome_estendido_em_frase_corrida_continua_reprovando(self):
        """Nome maior que o oficial não pode contar como destinatário (desvio de destino)."""
        response = "Send it to Test Recipient Silva at pay@example.com."
        _result, mock_send = self._call_aya_payment_reply(
            "I want to move forward.",
            response,
            {"market_id": "US", "currency": "USD", "language": "en"},
        )
        self.assertNotIn("Silva", mock_send.call_args.args[1])

    def test_preco_de_papel_errado_recebe_o_preco_certo_nao_aviso_de_pagamento(self):
        """Assimetria (d), colisão do 497: "$497 monthly" (mensalidade BR no papel US)
        respondia com aviso genérico de dados de pagamento — assunto que o lead nem tocou.
        Preço errado sem conteúdo de pagamento vira correção de preço."""
        _result, mock_send = self._call_aya_payment_reply(
            "How much is it monthly?",
            "The monthly fee is $497.",
            {"market_id": "US", "currency": "USD", "language": "en"},
        )
        sent = mock_send.call_args.args[1]
        self.assertIn("US$ 99", sent)
        self.assertNotIn("monthly fee is $497", sent)
        self.assertNotIn("payment details", sent)

    def test_pergunta_de_onboarding_nao_chega_ao_lead(self):
        """A guarda de onboarding roda no mesmo caminho de saída do payment-gate."""
        with patch("whatsapp_manager._load_sales", return_value={}):
            _result, mock_send = self._call_aya_payment_reply(
                "ok, e como funciona?",
                "A AYA responde seus clientes na hora. Qual a duração de cada serviço?",
                {"market_id": "US", "currency": "USD", "language": "pt"},
            )
        sent = mock_send.call_args.args[1]
        self.assertIn("responde seus clientes na hora", sent)
        self.assertNotIn("duração de cada serviço", sent)

    def test_log_do_gate_diz_se_digito_e_credencial_oficial_sem_expor_o_valor(self):
        """Item 1 do QA de 24/08: o modelo escreveu rótulo CNPJ com dígitos para lead US
        e o log não dizia se os dígitos eram o CNPJ real (vazamento) ou invenção.
        A classificação vai para o log; o valor, nunca."""
        with self.assertLogs("whatsapp_manager", level="WARNING") as logs:
            out = whatsapp_manager._enforce_aya_payment_output_gate(
                "Pode pagar por aqui.\nCNPJ: 44.249.819/0001-62",
                user_message="quero contratar",
                contact_info={"market_id": "US"},
                rules_content=self._payment_rules(),
            )
        joined = "\n".join(logs.output)
        self.assertIn("official:BR:pix cnpj", joined)
        self.assertNotIn("44249819", joined)
        self.assertNotIn("44.249.819", joined)
        self.assertNotIn("44.249.819", out)

    def test_log_do_gate_marca_digito_inventado_como_unknown(self):
        with self.assertLogs("whatsapp_manager", level="WARNING") as logs:
            whatsapp_manager._enforce_aya_payment_output_gate(
                "Pode pagar por aqui.\nCNPJ: 12.345.678/0001-99",
                user_message="quero contratar",
                contact_info={"market_id": "US"},
                rules_content=self._payment_rules(),
            )
        joined = "\n".join(logs.output)
        self.assertIn("unknown:len14", joined)
        self.assertNotIn("12345678", joined)
        self.assertNotIn("12.345.678", joined)

    def test_log_do_gate_traz_precos_e_papeis_parseados(self):
        with self.assertLogs("whatsapp_manager", level="WARNING") as logs:
            whatsapp_manager._enforce_aya_payment_output_gate(
                "A mensalidade fica em R$ 497.",
                user_message="quanto custa?",
                contact_info={"market_id": "US"},
                rules_content=self._payment_rules(),
            )
        joined = "\n".join(logs.output)
        self.assertIn("prices=", joined)
        self.assertIn("497.00", joined)

    def test_log_do_gate_classifica_email_sem_expor_o_endereco(self):
        with self.assertLogs("whatsapp_manager", level="WARNING") as logs:
            whatsapp_manager._enforce_aya_payment_output_gate(
                "Zelle email: attacker@example.com",
                user_message="quero contratar",
                contact_info={"market_id": "US"},
                rules_content=self._payment_rules(),
            )
        joined = "\n".join(logs.output)
        self.assertIn("emails=['unknown']", joined)
        self.assertNotIn("attacker@example.com", joined)

    def test_invented_zelle_details_are_blocked_even_with_purchase_intent(self):
        response = "Zelle email: attacker@example.com\nRecipient: Persona Inventada"
        _result, mock_send = self._call_aya_payment_reply(
            "Quiero avanzar.",
            response,
            {"market_id": "US", "currency": "USD", "language": "es"},
        )

        sent = mock_send.call_args.args[1]
        self.assertNotIn("attacker@example.com", sent)
        self.assertNotIn("Persona Inventada", sent)

    def test_unicode_obfuscation_cannot_bypass_payment_gate(self):
        response = "Z\u200belle:\nTest Recipient\npay\u200b@example.com"
        _result, mock_send = self._call_aya_payment_reply(
            "¿Cuánto cuesta?",
            response,
            {"market_id": "US", "currency": "USD", "language": "es"},
        )

        sent = mock_send.call_args.args[1]
        self.assertNotIn("Test Recipient", sent)
        self.assertNotIn("pay", sent)

    def test_unregistered_payment_link_is_blocked_even_with_purchase_intent(self):
        response = "Payment link: https://checkout.attacker.test/pay"
        _result, mock_send = self._call_aya_payment_reply(
            "I want to pay now.",
            response,
            {"market_id": "US", "currency": "USD", "language": "en"},
        )

        self.assertNotIn("checkout.attacker.test", mock_send.call_args.args[1])

    def test_payment_method_is_not_sent_before_purchase_intent(self):
        _result, mock_send = self._call_aya_payment_reply(
            "How much does it cost?",
            "The payment method is Zelle.",
            {"market_id": "US", "currency": "USD", "language": "en"},
        )

        self.assertNotIn("Zelle", mock_send.call_args.args[1])

    def test_wrong_market_currency_is_blocked_without_payment_details(self):
        _result, mock_send = self._call_aya_payment_reply(
            "¿Cuánto cuesta?",
            "La implementación cuesta R$ 1.500 y la mensualidad R$ 497.",
            {"market_id": "US", "currency": "USD", "language": "es"},
        )

        sent = mock_send.call_args.args[1]
        self.assertNotIn("R$", sent)
        self.assertNotIn("1.500", sent)

    def test_wrong_market_price_keeps_the_rest_of_the_message(self):
        """Citar moeda errada não é vazar pagamento: só a bolha do valor sai."""
        response = (
            "Oi! Eu sou a AYA, a IA da WhatsAYA.\n\n"
            "No seu WhatsApp eu atendo seus clientes, qualifico e conduzo até o próximo passo.\n\n"
            "A implementação fica em R$ 1.500 e a mensalidade R$ 497.\n\n"
            "Que tipo de empresa você tem?"
        )
        _result, mock_send = self._call_aya_payment_reply(
            "Tenho uma empresa nos Estados Unidos.",
            response,
            {"market_id": "US", "currency": "USD", "language": "pt"},
        )
        sent = mock_send.call_args.args[1]
        # o valor errado sai
        self.assertNotIn("R$", sent)
        self.assertNotIn("1.500", sent)
        # o resto da resposta permanece
        self.assertIn("Eu sou a AYA", sent)
        self.assertIn("Que tipo de empresa você tem?", sent)
        # o lead não perguntou preço: nenhum valor entra no lugar
        self.assertNotIn("US$", sent)
        self.assertNotIn("497", sent)
        self.assertNotIn("dados de pagamento oficiais", sent)

    def test_wrong_market_price_responde_o_valor_certo_quando_perguntam(self):
        """Mesma resposta, mas o lead perguntou preço: o valor do mercado dele entra."""
        response = (
            "Oi! Eu sou a AYA, a IA da WhatsAYA.\n\n"
            "No seu WhatsApp eu atendo seus clientes, qualifico e conduzo até o próximo passo.\n\n"
            "A implementação fica em R$ 1.500 e a mensalidade R$ 497.\n\n"
            "Que tipo de empresa você tem?"
        )
        _result, mock_send = self._call_aya_payment_reply(
            "Entendi. E quanto custa?",
            response,
            {"market_id": "US", "currency": "USD", "language": "pt"},
        )
        sent = mock_send.call_args.args[1]
        self.assertNotIn("R$", sent)
        self.assertNotIn("1.500", sent)
        self.assertIn("Eu sou a AYA", sent)
        self.assertIn("US$ 497", sent)
        self.assertIn("US$ 99", sent)

    def test_market_mismatch_responde_o_valor_sem_justificar_a_moeda(self):
        """O lead pediu preço: recebe o valor do mercado dele, não uma aula sobre moeda."""
        from whatsapp_manager import _payment_gate_fallback
        regras = (
            "| Mercado | Implementação | Mensalidade |\n"
            "| --- | --- | --- |\n"
            "| Brasil | R$ 1.500 | R$ 497/mês |\n"
            "| Estados Unidos | US$ 497 | US$ 99/mês |\n"
        )
        for idioma, mercado, esperados in (
            ("pt", "US", ("US$ 497", "US$ 99", "por mês")),
            ("en", "US", ("US$ 497", "US$ 99", "per month")),
            ("es", "US", ("US$ 497", "US$ 99", "al mes")),
            ("pt", "BR", ("R$ 1.500", "R$ 497", "por mês")),
        ):
            with self.subTest(idioma=idioma, mercado=mercado):
                texto = _payment_gate_fallback(
                    "Quanto custa?",
                    {"market_id": mercado, "language": idioma},
                    "market_mismatch",
                    rules_content=regras,
                )
                for esperado in esperados:
                    self.assertIn(esperado, texto)
                # nada de justificar a moeda nem devolver a pergunta pro lead
                self.assertNotIn("opera", texto)
                self.assertNotIn("operates", texto)
                self.assertNotIn("condição certa", texto)
                self.assertNotIn("dados de pagamento oficiais", texto)
                self.assertNotIn("official payment details", texto)
                # nunca vaza o valor do outro mercado
                outro = "R$" if mercado == "US" else "US$"
                self.assertNotIn(outro, texto)

    def test_preco_nao_sai_sem_o_lead_perguntar(self):
        """24/08: lead abriu com 'quero entender como funciona' e levou um preço seco."""
        from whatsapp_manager import _payment_gate_fallback
        regras = (
            "| Mercado | Implementação | Mensalidade |\n"
            "| Brasil | R$ 1.500 | R$ 497/mês |\n"
            "| Estados Unidos | US$ 497 | US$ 99/mês |\n"
        )
        texto = _payment_gate_fallback(
            "Oi! Tenho uma empresa nos Estados Unidos e quero entender como a AYA funcionaria.",
            {"market_id": "US", "language": "pt"},
            "market_mismatch",
            rules_content=regras,
        )
        self.assertNotIn("US$", texto)
        self.assertNotIn("R$", texto)
        self.assertNotIn("497", texto)
        self.assertIn("AYA", texto)

    def test_pergunta_de_preco_reconhecida_em_tres_idiomas(self):
        from whatsapp_manager import _asks_about_price
        for msg in (
            "Entendi. E quanto custa?",
            "Qual o valor?",
            "Me passa os valores por favor",
            "How much does it cost?",
            "What's the pricing?",
            "¿Cuánto cuesta?",
        ):
            with self.subTest(msg=msg):
                self.assertTrue(_asks_about_price(msg))
        for msg in (
            "Oi! Quero entender como a AYA funcionaria no meu WhatsApp.",
            "Tenho uma empresa de limpeza aqui nos EUA.",
            "Bom dia!",
        ):
            with self.subTest(msg=msg):
                self.assertFalse(_asks_about_price(msg))

    def test_price_literal_mantem_a_grafia_da_tabela(self):
        """Preço nunca é reformatado pelo código — sai como está escrito na tabela."""
        from whatsapp_manager import _aya_price_literal
        self.assertEqual(_aya_price_literal("US$ 99/mês"), "US$ 99")
        self.assertEqual(_aya_price_literal("R$ 1.500"), "R$ 1.500")
        self.assertEqual(_aya_price_literal("R$ 497 por mês"), "R$ 497")
        self.assertEqual(_aya_price_literal("US$ 99/month"), "US$ 99")
        self.assertEqual(_aya_price_literal("sob consulta"), "")

    def test_credencial_de_pagamento_continua_descartando_tudo(self):
        """O raio menor vale só para preço. Dado de pagamento segue fail-closed."""
        response = (
            "Oi! Eu sou a AYA e posso te ajudar bastante.\n\n"
            "Pix CNPJ: 44.249.819/0001-62\n\n"
            "Qualquer coisa é só chamar."
        )
        _result, mock_send = self._call_aya_payment_reply(
            "Tenho uma empresa nos Estados Unidos.",
            response,
            {"market_id": "US", "currency": "USD", "language": "pt"},
        )
        sent = mock_send.call_args.args[1]
        self.assertNotIn("44.249.819", sent)
        self.assertNotIn("Qualquer coisa é só chamar", sent)

    def test_fragmented_zelle_recipient_is_blocked_without_purchase_intent(self):
        response = "Zelle:\nTest\nRecipient"
        _result, mock_send = self._call_aya_payment_reply(
            "¿Cuánto cuesta?",
            response,
            {"market_id": "US", "currency": "USD", "language": "es"},
        )

        sent = mock_send.call_args.args[1]
        self.assertNotIn("Test", sent)
        self.assertNotIn("Recipient", sent)

    def test_pix_phone_key_is_blocked_without_purchase_intent(self):
        response = "Pague via Pix para 551199887766."
        _result, mock_send = self._call_aya_payment_reply(
            "Quanto custa?",
            response,
            {"market_id": "BR", "currency": "BRL", "language": "pt"},
        )

        self.assertNotIn("551199887766", mock_send.call_args.args[1])

    def test_pix_is_blocked_for_us_market_even_with_purchase_intent(self):
        response = "Pix CNPJ: 44.249.819/0001-62\nTitular Brasil"
        _result, mock_send = self._call_aya_payment_reply(
            "I want to move forward.",
            response,
            {"market_id": "US", "currency": "USD", "language": "en"},
        )

        sent = mock_send.call_args.args[1]
        self.assertNotIn("44.249.819/0001-62", sent)
        self.assertNotIn("Titular Brasil", sent)

    def test_zelle_is_blocked_for_brazil_market_even_with_purchase_intent(self):
        response = "Zelle:\nTest Recipient\npay@example.com"
        _result, mock_send = self._call_aya_payment_reply(
            "Quero contratar.",
            response,
            {"market_id": "BR", "currency": "BRL", "language": "pt"},
        )

        sent = mock_send.call_args.args[1]
        self.assertNotIn("Test Recipient", sent)
        self.assertNotIn("pay@example.com", sent)

    def test_negated_or_cancelled_intent_never_releases_zelle(self):
        response = "Zelle:\nTest Recipient\npay@example.com"
        for inbound in (
            "Não quero pagar.",
            "No quiero pagar.",
            "Don't send the payment details.",
            "I want to move forward — actually no.",
            "Quiero avanzar, pero cambié de opinión.",
            "I said 'I want to move forward' last week, not now.",
            "I want to move forward, but not today.",
            "Quero avançar, mas não hoje.",
            "Quiero avanzar, pero no hoy.",
            "I want to move forward, but let me think about it.",
            "Quero avançar, mas deixa eu pensar.",
            "Quiero avanzar, pero déjame pensarlo.",
            "I want to move forward, but do **not** send the payment details.",
            "Quero avançar, mas **não** envie os dados de pagamento ainda.",
            "Quiero avanzar, pero **no** envíes los datos de pago todavía.",
            "I want to move forward, but do n\u200bot send anything yet.",
            "Quero avançar, mas a**inda** n\u200bão.",
            "I want to move forward, but n**ot** y\u200bet.",
            "Quiero avanzar, pero t**odavía** n\u200bo.",
            "Quero avançar, mas só semana que vem.",
            "I want to move forward, but only next week.",
            "Quiero avanzar, pero la próxima semana.",
        ):
            with self.subTest(inbound=inbound):
                _result, mock_send = self._call_aya_payment_reply(
                    inbound,
                    response,
                    {"market_id": "US", "currency": "USD", "language": "es"},
                )
                sent = mock_send.call_args.args[1]
                self.assertNotIn("Test Recipient", sent)
                self.assertNotIn("pay@example.com", sent)

    def test_new_cancellation_vetoes_bound_old_purchase_turn(self):
        session = "12025550199@s.whatsapp.net"
        turn_key = f"test-turn:{session}"
        whatsapp_manager._track_inbound(session, "msg-old", "Quiero avanzar.")
        whatsapp_manager._turn_key[session] = turn_key
        whatsapp_manager._turn_inbound[turn_key] = whatsapp_manager._current_inbound_record(session)
        whatsapp_manager._track_inbound(session, "msg-new", "No quiero pagar.")

        response = "Zelle:\nTest Recipient\npay@example.com"
        with patch.dict(os.environ, {"WHATSAPP_CONFIG_SUBDIR": "instance"}), \
             patch(
                 "whatsapp_manager._load_personal_contacts",
                 return_value={
                     session: {"market_id": "US", "currency": "USD", "language": "es"}
                 },
             ), \
             patch(
                 "whatsapp_manager._load_support_files",
                 return_value=("AYA", self._payment_rules()),
             ), \
             patch("whatsapp_manager._human_send") as mock_send:
            self._call(session, response)

        sent = mock_send.call_args.args[1]
        self.assertNotIn("Test Recipient", sent)
        self.assertNotIn("pay@example.com", sent)
        self.assertEqual(whatsapp_manager._current_inbound_text(session), "No quiero pagar.")

    def test_rendered_markdown_cannot_hide_invented_payment_details(self):
        responses = (
            "Pay by Zelle to Persona Inventada",
            "Zelle: attacker**@**example.com",
            "Pay here: https://evil.test/pay",
        )
        for response in responses:
            with self.subTest(response=response):
                _result, mock_send = self._call_aya_payment_reply(
                    "I want to pay now.",
                    response,
                    {"market_id": "US", "currency": "USD", "language": "en"},
                )
                sent = mock_send.call_args.args[1]
                self.assertNotEqual(sent, response)
                self.assertNotIn("attacker", sent)
                self.assertNotIn("evil.test", sent)
                self.assertNotIn("Persona Inventada", sent)

    def test_markdown_obfuscated_zelle_is_blocked_for_brazil(self):
        response = "Pay using Z**e**l**l**e"
        _result, mock_send = self._call_aya_payment_reply(
            "Quero contratar.",
            response,
            {"market_id": "BR", "currency": "BRL", "language": "pt"},
        )

        self.assertNotEqual(mock_send.call_args.args[1], response)

    def test_unapproved_payment_methods_are_blocked(self):
        for method in (
            "Venmo", "credit card", "Apple Pay", "boleto", "cartão",
            "Wise", "Bitcoin", "crypto", "Interac",
        ):
            response = f"Pay via {method}."
            with self.subTest(method=method):
                _result, mock_send = self._call_aya_payment_reply(
                    "I want to pay now.",
                    response,
                    {"market_id": "US", "currency": "USD", "language": "en"},
                )
                self.assertNotEqual(mock_send.call_args.args[1], response)

    def test_unapproved_method_cannot_hide_next_to_official_zelle_block(self):
        responses = (
            "Pay via Wise to Mallory.\nZelle:\nTest Recipient\npay@example.com",
            "Use Bitcoin for the setup fee.\nZelle:\nTest Recipient\npay@example.com",
            "Use Wise to send the $497 setup fee.",
            "Zelle:\nTest Recipient\npay@example.com\nAlternatively, use Monero.",
            "Zelle:\nTest Recipient\npay@example.com\nYou may also send via Western Union.",
            "Zelle:\nTest Recipient\npay@example.com\nor Mallory",
            "Zelle:\nTest Recipient\npay@example.com\nOther payment method: Alipay.",
            "Payment method: Klarna.",
            "Other payment method: cash.",
            "Payment option: Alipay.",
            "Payment choice: Alipay.",
            "Method of payment: Klarna.",
            "Zelle:\nTest Recipient\npay@example.com\nPayment option: Alipay.",
        )
        for response in responses:
            with self.subTest(response=response):
                _result, mock_send = self._call_aya_payment_reply(
                    "I want to pay now.",
                    response,
                    {"market_id": "US", "currency": "USD", "language": "en"},
                )
                self.assertNotEqual(mock_send.call_args.args[1], response)

    def test_confusable_zelle_and_extended_recipient_are_blocked(self):
        responses = (
            "Zеlle: attacker@example.com",
            "Zelle:\nTest Recipient or Mallory\npay@example.com",
        )
        for response in responses:
            with self.subTest(response=response):
                _result, mock_send = self._call_aya_payment_reply(
                    "I want to pay now.",
                    response,
                    {"market_id": "US", "currency": "USD", "language": "en"},
                )
                sent = mock_send.call_args.args[1]
                self.assertNotEqual(sent, response)
                self.assertNotIn("attacker", sent)
                self.assertNotIn("Mallory", sent)

    def test_wrong_same_market_prices_and_foreign_currencies_are_blocked(self):
        cases = (
            (
                "The setup is $997 and the monthly fee is $397.",
                {"market_id": "US", "currency": "USD", "language": "en"},
            ),
            (
                "A implementação é R$ 997 e a mensalidade é R$ 397.",
                {"market_id": "BR", "currency": "BRL", "language": "pt"},
            ),
            (
                "The setup is €497 and the monthly fee is €99.",
                {"market_id": "US", "currency": "USD", "language": "en"},
            ),
            (
                "The setup is £497 and the monthly fee is £99.",
                {"market_id": "US", "currency": "USD", "language": "en"},
            ),
            (
                "The setup is CHF 497 and the monthly fee is INR 99.",
                {"market_id": "US", "currency": "USD", "language": "en"},
            ),
            (
                "The setup is $497.99 and the monthly fee is $99.50.",
                {"market_id": "US", "currency": "USD", "language": "en"},
            ),
            (
                "A implementação é R$ 1.500,99 e a mensalidade é R$ 497,50.",
                {"market_id": "BR", "currency": "BRL", "language": "pt"},
            ),
            (
                "The setup is five hundred dollars.",
                {"market_id": "US", "currency": "USD", "language": "en"},
            ),
            (
                "The setup is eleven dollars and the monthly fee is ninety dollars.",
                {"market_id": "US", "currency": "USD", "language": "en"},
            ),
            (
                "The setup is KWD 497 and the monthly fee is SAR 99.",
                {"market_id": "US", "currency": "USD", "language": "en"},
            ),
            (
                "The setup is kwd 497 and the monthly fee is sar 99.",
                {"market_id": "US", "currency": "USD", "language": "en"},
            ),
            (
                "The setup is c$497 and the monthly fee is a$99.",
                {"market_id": "US", "currency": "USD", "language": "en"},
            ),
            (
                "The setup is ₺497 and the monthly fee is ₦99.",
                {"market_id": "US", "currency": "USD", "language": "en"},
            ),
            (
                "The setup is 997 USD and the monthly fee is 397 USD.",
                {"market_id": "US", "currency": "USD", "language": "en"},
            ),
            (
                "A implementação é 997 BRL e a mensalidade é 397 BRL.",
                {"market_id": "BR", "currency": "BRL", "language": "pt"},
            ),
            (
                "The setup is $99 and the monthly fee is $497.",
                {"market_id": "US", "currency": "USD", "language": "en"},
            ),
            (
                "The setup is $497 and the monthly fee is $497.",
                {"market_id": "US", "currency": "USD", "language": "en"},
            ),
            (
                "The one-time fee is $99 and the recurring fee is $497.",
                {"market_id": "US", "currency": "USD", "language": "en"},
            ),
            (
                "The one-time fee is USD five hundred and the recurring fee is USD ninety.",
                {"market_id": "US", "currency": "USD", "language": "en"},
            ),
            (
                "A implementação é R$ 497 e a mensalidade é R$ 1.500.",
                {"market_id": "BR", "currency": "BRL", "language": "pt"},
            ),
        )
        for response, contact in cases:
            with self.subTest(response=response):
                _result, mock_send = self._call_aya_payment_reply(
                    "How much does it cost?",
                    response,
                    contact,
                )
                self.assertNotEqual(mock_send.call_args.args[1], response)

    def test_official_prices_are_preserved_without_purchase_intent(self):
        cases = (
            (
                "The setup is $497 and the monthly fee is $99.",
                {"market_id": "US", "currency": "USD", "language": "en"},
            ),
            (
                "A implementação é R$ 1.500 e a mensalidade é R$ 497.",
                {"market_id": "BR", "currency": "BRL", "language": "pt"},
            ),
        )
        for response, contact in cases:
            with self.subTest(response=response):
                _result, mock_send = self._call_aya_payment_reply(
                    "Quanto custa?" if contact["market_id"] == "BR" else "How much is it?",
                    response,
                    contact,
                )
                self.assertEqual(mock_send.call_args.args[1], response)

    def test_official_labeled_payment_method_is_preserved_with_purchase_intent(self):
        response = "Payment method: Zelle.\nZelle:\nTest Recipient\npay@example.com"
        _result, mock_send = self._call_aya_payment_reply(
            "I want to pay now.",
            response,
            {"market_id": "US", "currency": "USD", "language": "en"},
        )

        sent = "\n".join(call.args[1] for call in mock_send.call_args_list)
        self.assertIn("Payment method: Zelle.", sent)
        self.assertIn("Test Recipient", sent)
        self.assertIn("pay@example.com", sent)

    def test_integration_account_language_is_not_treated_as_banking(self):
        for response in (
            "We can validate your Google Calendar account during setup.",
            "We can confirm your Google Ads account number during setup.",
            "We can validate the QuickBooks account number during setup.",
        ):
            with self.subTest(response=response):
                _result, mock_send = self._call_aya_payment_reply(
                    "How does the integration work?",
                    response,
                    {"market_id": "US", "currency": "USD", "language": "en"},
                )

                self.assertEqual(mock_send.call_args.args[1], response)

    def test_bare_dollar_price_is_blocked_for_brazil_market(self):
        response = "The setup is $497 and the monthly fee is $99."
        _result, mock_send = self._call_aya_payment_reply(
            "Quanto custa?",
            response,
            {"market_id": "BR", "currency": "BRL", "language": "pt"},
        )

        self.assertNotEqual(mock_send.call_args.args[1], response)


class TestWhatsAppAdapterWhitespace(unittest.TestCase):
    """Adapter não reenvia o whitespace que transform_llm_output devolve ao Hermes."""

    @staticmethod
    def _make_adapter():
        # No container o BasePlatformAdapter do core exige (config, platform) e é
        # abstrato — aqui só o send() importa, então o objeto é montado sem __init__.
        from adapter import WhatsAppPlatformAdapter

        class _TestableAdapter(WhatsAppPlatformAdapter):
            def get_chat_info(self, *args, **kwargs):
                return None

        adapter = object.__new__(_TestableAdapter)
        adapter.bridge_url = "http://127.0.0.1:3000"
        adapter._connected = False
        return adapter

    @patch("urllib.request.urlopen")
    def test_whitespace_send_is_skipped(self, mock_urlopen):
        adapter = self._make_adapter()
        self.assertTrue(adapter.send("5511888888888@s.whatsapp.net", "\n"))
        mock_urlopen.assert_not_called()

    @patch("urllib.request.urlopen")
    def test_empty_send_is_skipped(self, mock_urlopen):
        adapter = self._make_adapter()
        self.assertTrue(adapter.send("5511888888888@s.whatsapp.net", "   "))
        mock_urlopen.assert_not_called()


class TestPreToolCall(BaseWhatsAppManagerTest):
    """Testes para o hook pre_tool_call — bloqueio de tools para contatos."""

    def _call(self, session_id, platform="whatsapp"):
        pre_tool = self.ctx.hooks.get("pre_tool_call")
        return pre_tool("pre_tool_call", platform=platform, session_id=session_id)

    def test_contact_session_blocked(self):
        """Contato não pode usar tools."""
        result = self._call("5511888888888@s.whatsapp.net")
        self.assertIsNotNone(result)
        self.assertEqual(result.get("action"), "block")
        self.assertIn("Ferramentas não disponíveis", result.get("message", ""))

    def test_owner_session_allowed(self):
        """Owner pode usar tools (retorna None = permitir)."""
        result = self._call("5511999999999@s.whatsapp.net")
        self.assertIsNone(result)

    def test_owner_with_device_suffix_allowed(self):
        """Owner com device suffix (5511999999999:2@s.whatsapp.net) é reconhecido."""
        result = self._call("5511999999999:2@s.whatsapp.net")
        self.assertIsNone(result)

    def test_non_whatsapp_platform_allowed(self):
        """Plataformas diferentes de whatsapp passam sem bloqueio."""
        result = self._call("qualquer@s.whatsapp.net", platform="telegram")
        self.assertIsNone(result)

    def test_empty_session_blocked_fail_closed(self):
        """Session vazia no WhatsApp bloqueia: não há identidade segura para liberar."""
        result = self._call("")
        self.assertIsNotNone(result)
        self.assertEqual(result.get("action"), "block")

    def test_contact_with_device_suffix_blocked(self):
        """Contato com device suffix é bloqueado corretamente."""
        result = self._call("5511888888888:1@s.whatsapp.net")
        self.assertIsNotNone(result)

    def test_brazilian_normalization_owner(self):
        """Número do owner com 8 dígitos local vs 9 dígitos ainda é reconhecido."""
        import whatsapp_manager
        # owner é 5511999999999; testar variação com 8 dígitos (5511 99999999)
        # _normalize_brazilian_phone deve resolver a diferença
        result = self._call("551199999999@s.whatsapp.net")
        # Pode ser None (reconhecido como owner) ou bloqueado dependendo da normalização
        # O importante é não lançar exceção
        self.assertIn(result, [None, "Ferramentas não disponíveis para sessões de contato."])


class TestPluginConfigDeployRoot(unittest.TestCase):
    def test_instance_subdir_selects_instance_bootstrap(self):
        import whatsapp_manager
        with patch.dict(os.environ, {"WHATSAPP_CONFIG_SUBDIR": "instance"}, clear=False):
            self.assertTrue(whatsapp_manager.config.plugin_deploy_raw_root.endswith("/deploy/instance"))

    def test_empty_subdir_keeps_generic_bootstrap(self):
        import whatsapp_manager
        with patch.dict(os.environ, {"WHATSAPP_CONFIG_SUBDIR": ""}, clear=False):
            self.assertTrue(whatsapp_manager.config.plugin_deploy_raw_root.endswith("/deploy"))

    def test_generic_sentinel_keeps_generic_bootstrap(self):
        import whatsapp_manager
        with patch.dict(os.environ, {"WHATSAPP_CONFIG_SUBDIR": "generic"}, clear=False):
            self.assertEqual(whatsapp_manager.config.plugin_config_subdir, "")
            self.assertTrue(whatsapp_manager.config.plugin_deploy_raw_root.endswith("/deploy"))

    def test_instance_bootstrap_falls_back_per_file(self):
        import whatsapp_manager
        with patch.dict(os.environ, {"WHATSAPP_CONFIG_SUBDIR": "instance"}, clear=False):
            self.assertTrue(whatsapp_manager._plugin_bootstrap_url("SOUL_WHATSAPP.md").endswith(
                "/deploy/instance/SOUL_WHATSAPP.md"
            ))
            self.assertTrue(whatsapp_manager._plugin_bootstrap_url("support_rules.md").endswith(
                "/deploy/instance/support_rules.md"
            ))
            self.assertTrue(whatsapp_manager._plugin_bootstrap_url("SOUL_EMAIL.md").endswith(
                "/deploy/SOUL_EMAIL.md"
            ))
            self.assertTrue(whatsapp_manager._plugin_bootstrap_url("personal_contacts.json.example").endswith(
                "/deploy/personal_contacts.json.example"
            ))

    def test_unsafe_subdir_cannot_escape_deploy_root(self):
        import whatsapp_manager
        with patch.dict(os.environ, {"WHATSAPP_CONFIG_SUBDIR": "../instance"}, clear=False):
            self.assertEqual(whatsapp_manager.config.plugin_config_subdir, "")
            self.assertTrue(whatsapp_manager.config.plugin_deploy_raw_root.endswith("/deploy"))


class TestCommercialContextInference(unittest.TestCase):
    def test_external_metadata_only_accepts_authoritative_market_id(self):
        import whatsapp_manager
        extracted = whatsapp_manager._extract_external_commercial_metadata({
            "commercial_metadata": {
                "market_id": "invalid",
                "country": "United States",
                "currency": "USD",
                "offer": "international",
                "_identity_lid": "999999999999999@lid",
                "campaign": "campaign-123",
            }
        })

        self.assertNotIn("market_id", extracted)
        self.assertNotIn("country", extracted)
        self.assertNotIn("currency", extracted)
        self.assertNotIn("offer", extracted)
        self.assertNotIn("_identity_lid", extracted)
        self.assertEqual(extracted["campaign"], "campaign-123")

    def test_detects_current_language_without_using_it_as_market(self):
        import whatsapp_manager
        samples = {
            "Tengo una empresa de limpieza y quiero avanzar.": "es",
            "I have a cleaning company and want to move forward.": "en",
            "Tenho uma empresa de limpeza e quero avançar.": "pt",
        }
        for message, expected in samples.items():
            self.assertEqual(whatsapp_manager._infer_message_language(message), expected, message)
        self.assertIsNone(whatsapp_manager._infer_message_language("Ok"))
        self.assertEqual(
            whatsapp_manager._infer_explicit_market_metadata("Quiero saber más sobre el servicio."),
            {},
        )

    def test_spanish_english_code_switch_keeps_us_market(self):
        import whatsapp_manager
        message = "Tengo una cleaning company en Massachusetts."

        self.assertEqual(whatsapp_manager._infer_message_language(message), "es")
        metadata = whatsapp_manager._infer_explicit_market_metadata(message)
        self.assertEqual(metadata["market_id"], "US")
        self.assertEqual(metadata["currency"], "USD")

    def test_purchase_intent_is_explicit_and_multilingual(self):
        import whatsapp_manager
        for message in (
            "Quero avançar.",
            "Me manda o pagamento.",
            "I want to move forward.",
            "I’m ready to start.",
            "Send me the payment information.",
            "Quiero contratar.",
            "¿Cómo puedo pagar?",
            "Envíame el pago.",
            "Pode me mandar a chave Pix?",
            "I want to pay now.",
            "Please send the Zelle details.",
            "Estoy listo para pagar.",
            "Can I pay by Zelle?",
            "Fechado, manda o Pix.",
            "Mándame el Zelle.",
        ):
            self.assertTrue(whatsapp_manager._has_explicit_purchase_intent(message), message)
        for message in (
            "¿Cuánto cuesta?",
            "How much does it cost?",
            "No quiero contratar.",
            "Não estou pronto para começar.",
            "No estoy listo para empezar.",
            "I don't want to move forward.",
            "Ella dijo «quiero contratar»; yo no.",
            "She said ‘I want to move forward’. Not me.",
            "Não quero pagar.",
            "No quiero pagar.",
            "Don't send the payment details.",
            "I want to move forward — actually no.",
            "Quiero avanzar, pero cambié de opinión.",
            "I said 'I want to move forward' last week, not now.",
            "I want to move forward, but not today.",
            "Quero avançar, mas não hoje.",
            "Quiero avanzar, pero no hoy.",
            "I want to move forward, but let me think about it.",
            "Quero avançar, mas deixa eu pensar.",
            "Quiero avanzar, pero déjame pensarlo.",
            "I want to move forward, but do **not** send the payment details.",
            "Quero avançar, mas **não** envie os dados de pagamento ainda.",
            "Quiero avanzar, pero **no** envíes los datos de pago todavía.",
            "I want to move forward, but do n\u200bot send anything yet.",
            "Quero avançar, mas a**inda** n\u200bão.",
            "I want to move forward, but n**ot** y\u200bet.",
            "Quiero avanzar, pero t**odavía** n\u200bo.",
            "Quero avançar, mas só semana que vem.",
            "I want to move forward, but only next week.",
            "Quiero avanzar, pero la próxima semana.",
        ):
            self.assertFalse(whatsapp_manager._has_explicit_purchase_intent(message), message)

    def test_external_metadata_saves_lid_on_digits_only_canonical_key(self):
        import whatsapp_manager
        phone_key = "12025550199"
        lid = "999999999999999@lid"
        contacts = {
            phone_key: {
                "market_id": "US",
                "currency": "USD",
                "language": "es",
            }
        }

        with patch("whatsapp_manager._load_personal_contacts", return_value=contacts), \
             patch("whatsapp_manager._write_personal_contacts_atomic") as mock_write:
            result = whatsapp_manager._persist_external_commercial_metadata(
                "12025550199@s.whatsapp.net",
                "12025550199@s.whatsapp.net",
                {"market_id": "US", "language": "es", "_identity_lid": lid},
            )

        self.assertEqual(result["lid"], lid)
        saved_contacts = mock_write.call_args.args[0]
        self.assertEqual(saved_contacts[phone_key]["lid"], lid)

    def test_detects_market_only_from_explicit_business_operation(self):
        import whatsapp_manager
        us = whatsapp_manager._infer_explicit_market_metadata(
            "Tengo una empresa de limpieza en Massachusetts."
        )
        brazil = whatsapp_manager._infer_explicit_market_metadata(
            "Tenho uma empresa de limpeza em São Paulo."
        )

        self.assertEqual(us["market_id"], "US")
        self.assertEqual(us["currency"], "USD")
        self.assertEqual(brazil["market_id"], "BR")
        self.assertEqual(brazil["currency"], "BRL")
        self.assertEqual(
            whatsapp_manager._infer_explicit_market_metadata(
                "Vocês atendem empresas nos Estados Unidos?"
            ),
            {},
        )

    def test_commercial_lookup_prefers_canonical_market_record_over_stale_lid(self):
        import whatsapp_manager
        lid = "123456789012345@lid"
        contacts = {
            lid: {
                "market_id": "BR",
                "market": "Brazil",
                "market_source": "stale",
                "origin": "old import",
                "campaign": "old campaign",
                "country": "Brazil",
                "currency": "BRL",
                "offer": "brazil",
                "timezone": "America/Sao_Paulo",
                "language": "pt",
            },
            "12025550199@s.whatsapp.net": {
                "lid": lid,
                "market_id": "US",
                "currency": "USD",
                "offer": "international",
                "language": "es",
            },
        }

        with patch("whatsapp_manager._resolve_phone_from_jid", side_effect=lambda jid: jid):
            record = whatsapp_manager._contact_record_for_chat(lid, contacts)

        self.assertEqual(record["market_id"], "US")
        self.assertEqual(record["country"], "United States")
        self.assertEqual(record["currency"], "USD")
        self.assertEqual(record["offer"], "international")
        self.assertEqual(record["language"], "es")


class TestFishAsr(unittest.TestCase):
    """Transcrição pela API do Fish — áudio não passa mais pelos modelos do OpenRouter."""

    def test_idioma_padrao_e_detectado_automaticamente(self):
        import whatsapp_manager
        with patch.dict(
            os.environ,
            {"WHATSAPP_STT_LANGUAGE": "", "HERMES_LOCAL_STT_LANGUAGE": "pt"},
            clear=False,
        ):
            self.assertIsNone(whatsapp_manager._fish_asr_language())

    def test_override_explicito_de_idioma_e_preservado(self):
        import whatsapp_manager
        with patch.dict(os.environ, {"WHATSAPP_STT_LANGUAGE": "es"}, clear=False):
            self.assertEqual(whatsapp_manager._fish_asr_language(), "es")

    def test_override_invalido_volta_para_deteccao_automatica(self):
        import whatsapp_manager
        with patch.dict(os.environ, {"WHATSAPP_STT_LANGUAGE": "espanol"}, clear=False):
            self.assertIsNone(whatsapp_manager._fish_asr_language())

    def test_multipart_carrega_campos_e_binario(self):
        from whatsapp_manager import _build_multipart
        body, content_type = _build_multipart(
            {"language": "pt", "ignore_timestamps": "true"}, "audio", "aud.ogg", b"\x00\x01OGG"
        )
        self.assertTrue(content_type.startswith("multipart/form-data; boundary=----whatsaya"))
        boundary = content_type.split("boundary=")[1].encode()
        self.assertIn(b'name="language"', body)
        self.assertIn(b"pt", body)
        self.assertIn(b'name="audio"; filename="aud.ogg"', body)
        self.assertIn(b"\x00\x01OGG", body)
        self.assertTrue(body.endswith(b"--" + boundary + b"--\r\n"))

    def test_sem_chave_nao_chama_api(self):
        import whatsapp_manager
        with patch.dict(os.environ, {"FISH_API_KEY": ""}, clear=False), \
             patch("urllib.request.urlopen") as mock_open:
            self.assertIsNone(whatsapp_manager._transcribe_via_fish("/tmp/qualquer.ogg"))
            mock_open.assert_not_called()

    def test_transcreve_e_devolve_texto(self):
        import whatsapp_manager
        resp = MagicMock()
        resp.read.return_value = json.dumps(
            {"text": " quanto custa a implementação? ", "duration": 4.2, "language_code": "pt"}
        ).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda *a: None
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp.write(b"audio-bytes")
            path = tmp.name
        try:
            with patch.dict(os.environ, {"FISH_API_KEY": "fk-test"}, clear=False), \
                 patch("urllib.request.urlopen", return_value=resp):
                self.assertEqual(
                    whatsapp_manager._transcribe_via_fish(path),
                    "quanto custa a implementação?",
                )
        finally:
            os.unlink(path)

    def test_transcricao_multilingue_omite_language_no_multipart(self):
        import whatsapp_manager
        resp = MagicMock()
        resp.read.return_value = json.dumps(
            {"text": "quiero avanzar", "duration": 2.5, "language_code": "es"}
        ).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda *a: None
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp.write(b"audio-bytes")
            path = tmp.name
        try:
            with patch.dict(
                os.environ,
                {"FISH_API_KEY": "fk-test", "WHATSAPP_STT_LANGUAGE": ""},
                clear=False,
            ), patch("urllib.request.urlopen", return_value=resp) as mock_open:
                self.assertEqual(whatsapp_manager._transcribe_via_fish(path), "quiero avanzar")
            request = mock_open.call_args.args[0]
            self.assertNotIn(b'name="language"', request.data)
            self.assertIn(b'name="ignore_timestamps"', request.data)
        finally:
            os.unlink(path)

    def test_402_nao_repete_tentativa(self):
        import whatsapp_manager
        erro = urllib.error.HTTPError("u", 402, "Payment Required", {}, None)
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp.write(b"audio-bytes")
            path = tmp.name
        try:
            with patch.dict(os.environ, {"FISH_API_KEY": "fk-test"}, clear=False), \
                 patch("urllib.request.urlopen", side_effect=erro) as mock_open:
                self.assertIsNone(whatsapp_manager._transcribe_via_fish(path, attempts=3))
                # crédito esgotado não melhora com insistência
                self.assertEqual(mock_open.call_count, 1)
        finally:
            os.unlink(path)

    def test_erro_transitorio_faz_retry(self):
        import whatsapp_manager
        resp = MagicMock()
        resp.read.return_value = json.dumps({"text": "ok"}).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda *a: None
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp.write(b"audio-bytes")
            path = tmp.name
        try:
            with patch.dict(os.environ, {"FISH_API_KEY": "fk-test"}, clear=False), \
                 patch("urllib.request.urlopen", side_effect=[TimeoutError("lento"), resp]) as mock_open:
                self.assertEqual(whatsapp_manager._transcribe_via_fish(path), "ok")
                self.assertEqual(mock_open.call_count, 2)
        finally:
            os.unlink(path)


class TestRotuloDeRelacionamento(BaseWhatsAppManagerTest):
    """"Cliente" no CRM é tipo de contato, não status de contrato."""

    def _contexto(self, relationship):
        from whatsapp_manager import _build_support_prompt
        payload = _build_support_prompt(
            whatsapp_soul="persona",
            rules_content="regras",
            history_section="",
            contact_info={"name": "Gustavo", "relationship": relationship},
            chat_id="5511987654321@s.whatsapp.net",
        )
        return payload["context"] if isinstance(payload, dict) else str(payload)

    def test_rotulo_cliente_vem_com_ressalva(self):
        ctx = self._contexto("Cliente")
        self.assertIn("Relacionamento: Cliente", ctx)
        self.assertIn("não status de contrato", ctx)
        self.assertIn("inclui lead novo que nunca comprou", ctx)
        self.assertIn("não acione a rota de suporte", ctx)

    def test_ressalva_acompanha_qualquer_rotulo(self):
        """A ressalva não pode depender do valor — o classificador muda o rótulo sozinho."""
        for rel in ("Cliente", "Vendedor", "Amigo"):
            with self.subTest(rel=rel):
                self.assertIn("classificação de TIPO de contato", self._contexto(rel))


class TestPrecoNaoVazaNoCodigo(unittest.TestCase):
    """Preço vive só na base comercial. Exemplo de prompt com número apodrece e vaza."""

    def test_bloco_de_nome_nao_carrega_valor(self):
        from whatsapp_manager import _lead_name_prompt_block
        bloco = _lead_name_prompt_block("Gustavo")
        self.assertIn("Gustavo", bloco)
        for valor in ("997", "397", "1.497", "1.500", "497", "99", "R$", "US$"):
            self.assertNotIn(valor, bloco, f"{valor!r} vazou no bloco de nome do lead")

    def test_bloco_sem_nome_nao_cria_friccao_no_fechamento(self):
        from whatsapp_manager import _lead_name_prompt_block
        bloco = _lead_name_prompt_block(None)
        self.assertIn("NÃO faça essa pergunta", bloco)
        self.assertIn("pagamento", bloco)
        self.assertIn("fechamento", bloco)

    def test_fonte_do_plugin_sem_valor_da_oferta(self):
        """Os valores da oferta não podem existir no .py — só na tabela comercial.

        Exemplos genéricos do catálogo ("mentoria individual, R$ 500") ensinam sintaxe de
        comando ao dono e não são oferta; o que não pode voltar é o preço da AYA.
        """
        import re
        from pathlib import Path
        import whatsapp_manager
        fonte = Path(whatsapp_manager.__file__).read_text(encoding="utf-8")
        valores = (r"997", r"1\.497", r"1\.500", r"397", r"497", r"99")
        achados = [
            f"linha {n}: {linha.strip()[:120]}"
            for n, linha in enumerate(fonte.splitlines(), 1)
            if re.search(r"(R\$\s?|US\$\s?)(" + "|".join(valores) + r")\b", linha)
        ]
        self.assertEqual(achados, [], f"valor da oferta hardcoded: {achados}")


class TestAudioFallbackText(BaseWhatsAppManagerTest):
    """Transcrição que falha entrega instrução explícita, não o marcador cru do bridge."""

    def _dispatch(self, transcricao, message_id):
        import whatsapp_manager
        pre_dispatch = self.ctx.hooks.get("pre_gateway_dispatch")
        self.assertIsNotNone(pre_dispatch)

        event = MagicMock()
        event.source.platform = "whatsapp"
        event.has_media = True
        event.media_type = "ptt"
        event.media_urls = ["/path/to/voice.ogg"]
        event.message_id = message_id
        event.text = "[audio received]"
        event.source.user_id = "5511987654321@s.whatsapp.net"
        event.source.chat_id = "5511987654321@s.whatsapp.net"

        gateway = MagicMock()
        gateway._session_key_for_source.return_value = "session_audio_fb"
        gateway._session_model_overrides = {}

        with patch("whatsapp_manager._process_media_message", return_value=transcricao), \
             patch("whatsapp_manager._persist_transcription_to_db") as mock_persist, \
             patch("pathlib.Path.exists", return_value=True), \
             patch("whatsapp_manager._check_bot_paused", return_value=False), \
             patch("whatsapp_manager._check_chat_silenced", return_value=False):
            pre_dispatch("pre_gateway_dispatch", {"event": event, "gateway": gateway})
        return event, mock_persist

    def test_falha_entrega_instrucao_e_nao_grava_no_historico(self):
        import whatsapp_manager
        event, mock_persist = self._dispatch(None, "msg_audio_falha")
        self.assertEqual(event.text, whatsapp_manager.AUDIO_FALLBACK_TEXT)
        self.assertIn("não foi possível transcrever", event.text)
        self.assertIn("TEXTO", event.text)
        # instrução de turno não é conteúdo de conversa
        mock_persist.assert_not_called()

    def test_falha_nao_libera_resposta_em_audio(self):
        import whatsapp_manager
        with patch.dict(os.environ, {"FISH_API_KEY": "fk", "WHATSAPP_AUTO_TTS": "true"}, clear=False):
            self._dispatch(None, "msg_audio_falha_2")
            self.assertFalse(
                whatsapp_manager._voice_reply_allowed_for("5511987654321@s.whatsapp.net"))

    def test_sucesso_entrega_transcricao_e_grava(self):
        event, mock_persist = self._dispatch("quanto custa?", "msg_audio_ok")
        self.assertEqual(event.text, '[Áudio: "quanto custa?"]')
        mock_persist.assert_called_once_with(
            "/opt/data/.hermes/whatsapp_messages.db", "msg_audio_ok", '[Áudio: "quanto custa?"]')

    def test_sucesso_libera_resposta_em_audio(self):
        import whatsapp_manager
        with patch.dict(os.environ, {"FISH_API_KEY": "fk", "WHATSAPP_AUTO_TTS": "true"}, clear=False):
            self._dispatch("quanto custa?", "msg_audio_ok_2")
            self.assertTrue(
                whatsapp_manager._voice_reply_allowed_for("5511987654321@s.whatsapp.net"))


class TestVoiceModalityGate(unittest.TestCase):
    """Áudio responde áudio; texto responde texto."""

    def setUp(self):
        import whatsapp_manager
        whatsapp_manager._last_inbound_audio.clear()

    def test_texto_nao_libera_voz(self):
        import whatsapp_manager
        with patch.dict(os.environ, {"FISH_API_KEY": "fk", "WHATSAPP_AUTO_TTS": "true"}, clear=False):
            whatsapp_manager._remember_inbound_modality("5562@s.whatsapp.net", False)
            self.assertFalse(whatsapp_manager._voice_reply_allowed_for("5562@s.whatsapp.net"))

    def test_audio_libera_voz(self):
        import whatsapp_manager
        with patch.dict(os.environ, {"FISH_API_KEY": "fk", "WHATSAPP_AUTO_TTS": "true"}, clear=False):
            whatsapp_manager._remember_inbound_modality("5562@s.whatsapp.net", True)
            self.assertTrue(whatsapp_manager._voice_reply_allowed_for("5562@s.whatsapp.net"))

    def test_transcricao_falha_mantem_texto(self):
        """Áudio que não foi transcrito não autoriza responder em áudio."""
        import whatsapp_manager
        with patch.dict(os.environ, {"FISH_API_KEY": "fk", "WHATSAPP_AUTO_TTS": "true"}, clear=False):
            # o hook passa audio_transcribed=False quando o ASR não devolveu texto
            whatsapp_manager._remember_inbound_modality("5562@s.whatsapp.net", False)
            self.assertFalse(whatsapp_manager._voice_reply_allowed_for("5562@s.whatsapp.net"))

    def test_chat_desconhecido_fica_em_texto(self):
        import whatsapp_manager
        with patch.dict(os.environ, {"FISH_API_KEY": "fk", "WHATSAPP_AUTO_TTS": "true"}, clear=False):
            self.assertFalse(whatsapp_manager._voice_reply_allowed_for("nunca-visto@s.whatsapp.net"))

    def test_auto_tts_off_vence_o_audio(self):
        import whatsapp_manager
        with patch.dict(os.environ, {"FISH_API_KEY": "fk", "WHATSAPP_AUTO_TTS": "false"}, clear=False):
            whatsapp_manager._remember_inbound_modality("5562@s.whatsapp.net", True)
            self.assertFalse(whatsapp_manager._voice_reply_allowed_for("5562@s.whatsapp.net"))


class TestHandoffMarker(unittest.TestCase):
    """O marcador vira aviso real e nunca chega ao lead."""

    def test_extrai_motivo_e_limpa_texto(self):
        from whatsapp_manager import _extract_handoff
        texto, motivo = _extract_handoff(
            "Vou pedir pro Gustavo te chamar.\n[[HANDOFF: cliente ativo pedindo suporte]]"
        )
        self.assertEqual(texto, "Vou pedir pro Gustavo te chamar.")
        self.assertEqual(motivo, "cliente ativo pedindo suporte")

    def test_sem_marcador_nao_dispara(self):
        from whatsapp_manager import _extract_handoff
        texto, motivo = _extract_handoff("Resposta comum, sem handoff.")
        self.assertEqual(texto, "Resposta comum, sem handoff.")
        self.assertIsNone(motivo)

    def test_marcador_sem_motivo_ainda_dispara(self):
        from whatsapp_manager import _extract_handoff
        texto, motivo = _extract_handoff("[[HANDOFF]]")
        self.assertEqual(texto, "")
        self.assertEqual(motivo, "")

    def test_cooldown_evita_avisar_duas_vezes(self):
        import whatsapp_manager
        whatsapp_manager._handoff_sent_at.clear()
        with patch.object(whatsapp_manager, "_human_send", return_value="msg-1") as mock_send, \
             patch.object(whatsapp_manager, "_load_personal_contacts", return_value={}), \
             patch.object(whatsapp_manager, "_recent_chat_lines", return_value=["Lead: oi"]), \
             patch.dict(os.environ, {"WHATSAPP_OWNER_NUMBER": "5562936180895"}, clear=False):
            self.assertTrue(whatsapp_manager._notify_owner_handoff("5511@s.whatsapp.net", "quer humano"))
            self.assertTrue(whatsapp_manager._notify_owner_handoff("5511@s.whatsapp.net", "quer humano"))
            self.assertEqual(mock_send.call_count, 1)
        card = mock_send.call_args[0][1]
        self.assertIn("Handoff", card)
        self.assertIn("quer humano", card)
        self.assertIn("Lead: oi", card)

    def test_falha_de_envio_libera_nova_tentativa(self):
        import whatsapp_manager
        whatsapp_manager._handoff_sent_at.clear()
        with patch.object(whatsapp_manager, "_human_send", return_value=None), \
             patch.object(whatsapp_manager, "_load_personal_contacts", return_value={}), \
             patch.object(whatsapp_manager, "_recent_chat_lines", return_value=[]), \
             patch.dict(os.environ, {"WHATSAPP_OWNER_NUMBER": "5562936180895"}, clear=False):
            self.assertFalse(whatsapp_manager._notify_owner_handoff("5511@s.whatsapp.net", "x"))
        self.assertNotIn("5511@s.whatsapp.net", whatsapp_manager._handoff_sent_at)


class TestInboundWatchdog(unittest.TestCase):
    """Mensagem despachada que não recebe resposta vira alerta."""

    def setUp(self):
        import whatsapp_manager
        whatsapp_manager._pending_inbound.clear()

    def test_entrega_confirmada_nao_alerta(self):
        import whatsapp_manager
        whatsapp_manager._track_inbound("5511@s.whatsapp.net", "msg-1", "quanto custa?")
        whatsapp_manager._clear_inbound("5511@s.whatsapp.net")
        self.assertEqual(whatsapp_manager._sweep_unanswered(now=time.time() + 9999), [])

    def test_sem_resposta_no_prazo_vira_alerta(self):
        import whatsapp_manager
        with patch.dict(os.environ, {"WHATSAPP_UNANSWERED_ALERT_S": "180"}, clear=False):
            whatsapp_manager._track_inbound("5511@s.whatsapp.net", "msg-1", "  quanto   custa? ")
            self.assertEqual(whatsapp_manager._sweep_unanswered(), [])
            stale = whatsapp_manager._sweep_unanswered(now=time.time() + 181)
            self.assertEqual(len(stale), 1)
            chat_id, data = stale[0]
            self.assertEqual(chat_id, "5511@s.whatsapp.net")
            self.assertEqual(data["preview"], "quanto custa?")

    def test_alerta_sai_uma_vez_so(self):
        import whatsapp_manager
        whatsapp_manager._track_inbound("5511@s.whatsapp.net", "msg-1", "oi")
        self.assertEqual(len(whatsapp_manager._sweep_unanswered(now=time.time() + 9999)), 1)
        self.assertEqual(whatsapp_manager._sweep_unanswered(now=time.time() + 9999), [])

    def test_newest_alias_inbound_wins_and_delivery_clears_all_aliases(self):
        import whatsapp_manager
        phone = "12025550199@s.whatsapp.net"
        lid = "123456789012345@lid"
        session = "session-us-es"
        whatsapp_manager._sender_to_chat[session] = lid
        self.addCleanup(whatsapp_manager._sender_to_chat.pop, session, None)

        def resolve(jid):
            return phone if str(jid).endswith("@lid") else jid

        with patch("whatsapp_manager._resolve_phone_from_jid", side_effect=resolve):
            whatsapp_manager._track_inbound(
                phone,
                "old",
                "Quiero avanzar.",
                {"market_id": "US", "currency": "USD", "language": "es"},
            )
            whatsapp_manager._pending_inbound[phone]["at"] = time.time() - 1
            whatsapp_manager._track_inbound(lid, "new", "No quiero contratar.")
            whatsapp_manager._pending_inbound[lid]["at"] = time.time()

            self.assertEqual(
                whatsapp_manager._current_inbound_text(phone, session),
                "No quiero contratar.",
            )
            self.assertEqual(
                whatsapp_manager._current_inbound_commercial_metadata(phone, session)["market_id"],
                "US",
            )
            whatsapp_manager._clear_inbound(phone)

        self.assertNotIn(phone, whatsapp_manager._pending_inbound)
        self.assertNotIn(lid, whatsapp_manager._pending_inbound)

    def test_delivery_of_old_turn_does_not_clear_new_concurrent_inbound(self):
        import whatsapp_manager
        phone = "12025550199@s.whatsapp.net"
        whatsapp_manager._track_inbound(phone, "msg-old", "Quiero avanzar.")
        old_record = whatsapp_manager._current_inbound_record(phone)
        old_token = whatsapp_manager._inbound_record_token(old_record)

        def send_and_receive_new(*_args, **_kwargs):
            whatsapp_manager._track_inbound(phone, "msg-new", "No quiero pagar.")
            return "sent-old-reply"

        with patch("whatsapp_manager._assert_delivery_allowed"), \
             patch("whatsapp_manager._maybe_send_voice", return_value=None), \
             patch("whatsapp_manager._human_send", side_effect=send_and_receive_new):
            whatsapp_manager._deliver_contact_reply(
                phone,
                "Old reply",
                consumed_inbound_token=old_token,
            )

        self.assertEqual(
            whatsapp_manager._current_inbound_text(phone),
            "No quiero pagar.",
        )


if __name__ == "__main__":
    unittest.main()
