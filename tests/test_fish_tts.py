"""written_only_reason do Fish TTS — conteúdo que precisa ficar copiável."""

import unittest
from pathlib import Path
import importlib.util

SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "scripts" / "fish_tts.py"


def _load():
    spec = importlib.util.spec_from_file_location("fish_tts", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestWrittenOnlyReason(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fish = _load()

    def test_plain_chat_is_spoken(self):
        self.assertIsNone(self.fish.written_only_reason("Opa, fechamos então. Te chamo já."))

    def test_pix_stays_written(self):
        self.assertEqual(self.fish.written_only_reason("A chave Pix é 44.249.819/0001-62"), "pix")

    def test_address_stays_written(self):
        self.assertEqual(
            self.fish.written_only_reason("Rua das Flores 100, bairro Centro"),
            "endereco",
        )

    def test_cep_stays_written(self):
        self.assertEqual(self.fish.written_only_reason("O CEP é 74000-000"), "cep")

    def test_email_stays_written(self):
        self.assertEqual(self.fish.written_only_reason("Me chama em aya@raizandu.com"), "email")

    def test_link_stays_written(self):
        self.assertEqual(self.fish.written_only_reason("Entra em https://raizandu.com/pay"), "link")

    def test_cues_do_not_trigger_written_only(self):
        self.assertIsNone(self.fish.written_only_reason("[warm and friendly] Oi, fechamos então."))


class TestVoiceTextSplit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fish = _load()

    def test_speakable_is_voice_only(self):
        spoken, before, after = self.fish.split_voice_and_text("[happy] Oi, fechamos então.")
        self.assertIn("fechamos", spoken)
        self.assertEqual(before, "")
        self.assertEqual(after, "")

    def test_pix_paragraph_is_written(self):
        spoken, before, after = self.fish.split_voice_and_text("A chave Pix é 44.249.819/0001-62")
        self.assertEqual(spoken, "")
        self.assertEqual(before, "")
        self.assertIn("44.249.819/0001-62", after)

    def test_mixed_voice_then_pix(self):
        spoken, before, after = self.fish.split_voice_and_text(
            "fechamos então\n\nA chave Pix é 44.249.819/0001-62"
        )
        self.assertIn("fechamos", spoken)
        self.assertEqual(before, "")
        self.assertIn("44.249.819/0001-62", after)

    def test_intro_stays_text(self):
        spoken, before, after = self.fish.split_voice_and_text(
            "vou te enviar um audio explicando\n\n[happy] o fluxo é simples"
        )
        self.assertIn("fluxo", spoken)
        self.assertIn("audio", before.lower())
        self.assertEqual(after, "")

    def test_sentence_split_keeps_pix_written(self):
        spoken, before, after = self.fish.split_voice_and_text(
            "Fechou. A chave Pix é 44.249.819/0001-62. Qualquer coisa me chama."
        )
        self.assertIn("Fechou", spoken)
        self.assertIn("Qualquer coisa", spoken)
        self.assertIn("44.249.819/0001-62", after)
        self.assertEqual(before, "")


class TestFishCues(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fish = _load()

    def test_strip_leaves_redaction(self):
        out = self.fish.strip_fish_cues("[happy] liga no [número omitido] agora")
        self.assertNotIn("[happy]", out)
        self.assertIn("[número omitido]", out)

    def test_default_cue_when_missing(self):
        out = self.fish.prepare_spoken_for_tts("Oi, tudo bem?")
        self.assertTrue(out.startswith("[warm and friendly]"))
        self.assertIn("Oi, tudo bem?", out)

    def test_keeps_existing_cue(self):
        out = self.fish.prepare_spoken_for_tts("[empathetic] Entendi.")
        self.assertTrue(out.startswith("[empathetic]"))
        self.assertNotIn("[warm and friendly]", out)

    def test_break_between_paragraphs(self):
        out = self.fish.prepare_spoken_for_tts("[happy] Oi\n\nfechamos então")
        self.assertIn("[break]", out)

    def test_resolve_model_defaults_free(self):
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FISH_TTS_MODEL", None)
            self.assertEqual(self.fish.resolve_model(), "s2.1-pro-free")
