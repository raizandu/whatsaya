#!/usr/bin/env python3
"""Reset completo do histórico de um contato (uso: número de teste após QA).

Uso:
    python3 wa_reset_contact.py 556282155750 [556281405459 ...]   # dry-run
    python3 wa_reset_contact.py 556282155750 --apply

Roda no HOST com o container PARADO. O SessionStore regrava `sessions.json` no
shutdown, então limpar com o container de pé faz a sessão ressuscitar — foi
exatamente assim que uma sessão sobreviveu a um reset em 21/08.

    docker stop hermes
    WA_BASE=/opt/whatsaya/data python3 wa_reset_contact.py <numero> --apply
    docker start hermes

Duas armadilhas que este script existe para não repetir:

1. **`@lid` é o mesmo contato com outros dígitos.** Resolver os `@lid` pelo
   `personal_contacts.json` não basta: num reset de 24/08 nenhum dos dois números
   de teste estava cadastrado lá, a auto-detecção devolveu zero, e 39 das 74
   mensagens sobreviveriam sob `@lid` — com a AYA seguindo "lembrando" do lead.
   A fonte confiável é o mapa `lidToPhone` do bridge; o cadastro é só fallback.
2. **O estado existe em mais de um lugar.** Tanto `sessions.json` quanto
   `state.db` podem estar sob `profiles/whatsapp/`, não só na raiz. As buscas
   são recursivas.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

BRIDGE_URL = os.environ.get("WHATSAPP_BRIDGE_URL", "http://127.0.0.1:3000")


def fetch_lid_map(bridge_url: str = BRIDGE_URL, timeout: int = 10) -> dict:
    """Mapa lid->telefone do bridge. Vazio se ele não estiver de pé."""
    try:
        with urllib.request.urlopen(f"{bridge_url}/bot-status", timeout=timeout) as resp:
            dados = json.loads(resp.read().decode("utf-8") or "{}")
    except Exception as err:
        print(f"[aviso] bridge indisponível ({type(err).__name__}); "
              f"os @lid virão só do cadastro, que pode estar incompleto")
        return {}
    bruto = dados.get("lidToPhone") or {}
    saida = {}
    for lid, phone in bruto.items():
        alvo = phone[0] if isinstance(phone, list) and phone else phone
        if isinstance(alvo, str):
            saida[str(lid).split("@")[0]] = "".join(c for c in alvo if c.isdigit())
    return saida


def resolve_identifiers(number: str, lid_map: dict, contacts: dict) -> list[str]:
    """Número + todos os `@lid` que apontam para ele, sem arrastar terceiros."""
    numero = "".join(c for c in str(number or "") if c.isdigit())
    if not numero or numero != str(number).strip():
        raise ValueError(f"número inválido: {number!r} (só dígitos, sem @ nem sinais)")

    idents = [numero]
    for lid, phone in (lid_map or {}).items():
        if phone == numero and lid not in idents:
            idents.append(lid)
    for chave, registro in (contacts or {}).items():
        if not isinstance(registro, dict):
            continue
        if numero in str(chave) and registro.get("lid"):
            lid = str(registro["lid"]).split("@")[0]
            if lid and lid not in idents:
                idents.append(lid)
        if "@lid" in str(chave):
            ligado = str(registro.get("phone") or "") + str(registro.get("jid") or "")
            if numero in ligado:
                lid = str(chave).split("@")[0]
                if lid not in idents:
                    idents.append(lid)
    return idents


def find_session_files(base: Path) -> list[Path]:
    """Todos os `sessions.json` sob a base — inclusive os de perfil."""
    base = Path(base)
    if not base.is_dir():
        return []
    return sorted(p for p in base.rglob("sessions.json") if p.is_file())


def find_state_files(base: Path) -> list[Path]:
    """Todos os `state.db` sob a base, inclusive os bancos de cada perfil."""
    base = Path(base)
    if not base.is_dir():
        return []
    return sorted(p for p in base.rglob("state.db") if p.is_file())


def _where(col: str, n: int) -> str:
    return "(" + " OR ".join([f"{col} LIKE ?"] * n) + ")"


def _targets(hermes: Path, patterns: list[str], identifiers: list[str]):
    p = patterns
    pp = patterns + patterns
    booking_keys = [
        hashlib.sha256(identifier.encode("utf-8")).hexdigest()[:32]
        for identifier in identifiers
    ]
    booking_placeholders = ",".join("?" for _key in booking_keys)
    targets = [
        ("whatsapp_messages/messages", hermes / "whatsapp_messages.db",
         "FROM messages WHERE " + _where("chat_id", len(p)), p),
        ("followups/lead_state", hermes / "commercial_followups.db",
         "FROM lead_state WHERE " + _where("chat_id", len(p)), p),
        ("followups/followup_jobs", hermes / "commercial_followups.db",
         "FROM followup_jobs WHERE " + _where("chat_id", len(p)), p),
        ("followups/crm_outbox", hermes / "commercial_followups.db",
         "FROM crm_outbox WHERE " + _where("chat_id", len(p)), p),
        ("calendar_bookings/current_bookings", hermes / "calendar_bookings.db",
         f"FROM current_bookings WHERE chat_key IN ({booking_placeholders})", booking_keys),
    ]
    for state_db in find_state_files(hermes):
        relative = state_db.relative_to(hermes).as_posix()
        targets.extend([
            (f"{relative}/messages", state_db,
             "FROM messages WHERE session_id IN (SELECT id FROM sessions WHERE "
             + _where("user_id", len(p)) + " OR "
             + _where("chat_id", len(p)) + ")", pp),
            (f"{relative}/sessions", state_db,
             "FROM sessions WHERE " + _where("user_id", len(p)) + " OR "
             + _where("chat_id", len(p)), pp),
        ])
    return targets


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Reset do histórico de contatos de teste.")
    ap.add_argument("numeros", nargs="+", help="números só com dígitos, sem @")
    ap.add_argument("--apply", action="store_true", help="sem isto é dry-run")
    ap.add_argument("--base", default=os.environ.get("WA_BASE", "/opt/data"))
    args = ap.parse_args(argv)

    base = Path(args.base)
    hermes = base / ".hermes"
    if not hermes.is_dir():
        print(f"erro: {hermes} não existe (use --base ou WA_BASE)")
        return 2

    pc_path = base / "personal_contacts.json"
    contatos = json.loads(pc_path.read_text(encoding="utf-8")) if pc_path.exists() else {}
    lid_map = fetch_lid_map()

    idents: list[str] = []
    for numero in args.numeros:
        try:
            for i in resolve_identifiers(numero, lid_map, contatos):
                if i not in idents:
                    idents.append(i)
        except ValueError as err:
            print(f"erro: {err}")
            return 2

    print("=== APLICANDO ===" if args.apply else "=== DRY-RUN (nada será alterado) ===")
    print("identificadores:", ", ".join(idents))
    nao_resolvidos = [n for n in args.numeros
                      if not any(lid_map.get(i) == n for i in idents if i != n)]
    if lid_map and nao_resolvidos:
        print(f"[aviso] sem @lid conhecido para: {', '.join(nao_resolvidos)} — "
              f"se o contato usa @lid, a conversa pode sobreviver ao reset")
    print()

    patterns = [f"%{i}%" for i in idents]
    alvos = _targets(hermes, patterns, idents)

    backup = None
    if args.apply:
        backup = base / "backups" / f"reset-{time.strftime('%Y%m%dT%H%M%S')}"
        backup.mkdir(parents=True, exist_ok=True)
        dump = {}
        for rotulo, db, corpo, params in alvos:
            if not Path(db).exists():
                continue
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            try:
                dump[rotulo] = [dict(r) for r in conn.execute("SELECT * " + corpo, params)]
            except sqlite3.Error as err:
                print(f"[backup] {rotulo}: {err}")
            finally:
                conn.close()
        (backup / "linhas.json").write_text(
            json.dumps(dump, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        for nome in ("personal_contacts.json", "sales.json"):
            if (base / nome).exists():
                shutil.copy2(base / nome, backup / nome)
        if (hermes / "contacts_cache.json").exists():
            shutil.copy2(hermes / "contacts_cache.json", backup / "contacts_cache.json")
        print(f"backup em {backup}\n")

    total = 0
    for rotulo, db, corpo, params in alvos:
        if not Path(db).exists():
            print(f"  {rotulo:<30} (sem arquivo)")
            continue
        conn = sqlite3.connect(db if args.apply else f"file:{db}?mode=ro",
                               uri=not args.apply, timeout=30)
        try:
            n = conn.execute("SELECT COUNT(*) " + corpo, params).fetchone()[0]
            total += n
            if args.apply and n:
                conn.execute("DELETE " + corpo, params)
                conn.commit()
            print(f"  {rotulo:<30} {n:>4}{' — apagadas' if args.apply and n else ''}")
        except sqlite3.Error as err:
            print(f"  {rotulo:<30} erro: {err}")
        finally:
            conn.close()

    for rotulo, caminho in (("personal_contacts.json", pc_path),
                            ("sales.json", base / "sales.json"),
                            ("contacts_cache.json", hermes / "contacts_cache.json")):
        if not Path(caminho).exists():
            print(f"  {rotulo:<30} (sem arquivo)")
            continue
        dados = json.loads(Path(caminho).read_text(encoding="utf-8"))
        if not isinstance(dados, dict):
            continue
        alvo = [k for k, v in dados.items()
                if any(i in str(k) for i in idents)
                or any(i in json.dumps(v, ensure_ascii=False) for i in idents)]
        total += len(alvo)
        if args.apply and alvo:
            for k in alvo:
                del dados[k]
            Path(caminho).write_text(
                json.dumps(dados, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"  {rotulo:<30} {len(alvo):>4}{' — removidos' if args.apply and alvo else ''}")

    for sj in find_session_files(hermes):
        try:
            dados = json.loads(sj.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as err:
            print(f"  {sj.name} ({sj.parent.name}): ilegível — {type(err).__name__}")
            continue
        if not isinstance(dados, dict):
            continue
        alvo = [k for k in dados
                if any(i in str(k) for i in idents)
                or any(i in json.dumps(dados[k], ensure_ascii=False) for i in idents)]
        total += len(alvo)
        if args.apply and alvo:
            shutil.copy2(sj, sj.with_name(sj.name + f".bak-{time.strftime('%Y%m%dT%H%M%S')}"))
            for k in alvo:
                del dados[k]
            sj.write_text(json.dumps(dados, indent=2, ensure_ascii=False), encoding="utf-8")
        rotulo = f"sessions.json ({sj.parent.parent.name})"
        print(f"  {rotulo:<30} {len(alvo):>4}{' — removidos' if args.apply and alvo else ''}")

    print(f"\ntotal: {total} item(ns)")
    if not args.apply:
        print("dry-run: nada foi alterado. rode com --apply para valer.")
    elif backup:
        print(f"backup: {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
