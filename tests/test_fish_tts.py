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
