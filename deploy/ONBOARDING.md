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
| Texto com `[confident]` / `[empathetic]` / `[happy]` | Cliente **não** vê a tag. Se vazar, o strip do `_human_send` / adapter não está no plugin que está rodando |

Sessão de teste suja o histórico. Apague a sessão Hermes daquele JID se for repetir o teste do zero.

---

## 8. Fullsync e triagem inicial

Depois do primeiro pareamento e de `/whatsapp/status` ficar `connected`, rode o pipeline em [`WHATSAPP_HISTORY_TRIAGE.md`](WHATSAPP_HISTORY_TRIAGE.md). Mensagens históricas vão para o SQLite e **não entram na fila do LLM**.

```bash
python3 deploy/scripts/whatsapp_history_triage.py \
  --config deploy/scripts/whatsapp_history_triage.yaml run
```

Isso espera o lote estabilizar e grava snapshot, CSV e o prompt da skill `whatsapp-client-triage`. Depois da classificação:

```bash
python3 deploy/scripts/whatsapp_history_triage.py \
  --config deploy/scripts/whatsapp_history_triage.yaml report \
  --snapshot /opt/data/.hermes/workspace/whatsapp_fullsync_YYYY-MM-DD.json \
  --classification /opt/data/.hermes/workspace/whatsapp_classification.json
```

O Markdown de revisão sai em `/opt/data/.hermes/workspace/whatsapp_triagem_revisao_cliente_YYYY-MM-DD.md`. O dono valida antes de qualquer flag ir para `personal_contacts.json`. Envio ao self-chat é explícito (`send`) e exige `--chat-id` do dono.

Não envie o snapshot bruto. Não declare o onboarding completo sem conferir `historical > 0` quando o número tem histórico.

---

## 9. Skills do Hermes (dono)

O perfil `whatsapp` (cliente) já sobe com `skills.enabled: false`. O perfil do dono herda o catálogo bundled — dezenas de skills de studio, MLOps, GitHub e desktop. O `command:` do compose grava `skills.disabled` no `config.yaml` a cada boot.

Ficam ligadas só as úteis nesta operação: `session-librarian`, OCR/PDF/DOCX, Google Workspace / e-mail, Notion (onboarding de clientes), mapas, atas, `plan`, `hermes-agent`, `grounded-citations`.

Para ver: `docker compose exec hermes hermes skills list`.

WhatsApp não recebe status interno do gateway: o compose grava `display.busy_ack_enabled: false`, `busy_input_mode: queue` e desliga heartbeat/clarify. O cliente não deve ver `⚡ Interrupting`, `⏳ Working` nem `iteration N/60`. PDF/proposta sai com subagentes em paralelo, sem a ferramenta `clarify`.

Áudio: o Hermes 0.20+ transcreve com Whisper local e, sem idioma, assume inglês. O compose grava `stt.language: pt` (e `HERMES_LOCAL_STT_LANGUAGE=pt`). Sem isso o PTT em português vira tradução zoada. `stt.echo_transcripts: false` — a transcrição fica só no contexto do agente; o cliente não vê a bolha `🎙️ "..."`.

Voz de resposta: Fish Audio, modelo `s2.1-pro-free` (campanha grátis até 31/08/2026; no dia 30 o cron avisa o dono para trocar para `s2.1-pro`). Gere a chave em https://fish.audio/app/api-keys/ e coloque `FISH_API_KEY` no `.env`. Opcional: `FISH_REFERENCE_ID`, `FISH_TTS_MODEL` e `FISH_TTS_VOLUME` (padrão `4`). Resposta falável vai **só em nota de voz**. PIX, endereço, link, e-mail, código e intro do tipo “vou te enviar um audio” ficam **texto**. Sem chave, o texto continua indo. Tags `[happy]` / `[confident]` / `[empathetic]` / `[warm and friendly]` são só para o Fish: o plugin corta no `_human_send` e o adapter corta no `send`, mesmo se o `fish_tts.py` não carregar. Se a tag aparecer no chat, o volume está com plugin velho. `[número omitido]` não é tag de voz e fica. Preço no áudio vai por extenso (`997 reais`). Duas mensagens seguidas esperam 2,5s e entram no mesmo lote (`WHATSAPP_DEBOUNCE_INITIAL_MS=2500`).

---

## 10. Depois do ar

- Atualizar plugin: push neste repo → restart. Confira no log `bridge.js atualizado` / `whatsapp-manager`.
- Mudar só persona/catálogo: edite `/opt/data/SOUL_WHATSAPP.md` e `support_rules.md` (ou o `CONFIG_REPO`) e reinicie.
- `stop_bot` / `start_bot` só valem no self-chat do dono.
- Cliente seguinte: volte ao passo 1. Código igual; mudam env + templates.
