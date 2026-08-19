#!/usr/bin/env bash
# Hook pós-pareamento: aguarda o fullsync estabilizar e prepara o snapshot/prompt.
# Classificação e envio são etapas explícitas para evitar resposta ou alteração
# automática em um número de cliente.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../scripts" && pwd)"
SCRIPT="${SCRIPT_DIR}/whatsapp_history_triage.py"
CONFIG="${WHATSAPP_TRIAGE_CONFIG:-${SCRIPT_DIR}/whatsapp_history_triage.yaml}"

exec python3 "$SCRIPT" run --config "$CONFIG" "${@}"
