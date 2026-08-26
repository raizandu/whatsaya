#!/usr/bin/env python3
"""Placar do A/B AYA (funil Brasil). Lê sqlite + log; não imprime credencial.

    python3 ab_aya_score.py --arm terra-medium --phone-tail 1234 \
      --since 2026-08-25T04:00:00
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

OFFICIAL = ("1.500", "1500", "497")
OLD_TABLE = ("997", "397")


def _fold(s: str) -> str:
    import unicodedata
    t = unicodedata.normalize("NFD", (s or "").lower())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", t).strip()


def _parse_since(value: str) -> float:
    if not value:
        return 0.0
    if value.isdigit():
        return float(value)
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def load_turns(db: Path, since: float, phone_tail: str) -> list[tuple[bool, str, float, int]]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = con.execute(
        """
        SELECT from_me, body, timestamp, length(body)
        FROM messages
        WHERE chat_id LIKE ?
        ORDER BY timestamp ASC
        """,
        (f"%{phone_tail}%",),
    ).fetchall()
    con.close()
    out = []
    for from_me, body, ts, n in rows:
        if ts and float(ts) < since:
            continue
        out.append((bool(from_me), body or "", float(ts or 0), int(n or 0)))
    return out


def score(turns: list[tuple[bool, str, float, int]]) -> dict:
    leads = [(b, n) for me, b, _, n in turns if not me]
    ayas = [(b, n) for me, b, _, n in turns if me]
    joined_aya = _fold(" ".join(b for b, _ in ayas))
    facts = {
        "turnos_lead": len(leads),
        "turnos_aya": len(ayas),
        "bolha_gt_400": sum(1 for _, n in ayas if n > 400),
        "lista": any(re.search(r"(?m)^\s*[-*]\s+", b) for b, _ in ayas),
        "perguntou_pais": "em qual pais" in joined_aya or "qual pais" in joined_aya,
        "preco_oficial": all(x in joined_aya.replace(".", "").replace(" ", "") or x in joined_aya for x in ("1.500", "497"))
        or ("1500" in joined_aya.replace(".", "") and "497" in joined_aya),
        "preco_velho_997_397": any(x in joined_aya for x in OLD_TABLE),
        "reperguntou_pais_depois_brasil": False,
        "pix_ou_pagamento": "pix" in joined_aya or "dados oficiais" in joined_aya,
        "pediu_comprovante": "comprovante" in joined_aya,
        "reabriu_checkout_pos_pix": False,
        "handoff": "[[handoff" in joined_aya or "time humano" in joined_aya
        or "alguem do time" in joined_aya or "pessoa do time" in joined_aya,
    }
    brasil_idx = next((i for i, (b, _) in enumerate(leads) if _fold(b) in {"brasil", "brasil."}), None)
    preco_idx = next(
        (i for i, (b, _) in enumerate(leads) if "custa" in _fold(b) or "quanto fica" in _fold(b)),
        None,
    )
    if brasil_idx is not None and preco_idx is not None and preco_idx > brasil_idx:
        after = [t for t in turns if t[0]]
        # AYA after the price question: country ask is a fail
        lead_price_ts = next(ts for me, b, ts, n in turns if (not me) and ("custa" in _fold(b) or "quanto fica" in _fold(b)))
        after_price = [_fold(b) for me, b, ts, n in turns if me and ts >= lead_price_ts]
        facts["reperguntou_pais_depois_brasil"] = any("qual pais" in x for x in after_price)

    pix_claim = next(
        (ts for me, b, ts, n in turns if (not me) and ("fiz o pix" in _fold(b) or "mandei o pix" in _fold(b) or "ja paguei" in _fold(b))),
        None,
    )
    if pix_claim:
        after = " ".join(_fold(b) for me, b, ts, n in turns if me and ts >= pix_claim)
        facts["reabriu_checkout_pos_pix"] = (
            "posso enviar os dados de pagamento" in after
            or "quando voce quiser avancar" in after
        )
        facts["pediu_comprovante_pos_pix"] = "comprovante" in after
    return facts


def parse_log(path: Path, since: float, phone_tail: str) -> dict:
    reasons: list[str] = []
    country = 0
    sends = 0
    if not path.exists():
        return {"payment_gate": reasons, "country_reply": 0, "human_sends": 0}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if phone_tail not in line and "country-reply" not in line and "payment-gate" not in line:
            continue
        m = re.search(r"reason=([a-z_]+)", line)
        if "[payment-gate]" in line and m:
            reasons.append(m.group(1))
        if "[country-reply]" in line:
            country += 1
        if "[human-send]" in line and phone_tail in line:
            sends += 1
    return {"payment_gate": reasons, "country_reply": country, "human_sends": sends}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", default="unknown")
    ap.add_argument("--since", default="")
    ap.add_argument(
        "--phone-tail",
        default=os.environ.get("WA_PHONE_TAIL", ""),
        help="últimos dígitos do JID de teste (ou WA_PHONE_TAIL)",
    )
    ap.add_argument("--db", default=os.environ.get("WA_DB", "/opt/whatsaya/data/.hermes/whatsapp_messages.db"))
    ap.add_argument("--log", default=os.environ.get("WA_LOG", "/opt/whatsaya/data/.hermes/logs/whatsapp_plugin.log"))
    ap.add_argument("--show-preview", action="store_true", help="exibe até 240 caracteres do último turno")
    args = ap.parse_args()
    phone_tail = re.sub(r"\D", "", args.phone_tail)
    if len(phone_tail) < 4:
        ap.error("informe --phone-tail com pelo menos 4 dígitos")
    since = _parse_since(args.since)
    try:
        turns = load_turns(Path(args.db), since, phone_tail)
    except (OSError, sqlite3.Error) as err:
        print(f"erro ao ler banco: {err}", file=sys.stderr)
        return 2
    facts = score(turns)
    log = parse_log(Path(args.log), since, phone_tail)
    print(f"arm={args.arm} since={args.since or 'epoch'} leads={facts['turnos_lead']} aya={facts['turnos_aya']}")
    for k, v in facts.items():
        if k.startswith("turnos_"):
            continue
        print(f"  {k}: {v}")
    print(f"  country_reply_log: {log['country_reply']}")
    print(f"  payment_gate: {log['payment_gate']}")
    print(f"  human_sends_log: {log['human_sends']}")
    if turns and args.show_preview:
        print("--- ultimo turno ---")
        me, body, ts, n = turns[-1]
        who = "AYA" if me else "Lead"
        print(f"  {who} ({n}c): {body.replace(chr(10), ' / ')[:240]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
