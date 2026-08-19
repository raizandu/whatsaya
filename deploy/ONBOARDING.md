# Onboarding de um cliente (operador)

Este é o caminho oficial para replicar o kit. Você opera o servidor; o cliente só entrega conteúdo (persona, catálogo, número, Pix).

Portainer, EasyPanel e domínio são caminhos extras — ver [README.md](README.md) e [DEPLOY_EASYPANEL.md](DEPLOY_EASYPANEL.md). Não comece por eles.

Agente: use a skill `whatsaya-onboard` para executar este roteiro e `whatsaya-diagnose` quando o bot já está no ar e o comportamento está errado.

---

## 1. Intake — o cliente manda isto antes do `up`

| Campo | Env / arquivo | Formato |
|---|---|---|
| Nome como os clientes chamam | `WHATSAPP_OWNER_NAME` + `{{OWNER_FIRST_NAME}}` | texto |
| Nome completo | `{{OWNER_NAME}}` nos SOULs | texto |
| WhatsApp do dono | `WHATSAPP_OWNER_NUMBER` | internacional sem `+` (`5562…`) |
| Chave Pix | `WHATSAPP_PIX_KEY` | sem default — vazio é melhor que chave errada |
| Catálogo e preços | `support_rules.md` | só o que existe de verdade |
| Tom / o que nunca dizer | `SOUL_WHATSAPP.md` | preencher todo `{{…}}` |

Não suba com placeholder. `{{PIX_KEY}}` literal no chat e produto inventado vêm daqui.

Não copie CNPJ, preço ou nome de outro cliente para o código. Cliente novo = env + templates.

---

## 2. VPS

- Ubuntu 24, Docker Engine + plugin Compose v2.
- SSH por chave. Pasta típica: `/opt/whatsaya`.
- IP basta. Domínio é opcional.
- Portas no host: `9119` (dashboard + `/whatsapp/qr`) e, se for usar a API, `8642`.

Use [`docker-compose.easypanel.yml`](docker-compose.easypanel.yml) neste caminho (funciona com `docker compose up`, sem Swarm). O [`docker-compose.yml`](docker-compose.yml) é Swarm/Portainer.

No `.env` do host, mapeie as portas se o compose não publicar `HOST:CONTAINER`:

```bash
# se o arquivo só lista "9119", ajuste para "9119:9119" no serviço hermes
```

---

## 3. Ambiente

Lista completa e defaults: cabeçalho de [`docker-compose.easypanel.yml`](docker-compose.easypanel.yml). Tabela curta: [CLAUDE.md — Ao replicar](../CLAUDE.md#ao-replicar-este-kit-para-outro-cliente).

Mínimo para o bot responder:

- `API_SERVER_KEY` — `openssl rand -hex 32`
- `WHATSAPP_OWNER_NUMBER` / `WHATSAPP_OWNER_NAME`
- **Um** provider de modelo. A cadeia do plugin é Google → OpenAI → OpenRouter e para na primeira chave preenchida. Deixe as outras vazias.
  - OpenRouter: `OPENROUTER_API_KEY` (default da stack EasyPanel)
  - Gemini: `GOOGLE_API_KEY`
  - Codex OAuth: autentique no dashboard do Hermes (fluxo “Other” / Codex). Não misture com chave OpenAI preenchida se a intenção for Codex.
- `WHATSAPP_PIX_KEY` se houver venda no chat
- `HERMES_SETUP_GITHUB_USER` — ver passo 4

`CONFIG_REPO` + `CONFIG_GITHUB_TOKEN` são opcionais (backup de contatos/personas). Sem eles o bot funciona no volume.

---

## 4. Plugin no volume

O `command:` do compose clona

```text
https://github.com/${HERMES_SETUP_GITHUB_USER:-raizandu}/${HERMES_SETUP_GITHUB_REPO:-whatsaya}
```

Defaults: usuário `raizandu`, repo `whatsaya`. Sobrescreva só se o fork do cliente tiver outro path.

Depois do primeiro `docker compose up -d`:

1. Confira se o código em `/opt/data/.hermes/plugins/whatsapp-manager` é **este** repo (`plugin.yaml` name `whatsapp-manager`, arquivos `whatsapp_manager.py` + `bridge.js` da raiz).
2. Se o clone falhou: copie este repo para esse diretório (incluindo `.git` se quiser puxar updates).
3. Correção de código = commit neste repo + restart. O boot faz `fetch` + `reset --hard` na `main`.
4. Patch temporário no volume: `KEEP_LOCAL_PLUGIN=true` no `.env` e recreate. Sem isso o próximo boot apaga o patch. O auto-update do plugin (`_self_update_plugin_code`) também respeita essa flag.
5. Habilite o plugin. `plugins.enabled: []` faz o cliente receber o Hermes padrão (`/sethome`), não a persona:

```bash
docker compose exec hermes hermes plugins enable whatsapp-manager
```

6. Se a sessão Baileys não gravar creds, o diretório costuma estar root-owned. Ajuste para o user do container (na prática, `10000`) e reinicie:

```bash
docker compose exec hermes ls -ld /opt/data/.hermes/platforms/whatsapp/session
# no host, no volume montado:
sudo chown -R 10000:10000 /opt/data/.hermes/platforms/whatsapp/session
```

---

## 5. Personas no volume

O compose só copia `SOUL*.md` e `support_rules.md` se **ainda não existirem** em `/opt/data`. Preencha os templates de `deploy/` **antes** do primeiro boot, ou edite os arquivos já copiados no volume.

```bash
grep -n '{{' /opt/data/SOUL.md /opt/data/SOUL_WHATSAPP.md /opt/data/SOUL_EMAIL.md /opt/data/support_rules.md
```

Zero matches. Depois: restart do container para o plugin reler.

`SOUL.md` = dono (self-chat, ferramentas). `SOUL_WHATSAPP.md` + `support_rules.md` = clientes.

---

## 6. Parear o WhatsApp

1. Suba com `WHATSAPP_ENABLED=true` só depois do plugin e das personas existirem. Para o *primeiro* QR, se o gateway ficar instável, use `false`, pareie, depois volte para `true` — detalhe em [DEPLOY_EASYPANEL.md](DEPLOY_EASYPANEL.md).
2. Abra `http://IP:9119/whatsapp/qr` (ou `?format=png`) e `…/whatsapp/status`.
3. No celular: **Aparelhos conectados → Conectar um aparelho**.

Se `/whatsapp/qr` do dashboard não gerar o primeiro QR, o fallback que funcionou em campo é o fluxo pair-only da ponte na porta `8080` (processo à parte). Depois do scan: pare esse processo, deixe só o bridge do container, confirme `connected` em `/whatsapp/status`.

Não pareie o mesmo número em dois bridges ao mesmo tempo — Baileys cai com `440 conflict / replaced`.

---

## 7. Fumaça (obrigatório)

Não declare o cliente no ar sem isto:

| Teste | Esperado |
|---|---|
| Mensagem de um número que **não** é o dono | Tom de `SOUL_WHATSAPP` + catálogo. **Não** é onboarding do Hermes (`/sethome`, “set your home”) |
| `{{` em qualquer resposta | Falhou o passo 5 |
| Dono no self-chat: `quais comandos` | Ajuda do plugin (`stop_bot`, `start_bot`, …) |
| Duas ideias na resposta | Duas bolhas, se o `main` já tiver o hook `transform_llm_output` (PR de bolhas). Um bloco só + SOUL pedindo `\n\n` = hook ausente ou plugin velho |

Sessão de teste suja o histórico. Apague a sessão Hermes daquele JID se for repetir o teste do zero.

---

## 8. Skills do Hermes (dono)

O perfil `whatsapp` (cliente) já sobe com `skills.enabled: false`. O perfil do dono herda o catálogo bundled — dezenas de skills de studio, MLOps, GitHub e desktop. O `command:` do compose grava `skills.disabled` no `config.yaml` a cada boot.

Ficam ligadas só as úteis nesta operação: `session-librarian`, OCR/PDF/DOCX, Google Workspace / e-mail, Notion (onboarding de clientes), mapas, atas, `plan`, `hermes-agent`, `grounded-citations`.

Para ver: `docker compose exec hermes hermes skills list`.

Áudio: o Hermes 0.20+ transcreve com Whisper local e, sem idioma, assume inglês. O compose grava `stt.language: pt` (e `HERMES_LOCAL_STT_LANGUAGE=pt`). Sem isso o PTT em português vira tradução zoada.

---

## 9. Depois do ar

- Atualizar plugin: push neste repo → restart. Confira no log `bridge.js atualizado` / `whatsapp-manager`.
- Mudar só persona/catálogo: edite `/opt/data/SOUL_WHATSAPP.md` e `support_rules.md` (ou o `CONFIG_REPO`) e reinicie.
- `stop_bot` / `start_bot` só valem no self-chat do dono.
- Cliente seguinte: volte ao passo 1. Código igual; mudam env + templates.
