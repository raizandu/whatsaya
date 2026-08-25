"""Regression guards for AYA's Brazil/United States commercial split."""

import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
INSTANCE_DIR = REPO_ROOT / "deploy" / "instance"
RULES = (INSTANCE_DIR / "support_rules.md").read_text(encoding="utf-8")
SOUL = (INSTANCE_DIR / "SOUL_WHATSAPP.md").read_text(encoding="utf-8")
MASTER_PROMPT = (INSTANCE_DIR / "PROMPT_MESTRE.md").read_text(encoding="utf-8")
COMPOSE = (REPO_ROOT / "deploy" / "docker-compose.yml").read_text(encoding="utf-8")
PLUGIN_SOURCE = (REPO_ROOT / "whatsapp_manager.py").read_text(encoding="utf-8")


class TestAyaMarketRules(unittest.TestCase):
    def test_official_terms_are_partitioned_by_market(self):
        self.assertIn("| Brasil | R$ 1.500 | R$ 497/mês |", RULES)
        self.assertIn("| Estados Unidos | US$ 497 | US$ 99/mês |", RULES)
        self.assertNotIn("R$ 997", RULES)
        self.assertNotIn("R$ 1.497", RULES)
        self.assertNotIn("R$ 397", RULES)

    def test_runtime_bootstrap_selects_the_aya_instance(self):
        self.assertIn("WHATSAPP_CONFIG_SUBDIR=${WHATSAPP_CONFIG_SUBDIR:-instance}", COMPOSE)
        self.assertIn("deploy/$$CONFIG_SUBDIR/$$f", COMPOSE)
        self.assertIn('_plugin_bootstrap_url("SOUL_WHATSAPP.md")', PLUGIN_SOURCE)
        self.assertIn('_plugin_bootstrap_url("support_rules.md")', PLUGIN_SOURCE)
        self.assertIn('_plugin_bootstrap_url("SOUL_EMAIL.md")', PLUGIN_SOURCE)

    def test_united_states_is_confirmed_without_advertising_spanish(self):
        """EUA é mercado confirmado; espanhol não entra como idioma da oferta."""
        normalized = RULES.lower()
        self.assertIn("empresas nos estados unidos", normalized)
        self.assertIn("capacidade confirmada", normalized)
        self.assertIn("não anuncie espanhol como idioma da oferta", normalized)
        self.assertNotRegex(normalized, r"ingl[eê]s, portugu[eê]s e espanhol")

    def test_spanish_changes_the_reply_language_not_the_us_market(self):
        normalized = RULES.lower()
        self.assertIn("empresa que opera nos estados unidos", normalized)
        self.assertIn("responda em espanhol", normalized)
        self.assertIn("mantenha usd, oferta internacional", normalized)
        self.assertIn("e zelle", normalized)
        self.assertIn("espanhol não puxa preço, pix ou regras do brasil", normalized)
        self.assertIn("quiero contratar", normalized)
        self.assertIn("envíame los datos de pago", normalized)
        self.assertIn("o idioma acompanha o lead e não altera o mercado", MASTER_PROMPT.lower())
        self.assertIn("controla somente a\nlíngua da resposta", SOUL.lower())

    def test_zelle_is_structured_and_gated_by_purchase_intent(self):
        self.assertIn("Izabella Kristiny de Freitas", RULES)
        self.assertIn("izabellafreitas2002@hotmail.com", RULES)
        normalized = RULES.lower()
        self.assertIn("intenção explícita", normalized)
        self.assertIn("não envie", normalized)
        self.assertIn("zelle", normalized)
        self.assertIn("comprovante", normalized)

    def test_sensitive_payment_values_only_live_inside_runtime_gate_blocks(self):
        payment_block = re.compile(
            r"<!--\s*AYA_PAYMENT_DETAILS:(BR|US):START\s*-->.*?"
            r"<!--\s*AYA_PAYMENT_DETAILS:\1:END\s*-->",
            re.IGNORECASE | re.DOTALL,
        )
        markets = {match.group(1).upper() for match in payment_block.finditer(RULES)}
        rules_without_gated_details = payment_block.sub("", RULES)

        self.assertEqual(markets, {"BR", "US"})
        for sensitive_value in (
            "44.249.819/0001-62",
            "Gustavo Henrique Vieira Batista",
            "Izabella Kristiny de Freitas",
            "izabellafreitas2002@hotmail.com",
        ):
            self.assertNotIn(sensitive_value, rules_without_gated_details)

    def test_market_is_sticky_and_blocks_cross_market_details(self):
        normalized = RULES.lower()
        self.assertIn("não reclassifique", normalized)
        self.assertIn("não mencione pix", normalized)
        self.assertIn("não mencione zelle", normalized)
        self.assertIn("timezone", normalized)

    def test_price_numbers_live_only_in_the_knowledge_base(self):
        price_pattern = re.compile(r"(?:R\$|US\$)\s*[\d.]", re.IGNORECASE)
        for path, content in (
            ("SOUL_WHATSAPP.md", SOUL),
            ("PROMPT_MESTRE.md", MASTER_PROMPT),
        ):
            self.assertIsNone(price_pattern.search(content), f"commercial price duplicated in {path}")

    def test_internal_approval_language_is_not_client_guidance(self):
        combined = f"{RULES}\n{SOUL}\n{MASTER_PROMPT}".lower()
        self.assertNotIn("autorização explícita do gustavo", combined)
        self.assertNotIn("the next step is human validation", combined)
        self.assertNotIn("precisa de technical validation", combined)
        self.assertIn("não usar “human validation”", combined)


if __name__ == "__main__":
    unittest.main()
