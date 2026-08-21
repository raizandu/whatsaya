#!/usr/bin/env python3
"""Armazenamento local das mensagens do WhatsApp.

O bridge Node envia lotes de mensagens para este processo curto. SQLite fica
no volume persistente do Hermes e serve tanto para o histórico importado quanto
para as mensagens recebidas em tempo real.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any


SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL,
    sender_id TEXT,
    sender_name TEXT,
    message_id TEXT NOT NULL,
    message_type TEXT,
    body TEXT,
    timestamp REAL,
    from_me INTEGER NOT NULL DEFAULT 0,
    is_historical INTEGER NOT NULL DEFAULT 0,
    has_media INTEGER NOT NULL DEFAULT 0,
    media_type TEXT,
    sync_type TEXT,
    inserted_at REAL NOT NULL DEFAULT (strftime('%s','now'))
);
CREATE TABLE IF NOT EXISTS whatsapp_history_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL
);
"""

REQUIRED_COLUMNS = {
    "sender_id": "TEXT",
    "sender_name": "TEXT",
    "message_id": "TEXT",
    "message_type": "TEXT",
    "body": "TEXT",
    "timestamp": "REAL",
    "from_me": "INTEGER NOT NULL DEFAULT 0",
    "is_historical": "INTEGER NOT NULL DEFAULT 0",
    "has_media": "INTEGER NOT NULL DEFAULT 0",
    "media_type": "TEXT",
    "sync_type": "TEXT",
    "inserted_at": "REAL NOT NULL DEFAULT 0",
}


def connect(db_path: str) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
    for name, definition in REQUIRED_COLUMNS.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE messages ADD COLUMN {name} {definition}")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_whatsapp_messages_chat_ts "
        "ON messages(chat_id, timestamp)"
    )
    # Índice por message_id sozinho continua servindo o UPDATE da transcrição,
    # que procura só pelo id (_update_db_message em whatsapp_manager.py).
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_whatsapp_messages_message_id "
        "ON messages(message_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_whatsapp_messages_from_me "
        "ON messages(from_me, timestamp)"
    )
    ensure_unique_message_index(conn)
    conn.commit()


def dedupe_messages(conn: sqlite3.Connection) -> int:
    """Colapsa (chat_id, message_id) repetidos numa linha só. Devolve quantas removeu.

    Duas origens de repetição, ambas verificadas em produção:

    - Writers concorrentes gravando a mesma mensagem. Os três usam
      `INSERT OR IGNORE`, que sem constraint UNIQUE não tem contra o que conflitar
      e insere de novo — a intenção de dedup estava escrita, o schema anulava.
    - O agregado do debounce regravado sob o id do primeiro fragmento. Produz uma
      mensagem que nunca existiu no WhatsApp ("Vdd\\nE la é bom" quando o dono
      mandou "Vdd" e depois "E la é bom"), com as partes já gravadas uma a uma.

    Mantém o menor rowid com corpo não vazio. O critério de corpo vem antes do
    rowid porque o histórico importado pode chegar sem texto e ser preenchido
    depois: ordenar só por rowid manteria o placeholder e jogaria fora a
    transcrição.
    """
    cur = conn.execute(
        """
        DELETE FROM messages WHERE id NOT IN (
            SELECT id FROM (
                SELECT id, ROW_NUMBER() OVER (
                    PARTITION BY chat_id, message_id
                    ORDER BY CASE WHEN body IS NULL OR TRIM(body) = '' THEN 1 ELSE 0 END, id
                ) AS rn
                FROM messages
            ) WHERE rn = 1
        )
        """
    )
    return cur.rowcount or 0


def ensure_unique_message_index(conn: sqlite3.Connection) -> int:
    """Garante UNIQUE(chat_id, message_id); deduplica antes se a base já tiver repetição.

    É o que faz o `INSERT OR IGNORE` dos outros writers finalmente deduplicar de
    verdade. Sem isso a mesma mensagem entra várias vezes e o histórico injetado
    no contexto do LLM (pre_llm_call) chega com falas repetidas.

    Devolve quantas linhas foram removidas na migração (0 numa base já limpa).
    """
    sql = (
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_whatsapp_messages_unique "
        "ON messages(chat_id, message_id)"
    )
    try:
        conn.execute(sql)
        return 0
    except sqlite3.IntegrityError:
        removed = dedupe_messages(conn)
        conn.execute(sql)
        return removed


def set_meta(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO whatsapp_history_meta(key,value,updated_at) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, json.dumps(value, ensure_ascii=False), time.time()),
    )


def insert_records(conn: sqlite3.Connection, records: list[dict[str, Any]]) -> tuple[int, int]:
    inserted = 0
    skipped = 0
    seen: set[tuple[str, str]] = set()
    for item in records:
        chat_id = str(item.get("chat_id") or "").strip()
        message_id = str(item.get("message_id") or "").strip()
        if not chat_id or not message_id:
            skipped += 1
            continue
        dedup_key = (chat_id, message_id)
        if dedup_key in seen:
            skipped += 1
            continue
        seen.add(dedup_key)
        exists = conn.execute(
            "SELECT id FROM messages WHERE chat_id=? AND message_id=? LIMIT 1",
            (chat_id, message_id),
        ).fetchone()
        if exists:
            skipped += 1
            # Histórico pode chegar primeiro sem texto e depois com texto.
            body = item.get("body")
            if body:
                conn.execute(
                    "UPDATE messages SET body=COALESCE(NULLIF(body,''),?), "
                    "sender_name=COALESCE(NULLIF(sender_name,''),?), "
                    "message_type=COALESCE(NULLIF(message_type,''),?) WHERE id=?",
                    (body, item.get("sender_name"), item.get("message_type"), exists[0]),
                )
            continue
        conn.execute(
            """INSERT INTO messages
               (chat_id,sender_id,sender_name,message_id,message_type,body,timestamp,
                from_me,is_historical,has_media,media_type,sync_type,inserted_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                chat_id,
                item.get("sender_id"),
                item.get("sender_name"),
                message_id,
                item.get("message_type"),
                item.get("body"),
                item.get("timestamp"),
                1 if item.get("from_me") else 0,
                1 if item.get("is_historical") else 0,
                1 if item.get("has_media") else 0,
                item.get("media_type"),
                item.get("sync_type"),
                time.time(),
            ),
        )
        inserted += 1
    return inserted, skipped


def command_init(db_path: str) -> dict[str, Any]:
    with connect(db_path) as conn:
        ensure_schema(conn)
        set_meta(conn, "store_initialized", True)
        conn.commit()
    return {"ok": True, "db_path": db_path}


def command_batch(db_path: str) -> dict[str, Any]:
    payload = json.load(sys.stdin)
    records = payload.get("records") or []
    sync_type = payload.get("sync_type")
    with connect(db_path) as conn:
        ensure_schema(conn)
        inserted, skipped = insert_records(conn, records)
        if payload.get("historical"):
            set_meta(conn, "last_history_sync_type", sync_type or "unknown")
            set_meta(conn, "last_history_batch_size", len(records))
            set_meta(conn, "last_history_inserted", inserted)
            set_meta(conn, "last_history_sync_at", time.time())
        conn.commit()
    return {"ok": True, "inserted": inserted, "skipped": skipped, "received": len(records)}


def command_get(db_path: str, chat_id: str, message_id: str) -> dict[str, Any]:
    with connect(db_path) as conn:
        ensure_schema(conn)
        row = conn.execute(
            "SELECT body,message_type FROM messages WHERE chat_id=? AND message_id=? LIMIT 1",
            (chat_id, message_id),
        ).fetchone()
    if not row:
        return {"ok": True, "found": False}
    return {"ok": True, "found": True, "body": row[0] or "", "message_type": row[1] or ""}


def command_status(db_path: str) -> dict[str, Any]:
    with connect(db_path) as conn:
        ensure_schema(conn)
        count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        historical = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE is_historical=1"
        ).fetchone()[0]
        chats = conn.execute("SELECT COUNT(DISTINCT chat_id) FROM messages").fetchone()[0]
        meta = {
            row[0]: json.loads(row[1])
            for row in conn.execute("SELECT key,value FROM whatsapp_history_meta")
        }
    return {"ok": True, "messages": count, "historical": historical, "chats": chats, "meta": meta}


def command_oldest(db_path: str) -> dict[str, Any]:
    """Retorna a mensagem mais antiga conhecida por chat para paginação on-demand."""
    with connect(db_path) as conn:
        ensure_schema(conn)
        rows = conn.execute(
            """SELECT m.chat_id,m.message_id,m.from_me,m.timestamp
               FROM messages AS m
               JOIN (SELECT chat_id, MIN(timestamp) AS min_ts FROM messages GROUP BY chat_id) AS x
                 ON x.chat_id=m.chat_id AND x.min_ts=m.timestamp
               ORDER BY m.chat_id"""
        ).fetchall()
    return {
        "ok": True,
        "chats": [
            {"chat_id": r[0], "message_id": r[1], "from_me": bool(r[2]), "timestamp": r[3]}
            for r in rows
        ],
    }


def main() -> int:
    if len(sys.argv) < 3:
        print("uso: history_store.py init|batch|status|get DB [CHAT_ID MESSAGE_ID]", file=sys.stderr)
        return 2
    command = sys.argv[1]
    db_path = sys.argv[2]
    try:
        if command == "init":
            result = command_init(db_path)
        elif command == "batch":
            result = command_batch(db_path)
        elif command == "status":
            result = command_status(db_path)
        elif command == "oldest":
            result = command_oldest(db_path)
        elif command == "get" and len(sys.argv) >= 5:
            result = command_get(db_path, sys.argv[3], sys.argv[4])
        else:
            print("comando inválido", file=sys.stderr)
            return 2
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:  # pragma: no cover - boundary de processo
        print(f"history_store: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
