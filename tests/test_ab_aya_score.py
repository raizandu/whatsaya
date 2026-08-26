from __future__ import annotations

import importlib.util
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "scripts" / "ab_aya_score.py"
SPEC = importlib.util.spec_from_file_location("ab_aya_score", SCRIPT)
AB = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(AB)


class TestAyaAbScore(unittest.TestCase):
    def test_load_turns_filters_by_explicit_phone_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "messages.db"
            with closing(sqlite3.connect(db)) as con:
                con.execute(
                    "CREATE TABLE messages (chat_id TEXT, from_me INTEGER, body TEXT, timestamp REAL)"
                )
                con.executemany(
                    "INSERT INTO messages VALUES (?, ?, ?, ?)",
                    [
                        ("551199991234@s.whatsapp.net", 0, "Quanto custa a AYA?", 10),
                        ("551188889999@s.whatsapp.net", 0, "Conversa de outro contato", 11),
                        ("551199991234@s.whatsapp.net", 1, "Resposta comercial", 12),
                    ],
                )
                con.commit()

            turns = AB.load_turns(db, since=0, phone_tail="1234")

        self.assertEqual(len(turns), 2)
        self.assertEqual(turns[0][1], "Quanto custa a AYA?")
        self.assertTrue(turns[1][0])

    def test_score_flags_old_prices_and_long_bubbles(self):
        facts = AB.score([
            (False, "Quanto custa?", 1.0, 13),
            (True, "R$ 997 de implementação e R$ 397 por mês" + (" x" * 220), 2.0, 480),
        ])

        self.assertTrue(facts["preco_velho_997_397"])
        self.assertEqual(facts["bolha_gt_400"], 1)


if __name__ == "__main__":
    unittest.main()
