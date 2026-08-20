---
name: deploy-plugin
description: "Realiza o deploy do plugin whatsapp-manager no servidor Hermes — commit, push, pull e restart do container."
category: deploy
---

# Deploy do Plugin WhatsApp Manager

Esta skill guia o processo completo de deploy de alterações no plugin `whatsapp-manager` para o servidor Hermes em produção.

---

## Quando usar esta skill

Use quando o usuário disser algo como:
- "faz o deploy"
- "publica as alterações"
- "sobe pro servidor"
- "atualiza o plugin"
- "deploy do whatsapp"
- "deploy do plugin"
- "manda pro hermes"

---

## Pré-requisitos

- O repositório local está no workspace ativo
- O remote `origin` aponta para o repositório no GitHub
- O servidor Hermes tem o plugin clonado em `/opt/data/.hermes/plugins/whatsapp-manager`
- Acesso SSH ao host que roda o container `hermes` (não precisa de painel nenhum)

---

## Etapa 1 — Commit e Push no GitHub

### 1.1 Verificar o que mudou

```bash
git status
```

Revisar as alterações e confirmar com o usuário se está tudo certo.

### 1.2 Adicionar e commitar

```bash
git add -A && git commit -m "MENSAGEM_DO_COMMIT"
```

> **Regra:** A mensagem de commit deve ser descritiva e em português. Exemplos:
> - `fix: bot continuava respondendo após stop_bot`
> - `feat: adiciona delay na primeira resposta ao cliente`
> - `chore: atualiza SOUL_WHATSAPP.md com novas regras`

### 1.3 Push para o GitHub

```bash
git push origin main
```

Se der erro de divergência, usar `git pull --rebase origin main` antes do push.

---

## Etapa 2 — Git Pull dentro do container (SSH)

O plugin é um clone git de verdade dentro do volume — atualiza com um `git pull` comum, via SSH.

**Antes de puxar: `git fetch origin` e comparar com o `main` local.** Se outra sessão/agente também estiver trabalhando neste repo, `origin/main` pode ter avançado com commits que você não tem localmente — nesse caso, integre (`git log --oneline main..origin/main`) antes de dar push, ou seu commit vai divergir e o pull no servidor vai falhar/conflitar.

```bash
ssh <host-da-vps>
docker exec hermes sh -c 'cd /opt/data/.hermes/plugins/whatsapp-manager && git fetch origin && git pull --ff-only origin main && git log --oneline -1'
```

> **Verificar:** o `git log` no fim deve mostrar o commit que você acabou de dar push. Se o `pull --ff-only` falhar, o clone do servidor tem commits locais não sincronizados — investigue antes de forçar (`reset --hard` descarta trabalho, só use se tiver certeza).

---

## Etapa 3 — Restart (ou recreate) do Container

As alterações no plugin só são carregadas quando o Hermes reinicia. Isso é feito por SSH:

```bash
docker restart hermes
# ou, se o container for gerenciado por docker compose:
docker compose restart hermes    # (a partir da pasta com o docker-compose.yml)
```

**Atenção — restart não é sempre suficiente.** `docker restart` reexecuta o `Cmd` já gravado no container, então pega mudança de *código* (o `git pull` da Etapa 2). Mas se a mudança envolveu o `docker-compose.yml`/`.env` (nova variável de ambiente, novo valor de uma existente), o `Cmd`/env do container ficam desatualizados até você recriar de verdade:

```bash
cd /opt/whatsaya   # ou onde estiver o docker-compose.yml deste host
docker compose up -d   # recria o container com o Cmd/env atualizados
```

---

## Verificação Pós-Deploy

### Conferir se o container subiu

```bash
docker ps --filter name=hermes --format '{{.Status}}'
```

Deve mostrar `Up` com poucos segundos/minutos de uptime.

### Conferir logs do plugin

```bash
docker exec hermes grep "whatsapp-manager" /opt/data/.hermes/logs/hermes.log | tail -20
# ou, se esse arquivo não existir na versão atual do Hermes:
docker logs hermes --since 1m | grep -i "whatsapp-manager\|bridge.js"
```

Procurar por:
- `✓ bridge.js atualizado` (ou `bridge.js atualizado em <path>`) — confirma que o bridge foi copiado
- `✓ Skills registradas` — confirma que as skills carregaram
- Ausência de erros `⚠️` ou `❌`

### Testar o bot

Envie `start_bot` ou `stop_bot` no WhatsApp para confirmar que o bridge está respondendo.

---

## Troubleshooting

| Problema | Solução |
|----------|---------|
| `git pull --ff-only` falha no container | `git fetch origin && git log --oneline origin/main -5` — provavelmente outra sessão pushou commits que você não integrou local antes de dar push. Puxe pro seu repo local, resolva, dê push de novo, então repita a Etapa 2. Só use `git reset --hard origin/main` se tiver certeza de que não há trabalho a preservar no clone do servidor |
| Container não sobe após restart | `docker logs hermes --since 2m` |
| Plugin não carrega as alterações | Conferir se o `git pull` trouxe os arquivos (`git log --oneline -1` no clone do servidor) e se o container foi reiniciado |
| Mudou env var/`docker-compose.yml` mas não pegou | `docker restart` não recarrega env/Cmd — precisa `docker compose up -d` (recreate) |
| `bridge.js` não atualiza no bridge, ou boot loga `Permission denied` copiando `bridge.js` | Flakiness conhecida do bootstrap (`shutil.copy2`, provável race no bind mount). Copie manualmente pros 3 caminhos que o Node pode estar lendo e reinicie: `docker exec hermes sh -c 'for T in /opt/data/.hermes/platforms/whatsapp/bridge/bridge.js /opt/data/.hermes/scripts/whatsapp-bridge/bridge.js /opt/data/.hermes/profiles/whatsapp/scripts/whatsapp-bridge/bridge.js; do cp /opt/data/.hermes/plugins/whatsapp-manager/bridge.js "$T"; done'` |
| Push rejeitado por divergência | `git pull --rebase origin main && git push origin main` |

---

## Notas Importantes

- O **branch principal** é `main`
- O plugin é carregado automaticamente pelo Hermes no boot via `register()` em `__init__.py`
- O `bridge.js` é copiado automaticamente do plugin para `/opt/data/.hermes/platforms/whatsapp/bridge/` durante o `register()`
- Arquivos como `SOUL_WHATSAPP.md`, `support_rules.md` e `SOUL_EMAIL.md` são baixados automaticamente na primeira inicialização se não existirem no volume
- O `bot_state.json` (estado de pause do bot) é persistido e **não é afetado** pelo restart
