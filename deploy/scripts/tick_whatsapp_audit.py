#!/usr/bin/env python3
"""Auditoria diária do atendimento da WhatsAYA.

Uso no Hermes (cron do próprio Hermes, sem LLM de agente — o auditor é chamado
por dentro, no provider limpo configurado em WHATSAPP_AUDIT_*):

    hermes cron create "0 20 * * *" --name wa-auditoria-diaria \\
      --script /opt/data/.hermes/scripts/tick_whatsapp_audit.py --no-agent

Sem argumento audita o dia corrente (rode no fim do expediente). Com uma data
`YYYY-MM-DD` audita aquele dia — útil para reprocessar.

Modo agente (`WHATSAPP_AUDIT_AGENT_MODE=true`, ou `--material` à mão): não chama
LLM nenhuma. Coleta, grava o relatório sem
veredito e imprime instruções + material no stdout, para o agente do Hermes
produzir o parecer. É assim que o auditor herda a cadeia Codex→OpenRouter da
assinatura, sem auth nova — o plugin não tem credencial do backend Codex, que é
config do gateway. Registre SEM `--no-agent`:

    hermes cron create "0 20 * * *" --name wa-auditoria-diaria \
      --script /opt/data/.hermes/scripts/tick_whatsapp_audit.py

Nesse modo o portão sim/não da fase 2 não arma: o veredito não volta ao processo
do plugin. O parse é fail-safe, então proposta sem estrutura vira nota — o dono
lê e aplica à mão. Foi uma troca aceita de propósito.
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import date
from pathlib import Path

PLUGIN = Path("/opt/data/.hermes/plugins/whatsapp-manager")
if str(PLUGIN) not in sys.path:
    sys.path.insert(0, str(PLUGIN))

# `basicConfig(filename=...)` explode no import se o diretório não existir, e um
# cron que morre com traceback some do radar. Garante o diretório e cai para
# stderr se nem isso der.
_LOG = Path("/opt/data/.hermes/logs/whatsapp_audit_cron.log")
try:
    _LOG.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=str(_LOG),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
except OSError:
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


def main(argv: list[str]) -> int:
    try:
        import whatsapp_manager as wm
    except Exception as err:
        logging.error("import plugin: %s", err)
        return 1

    # O logger do plugin imprime em stdout, e o cron --no-agent entrega stdout no
    # home do dono — um relatório não pode virar mensagem solta lá. Mas anular
    # tudo com NullHandler matava junto o diagnóstico do próprio auditor: o 402
    # do provider na primeira rodada não apareceu em lugar nenhum. Em vez de
    # silenciar, redireciona para o log deste cron: stdout continua protegido e
    # o motivo da falha sobrevive.
    plugin_log = logging.getLogger("whatsapp_manager")
    for handler in list(plugin_log.handlers):
        plugin_log.removeHandler(handler)
    plugin_log.propagate = False
    for handler in logging.getLogger().handlers:
        plugin_log.addHandler(handler)
    if not plugin_log.handlers:
        plugin_log.addHandler(logging.NullHandler())
    plugin_log.setLevel(logging.INFO)

    if not wm.config.whatsapp_audit_enabled:
        logging.info("auditoria desligada (WHATSAPP_AUDIT_ENABLED)")
        return 0

    # `hermes cron create --script` só aceita caminho, sem argv. Por isso o modo
    # agente também liga por env; a flag continua para rodar à mão.
    apenas_material = (
        "--material" in argv
        or os.getenv("WHATSAPP_AUDIT_AGENT_MODE", "").strip().lower() in {"1", "true", "yes"}
    )
    datas = [a for a in argv if not a.startswith("-")]
    dia = None
    if datas:
        try:
            dia = date.fromisoformat(datas[0])
        except ValueError:
            logging.error("data inválida: %r (esperado YYYY-MM-DD)", datas[0])
            return 2

    if apenas_material:
        try:
            material, caminho = wm._collect_audit_material(dia)
        except Exception as err:
            logging.exception("coleta: %s", err)
            return 1
        logging.info("material pronto; relatório sem veredito em %s", caminho)
        # Única coisa no stdout: o que o agente precisa ler.
        print(wm._AUDIT_SYSTEM_PROMPT)
        print()
        print(material)
        return 0

    try:
        caminho = wm._run_daily_audit(dia)
    except Exception as err:
        logging.exception("auditoria: %s", err)
        return 1
    logging.info("relatório: %s", caminho)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
