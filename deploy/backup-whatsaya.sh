#!/bin/sh
# Snapshot local dos dados de operação do WhatsAYA.
#
# Contato, venda, catálogo, personas e a sessão do Baileys vivem só no bind mount da
# VPS. O sync com o GitHub (CONFIG_REPO) é opt-in e fica desligado por padrão, então
# sem este script não existe cópia nenhuma — um `docker volume rm` errado ou um disco
# perdido leva junto o histórico do cliente e o pareamento do WhatsApp.
#
# A sessão entra no pacote de propósito: restaurar sem ela obriga a reparear o número
# lendo QR no celular, o que na prática significa o bot fora do ar até alguém fazer isso.
#
# Instalação:
#   cp deploy/backup-whatsaya.sh /opt/whatsaya/backup-whatsaya.sh
#   chmod +x /opt/whatsaya/backup-whatsaya.sh
#   (crontab -l 2>/dev/null; echo '17 4 * * * /opt/whatsaya/backup-whatsaya.sh >> /var/log/whatsaya-backup.log 2>&1') | crontab -
#
# Ajuste por env se a pasta de trabalho não for /opt/whatsaya:
#   WHATSAYA_DATA, WHATSAYA_BACKUPS, WHATSAYA_KEEP
set -e

DATA=${WHATSAYA_DATA:-/opt/whatsaya/data}
DEST=${WHATSAYA_BACKUPS:-/opt/whatsaya/backups}
KEEP=${WHATSAYA_KEEP:-14}

[ -d "$DATA" ] || { echo "backup-whatsaya: $DATA não existe"; exit 1; }

TS=$(date +%Y%m%dT%H%M%S)
D=$DEST/snapshot-$TS
mkdir -p "$D/.hermes"

cp "$DATA"/*.json "$DATA"/*.md "$D"/ 2>/dev/null || true

# `.backup` do sqlite3 tira snapshot consistente de banco aberto; `cp` de um .db com
# escrita em andamento pode gerar arquivo corrompido. O cp só existe como fallback
# para host sem o cliente sqlite3 instalado.
for db in whatsapp_messages state commercial_followups; do
  [ -f "$DATA/.hermes/$db.db" ] || continue
  sqlite3 "$DATA/.hermes/$db.db" ".backup $D/.hermes/$db.db" 2>/dev/null \
    || cp "$DATA/.hermes/$db.db" "$D/.hermes/$db.db"
done

cp -r "$DATA/.hermes/platforms/whatsapp/session" "$D/.hermes/session" 2>/dev/null || true

tar czf "$D.tar.gz" -C "$DEST" "snapshot-$TS"
rm -rf "$D"

ls -1t "$DEST"/snapshot-*.tar.gz | tail -n +$((KEEP + 1)) | xargs -r rm -f
