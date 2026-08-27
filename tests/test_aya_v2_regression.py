"""Valida o fixture sanitizado de conversas douradas da AYA V2."""

import json
import re
import unittest
from pathlib import Path


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "aya_v2_golden_conversations.json"
HANDOFF_MARKER = "[[HANDOFF: lead topou call]]"
EXPECTED_CONVERSATION_IDS = [
    "v2-01-dentista",
    "v2-02-psicoterapeuta",
    "v2-03-salao-de-beleza",
    "v2-04-advogado",
    "v2-05-contador",
]
EXPECTED_HANDOFF_TURNS = {
    "v2-01-dentista": [4],
    "v2-02-psicoterapeuta": [4],
    "v2-03-salao-de-beleza": [6],
    "v2-04-advogado": [5],
    "v2-05-contador": [3],
}
PII_PATTERNS = {
    "email": re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b"),
    "url": re.compile(r"(?:https?://|www\.)", re.IGNORECASE),
    "phone": re.compile(r"(?<!\w)(?:\+?\d[\d .()/-]{6,}\d)(?!\w)"),
    "cpf": re.compile(r"\b\d{3}\.\d{3}\.\d{3}-\d{2}\b"),
    "cnpj": re.compile(r"\b\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}\b"),
}
FALSE_SCHEDULING_RE = re.compile(
    r"\b(?:agendad[oa]s?|reservei|reservad[oa]s?|confirmad[oa]s?|marquei|"
    r"vaga garantida|horário (?:já )?(?:está )?fechado)\b",
    re.IGNORECASE,
)


def _visible_reply(raw_reply):
    return raw_reply.replace(HANDOFF_MARKER, "").strip()


def _sentence_count(reply):
    return len(re.findall(r"[^.!?]+[.!?](?=\s|$)", reply))


class AyaV2GoldenFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        cls.conversations = cls.fixture["conversations"]

    def test_fixture_schema_is_complete_and_turns_are_ordered(self):
        self.assertEqual(
            set(self.fixture),
            {"schema_version", "fixture_id", "sanitized", "conversations"},
        )
        self.assertEqual(self.fixture["schema_version"], 2)
        self.assertEqual(self.fixture["fixture_id"], "aya-v2-golden-conversations")
        self.assertIs(self.fixture["sanitized"], True)
        self.assertEqual(len(self.conversations), 5)

        for conversation in self.conversations:
            with self.subTest(conversation=conversation["id"]):
                self.assertEqual(
                    set(conversation),
                    {"id", "niche", "covered_regression_scenarios", "turns"},
                )
                self.assertIsInstance(conversation["niche"], str)
                self.assertTrue(conversation["niche"].strip())
                self.assertTrue(conversation["covered_regression_scenarios"])
                self.assertTrue(
                    all(
                        re.fullmatch(r"\d{2}", scenario)
                        for scenario in conversation["covered_regression_scenarios"]
                    )
                )
                self.assertEqual(
                    [turn["turn"] for turn in conversation["turns"]],
                    list(range(1, len(conversation["turns"]) + 1)),
                )

                for turn in conversation["turns"]:
                    self.assertEqual(
                        set(turn),
                        {
                            "turn",
                            "lead",
                            "reference_raw_reply",
                            "rationale",
                            "lead_accepts_call",
                            "observable_assertions",
                        },
                    )
                    self.assertIsInstance(turn["lead_accepts_call"], bool)
                    for field in ("lead", "reference_raw_reply", "rationale"):
                        self.assertIsInstance(turn[field], str)
                        self.assertTrue(turn[field].strip())
                    self.assertTrue(turn["observable_assertions"])
                    self.assertTrue(
                        all(
                            isinstance(assertion, str) and assertion.strip()
                            for assertion in turn["observable_assertions"]
                        )
                    )

    def test_conversation_ids_are_unique_and_stable(self):
        conversation_ids = [conversation["id"] for conversation in self.conversations]

        self.assertEqual(conversation_ids, EXPECTED_CONVERSATION_IDS)
        self.assertEqual(len(conversation_ids), len(set(conversation_ids)))

    def test_fixture_contains_no_common_pii_patterns(self):
        serialized_fixture = json.dumps(self.fixture, ensure_ascii=False)

        for label, pattern in PII_PATTERNS.items():
            with self.subTest(pattern=label):
                self.assertIsNone(pattern.search(serialized_fixture))

    def test_fixture_uses_no_em_dash(self):
        serialized_fixture = json.dumps(self.fixture, ensure_ascii=False)

        self.assertNotIn("—", serialized_fixture)

    def test_visible_replies_follow_v2_style_contract(self):
        for conversation in self.conversations:
            for turn in conversation["turns"]:
                visible_reply = _visible_reply(turn["reference_raw_reply"])
                context = f"{conversation['id']} turno {turn['turn']}"

                with self.subTest(context=context):
                    self.assertLessEqual(_sentence_count(visible_reply), 4)
                    self.assertEqual(visible_reply.count("?"), 1)
                    self.assertTrue(visible_reply.endswith("?"))

    def test_handoff_marker_appears_only_when_lead_accepts_call(self):
        actual_handoff_turns = {}

        for conversation in self.conversations:
            handoff_turns = []
            for turn in conversation["turns"]:
                raw_reply = turn["reference_raw_reply"]
                marker_count = raw_reply.count(HANDOFF_MARKER)
                expected_marker_count = 1 if turn["lead_accepts_call"] else 0

                with self.subTest(conversation=conversation["id"], turn=turn["turn"]):
                    self.assertEqual(marker_count, expected_marker_count)
                    self.assertNotIn("[[HANDOFF:", _visible_reply(raw_reply))

                if marker_count:
                    handoff_turns.append(turn["turn"])

            actual_handoff_turns[conversation["id"]] = handoff_turns

        self.assertEqual(actual_handoff_turns, EXPECTED_HANDOFF_TURNS)

    def test_visible_replies_do_not_claim_false_scheduling(self):
        for conversation in self.conversations:
            for turn in conversation["turns"]:
                visible_reply = _visible_reply(turn["reference_raw_reply"])

                with self.subTest(conversation=conversation["id"], turn=turn["turn"]):
                    self.assertIsNone(FALSE_SCHEDULING_RE.search(visible_reply))


if __name__ == "__main__":
    unittest.main()
