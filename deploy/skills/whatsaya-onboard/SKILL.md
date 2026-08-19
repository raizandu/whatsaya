---
name: whatsaya-onboard
description: >
  Sobe um cliente novo do kit Whatsaya no VPS (Compose + IP).
  Use quando pedirem onboarding, instalar whatsaya, replicar o kit,
  novo cliente, deploy sem painel, ou "faz igual o último cliente".
---

# Onboard Whatsaya

Fonte de verdade: [ONBOARDING.md](../../ONBOARDING.md). Execute na ordem. Não invente env, não edite o plugin no volume, não copie persona/Pix de outro cliente.

## Critério de pronto

Os quatro testes da seção 7 do runbook passam. Sem isso o cliente não está no ar.

## Passos

1. **Intake** — peça a tabela da seção 1. Falta número, nome ou catálogo: pare. Não complete `{{…}}` com chute.
2. **VPS** — Ubuntu 24, Docker Compose v2, pasta `/opt/whatsaya`, compose [`docker-compose.easypanel.yml`](../../docker-compose.easypanel.yml), porta `9119` no host.
3. **`.env`** — mínimo da seção 3. Um provider só (outras chaves vazias). `WHATSAPP_PIX_KEY` só se o cliente mandou a chave.
4. **Plugin** — seção 4. O compose ainda clona `…/whatsappkit`. Confirme que `/opt/data/.hermes/plugins/whatsapp-manager` é este repo. Depois:
   `docker compose exec hermes hermes plugins enable whatsapp-manager`
5. **Personas** — copie/preencha os templates em `/opt/data`. `grep '{{'` tem que voltar vazio.
6. **QR** — seção 6. Um bridge só no número. Status `connected`.
7. **Fumaça** — seção 7. Se o cliente vê Hermes default (`/sethome`), o plugin não está enabled: vá para `whatsaya-diagnose`.

## Fora desta skill

Compose rewrite, cartão Pix nativo, EasyPanel/Portainer como caminho principal, diagnóstico de bot já no ar (`whatsaya-diagnose`).
