#!/usr/bin/env python3
"""Hermes command-TTS backend for Fish Audio.

Usage (Hermes tts.providers.fishaudio.command):
    python3 fish_tts.py {input_path} {output_path} {format}

Env:
    FISH_API_KEY          required
    FISH_REFERENCE_ID     optional voice model id from fish.audio
    FISH_TTS_MODEL        default s2.1-pro-free
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

_WRITTEN_ONLY = (
    (re.compile(r"\b(chave\s+)?pix\b|\bcnpj\b|\bcpf\b", re.I), "pix"),
    (re.compile(r"\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}"), "cnpj"),
    (re.compile(r"\d{3}\.\d{3}\.\d{3}-\d{2}"), "cpf"),
    (re.compile(r"\b\d{5}-?\d{3}\b"), "cep"),
    (re.compile(r"\b(rua|r\.|avenida|av\.|alameda|travessa|rodovia|estrada|bairro|complemento|cep)\b", re.I), "endereco"),
    (re.compile(r"https?://|\bwww\.", re.I), "link"),
    (re.compile(r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}", re.I), "email"),
    (re.compile(r"\b(ag[eê]ncia|conta\s+corrente|banco\s+\w+|c[oó]digo\s+de\s+barras)\b", re.I), "dado-bancario"),
    (re.compile(r"\b(copia\s+e\s+cola|copiar\s+e\s+colar|qr\s*code)\b", re.I), "copia-cola"),
    (re.compile(r"(?<!\d)(?:\d[ .\-]?){10,}(?!\d)"), "codigo"),
)


def written_only_reason(text: str) -> str | None:
    """If the reply must stay copyable as text, return why. Else None."""
    blob = text or ""
    for pattern, reason in _WRITTEN_ONLY:
        if pattern.search(blob):
            return reason
    return None


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: fish_tts.py INPUT_TEXT_FILE OUTPUT_PATH [format]", file=sys.stderr)
        return 2

    text_path = Path(sys.argv[1])
    out_path = Path(sys.argv[2])
    fmt = (sys.argv[3] if len(sys.argv) > 3 else "mp3").lower().lstrip(".")
    if fmt in {"ogg", "opus"}:
        fmt = "opus"
    elif fmt not in {"mp3", "wav", "pcm"}:
        fmt = "mp3"

    key = os.environ.get("FISH_API_KEY", "").strip()
    if not key:
        print("FISH_API_KEY ausente", file=sys.stderr)
        return 1

    text = text_path.read_text(encoding="utf-8").strip()
    if not text:
        print("texto vazio", file=sys.stderr)
        return 1

    skip = written_only_reason(text)
    if skip:
        print(f"skip tts: {skip}", file=sys.stderr)
        return 1

    body: dict = {"text": text, "format": fmt, "normalize": True}
    ref = os.environ.get("FISH_REFERENCE_ID", "").strip()
    if ref:
        body["reference_id"] = ref
    model = os.environ.get("FISH_TTS_MODEL", "s2.1-pro-free").strip() or "s2.1-pro-free"

    req = urllib.request.Request(
        "https://api.fish.audio/v1/tts",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "model": model,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(resp.read())
    except urllib.error.HTTPError as err:
        detail = err.read().decode("utf-8", errors="replace")[:400]
        print(f"Fish Audio HTTP {err.code}: {detail}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
