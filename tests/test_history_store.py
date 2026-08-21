"""Testes do schema do histórico; não importam o plugin vivo."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from history_store import (
    SCHEMA,
    connect,
    dedupe_messages,
    ensure_schema,
    ensure_unique_message_index,
    insert_records,
)


INSERT = (
    "INSERT INTO messages (chat_id, message_id, body, timestamp, from_me, inserted_at) "
    "VALUES (?, ?, ?, ?, ?, ?)"
)


class BaseHistoryStoreTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = str(Path(self._tmp.name) / "whatsapp_messages.db")
        self.conn = connect(self.db)
        self.addCleanup(self.conn.close)

    def rows(self):
        return list(self.conn.execute(
            "SELECT chat_id, message_id, body FROM messages ORDER BY id"
        ))

    def count(self):
        return self.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]


class TestUniqueMessageIndexMigration(BaseHistoryStoreTest):
    """Base legada com repetição: migrar tem que colapsar sem perder texto real."""

    def setUp(self):
        super().setUp()
        # Schema antigo — sem a UNIQUE, que é justamente o que a migração instala.
        self.conn.executescript(SCHEMA)
        for row in [
            # Writers concorrentes gravaram a mesma mensagem duas vezes.
            ("c1@s", "ID1", "[audio received]", 100, 1, 100),
            ("c1@s", "ID1", "[audio received]", 100, 1, 100),
            # Agregado do debounce regravado sob o id do primeiro fragmento; as
            # partes já existem como mensagens próprias (ID2 e ID3).
            ("c1@s", "ID2", "Vdd", 200, 1, 200),
            ("c1@s", "ID2", "Vdd\nE la é bom", 201, 1, 209),
            ("c1@s", "ID3", "E la é bom", 201, 1, 201),
            # Placeholder sem corpo chegou antes da transcrição.
            ("c1@s", "ID4", "", 300, 0, 300),
            ("c1@s", "ID4", "transcrição do áudio", 300, 0, 305),
            # Mesmo message_id em chats diferentes não é repetição.
            ("c2@s", "ID1", "oi", 400, 0, 400),
            ("c1@s", "ID5", "tudo certo", 500, 0, 500),
        ]:
            self.conn.execute(INSERT, row)
        self.conn.commit()

    def test_migration_collapses_duplicates_keeping_real_text(self):
        removed = ensure_unique_message_index(self.conn)
        self.conn.commit()
        self.assertEqual(removed, 3)
        self.assertEqual(self.rows(), [
            ("c1@s", "ID1", "[audio received]"),
            ("c1@s", "ID2", "Vdd"),
            ("c1@s", "ID3", "E la é bom"),
            ("c1@s", "ID4", "transcrição do áudio"),
            ("c2@s", "ID1", "oi"),
            ("c1@s", "ID5", "tudo certo"),
        ])

    def test_transcription_wins_over_empty_placeholder(self):
        """Ordenar só por rowid manteria o placeholder e jogaria fora a transcrição."""
        ensure_unique_message_index(self.conn)
        self.conn.commit()
        body = self.conn.execute(
            "SELECT body FROM messages WHERE chat_id='c1@s' AND message_id='ID4'"
        ).fetchone()[0]
        self.assertEqual(body, "transcrição do áudio")

    def test_same_message_id_in_another_chat_survives(self):
        ensure_unique_message_index(self.conn)
        self.conn.commit()
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE message_id='ID1'"
        ).fetchone()[0], 2)

    def test_migration_is_idempotent(self):
        ensure_unique_message_index(self.conn)
        self.conn.commit()
        antes = self.count()
        self.assertEqual(ensure_unique_message_index(self.conn), 0)
        self.conn.commit()
        self.assertEqual(self.count(), antes)

    def test_insert_or_ignore_finally_deduplicates(self):
        """O motivo da migração: os outros writers usam INSERT OR IGNORE, que sem
        UNIQUE não tinha contra o que conflitar e inseria repetido."""
        ensure_unique_message_index(self.conn)
        self.conn.commit()
        antes = self.count()
        self.conn.execute(
            "INSERT OR IGNORE INTO messages "
            "(chat_id, message_id, body, timestamp, from_me, inserted_at) "
            "VALUES ('c1@s', 'ID5', 'tudo certo', 500, 0, 500)"
        )
        self.conn.commit()
        self.assertEqual(self.count(), antes)


class TestEnsureSchemaOnFreshDatabase(BaseHistoryStoreTest):
    def test_fresh_database_gets_the_unique_index(self):
        ensure_schema(self.conn)
        indexes = {
            row[1] for row in self.conn.execute("PRAGMA index_list(messages)")
        }
        self.assertIn("idx_whatsapp_messages_unique", indexes)
        # O índice por message_id sozinho continua servindo o UPDATE da transcrição.
        self.assertIn("idx_whatsapp_messages_message_id", indexes)

    def test_insert_records_still_dedupes_and_fills_empty_body(self):
        ensure_schema(self.conn)
        base = {"chat_id": "c1@s", "message_id": "ID9", "timestamp": 10, "from_me": 0}
        inserted, _ = insert_records(self.conn, [{**base, "body": ""}])
        self.assertEqual(inserted, 1)
        inserted, skipped = insert_records(self.conn, [{**base, "body": "chegou depois"}])
        self.conn.commit()
        self.assertEqual((inserted, skipped), (0, 1))
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.rows()[0][2], "chegou depois")

    def test_dedupe_on_clean_database_removes_nothing(self):
        ensure_schema(self.conn)
        self.conn.execute(INSERT, ("c1@s", "ID1", "oi", 1, 0, 1))
        self.conn.commit()
        self.assertEqual(dedupe_messages(self.conn), 0)
        self.assertEqual(self.count(), 1)


if __name__ == "__main__":
    unittest.main()
