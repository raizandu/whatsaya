"""Testes do reset de contato de teste.

O script existe porque o reset anterior falhava em silêncio: descobria os `@lid`
pelo `personal_contacts.json` e, quando o contato não estava registrado lá,
apagava metade da conversa e deixava a outra metade viva sob o `@lid` — a AYA
seguia "lembrando" do lead depois do reset.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.append(str(Path(__file__).parent.parent / "deploy" / "scripts"))

from wa_reset_contact import find_session_files, main, resolve_identifiers


LID_MAP = {
    "226199502602474": "556282155750",
    "111437255037108": "556281405459",
    "177073666707485": "551836270184",
}


class ResolveIdentifiersTest(unittest.TestCase):
    def test_resolve_lid_pelo_mapa_do_bridge(self):
        idents = resolve_identifiers("556282155750", LID_MAP, {})

        self.assertIn("556282155750", idents)
        self.assertIn("226199502602474", idents)

    def test_funciona_quando_o_contato_nao_esta_no_personal_contacts(self):
        # A falha real: `personal_contacts.json` vazio fazia a auto-detecção
        # devolver zero lid, e 39 mensagens sobreviviam ao reset.
        idents = resolve_identifiers("556281405459", LID_MAP, {})

        self.assertIn("111437255037108", idents)

    def test_nao_arrasta_lid_de_outro_numero(self):
        # `177073666707485` é de um terceiro real; incluí-lo apagaria conversa
        # de quem não era teste.
        idents = resolve_identifiers("556282155750", LID_MAP, {})

        self.assertNotIn("177073666707485", idents)
        self.assertNotIn("551836270184", idents)

    def test_tambem_aceita_lid_vindo_do_personal_contacts(self):
        # Fallback: se o bridge estiver fora do ar, o cadastro ainda ajuda.
        contatos = {"556282155750@s.whatsapp.net": {"lid": "999888777@lid"}}

        idents = resolve_identifiers("556282155750", {}, contatos)

        self.assertIn("999888777", idents)

    def test_numero_sem_lid_devolve_so_ele(self):
        self.assertEqual(resolve_identifiers("5511999999999", LID_MAP, {}), ["5511999999999"])

    def test_dedup_e_ordem_estavel(self):
        contatos = {"556282155750@s.whatsapp.net": {"lid": "226199502602474@lid"}}

        idents = resolve_identifiers("556282155750", LID_MAP, contatos)

        self.assertEqual(len(idents), len(set(idents)))
        self.assertEqual(idents[0], "556282155750")

    def test_numero_invalido_e_recusado(self):
        for ruim in ("", "abc", "55-62-8888"):
            with self.subTest(ruim=ruim):
                with self.assertRaises(ValueError):
                    resolve_identifiers(ruim, LID_MAP, {})


class FindSessionFilesTest(unittest.TestCase):
    """A sessão viva estava em `profiles/whatsapp/sessions/`, não no caminho
    documentado — procurar só um lugar deixava a sessão ressuscitar no restart."""

    def test_acha_sessions_json_em_subdiretorio_de_perfil(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "sessions").mkdir(parents=True)
            (base / "sessions" / "sessions.json").write_text("{}", encoding="utf-8")
            (base / "profiles" / "whatsapp" / "sessions").mkdir(parents=True)
            (base / "profiles" / "whatsapp" / "sessions" / "sessions.json").write_text(
                "{}", encoding="utf-8")

            achados = find_session_files(base)

            self.assertEqual(len(achados), 2)
            self.assertTrue(any("profiles" in str(a) for a in achados))

    def test_base_sem_sessions_nao_explode(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(find_session_files(Path(tmp)), [])


class ResetProfileStateTest(unittest.TestCase):
    """O multiplexador persiste sessões em um `state.db` por perfil.

    Limpar somente o banco raiz deixa o histórico comercial vivo, mesmo quando
    `sessions.json` já foi removido.
    """

    @staticmethod
    def _create_state_db(path: Path, target: str, keep: str | None = None) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as conn:
            conn.execute(
                "CREATE TABLE sessions (id TEXT PRIMARY KEY, user_id TEXT, chat_id TEXT)"
            )
            conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT)")
            conn.execute(
                "INSERT INTO sessions (id, user_id, chat_id) VALUES (?, ?, ?)",
                ("target-session", target, target),
            )
            conn.execute(
                "INSERT INTO messages (session_id) VALUES (?)", ("target-session",)
            )
            if keep:
                conn.execute(
                    "INSERT INTO sessions (id, user_id, chat_id) VALUES (?, ?, ?)",
                    ("keep-session", keep, keep),
                )
                conn.execute(
                    "INSERT INTO messages (session_id) VALUES (?)", ("keep-session",)
                )

    def test_apply_apaga_state_db_raiz_e_do_perfil(self):
        target = "556281405459"
        keep = "5511999999999"
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            hermes = base / ".hermes"
            root_db = hermes / "state.db"
            profile_db = hermes / "profiles" / "whatsapp" / "state.db"
            self._create_state_db(root_db, target)
            self._create_state_db(profile_db, target, keep)

            with mock.patch("wa_reset_contact.fetch_lid_map", return_value={}):
                result = main([target, "--base", str(base), "--apply"])

            self.assertEqual(result, 0)
            for db in (root_db, profile_db):
                with sqlite3.connect(db) as conn:
                    target_count = conn.execute(
                        "SELECT COUNT(*) FROM sessions WHERE user_id = ?", (target,)
                    ).fetchone()[0]
                self.assertEqual(target_count, 0, db)
            with sqlite3.connect(profile_db) as conn:
                keep_count = conn.execute(
                    "SELECT COUNT(*) FROM sessions WHERE user_id = ?", (keep,)
                ).fetchone()[0]
            self.assertEqual(keep_count, 1)


if __name__ == "__main__":
    unittest.main()
