# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> Documentação e código deste projeto são em português (pt-BR). Mantenha esse idioma em comentários, logs e mensagens ao usuário.

## O que é este repositório

Plugin **`whatsapp-manager`** (v1.1) para o **Hermes Agent v2026** — não é uma aplicação autônoma. Não existe entrypoint local: nada de `npm start`. O código é instalado dentro do container do Hermes, que carrega `whatsapp_manager.py` e chama `register(ctx)` (final do arquivo) para registrar hooks. Deploy é SSH + `docker compose` direto na VPS — sem painel.

Licença **BUSL-1.1** (Licensor: André Alencar, Change Date 2031-06-25 → MIT). Copiar/modificar/redistribuir é permitido; o Additional Use Grant limita o uso a "development, evaluation, and personal testing" — uso em produção exige licença comercial do autor.

## Comandos

```bash
npm install                                   # deps do bridge (package-lock.json é gitignorado de propósito)
npm test                                      # suíte completa: node --test tests/bridge.test.js && python3 -m unittest tests/plugin_test.py
python3 -m unittest tests/plugin_test.py      # só Python (312 testes, 49 classes)
node --test tests/bridge.test.js              # só o bridge (23 asserções; o processo
                                              # não encerra sozinho: importar bridge.js
                                              # sobe o Express na 3000)
python3 validate_dedup.py                     # validação de dedup (roda dentro do container)
```

Rodar um único teste ou classe:

```bash
python3 -m unittest tests.plugin_test.TestSalesDetection
python3 -m unittest tests.plugin_test.TestSalesDetection.test_find_product_matches_partial_keyword
```

**Requer Python ≥ 3.12.** `whatsapp_manager.py` usa f-strings com backslash na parte de expressão (PEP 701); em 3.11 o arquivo nem compila.

**A suíte tem efeitos colaterais reais.** Importar `whatsapp_manager` dispara `register()`, que escreve em `/opt/data/.hermes/...` e faz requisições HTTP ao GitHub no boot. Fora do container Linux isso cria diretórios no host (em Windows, `C:\opt\data`). Os testes assumem paths POSIX — em Windows, asserts de path (`/opt/data/...` vs `\opt\data\...`) falham por diferença de separador, sem ser bug real. Rode a suíte no container ou em Linux.

Scripts de diagnóstico ficam em `deploy/scripts/` (`diagnose_bridge_dedup.py`, `diagnose_native_whatsapp_conflict.sh`, `capture_logs.sh`, `test_*.py` avulsos que não fazem parte de `npm test`).

## Arquitetura

### Divisão plugin × core (importante e contraintuitivo)

Desde o Hermes Agent v0.19 ("Quicksilver"), o **core** (`hermes_plugins.whatsapp_platform`) é dono do ciclo de vida do processo Node: spawn, pidfile com detecção de PID reciclado, restart em crash, e parsing das mensagens recebidas. Este plugin **não spawna mais o `bridge.js`** — `register()` apenas copia o arquivo para `/opt/data/.hermes/platforms/whatsapp/bridge/` para o core encontrar. Isso eliminou o container duplicado e o erro de desconexão `440 conflict / replaced`. Toda a regra de negócio fica nos hooks Python.

**Um único gateway consome o WhatsApp — o do perfil fica off.** O s6 do container sobe um gateway por perfil (`hermes -p whatsapp gateway run`), e cada um lê só o `.env` do próprio perfil. Por isso o compose grava `WHATSAPP_ENABLED=false` em `profiles/whatsapp/.env` (e `true` só no `.env` principal): com `true` nos dois, o gateway do perfil subia como segundo consumidor do mesmo bridge **sem o plugin carregado** — respondia contato bloqueado/silenciado por fora de todas as regras, e a duplicata só não aparecia porque o dedup do bridge engolia. O perfil `whatsapp` existe apenas como perfil de isolamento das conversas de cliente dentro do gateway principal. Como o `command:` do compose reescreve esses `.env` a cada boot do container, o estado se autocorrige em restart/recreate/reboot.

### Os hooks (`whatsapp_manager.py`, ~7.200 linhas)

| Hook | Linha | Responsabilidade |
|---|---|---|
| `pre_gateway_dispatch` | ~7233 | O maior. Roteia dono × cliente, executa comandos de controle, update de contato em linguagem natural, catálogo de produtos e registro de vendas |
| `pre_llm_call` | ~8626 | Detecta pergunta cross-session ("o que a Isabel falou sobre X?") e injeta histórico de `whatsapp_messages.db` + `state.db` no contexto |
| `pre_tool_call` | ~8957 | Firewall: aborta qualquer chamada de ferramenta vinda do perfil `whatsapp` (clientes) |
| `transform_llm_output` | ~9378 | Único caminho de saída para cliente: extrai o marcador `[[HANDOFF: motivo]]`, dispara o aviso real ao dono e agenda a entrega em bolhas. Devolve `"\n"` para o Hermes não reenviar o bloco |
| `post_llm_call` | ~9438 | Só sessão do dono: executa os `EXEC` de update de contato. Para cliente retorna `None` — o Hermes ignora o retorno deste hook no turno final |
| `register` | ~9604 | Instala `bridge.js` no volume, migra a sessão Baileys do path antigo, inicializa SOULs e `personal_contacts.json`, e sobe o watchdog de recepção |

### Handoff real e watchdog de recepção

Duas coisas que a IA **não** consegue fazer por conta própria, e que o plugin resolve fora do prompt:

- **Handoff.** A persona termina a resposta com `[[HANDOFF: motivo]]`. `transform_llm_output` tira o marcador antes de a mensagem sair, e `_notify_owner_handoff` manda para o self-chat do dono um card com nome, número, motivo e as últimas 8 mensagens. Há cooldown de 15 min por chat, e o cooldown é revertido se o envio falhar — melhor repetir o aviso do que engolir. Só depois de escrever o marcador a IA pode dizer que avisou; sem ele, "já encaminhei" é alucinação e o prompt proíbe.
- **Watchdog de recepção.** Toda mensagem de lead que chega ao fim de `pre_gateway_dispatch` entra em `_pending_inbound`; a entrega confirmada em `_deliver_contact_reply` remove. O que passar de `WHATSAPP_UNANSWERED_ALERT_S` (padrão 180s) vira `logger.error` e aviso ao dono. Existe porque no QA de 21/08 uma mensagem morreu em silêncio: o Codex estourou a cota (`429`) e o fallback OpenRouter estava sem crédito.

### Auditoria diária (`daily_audit.py` + `deploy/scripts/tick_whatsapp_audit.py`)

Módulo **puro**, no mesmo molde de `commercial_followups.py`: não envia mensagem, não
chama LLM e não importa o plugin. Lê linhas de log e linhas de banco, agrega o dia e
devolve texto. Quem agenda, chama o modelo e entrega ao dono é o `whatsapp_manager`.

Três coisas contraintuitivas:

- **A fonte do log é um arquivo, não `docker logs`.** O plugin escrevia só no stdout do
  container, e o cron do auditor roda *dentro* dele, onde `docker` não existe. Por isso
  `_attach_plugin_file_log()` espelha o logger em `logs/whatsapp_plugin.log` (rotativo).
  Isso também tira a retenção do log das mãos do daemon do Docker.
- **O placar "guarda salvou × modelo acertou" é calculado por código, nunca pelo modelo.**
  É a distinção que o relatório existe para mostrar: um dia em que a guarda determinística
  segurou cada turno não é um dia bom, e um auditor livre para resumir diria que foi.
  `classify_reply` marca o turno pela presença de uma frase conhecida da guarda —
  `FallbackCatalogDriftTest` é o que impede o catálogo de divergir das constantes do
  plugin em silêncio.
- **Nada de valor de credencial sai da máquina.** `redact()` corta documento, telefone,
  e-mail e chave, e preserva preço (que é a evidência do achado); `mask_chat()` deixa só
  os 4 últimos dígitos. A prova de que a credencial reproduzida era a real vai pela
  classificação do log (`digits=official:…` × `unknown:…`), não pelo valor.

O parser do log lê as gerações antigas do `[payment-gate]` (`methods=` antes de
`markets=`/`prices=`) de propósito: no dia de um deploy as duas convivem no mesmo arquivo.

Agendamento é o cron do próprio Hermes, como no follow-up:

```bash
hermes cron create "0 20 * * *" --name wa-auditoria-diaria \
  --script /opt/data/.hermes/scripts/tick_whatsapp_audit.py --no-agent
```

`register()` copia o tick para `/opt/data/.hermes/scripts/` no boot. Rodar
`tick_whatsapp_audit.py 2026-08-24` reprocessa um dia específico.

**Dois modos, e a escolha é sobre qual provider paga a conta.** No modo script
(`--no-agent`) o plugin chama o modelo direto por chave — só sabe Google/OpenAI/
OpenRouter, porque credencial do backend Codex é config do *gateway*, não do
plugin. No modo agente (`--material`, cron registrado **sem** `--no-agent`) o
tick não chama LLM nenhuma: imprime instruções + material no stdout e o agente
do Hermes produz o parecer, herdando a cadeia Codex→OpenRouter da assinatura.
A diretiva do dono é Codex primeiro, OpenRouter só em erro — logo, modo agente.
O custo aceito: o veredito não volta ao processo, então o portão sim/não da fase
2 não arma e a proposta vira nota para aplicar à mão.

Atenção ao `max_tokens`: sem teto explícito o OpenRouter **reserva** o máximo de
saída do modelo e cobra a reserva, não o uso — a primeira auditoria morreu com
`402 requested up to 65536 tokens, but can only afford 19788`.

**Propostas com portão (fase 2).** O auditor responde em JSON e cada achado vem
tipado; o tipo decide quem aplica, e os limites são de segurança, não de gosto:

| Tipo | Portão |
|---|---|
| `DADO` | Único aplicável por *sim/não* no chat do dono (`_pending_audit_action`, TTL 15 min, ao lado do fluxo de catálogo em `pre_gateway_dispatch`). Só nota de contato e campo de item de catálogo |
| `PROMPT` | Nunca automático — o texto iria direto ao prompt de produção sem suíte cobrindo, e a regra medida aqui é que instruir não funciona |
| `CODIGO` | Nunca automático. Vira corpo de ticket no relatório, no ciclo de 24/08 (achado com texto cru → teste vermelho com a frase literal → filtro determinístico → deploy) |

`pix_key` e `link` ficam **fora do aplicável mesmo sendo campo de catálogo**: são
destino de dinheiro e de tráfego, um "sim" distraído não é consentimento
suficiente para redirecionar pagamento, e o modelo deste sistema já reproduziu
credencial real por contaminação de provider. O mesmo vale para `summary`/`tone`/
`guidelines` do contato, que são do classificador. `_apply_audit_proposal`
**revalida o alvo no backend** — é a segunda camada da mesma decisão que mantém
`toolsets: []` no perfil de cliente: o agente não se automodifica por caminho
nenhum.

**Ticket automático.** Achado de `CODIGO` abre ticket na base "Tickets — Suporte"
via `NOTION_API_KEY` + `NOTION_TICKETS_DB` (`POST /v1/pages`). Precisa de chave de
**integração interna** (`ntn_`/`secret_`), não de OAuth — o plugin roda no
container, onde não há MCP —, e a integração tem de estar compartilhada com a
base, senão a API devolve 404 mesmo com a chave certa. Entra como `Status=Triagem`
(criado por máquina, ainda não aceito por ninguém) e `Tipo=Melhoria`; as opções de
`select` são travadas em teste porque valor inexistente faz a API recusar a página
inteira e o achado se perde em silêncio. **Fail-closed:** sem as duas envs não há
chamada, e o corpo do ticket continua saindo no relatório para copiar.

O corpo passa por `redact` antes de sair — o TKT-1 aberto nessa mesma base é
"credenciais de produção em texto aberto no Notion", e a automação não pode
piorar justamente o ticket crítico.

### Dois perfis de isolamento

- **`default`** — dono, no SelfChat. Persona `SOUL.md`, histórico completo, todas as ferramentas.
- **`whatsapp`** — clientes. Persona `SOUL_WHATSAPP.md` + `support_rules.md`, `toolsets: []`, todas as 25 famílias de ferramentas desativadas, `skills.enabled: false`. `pre_tool_call` é a segunda camada, no backend.

### Pausa global × silêncio por chat (não confundir)

São mecanismos distintos, em camadas diferentes:

- **Pausa global** — `stop_bot` / `start_bot` (sinônimos `!pausar`, `!retomar`, `!parar`, `!iniciar`). Aplicada no Node (`bridge.js`), persistida em `bot_state.json` dentro de `SESSION_DIR`. Descarta na origem mensagens de qualquer um que não seja o dono. **Só funciona se enviada pelo dono no self-chat** — digitar na conversa de cliente não faz nada. Estado consultável via `GET /bot-status` (`{ botPaused, uptime }`).
- **Silêncio de 10 min** (`WHATSAPP_SILENCE_DURATION_MIN`) — por chat individual. Dois gatilhos: o dono **lê** a conversa (detectado por `chats.update` quando não-lidas cai para `0`/`-1`), ou o dono **envia mensagem manual** (`fromMe: true` e o id não está em `recentlySentIds`). Mensagens começando com `!` ou comandos de controle não disparam o silêncio. Consultável via `GET /chat-status/:chatId`; `POST /chat-unsilence` limpa manualmente antes dos 10 min.

`DESIGN.md` tem o fluxograma completo em Mermaid.

### Volume `/opt/data` — persistente × efêmero

Persistem: `.hermes/platforms/whatsapp/session/` (creds Baileys), `.hermes/whatsapp_messages.db`, `.hermes/state.db`, `personal_contacts.json`, `support_rules.md`, `SOUL*.md`.

**É wipeado em rebuild:** `.hermes/platforms/whatsapp/bridge/bridge.js`. Editar o `bridge.js` do volume é inútil — edite `bridge.js` na raiz do repo, que é o que `register()` copia para lá.

### Sync de contatos

`personal_contacts.json` **pode** ser versionado num repositório GitHub privado (`CONFIG_REPO` + `CONFIG_GITHUB_TOKEN`) — é opt-in e vem desligado. O GitHub do projeto é do **produto** (`raizandu/whatsaya`: código e templates de persona); contato, venda e catálogo são dado de operação do cliente e ficam no volume. Sem os dois, o plugin nem tenta o push e não avisa o dono; a proteção nesse modo é `deploy/backup-whatsaya.sh` (snapshot local no cron, inclui a sessão do Baileys para não precisar reparear). O sync roda sempre em thread daemon via `_run_sync_in_background` — **nunca no boot**, só no intervalo periódico (`WHATSAPP_SYNC_INTERVAL_HOURS`) ou por comando no chat. Contatos são classificados por LLM em `Cliente | Amigo | AmigoProximo | Parente | Filho | Vendedor`; o campo `notes` entra no prompt como instrução obrigatória; `full_summary` acumula por sessão e é comprimido em `summary` quando fica longo. Campos auto-gerados (`tone`, `summary`, `guidelines`) não são sobrescritos por update manual.

## Armadilhas de arquivo duplicado

O mesmo arquivo existe em vários caminhos — edite o certo:

- `bridge.js` (raiz, 77 KB) é a **fonte da verdade**. `docs/bridge-artifacts/bridge.js` e `deploy/docs/bridge-artifacts/bridge.js` (63 KB) são artefatos defasados.
- `google_api.py` existe na raiz e em `deploy/scripts/google_api.py`.
- `skills/whatsapp-logs-diagnostics/SKILL.md` está duplicado em `deploy/skills/`.

## Deploy de alterações

O runbook completo está em `.gemini/skills/deploy-plugin/SKILL.md` (escrito para o Gemini CLI, mas o procedimento é agnóstico e roda por SSH, sem painel): commit e push no `main` → dentro do container, `git pull` no clone do plugin (`docker exec hermes sh -c 'cd /opt/data/.hermes/plugins/whatsapp-manager && git pull --ff-only origin main'`) → `docker restart hermes` (ou `docker compose restart`, do host) → conferir `grep "whatsapp-manager" /opt/data/.hermes/logs/hermes.log | tail -20` procurando por `bridge.js atualizado` → testar com `stop_bot` no WhatsApp. Alterações no plugin só carregam após o restart. `bot_state.json` sobrevive ao restart.

**Restart não é sempre suficiente.** `docker restart` reexecuta o `Cmd` já gravado no container — pega mudança de *código* (o plugin git-pulled acima), mas **não** pega mudança no próprio `docker-compose.yml`/`.env` (chave nova, provider novo, porta nova). Pra isso precisa `docker compose up -d` no host, que recria o container com o `Cmd`/env atualizados.

**`bridge.js` tem uma flakiness conhecida no boot:** o bootstrap do plugin (`shutil.copy2`) às vezes falha com `Permission denied` ao copiar `bridge.js` do clone pros caminhos que o Node realmente lê (`platforms/whatsapp/bridge/`, `scripts/whatsapp-bridge/`, `profiles/whatsapp/scripts/whatsapp-bridge/` — o processo em execução usa o último). Parece uma race transitória do bind mount, não reproduz sob demanda. Se depois de um restart `grep -c` de uma mudança recente em `bridge.js` der 0 nesses 3 caminhos, copie manualmente (`docker exec hermes cp <clone>/bridge.js <caminho>`) e reinicie de novo.

## Grafo de conhecimento (graphify)

`graphify-out/` está commitado (grafo + cache AST + `GRAPH_REPORT.md`). `GEMINI.md` e `.agents/rules/graphify.md` mandam usar `graphify query "<pergunta>"` antes de grepar, `graphify path "<A>" "<B>"` para relações e `graphify explain "<conceito>"`; e `graphify update .` após mudar código. Verifique se o CLI `graphify` existe antes de depender disso — sem ele, `GRAPH_REPORT.md` ainda serve para visão de arquitetura.

## Ao replicar este kit para outro cliente

Runbook do operador: [`deploy/ONBOARDING.md`](deploy/ONBOARDING.md). Skills: `whatsaya-onboard`, `whatsaya-diagnose`.

Tudo o que muda por cliente é **variável de ambiente** + os templates em `deploy/SOUL*.md` e `support_rules.md`. Não edite código para trocar de cliente — se você se pegar fazendo isso, é sinal de que falta parametrizar algo.

| Variável | Para que serve |
|---|---|
| `WHATSAPP_OWNER_NAME` | Nome do dono nos prompts, via `_owner_name()`. `_owner_name_norms()` e `_is_owner_name()` derivam dele as variações usadas para reconhecer as mensagens do dono no histórico |
| `WHATSAPP_OWNER_NUMBER` | Número sem `+`. Também lido pelo `bridge.js` |
| `WHATSAPP_PIX_KEY` | Chave Pix quando o item do catálogo não define a sua. **Sem default de propósito** — errar aqui manda o pagamento do cliente para a conta errada |
| `OPENROUTER_API_KEY` | Provider padrão. Deixe `GOOGLE_API_KEY` e `OPENAI_API_KEY` **vazias**: a cadeia é Google → OpenAI → OpenRouter e para na primeira chave preenchida |
| `WHATSAPP_*_MODEL` / `*_PROVIDER` | Slugs do OpenRouter (`vendor/modelo`). Texto usa `deepseek/deepseek-v4-flash`; `WHATSAPP_CLIENT_MEDIA_MODEL` é **só imagem** e precisa aceitar imagem (o DeepSeek é só texto). **Áudio não é modelo de LLM**: transcrição é do Fish ASR (`FISH_API_KEY`). Nenhum slug de texto/visão do OpenRouter aceita entrada de áudio — apontar áudio para lá devolve `404 No endpoints found that support input audio` e a mensagem chega ao agente como `[audio received]` cru |
| `WHATSAPP_AUDIT_*` | Auditoria diária do atendimento. `WHATSAPP_AUDIT_ENABLED` vem **desligada**; ligar manda o resumo do dia ao dono e envia o material a um provider externo. `WHATSAPP_AUDIT_MODEL`/`_PROVIDER` são **env próprias e nunca herdam `WHATSAPP_CLIENT_*`** — o provider do cliente roda no backend da conta ChatGPT do dono e já reproduziu credencial e preço que não estavam no prompt; auditor ali aprenderia da contaminação que existe para detectar. Sem a chave do provider escolhido não há chamada, e o relatório sai só com o placar determinístico |
| `CONFIG_REPO` + `CONFIG_GITHUB_TOKEN` | **Vazios por padrão.** Opt-in para versionar contatos e personas num repo privado. Preencher só um dos dois faz o dono receber "não consegui sincronizar" no WhatsApp a cada contato e venda — preencha os dois ou nenhum. Sem `user/repo`, o dono cai em `config.github_user` (`HERMES_SETUP_GITHUB_USER` → `DEV_GITHUB_USER` → `raizandu`). Para backup sem GitHub: `deploy/backup-whatsaya.sh` |
| `HERMES_SETUP_GITHUB_USER` / `HERMES_SETUP_GITHUB_REPO` | De onde o plugin se clona e se atualiza. Padrão: `raizandu` / `whatsaya` |
| `KEEP_LOCAL_PLUGIN` | `true` — o boot e o `_self_update_plugin_code` não fazem fetch/reset no volume |
| `WHATSAPP_GROUPS_ENABLED` | Padrão desligado. Mensagem de `@g.us` e `@broadcast` é descartada no ponto de entrada do `bridge.js` — não baixa mídia, não enfileira pro agente, não grava no histórico. Ligar é ato deliberado |
| `FISH_API_KEY` (+ `FISH_REFERENCE_ID`, `FISH_TTS_MODEL`, `FISH_TTS_VOLUME`) | Voz das respostas **e transcrição dos áudios recebidos** (TTS `/v1/tts` + ASR `/v1/asr`, mesma chave). Atenção: a **API credit do Fish é uma carteira separada** da plataforma — o TTS pode seguir funcionando com o modelo grátis enquanto o ASR devolve `402 Insufficient API credit`. **Vazia = áudio desligado, tudo vai em texto** — o encanamento (`tts.provider=fishaudio` → `deploy/scripts/fish_tts.py`) é auto-instalado pelo compose no boot; só falta a chave. `FISH_REFERENCE_ID` escolhe a voz; modelo default `s2.1-pro-free` (campanha grátis até 31/08/2026 — `fish_model_campaign_notice.sh` avisa o dono de trocar). Pix, endereço, link, e-mail e afins nunca vão em áudio (regra `written_only` no `fish_tts.py`). Detalhes: `deploy/ONBOARDING.md` |

Fora as envs, só os arquivos de conteúdo: `deploy/SOUL.md`, `SOUL_WHATSAPP.md`, `SOUL_EMAIL.md` e `support_rules.md` são **templates com placeholders `{{...}}`**. Preencha antes de subir — placeholder não substituído vai literal para o cliente, e um `support_rules.md` com produto errado faz o bot inventar oferta que não existe.

URL do plugin: `config.plugin_git_url` / `config.plugin_raw_root`. Caminho oficial: [`deploy/ONBOARDING.md`](deploy/ONBOARDING.md).

## Como o código chega na VPS

O `command:` do `deploy/docker-compose.yml` clona `raizandu/whatsaya` (ou `HERMES_SETUP_GITHUB_*`) a cada boot, salvo `KEEP_LOCAL_PLUGIN`. Fluxo de atualização: `git push` na `main` → restart do container (recreate só é necessário se o `docker-compose.yml`/env vars mudaram).

Duas decisões deliberadas nesse bloco:
- **É clone, não download de arquivos avulsos.** O `setup.sh` original baixava 9 arquivos individuais e não criava `.git`, o que fazia `_self_update_plugin_code()` cair no fallback de lista fixa — arquivos novos nunca chegavam.
- **O perfil de isolamento é escrito pelo compose, não pelo repositório clonado.** Se o clone falhar, o container sobe seguro em vez de liberar terminal e leitura de arquivos para quem manda mensagem.

## Pareamento e status

Após subir o container: `/whatsapp/qr` (tela HTML), `/whatsapp/qr?format=png`, `/whatsapp/status` (JSON). O bridge sobe na porta 3000; `adapter.py` (`WhatsAppPlatformAdapter`) conversa com ele por HTTP via `WHATSAPP_BRIDGE_URL`. Endpoints extras já implementados no bridge: `POST /send-poll`, `POST /send-location`.
