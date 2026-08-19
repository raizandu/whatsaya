#!/usr/bin/env bash
# Avisa o dono um dia antes do fim da campanha s2.1-pro-free (31/08/2026).
# Instalar em /etc/cron.d/whatsaya-fish-model:
#   CRON_TZ=America/Sao_Paulo
#   0 9 30 8 * root /opt/whatsaya/scripts/fish_model_campaign_notice.sh >> /opt/whatsaya/backups/fish-model.log 2>&1
set -euo pipefail

ROOT="${WHATSAPP_ROOT:-/opt/whatsaya}"
STAMP="$ROOT/backups/fish-model-notice.2026-08-30.sent"
TODAY="$(TZ=America/Sao_Paulo date +%F)"
PAID_MODEL="s2.1-pro"

if [[ "$TODAY" != "2026-08-30" ]]; then
  exit 0
fi
if [[ -f "$STAMP" ]]; then
  exit 0
fi

if [[ -f "$ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/.env"
  set +a
fi

MODEL="${FISH_TTS_MODEL:-s2.1-pro-free}"
mkdir -p "$(dirname "$STAMP")"

if [[ "$MODEL" != *free* ]]; then
  echo "$TODAY already on $MODEL" > "$STAMP"
  exit 0
fi

OWNER="${WHATSAPP_OWNER_NUMBER:-}"
if [[ -z "$OWNER" ]]; then
  echo "$TODAY missing WHATSAPP_OWNER_NUMBER" >&2
  exit 1
fi
if [[ "$OWNER" != *"@"* ]]; then
  OWNER="${OWNER}@s.whatsapp.net"
fi

MSG="Amanhã (31/08) acaba a campanha grátis do Fish Audio (s2.1-pro-free).

No servidor: FISH_TTS_MODEL=${PAID_MODEL}
Depois: cd /opt/whatsaya && docker compose up -d

Modelo pago: ${PAID_MODEL} (US\$15 / milhão de bytes UTF-8). Sem o header certo a API cai no pago sozinha — não deixe o campo vazio."

cd "$ROOT"
if docker compose exec -T hermes python3 - "$OWNER" "$MSG" <<'PY'
import json, sys, urllib.request
chat_id, message = sys.argv[1], sys.argv[2]
req = urllib.request.Request(
    "http://127.0.0.1:3000/send",
    data=json.dumps({"chatId": chat_id, "message": message}).encode("utf-8"),
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(req, timeout=15) as resp:
    resp.read()
print("notice sent")
PY
then
  echo "$TODAY sent to $OWNER model=$MODEL" > "$STAMP"
else
  echo "$TODAY send failed" >&2
  exit 1
fi
