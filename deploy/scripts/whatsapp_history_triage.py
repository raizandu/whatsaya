#!/usr/bin/env python3
"""Pipeline reproduzível de fullsync e triagem de WhatsApp da WhatsAYA.

Fluxo seguro:
  1. aguarda a ponte conectada e o lote histórico estabilizar;
  2. extrai um snapshot local sanitizado do SQLite;
  3. gera um prompt/schema para classificação por Hermes/LLM;
  4. transforma a classificação em relatório Markdown de revisão;
  5. só envia ao self-chat quando o operador usa o subcomando ``send``.

O script nunca altera o roteamento do WhatsApp por conta própria e nunca
responde mensagens históricas.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover - fallback para ambientes mínimos
    yaml = None


DEFAULT_CONFIG = {
    "paths": {
        "hermes_home": "/opt/data/.hermes",
        "history_db": "/opt/data/.hermes/whatsapp_messages.db",
        "workspace": "/opt/data/.hermes/workspace",
        "contacts": "/opt/data/personal_contacts.json",
    },
    "bridge": {
        "health_url": "http://127.0.0.1:3000/health",
        "wait_timeout_seconds": 900,
        "poll_seconds": 10,
        "settle_seconds": 20,
        "require_connected": True,
    },
    "classification": {
        "review_automations": ["revisao", "review"],
        "review_flags": ["Revisar"],
        "max_evidence": 3,
    },
    "delivery": {
        "owner_chat_id": "",
        "bridge_send_url": "http://127.0.0.1:3000/send",
    },
}

SECRET_PATTERNS = [
    (re.compile(r"(?i)\b(password|senha)\b\s*[:=]?\s*[^\s,;]+"), r"\1: [REDACTED]"),
    (re.compile(r"(?i)\b(api[_ -]?key|token|secret)\b\s*[:=]?\s*[^\s,;]+"), r"\1: [REDACTED]"),
]


def now_local() -> dt.datetime:
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=-3)))


def iso_timestamp(value: Any) -> str | None:
    if value in (None, ""):
        return None
    try:
        return dt.datetime.fromtimestamp(
            float(value), dt.timezone(dt.timedelta(hours=-3))
        ).isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


def scrub(value: Any) -> Any:
    if isinstance(value, str):
        result = value
        for pattern, replacement in SECRET_PATTERNS:
            result = pattern.sub(replacement, result)
        return result
    if isinstance(value, list):
        return [scrub(item) for item in value]
    if isinstance(value, dict):
        return {key: scrub(item) for key, item in value.items()}
    return value


def deep_merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _scalar_yaml(value: str) -> Any:
    value = value.strip()
    if not value:
        return None
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [_scalar_yaml(item) for item in inner.split(",")]
    if (value.startswith("\"") and value.endswith("\"")) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    lowered = value.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if lowered in {"null", "none", "~"}:
        return None
    try:
        return int(value)
    except ValueError:
        try:
            return float(value)
        except ValueError:
            return value


def _minimal_yaml_load(raw: str) -> dict[str, Any]:
    """Parser pequeno para o YAML de configuração deste pipeline.

    O arquivo é deliberadamente limitado a mapas, listas simples e escalares;
    isso mantém o onboarding funcionando em Python sem PyYAML instalado.
    """
    root: dict[str, Any] = {}
    stack: list[tuple[int, Any]] = [(-1, root)]
    lines = raw.splitlines()
    for index, original in enumerate(lines):
        if not original.strip() or original.lstrip().startswith("#"):
            continue
        indent = len(original) - len(original.lstrip(" "))
        text = original.strip()
        if " #" in text:
            text = text.split(" #", 1)[0].rstrip()
        while stack and indent <= stack[-1][0]:
            stack.pop()
        if not stack:
            raise ValueError(f"indentação YAML inválida na linha {index + 1}")
        parent = stack[-1][1]
        if text.startswith("- "):
            if not isinstance(parent, list):
                raise ValueError(f"lista YAML sem chave na linha {index + 1}")
            parent.append(_scalar_yaml(text[2:]))
            continue
        if ":" not in text or not isinstance(parent, dict):
            raise ValueError(f"linha YAML não suportada {index + 1}: {original}")
        key, raw_value = text.split(":", 1)
        key = key.strip()
        raw_value = raw_value.strip()
        if raw_value:
            parent[key] = _scalar_yaml(raw_value)
            continue
        next_text = ""
        for following in lines[index + 1:]:
            if following.strip() and not following.lstrip().startswith("#"):
                next_text = following.strip()
                break
        child: Any = [] if next_text.startswith("- ") else {}
        parent[key] = child
        stack.append((indent, child))
    return root


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return DEFAULT_CONFIG
    raw = path.read_text(encoding="utf-8")
    if yaml is None:
        data = _minimal_yaml_load(raw)
    else:
        data = yaml.safe_load(raw) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Configuração inválida: {path}")
    return deep_merge(DEFAULT_CONFIG, data)


def cfg(config: dict[str, Any], *keys: str, default: Any = None) -> Any:
    value: Any = config
    for key in keys:
        if not isinstance(value, dict) or key not in value:
            return default
        value = value[key]
    return value


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(scrub(data), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def http_json(url: str, method: str = "GET", payload: dict[str, Any] | None = None) -> dict[str, Any]:
    body = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=8) as response:
        raw = response.read().decode("utf-8")
    parsed = json.loads(raw or "{}")
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def db_status(db_path: Path) -> dict[str, Any]:
    if not db_path.exists():
        return {"exists": False, "messages": 0, "historical": 0, "chats": 0}
    with sqlite3.connect(str(db_path)) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "messages" not in tables:
            return {"exists": True, "messages": 0, "historical": 0, "chats": 0}
        total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        historical = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE COALESCE(is_historical,0)=1"
        ).fetchone()[0]
        chats = conn.execute("SELECT COUNT(DISTINCT chat_id) FROM messages").fetchone()[0]
    return {"exists": True, "messages": total, "historical": historical, "chats": chats}


def wait_for_sync(config: dict[str, Any], db_path: Path, no_wait: bool = False) -> dict[str, Any]:
    if no_wait:
        return db_status(db_path)
    health_url = str(cfg(config, "bridge", "health_url"))
    timeout = int(cfg(config, "bridge", "wait_timeout_seconds", default=900))
    poll = max(1, int(cfg(config, "bridge", "poll_seconds", default=10)))
    settle = max(0, int(cfg(config, "bridge", "settle_seconds", default=20)))
    require_connected = bool(cfg(config, "bridge", "require_connected", default=True))
    deadline = time.monotonic() + timeout
    previous: tuple[int, int] | None = None
    stable_since: float | None = None
    last = db_status(db_path)
    while time.monotonic() < deadline:
        health: dict[str, Any] = {}
        try:
            health = http_json(health_url)
        except (OSError, urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError):
            health = {}
        connected = health.get("status") == "connected"
        last = db_status(db_path)
        current = (int(last.get("historical", 0)), int(last.get("messages", 0)))
        if (not require_connected or connected) and current == previous:
            if stable_since is None:
                stable_since = time.monotonic()
            if time.monotonic() - stable_since >= settle:
                return {**last, "health": health, "stable_seconds": settle}
        else:
            stable_since = None
            previous = current
        time.sleep(poll)
    raise TimeoutError(
        f"fullsync não estabilizou em {timeout}s; último estado={last}"
    )


def load_lid_map(hermes_home: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    session = hermes_home / "platforms" / "whatsapp" / "session"
    for path in session.glob("lid-mapping-*.json"):
        try:
            phone = path.stem.removeprefix("lid-mapping-") + "@s.whatsapp.net"
            lid = json.loads(path.read_text(encoding="utf-8"))
            result[f"{lid}@lid"] = phone
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return result


def load_contact_names(contacts_path: Path, hermes_home: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in (hermes_home / "contacts_cache.json", contacts_path):
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        for jid, info in data.items():
            if isinstance(info, dict):
                name = info.get("name") or info.get("notify") or info.get("pushName") or info.get("spoken_name")
                if name:
                    result[str(jid)] = str(name)
    return result


def message_name(chat_id: str, names: dict[str, str], lid_map: dict[str, str]) -> str:
    if names.get(chat_id):
        return names[chat_id]
    phone = lid_map.get(chat_id)
    if phone and names.get(phone):
        return names[phone]
    return (phone or chat_id).split("@")[0]


def extract_snapshot(config: dict[str, Any], status: dict[str, Any]) -> tuple[Path, Path]:
    hermes_home = Path(str(cfg(config, "paths", "hermes_home")))
    db_path = Path(str(cfg(config, "paths", "history_db")))
    workspace = Path(str(cfg(config, "paths", "workspace")))
    contacts = Path(str(cfg(config, "paths", "contacts")))
    date_label = now_local().date().isoformat()
    snapshot_path = workspace / f"whatsapp_fullsync_{date_label}.json"
    index_path = workspace / f"whatsapp_fullsync_chat_index_{date_label}.csv"
    lid_map = load_lid_map(hermes_home)
    names = load_contact_names(contacts, hermes_home)
    if not db_path.exists():
        raise FileNotFoundError(db_path)
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
        required = {"chat_id", "message_id", "timestamp", "body", "from_me"}
        missing = required - columns
        if missing:
            raise RuntimeError(f"schema messages incompleto; faltam: {sorted(missing)}")
        select = [
            "chat_id", "sender_id", "sender_name", "message_id", "message_type",
            "body", "timestamp", "from_me",
        ]
        for optional in ("is_historical", "has_media", "media_type"):
            select.append(optional if optional in columns else f"0 AS {optional}")
        rows = list(conn.execute(f"SELECT {','.join(select)} FROM messages ORDER BY timestamp ASC, id ASC"))
    chats: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = chats.setdefault(
            str(row["chat_id"]),
            {
                "chat_id": str(row["chat_id"]),
                "phone_jid": lid_map.get(str(row["chat_id"])),
                "display_name": message_name(str(row["chat_id"]), names, lid_map),
                "messages": [],
            },
        )
        item["messages"].append(
            {
                "message_id": row["message_id"],
                "sender_id": row["sender_id"],
                "sender_name": row["sender_name"],
                "timestamp": row["timestamp"],
                "datetime": iso_timestamp(row["timestamp"]),
                "from_me": bool(row["from_me"]),
                "is_historical": bool(row["is_historical"]),
                "message_type": row["message_type"],
                "body": row["body"] or "",
                "has_media": bool(row["has_media"]),
                "media_type": row["media_type"],
            }
        )
    for item in chats.values():
        messages = item["messages"]
        texts = [m["body"] for m in messages if str(m["body"]).strip()]
        item.update(
            {
                "count": len(messages),
                "historical_count": sum(m["is_historical"] for m in messages),
                "inbound_count": sum(not m["from_me"] for m in messages),
                "outbound_count": sum(m["from_me"] for m in messages),
                "first_datetime": messages[0]["datetime"] if messages else None,
                "last_datetime": messages[-1]["datetime"] if messages else None,
                "text_message_count": len(texts),
                "text_chars": sum(len(str(text)) for text in texts),
            }
        )
    snapshot = {
        "generated_at": now_local().isoformat(),
        "source": {"db": str(db_path), "status": status},
        "message_count": len(rows),
        "chat_count": len(chats),
        "first_message_datetime": iso_timestamp(rows[0]["timestamp"]) if rows else None,
        "last_message_datetime": iso_timestamp(rows[-1]["timestamp"]) if rows else None,
        "records": sorted(chats.values(), key=lambda item: item.get("last_datetime") or "", reverse=True),
    }
    write_json(snapshot_path, snapshot)
    fields = [
        "chat_id", "phone_jid", "display_name", "count", "historical_count",
        "inbound_count", "outbound_count", "first_datetime", "last_datetime",
        "text_message_count", "text_chars",
    ]
    index_path.parent.mkdir(parents=True, exist_ok=True)
    with index_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for record in snapshot["records"]:
            writer.writerow({field: record.get(field, "") for field in fields})
    try:
        index_path.chmod(0o600)
    except OSError:
        pass
    return snapshot_path, index_path


def write_classification_prompt(snapshot_path: Path, workspace: Path) -> Path:
    prompt_path = workspace / f"whatsapp_triagem_prompt_{now_local().date().isoformat()}.md"
    prompt = f"""# Prompt de classificação WhatsAYA\n\nLeia o snapshot sanitizado em `{snapshot_path}`.\n\nClassifique todos os records e escreva JSON válido em `{workspace / 'whatsapp_classification.json'}` com:\n\n```json\n{{\"records\": [{{\"chat_id\": \"...\", \"display_name\": \"...\", \"flag\": \"Pessoal|Lead|Cliente|Fornecedor/parceiro|Spam/irrelevante|Revisar\", \"relationship\": \"Amigo|AmigoProximo|Parente|Filho|Cliente|Vendedor|Desconhecido\", \"automation\": \"bloqueada|comercial|revisao\", \"stage\": \"pessoal|lead_novo|lead_qualificado|proposta|cliente|fornecedor|incerto|spam\", \"confidence\": \"alta|media|baixa\", \"contact_data\": {{}}, \"summary\": \"...\", \"evidence\": [\"...\"], \"next_action\": \"...\"}}]}}\n```\n\nRegras: não inventar dados; pessoal nunca recebe automação; cliente só quando há evidência; não aplicar flags nem enviar mensagens durante a classificação.\n"""
    prompt_path.write_text(prompt, encoding="utf-8")
    try:
        prompt_path.chmod(0o600)
    except OSError:
        pass
    return prompt_path


def load_classification(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("records"), list):
        raise ValueError(f"classificação inválida: {path}")
    return data


def format_classification(record: dict[str, Any]) -> str:
    return f"{record.get('flag','Revisar')} *(Estágio: {record.get('stage','incerto')} | Confiança: {record.get('confidence','baixa')})*"


def generate_review(snapshot_path: Path, classification_path: Path, output_path: Path, config: dict[str, Any]) -> dict[str, Any]:
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    classification = load_classification(classification_path)
    by_id = {str(record.get("chat_id")): record for record in classification["records"]}
    snapshot_by_id = {str(record.get("chat_id")): record for record in snapshot.get("records", [])}
    expected = set(snapshot_by_id)
    actual = set(by_id)
    missing = expected - actual
    if missing:
        raise ValueError(f"classificação incompleta; faltam {len(missing)} chats: {sorted(missing)[:8]}")
    review_automations = set(cfg(config, "classification", "review_automations", default=["revisao"]))
    review_flags = set(cfg(config, "classification", "review_flags", default=["Revisar"]))
    pending = []
    for chat_id, record in by_id.items():
        if record.get("automation") in review_automations or record.get("flag") in review_flags:
            merged = {**snapshot_by_id[chat_id], **record}
            pending.append(merged)
    pending.sort(key=lambda record: (str(record.get("flag")), -int(record.get("count", 0))))
    date_label = now_local().date().isoformat()
    lines = [
        "# 📋 Relatório de Triagem - Contatos para Revisão e Validação",
        "",
        f"Olá! Preparamos esta lista com **{len(pending)} contatos** identificados na triagem do WhatsApp que precisam de **validação manual**.",
        "",
        "Nenhuma mensagem foi enviada e nenhum roteamento foi alterado por este relatório.",
        "",
        "---",
        "",
        "## 📌 Resumo da Triagem",
        f"* **Total de chats no snapshot:** {len(snapshot_by_id)}",
        f"* **Mensagens no snapshot:** {snapshot.get('message_count', 0)}",
        f"* **Período:** {snapshot.get('first_message_datetime')} até {snapshot.get('last_message_datetime')}",
        f"* **Contatos pendentes de validação:** {len(pending)}",
        "",
    ]
    groups = [
        ("Lead e Negociação", lambda r: r.get("flag") == "Lead"),
        ("Parceiros, Fornecedores e Ferramentas", lambda r: r.get("flag") == "Fornecedor/parceiro"),
        ("Contatos Pessoais ou Sem Automação", lambda r: r.get("flag") == "Pessoal"),
        ("Contatos Específicos e Mensagens Ambíguas", lambda r: r.get("flag") == "Revisar"),
        ("Mensagens Ilegíveis, Sem Conteúdo ou Spam", lambda r: r.get("flag") == "Spam/irrelevante"),
    ]
    item_number = 0
    for title, predicate in groups:
        group = [record for record in pending if predicate(record)]
        if not group:
            continue
        lines += [f"## {title}", ""]
        for record in group:
            item_number += 1
            name = record.get("display_name") or record.get("chat_id")
            phone = record.get("phone_jid") or record.get("chat_id")
            evidence = record.get("evidence") or []
            contact_data = record.get("contact_data") or {}
            lines += [
                f"### {item_number}. {name}",
                f"* **Telefone / ID:** `{phone}`",
                f"* **Classificação Atual:** {format_classification(record)}",
                f"* **Resumo do Histórico:** {scrub(record.get('summary') or 'Sem resumo disponível.')}",
            ]
            if contact_data:
                lines.append(f"* **Dados extraídos:** `{json.dumps(scrub(contact_data), ensure_ascii=False)}`")
            if evidence:
                lines.append("* **Evidências:** " + " | ".join(f'"{scrub(str(item))}"' for item in evidence[: int(cfg(config, 'classification', 'max_evidence', default=3))]))
            lines += [
                f"* **Ação Recomendada:** {scrub(record.get('next_action') or 'Revisar manualmente antes de automatizar.')}",
                "* **✍️ SEU FEEDBACK / INSTRUÇÃO:**",
                "  - [ ] Aprovado (seguir ação recomendada)",
                "  - [ ] Alterar para: _____________________",
                "",
                "---",
                "",
            ]
    if not pending:
        lines += ["## ✅ Nenhum contato pendente", "", "Todos os contatos classificados passaram pelos critérios de automação definidos.", ""]
    lines += [
        "## 📩 Como enviar feedback",
        "Responda este documento marcando `[x]` ou envie no self-chat o número do item e a instrução.",
        "",
        f"_Gerado automaticamente em {date_label} pelo pipeline de triagem WhatsAYA._",
        "",
    ]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines), encoding="utf-8")
    try:
        output_path.chmod(0o600)
    except OSError:
        pass
    return {"output": str(output_path), "total": len(snapshot_by_id), "pending": len(pending)}


def send_report(report_path: Path, config: dict[str, Any], chat_id: str | None = None) -> dict[str, Any]:
    target = chat_id or str(cfg(config, "delivery", "owner_chat_id", default=""))
    if not target:
        raise ValueError("owner_chat_id não configurado; recusei enviar para destino desconhecido")
    url = str(cfg(config, "delivery", "bridge_send_url", default="http://127.0.0.1:3000/send"))
    text = report_path.read_text(encoding="utf-8")
    result = http_json(url, method="POST", payload={"chatId": target, "message": text})
    if result.get("success") is not True:
        raise RuntimeError(f"bridge não confirmou envio: {result}")
    return {"target": target, "send_result": result, "report": str(report_path)}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("whatsapp_history_triage.yaml"))
    sub = parser.add_subparsers(dest="command", required=True)
    snap = sub.add_parser("snapshot")
    snap.add_argument("--no-wait", action="store_true")
    prompt = sub.add_parser("prompt")
    prompt.add_argument("--snapshot", type=Path, required=True)
    run = sub.add_parser("run")
    run.add_argument("--no-wait", action="store_true")
    run.add_argument("--classification", type=Path)
    run.add_argument("--output", type=Path)
    report = sub.add_parser("report")
    report.add_argument("--snapshot", type=Path, required=True)
    report.add_argument("--classification", type=Path, required=True)
    report.add_argument("--output", type=Path)
    send = sub.add_parser("send")
    send.add_argument("--report", type=Path, required=True)
    send.add_argument("--chat-id")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    config = load_config(args.config)
    workspace = Path(str(cfg(config, "paths", "workspace")))
    if args.command == "snapshot":
        db_path = Path(str(cfg(config, "paths", "history_db")))
        status = wait_for_sync(config, db_path, no_wait=args.no_wait)
        snapshot, index = extract_snapshot(config, status)
        print(json.dumps({"snapshot": str(snapshot), "index": str(index), "status": status}, ensure_ascii=False))
        return 0
    if args.command == "prompt":
        prompt_path = write_classification_prompt(args.snapshot, workspace)
        print(json.dumps({"prompt": str(prompt_path), "snapshot": str(args.snapshot)}, ensure_ascii=False))
        return 0
    if args.command == "run":
        db_path = Path(str(cfg(config, "paths", "history_db")))
        status = wait_for_sync(config, db_path, no_wait=args.no_wait)
        snapshot, index = extract_snapshot(config, status)
        if not args.classification:
            prompt_path = write_classification_prompt(snapshot, workspace)
            print(json.dumps({"snapshot": str(snapshot), "index": str(index), "prompt": str(prompt_path), "status": status}, ensure_ascii=False))
            return 0
        output = args.output or workspace / f"whatsapp_triagem_revisao_cliente_{now_local().date().isoformat()}.md"
        result = generate_review(snapshot, args.classification, output, config)
        print(json.dumps({**result, "snapshot": str(snapshot), "index": str(index), "status": status}, ensure_ascii=False))
        return 0
    if args.command == "report":
        output = args.output or workspace / f"whatsapp_triagem_revisao_cliente_{now_local().date().isoformat()}.md"
        print(json.dumps(generate_review(args.snapshot, args.classification, output, config), ensure_ascii=False))
        return 0
    if args.command == "send":
        print(json.dumps(send_report(args.report, config, args.chat_id), ensure_ascii=False))
        return 0
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
