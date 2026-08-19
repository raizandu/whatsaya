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

# S2 free-form cues: [happy], [warm and friendly], [very excited], [break]…
# Don't touch redaction placeholders like [número omitido].
_FISH_CUE = re.compile(
    r"\[(?!(?:n[uú]mero omitido)\])"
    r"(?:very |slightly |extremely |a bit |um pouco )?"
    r"[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ0-9 ,\-]{0,48}\]",
    re.I,
)
_AUDIO_INTRO = re.compile(
    r"(vou te (enviar|mandar) um [áa]udio|"
    r"te (envio|mando) um [áa]udio|"
    r"olha(r)? (no|o) [áa]udio|"
    r"segue (o )?[áa]udio|"
    r"vou gravar|"
    r"te mando o [áa]udio)",
    re.I,
)
_DEFAULT_CUE = "[warm and friendly]"
_FREE_MODEL = "s2.1-pro-free"
_PAID_MODEL = "s2.1-pro"


def written_only_reason(text: str) -> str | None:
    """If the reply must stay copyable as text, return why. Else None."""
    blob = strip_fish_cues(text or "")
    for pattern, reason in _WRITTEN_ONLY:
        if pattern.search(blob):
            return reason
    return None


def strip_fish_cues(text: str) -> str:
    """Remove S2 emotion/tone markers so they never leak into WhatsApp text."""
    cleaned = _FISH_CUE.sub("", text or "")
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r" *\n *", "\n", cleaned)
    return cleaned.strip()


def is_audio_intro(text: str) -> bool:
    return bool(_AUDIO_INTRO.search(strip_fish_cues(text or "")))


def _split_sentences(block: str) -> list[str]:
    parts = re.split(r"(?<=[.!?…])\s+", (block or "").strip())
    return [p.strip() for p in parts if p.strip()]


def split_voice_and_text(text: str) -> tuple[str, str, str]:
    """Split a reply into (spoken, text_before, text_after).

    spoken keeps Fish cues. text_before is an audio intro. text_after is
    copyable data (PIX, address, codes). Empty spoken means text-only.
    """
    spoken: list[str] = []
    before: list[str] = []
    after: list[str] = []

    for para in re.split(r"\n\s*\n+", text or ""):
        para = para.strip()
        if not para:
            continue
        visible = strip_fish_cues(para)
        if is_audio_intro(visible):
            before.append(visible)
            continue
        if not written_only_reason(para):
            spoken.append(para)
            continue
        sents = _split_sentences(para)
        if len(sents) <= 1:
            after.append(visible)
            continue
        for sent in sents:
            sv = strip_fish_cues(sent)
            if is_audio_intro(sv):
                before.append(sv)
            elif written_only_reason(sent):
                after.append(sv)
            else:
                spoken.append(sent)

    return (
        "\n\n".join(spoken).strip(),
        "\n\n".join(before).strip(),
        "\n\n".join(after).strip(),
    )


_UNITS = "zero um dois três quatro cinco seis sete oito nove".split()
_TEENS = "dez onze doze treze quatorze quinze dezesseis dezessete dezoito dezenove".split()
_TENS = ["", "", "vinte", "trinta", "quarenta", "cinquenta", "sessenta", "setenta", "oitenta", "noventa"]
_HUNDREDS = [
    "", "cento", "duzentos", "trezentos", "quatrocentos",
    "quinhentos", "seiscentos", "setecentos", "oitocentos", "novecentos",
]


def _br_int_words(n: int) -> str:
    if n < 0:
        return str(n)
    if n == 0:
        return "zero"
    if n == 100:
        return "cem"
    if n < 10:
        return _UNITS[n]
    if n < 20:
        return _TEENS[n - 10]
    if n < 100:
        tens, unit = divmod(n, 10)
        return _TENS[tens] if unit == 0 else f"{_TENS[tens]} e {_UNITS[unit]}"
    if n < 1000:
        hund, rest = divmod(n, 100)
        head = _HUNDREDS[hund]
        return head if rest == 0 else f"{head} e {_br_int_words(rest)}"
    if n < 1_000_000:
        thou, rest = divmod(n, 1000)
        head = "mil" if thou == 1 else f"{_br_int_words(thou)} mil"
        return head if rest == 0 else f"{head} e {_br_int_words(rest)}"
    return str(n)


def speak_money(text: str) -> str:
    """R$997 / R$397/mês → 'novecentos e noventa e sete reais' para o S2 não engasgar."""

    def _parse_int(raw: str) -> int:
        return int(re.sub(r"[^\d]", "", raw) or "0")

    out = re.sub(
        r"R\$\s*(\d{1,3}(?:\.\d{3})*|\d+)\s*/\s*m[eê]s",
        lambda m: f"{_br_int_words(_parse_int(m.group(1)))} reais por mês",
        text or "",
        flags=re.I,
    )
    out = re.sub(
        r"R\$\s*(\d{1,3}(?:\.\d{3})*|\d+)",
        lambda m: f"{_br_int_words(_parse_int(m.group(1)))} reais",
        out,
        flags=re.I,
    )
    return out


def prepare_spoken_for_tts(spoken: str) -> str:
    """Join paragraphs with a pause and add a warm default cue if none exist."""
    parts = [p.strip() for p in re.split(r"\n\s*\n+", spoken or "") if p.strip()]
    if not parts:
        return ""
    joined = " [break] ".join(parts)
    joined = speak_money(joined)
    if not _FISH_CUE.search(joined):
        joined = f"{_DEFAULT_CUE} {joined}"
    return joined


def resolve_model() -> str:
    return os.environ.get("FISH_TTS_MODEL", _FREE_MODEL).strip() or _FREE_MODEL


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

    text = prepare_spoken_for_tts(text)
    try:
        temperature = float(os.environ.get("FISH_TTS_TEMPERATURE", "0.8"))
    except ValueError:
        temperature = 0.8
    try:
        speed = float(os.environ.get("FISH_TTS_SPEED", "1.02"))
    except ValueError:
        speed = 1.02
    try:
        volume = float(os.environ.get("FISH_TTS_VOLUME", "4"))
    except ValueError:
        volume = 4.0

    body: dict = {
        "text": text,
        "format": fmt,
        # False: S2 [emotion] cues stay acoustic. Numbers are spoken via speak_money().
        "normalize": False,
        "temperature": temperature,
        "top_p": 0.75,
        "latency": "normal",
        "opus_bitrate": 48000 if fmt == "opus" else None,
        "prosody": {
            "speed": speed,
            "volume": volume,
            "normalize_loudness": True,
        },
    }
    if body["opus_bitrate"] is None:
        body.pop("opus_bitrate")
    ref = os.environ.get("FISH_REFERENCE_ID", "").strip()
    if ref:
        body["reference_id"] = ref
    model = resolve_model()
    print(f"fish model={model} format={fmt} chars={len(text)}", file=sys.stderr)

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
