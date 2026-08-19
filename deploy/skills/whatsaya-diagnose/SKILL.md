---
name: whatsaya-diagnose
description: >
  Diagnostica Whatsaya já no ar: Hermes default no lugar da persona,
  plugin desligado, QR que não abre, placeholder literal, uma bolha só,
  sessão que não grava. Use quando o bot responde errado depois do deploy.
---

# Diagnose Whatsaya

Runbook de instalação: [ONBOARDING.md](../../ONBOARDING.md). Logs brutos do bridge: skill `whatsapp-logs-diagnostics`.

Trabalhe no sintoma. Não recomece o onboard inteiro.

## Sintoma → causa

| O cliente vê | Confira, nesta ordem |
|---|---|
| `/sethome`, “set your home”, onboarding do Hermes | `hermes plugins list` — `whatsapp-manager` enabled? `plugins.enabled` no `config.yaml` do volume. Sem plugin a persona nunca carrega. |
| `{{OWNER_NAME}}` / `{{PIX_KEY}}` no chat | `grep '{{' /opt/data/SOUL*.md /opt/data/support_rules.md`. Preencha e reinicie. |
| Preço ou produto que não existe | `support_rules.md` vazio ou de outro cliente. Não “corrija” no código. |
| Uma mensagem-bloco quando deveria quebrar | Plugin no volume tem `transform_llm_output`? `SOUL_WHATSAPP.md` pede linha em branco (`\n\n`)? Restart depois do pull. |
| QR vazio / não conecta | `/whatsapp/status`. Um bridge só (senão `440 conflict`). Sessão gravável: `ls -ld …/platforms/whatsapp/session` — se root, `chown` para o user do container (`10000`). Fallback de QR: runbook §6. |
| Dono tratado como cliente (ou o contrário) | `WHATSAPP_OWNER_NUMBER` sem `+`, mesmo DDD/dígito 9 do JID. Device suffix `:0` no JID é normal. |
| Ferramenta (`read_file`, terminal) vaza no chat do cliente | Perfil `whatsapp` com `toolsets: []` (escrito pelo compose no boot). `pre_tool_call` deve bloquear contato. |
| Patch no volume sumiu após restart | Sem `KEEP_LOCAL_PLUGIN=true` o boot e o auto-update fazem `reset --hard`. Correção no git deste repo, ou ligue a flag só enquanto o patch existir. |

## Comandos úteis

```bash
docker compose exec hermes hermes plugins list
curl -sS http://127.0.0.1:9119/whatsapp/status
curl -sS http://127.0.0.1:3000/whatsapp/status
grep -n '{{' /opt/data/SOUL.md /opt/data/SOUL_WHATSAPP.md /opt/data/support_rules.md
grep -E 'whatsapp-manager|transform_llm_output|bridge.js atualizado' /opt/data/.hermes/logs/hermes.log | tail -30
```

Portas: dashboard `9119`, bridge interno `3000`. Se o status só responde dentro da rede do compose, use `docker compose exec`.

## Critério de pronto

O sintoma reportado some no WhatsApp, não só no log. Repita o teste da seção 7 do runbook no número que falhou (apague a sessão Hermes desse JID se o histórico estiver contaminado).
