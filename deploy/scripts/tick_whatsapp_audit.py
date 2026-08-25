#!/usr/bin/env python3
"""Auditoria diária do atendimento da WhatsAYA.

Uso no Hermes (cron do próprio Hermes, sem LLM de agente — o auditor é chamado
por dentro, no provider limpo configurado em WHATSAPP_AUDIT_*):

    hermes cron create "0 20 * * *" --name wa-auditoria-diaria \\
      --script /opt/data/.hermes/scripts/tick_whatsapp_audit.py --no-agent

Sem argumento audita o dia corrente (rode no fim do expediente). Com uma data
`YYYY-MM-DD` audita aquele dia — útil para reprocessar.
"""
from __future__ import annotations

import logging
import sys
from datetime import date
from pathlib import Path

PLUGIN = Path("/opt/data/.hermes/plugins/whatsapp-manager")
if str(PLUGIN) not in sys.path:
    sys.path.insert(0, str(PLUGIN))

logging.basicConfig(
    filename="/opt/data/.hermes/logs/whatsapp_audit_cron.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)


def main(argv: list[str]) -> int:
    try:
        import whatsapp_manager as wm
    except Exception as err:
        logging.error("import plugin: %s", err)
        return 1

    # O logger do plugin imprime em stdout; o cron --no-agent entrega stdout no
    # home do dono, e um relatório não pode virar mensagem solta lá.
    plugin_log = logging.getLogger("whatsapp_manager")
    plugin_log.handlers = []
    plugin_log.addHandler(logging.NullHandler())
    plugin_log.propagate = False

    if not wm.config.whatsapp_audit_enabled:
        logging.info("auditoria desligada (WHATSAPP_AUDIT_ENABLED)")
        return 0

    dia = None
    if argv:
        try:
            dia = date.fromisoformat(argv[0])
        except ValueError:
            logging.error("data inválida: %r (esperado YYYY-MM-DD)", argv[0])
            return 2

    try:
        caminho = wm._run_daily_audit(dia)
    except Exception as err:
        logging.exception("auditoria: %s", err)
        return 1
    logging.info("relatório: %s", caminho)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
