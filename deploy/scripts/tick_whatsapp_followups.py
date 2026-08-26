#!/usr/bin/env python3
"""Tick de follow-up de silêncio no WhatsApp.

Uso no Hermes (watchdog, sem LLM, stdout vazio = silêncio):
    hermes cron create 1m --name wa-silencio-followup \\
      --script /opt/data/.hermes/scripts/tick_whatsapp_followups.py --no-agent
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

PLUGIN = Path("/opt/data/.hermes/plugins/whatsapp-manager")
if str(PLUGIN) not in sys.path:
    sys.path.insert(0, str(PLUGIN))

logging.basicConfig(
    filename="/opt/data/.hermes/logs/whatsapp_followup_cron.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)


def main() -> int:
    try:
        import whatsapp_manager as wm
    except Exception as err:
        logging.error("import plugin: %s", err)
        return 1
    # Plugin logger imprime em stdout; cron --no-agent entrega stdout no home.
    plugin_log = logging.getLogger("whatsapp_manager")
    plugin_log.handlers = []
    plugin_log.addHandler(logging.NullHandler())
    plugin_log.propagate = False
    try:
        sent = wm._tick_followups()
        logging.info("tick sent=%s", sent)
    except Exception as err:
        logging.exception("tick: %s", err)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
