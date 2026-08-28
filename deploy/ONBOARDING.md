# Onboarding de um cliente (operador)

Este é o caminho oficial para replicar o kit. Você opera o servidor; o cliente só entrega conteúdo (persona, catálogo, número, Pix).

Deploy é SSH + `docker compose` na VPS, sem painel — se você (ou seu agente de IA) tem acesso SSH ao host, não precisa de mais nada. Domínio é opcional (veja README.md).

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
- O compose usa bind mount (`/opt/whatsaya/data:/opt/data`), não volume Docker nomeado. **Nunca troque pra volume nomeado num host que já tem dados** — `docker compose up -d` cria um volume vazio em vez de montar os dados existentes, e o container sobe "limpo" (sessão do WhatsApp, contatos, vendas — tudo sumindo da vista do container, embora continue intacto em `/opt/whatsaya/data` no disco).
- IP basta. Domínio é opcional.
- Portas no host: `9119` (dashboard + `/whatsapp/qr`) e, se for usar a API, `8642`.

Use [`docker-compose.yml`](docker-compose.yml) — funciona com `docker compose up -d` puro, sem Swarm, sem painel. Um `.env` na mesma pasta preenche as variáveis `${VAR}` do compose.

No `.env` do host, mapeie as portas se o compose não publicar `HOST:CONTAINER`:

```bash
# se o arquivo só lista "9119", ajuste para "9119:9119" no serviço hermes
```

---

## 3. Ambiente

Lista completa e defaults: cabeçalho de [`docker-compose.yml`](docker-compose.yml). Tabela curta: [CLAUDE.md — Ao replicar](../CLAUDE.md#ao-replicar-este-kit-para-outro-cliente).

Mínimo para o bot responder:

- `API_SERVER_KEY` — `openssl rand -hex 32`
- `WHATSAPP_OWNER_NUMBER` / `WHATSAPP_OWNER_NAME`
- **Um** provider de modelo. A cadeia do plugin é Google → OpenAI → OpenRouter e para na primeira chave preenchida. Deixe as outras vazias.
  - OpenRouter: `OPENROUTER_API_KEY` (default da stack)
  - Gemini: `GOOGLE_API_KEY`
  - Codex OAuth: autentique no dashboard do Hermes (fluxo “Other” / Codex). Não misture com chave OpenAI preenchida se a intenção for Codex.
- `WHATSAPP_PIX_KEY` se houver venda no chat
- `HERMES_SETUP_GITHUB_USER` — ver passo 4

`CONFIG_REPO` + `CONFIG_GITHUB_TOKEN` vêm **vazios** e o normal é deixar assim: o GitHub do projeto é do produto (código e templates), não dos dados de operação do cliente. Se for preencher, preencha **os dois** — só um faz o dono receber "não consegui sincronizar" no WhatsApp a cada contato salvo e venda registrada.

Backup nesse modo é local: instale `deploy/backup-whatsaya.sh` no cron (instruções no cabeçalho do próprio arquivo). Ele inclui a sessão do Baileys, então restaurar não obriga a reparear o número.

### Campanhas CTWA e mercado do lead

Use `WHATSAPP_LEAD_CAMPAIGN_METADATA_JSON` para associar o ID ou nome nativo de uma campanha Click-to-WhatsApp ao mercado comercial correto. As chaves são comparadas exatamente com `smbClientCampaignId`, `smbServerCampaignId` ou `utm.utmCampaign`, nessa ordem de prioridade; texto do anúncio, palavras do nome da campanha e idioma da primeira mensagem nunca são usados para adivinhar o mercado.

Exemplo no `.env` para uma campanha dos EUA cujo atendimento começa em espanhol:

```dotenv
WHATSAPP_LEAD_CAMPAIGN_METADATA_JSON='{"120012345678900001":{"market_id":"US","language":"es","timezone":"America/New_York","origin":"meta_ads"}}'
```

Cada entrada aceita somente `market_id` (`BR` ou `US`), `language` (`pt`, `en` ou `es`), `timezone` e `origin` (strings de até 100 caracteres). O mercado governa moeda, oferta e pagamento; o idioma governa apenas a língua da conversa. Portanto, um lead da campanha dos EUA que escreve em espanhol continua no fluxo internacional em USD/Zelle. Campanhas desconhecidas não herdam metadados por semelhança de nome. Depois de alterar essa variável, rode `docker compose up -d` para recriar o container; `docker restart` não atualiza o ambiente.

Quando o WhatsApp entrega dados nativos do anúncio, o bridge armazena no cadastro do contato somente `origin` e o identificador/nome exato de `campaign`, mesmo que essa campanha ainda não exista no mapa acima. Isso preserva a atribuição para análise, relatórios ou classificação posterior sem confiar em texto livre do anúncio. Uma campanha sem mapeamento não define mercado automaticamente; até existir uma origem confiável ou o lead informar onde a empresa opera, o fluxo deve perguntar o país.

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

Esta composição usa `WHATSAPP_CONFIG_SUBDIR=instance` por padrão: no primeiro boot, `SOUL.md`, `SOUL_WHATSAPP.md` e `support_rules.md` vêm de `deploy/instance/`; arquivos não específicos, como `SOUL_EMAIL.md`, vêm de `deploy/`. Use `WHATSAPP_CONFIG_SUBDIR=generic` somente em instalações que devam partir dos templates genéricos.

O compose só copia esses arquivos se **ainda não existirem** em `/opt/data`. Em uma instalação existente, atualizar o código não substitui a configuração comercial ativa: copie ou sincronize explicitamente os arquivos da instância. Confirme `WHATSAPP_CONFIG_SUBDIR=instance` no `.env` e rode `docker compose up -d`; um `docker restart` isolado não aplica essa variável nem ativa os gates específicos da AYA.

```bash
grep -n '{{' /opt/data/SOUL.md /opt/data/SOUL_WHATSAPP.md /opt/data/SOUL_EMAIL.md /opt/data/support_rules.md
```

Zero matches. Depois: restart do container para o plugin reler.

`SOUL.md` = dono (self-chat, ferramentas). `SOUL_WHATSAPP.md` + `support_rules.md` = clientes.

---

## 6. Parear o WhatsApp

A URL `/whatsapp/qr` serve para **reconexão**, não para o primeiro pareamento. Para o primeiro QR:

1. Suba com `WHATSAPP_ENABLED=false` (mantém o gateway estável enquanto plugin/personas terminam de ser conferidos).
2. Por SSH: `docker compose exec hermes hermes whatsapp` e escaneie o QR que aparece no terminal — **Aparelhos conectados → Conectar um aparelho** no celular.
3. Mude `WHATSAPP_ENABLED=true` no `.env` e `docker compose up -d` de novo (recreate — variável de ambiente não pega só com restart).
4. Daí em diante `http://IP:9119/whatsapp/qr` (ou `?format=png`) e `…/whatsapp/status` funcionam para reconexões futuras.

O card do dashboard (Bot / Self-chat) lê `WHATSAPP_MODE` do `.env` do Hermes (`/opt/data/.hermes/.env`). O número `15551234567` é só placeholder da UI — a allowlist real é `WHATSAPP_ALLOWED_USERS`. **Deixe Mode = Bot.** Self-chat nativo do Hermes atende só você mesmo e corta os clientes. Comando do dono no “mensagem para si” (`quais comandos`, `stop_bot`) já funciona em modo Bot, via plugin. O compose regrava isso no boot para não sumir no reset.

Modelo persistente: clientes/WhatsApp = `WHATSAPP_CLIENT_MODEL` (padrão `gpt-5.6-terra`, `WHATSAPP_CLIENT_REASONING_EFFORT` padrão `medium`). Uso interno no perfil default = `WHATSAPP_OWNER_MODEL` (padrão `gpt-5.6-luna`, `WHATSAPP_OWNER_REASONING_EFFORT` padrão `high` — não `max`, porque o `deepseek-v4-flash` do fallback nem sempre suporta esse nível). Sem isso o dashboard volta para o modelo que estiver no `config.yaml` antigo.

Se `/whatsapp/qr` do dashboard não gerar o primeiro QR, o fallback que funcionou em campo é o fluxo pair-only da ponte na porta `8080` (processo à parte). Depois do scan: pare esse processo, deixe só o bridge do container, confirme `connected` em `/whatsapp/status`.

Para manter uma página de QR independente do dashboard, instale o serviço versionado no host:

```bash
sudo apt-get install -y python3-qrcode
sudo install -m 0755 deploy/qr-server.py /opt/whatsaya/qr-server.py
sudo install -m 0644 deploy/whatsaya-qr.service /etc/systemd/system/whatsaya-qr.service
sudo systemctl daemon-reload
sudo systemctl enable --now whatsaya-qr.service
curl -fsS http://127.0.0.1/whatsapp/status
```

O serviço consulta o bridge vivo dentro do container; um `creds.json` antigo não é tratado como conexão ativa. Por padrão ele escuta só em `127.0.0.1`: publique a página por um proxy reverso autenticado, porque o QR permite vincular uma nova sessão do WhatsApp.

Não pareie o mesmo número em dois bridges ao mesmo tempo — Baileys cai com `440 conflict / replaced`.

### Follow-up transacional

O plugin copia `tick_whatsapp_followups.py` para `/opt/data/.hermes/scripts` em todo boot. Crie o ticker uma única vez e confirme que não há job duplicado:

```bash
docker compose exec hermes hermes cron list
docker compose exec hermes hermes cron create 1m \
  --name wa-silencio-followup \
  --script /opt/data/.hermes/scripts/tick_whatsapp_followups.py \
  --no-agent
```

O padrão do template é `WHATSAPP_FOLLOWUP_SILENCE_MIN=5`. O ticker só envia para lead comercial explicitamente habilitado, revalida takeover/opt-out antes do envio e não usa LLM. Contatos pessoais ou com escopo comercial não confirmado ficam pausados e têm follow-up cancelado.

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

Antes de encaminhar a versão para QA Final, execute o runbook
[`AYA_V1_DEFINITION_OF_DONE.md`](AYA_V1_DEFINITION_OF_DONE.md). `npm test` sozinho
valida os guards, mas não substitui os cenários de staging que dependem de modelo,
memória, sessão e entrega real.

### Reset de um contato de teste

O reset é dry-run por padrão. Confira os aliases `@lid` encontrados e só aplique com o
container parado, para o SessionStore não regravar a sessão durante o shutdown:

```bash
docker stop hermes
WA_BASE=/opt/whatsaya/data python3 deploy/scripts/wa_reset_contact.py NUMERO_DE_TESTE
WA_BASE=/opt/whatsaya/data python3 deploy/scripts/wa_reset_contact.py NUMERO_DE_TESTE --apply
docker start hermes
```

O script cria backup antes dos `DELETE`. Nunca use esse procedimento em contato real ou
sem conferir o número resolvido no dry-run.

**Riscos conhecidos:**
- Transcrição de áudio **não passa por LLM**. Desde 22/08/2026 o áudio vai para o ASR do Fish (`POST /v1/asr`, multipart, mesma `FISH_API_KEY` do TTS) e o `WHATSAPP_CLIENT_MEDIA_MODEL` cuida só de imagem. O motivo: nenhum slug de texto/visão do OpenRouter aceita entrada de áudio — a chamada volta `404 No endpoints found that support input audio` e o agente recebe `[audio received]` cru, sem saber que houve áudio. Log de sucesso: `[asr] fish ok tentativa=1 dur=... chars=...`; de falha: `[asr] transcrição não obtida para <arquivo>: ...`. **A API credit do Fish é carteira separada da plataforma**: o TTS pode continuar funcionando no modelo grátis enquanto o ASR devolve `402 Insufficient API credit` — confira em https://fish.audio/app/developers.
- `git: dubious ownership` no boot é corrigido automaticamente pelo próprio `command:` do compose (`git config --global --add safe.directory`) — não é erro.
- `Cannot find package '@whiskeysockets/baileys'` ao rodar os testes locais significa que as dependências ainda não foram instaladas — rode `npm ci` antes de `npm test`.

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

Áudio: o Hermes 0.20+ transcreve com Whisper local e, sem idioma, assume inglês. O compose grava `stt.language: pt` (e `HERMES_LOCAL_STT_LANGUAGE=pt`) para esse fallback local. O plugin da AYA usa Fish ASR e, por padrão, omite `language` para detectar português, inglês ou espanhol automaticamente; `WHATSAPP_STT_LANGUAGE=pt|en|es` força um idioma quando necessário. `stt.echo_transcripts: false` — a transcrição fica só no contexto do agente; o cliente não vê a bolha `🎙️ "..."`.

Voz de resposta: Fish Audio, modelo `s2.1-pro-free` (campanha grátis até 31/08/2026; no dia 30 o cron avisa o dono para trocar para `s2.1-pro`). Gere a chave em https://fish.audio/app/api-keys/ e coloque `FISH_API_KEY` no `.env`. Opcional: `FISH_REFERENCE_ID`, `FISH_TTS_MODEL` e `FISH_TTS_VOLUME` (padrão `4`). Resposta falável vai em nota de voz **apenas quando o lead mandou áudio** (gate de modalidade: quem escreve recebe texto). `WHATSAPP_AUTO_TTS=false` desliga a voz de vez. Dados de pagamento, endereço, link, e-mail, código e intro do tipo “vou te enviar um audio” ficam **texto**. Sem chave, o texto continua indo. Tags `[happy]` / `[confident]` / `[empathetic]` / `[warm and friendly]` são só para o Fish: o plugin corta no `_human_send` e o adapter corta no `send`, mesmo se o `fish_tts.py` não carregar. Se a tag aparecer no chat, o volume está com plugin velho. `[número omitido]` não é tag de voz e fica. Preço no áudio vai por extenso na moeda do mercado. O primeiro fragmento de texto espera até 14s para juntar mensagens rápidas do mesmo lead (`WHATSAPP_DEBOUNCE_INITIAL_MS=14000`); fragmentos seguintes reduzem progressivamente essa espera.

---

## 10. Depois do ar

- Atualizar plugin: push neste repo → `docker restart hermes` (ou `docker compose restart`) já basta — o boot faz `fetch` + `reset --hard` do plugin. Confira no log `bridge.js atualizado` / `whatsapp-manager`.
- Mudou env var ou o próprio `docker-compose.yml`? `docker restart` **não** pega — precisa `docker compose up -d` (recria o container com o `Cmd`/env novos).
- Mudar só persona/catálogo: edite `/opt/data/SOUL_WHATSAPP.md` e `support_rules.md` (ou o `CONFIG_REPO`) e reinicie.
- `stop_bot` / `start_bot` só valem no self-chat do dono.
- Cliente seguinte: volte ao passo 1. Código igual; mudam env + templates.

---

Aviso histórico: este kit não é o fork `leoalvesia/whatsappkit` (Comunidade Empreendedor Serial, Portainer, Gemini, `setup.sh` via curl). Não faça fork de `whatsappkit` nem rode aquele `setup.sh` apontando pra este projeto — não é o mesmo fluxo.
