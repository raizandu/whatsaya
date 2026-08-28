# Hermes WhatsApp Plugin

Plugin **`whatsapp-manager`** para o [Hermes Agent v2026](https://github.com/nousresearch/hermes). Transforma o WhatsApp em um assistente pessoal inteligente no **SelfChat** (para o dono) e um atendente autônomo e seguro para clientes — tudo no mesmo número, com isolamento total de permissões e inteligência contextual.

> **Licença:** [BUSL-1.1](LICENSE) — uso livre para desenvolvimento e testes. Converte para MIT em 2031-06-25.

---

## 🚀 Destaques da Versão v2026

- **Arquitetura de Container Único (Single-Container):** Desde o Hermes Agent v0.19 ("Quicksilver"), o core ganhou uma plataforma WhatsApp nativa (`hermes_plugins.whatsapp_platform`) que descobre, sobe e monitora o processo Node.js da ponte automaticamente — em vez deste plugin gerenciar seu próprio subprocesso. O `bridge.js` deste repositório continua sendo o código que roda; o Hermes só assumiu o ciclo de vida do processo (spawn, pidfile, restart em crash), eliminando o container duplicado e o erro de desconexão `440 conflict / replaced`.
- **Roteamento Nativo por Perfis (`default` vs `whatsapp`):**
  - **`SelfChat` (Perfil: `default`):** Acesso à persona executiva (`SOUL.md`), histórico completo, comandos de controle e **todas as ferramentas ativas** (código, terminal, busca web, mídias).
  - **`Clientes/Contatos` (Perfil: `whatsapp`):** Persona de suporte (`SOUL_WHATSAPP.md` + `support_rules.md`), respostas baseadas nas regras de negócio e somente o toolset comercial de agenda habilitado; ele fica oculto sem OAuth válido e o firewall bloqueia as demais ferramentas no backend.
- **Silêncio de Avisos Brutais:** Ocultação automática de mensagens do sistema (como aviso de reset de 24h e metadados `◆ Model: ...`).

---

## 🛠️ O que faz

### Para o Dono (SelfChat)
- Assistente pessoal executivo com acesso ao histórico completo de todas as conversas
- Consultas cruzadas em linguagem natural: *"o que a Isabel falou sobre o contrato?"*
- Atualização de contatos em linguagem natural: *"a Isabel é minha filha, apelido Bebel"*
- Comandos de controle do bot: `stop_bot` (pausar), `start_bot` (retomar), `sincronizar contatos`, `quais comandos`

### Para Clientes e Contatos
- Atendimento autônomo guiado por `support_rules.md` (produtos, preços, FAQs)
- Tom personalizado por contato via `personal_contacts.json`
- Transcrição automática de áudios e descrição de imagens via Gemini
- Silêncio automático de 10 minutos quando o dono lê ou responde manualmente

### Inteligência de Contatos
- Classificação automática: `Cliente | Amigo | AmigoProximo | Parente | Filho | Vendedor`
- Campo `notes` injetado como **instrução obrigatória** no prompt (o LLM obedece)
- Resumo cumulativo por período (`full_summary`) comprimido a cada sync
- Sync automático a cada 24h com repositório privado do GitHub — contatos e personas versionados

---

## 📐 Arquitetura do Container

```
┌──────────────────────────────────────────────────────────────────┐
│  Container Único: hermes (nousresearch/hermes-agent)            │
│  ├─ Hermes Gateway (Porta 9119 — Dashboard/REST API)             │
│  ├─ hermes_plugins.whatsapp_platform (NATIVO, core >= v0.19)     │
│  │    └─ descobre, sobe e monitora o processo Node abaixo        │
│  ├─ Microprocesso Baileys Node.js (bridge.js deste repo, :3000)  │
│  ├─ Plugin whatsapp-manager (Python Hooks — regras de negócio)   │
│  └─ Isolation Profiles:                                          │
│     ├─ /profiles/default/   → Dono (Full Tools + SOUL)           │
│     └─ /profiles/whatsapp/  → Clientes (No Tools + Prompt)       │
└──────────────────────────────────────────────────────────────────┘

Quem faz o quê:
  - whatsapp-manager (este repo) → instala o bridge.js no volume, registra
    os hooks pre_gateway_dispatch/pre_llm_call/post_llm_call/pre_tool_call
    (toda a lógica de negócio: classificação de contato, personas, sync).
  - hermes_plugins.whatsapp_platform (core do Hermes) → dono do ciclo de
    vida do processo Node: spawn, pidfile com detecção de PID reciclado,
    restart automático em crash, e o parsing de mensagens recebidas.
  - O plugin NÃO spawna mais o bridge.js diretamente — só o entrega no
    caminho convencional para o core encontrar e gerenciar.

Volume Compartilhado: /opt/data
  ├─ .hermes/plugins/whatsapp-manager/   → Código do plugin
  ├─ .hermes/platforms/whatsapp/bridge/  → bridge.js (gerenciado pelo core)
  ├─ .hermes/platforms/whatsapp/session/ → Sessão Baileys (creds, chaves)
  ├─ .hermes/profiles/whatsapp/          → Config e SOUL de clientes
  ├─ .hermes/profiles/default/           → Config e SOUL do dono
  ├─ .hermes/whatsapp_messages.db        → Histórico raw (bridge)
  ├─ .hermes/state.db                    → Sessões e contexto
  ├─ personal_contacts.json              → Perfis dos contatos
  ├─ support_rules.md                    → Base de conhecimento (clientes)
  └─ SOUL_WHATSAPP.md                    → Persona e estilo de escrita
```

> Detalhe de compatibilidade: o Hermes Agent v0.19+ também ganhou uma plataforma WhatsApp **totalmente nativa** (`plugins/platforms/whatsapp/` no core), com endpoints próprios de polls (`/send-poll`) e localização (`/send-location`). Neste projeto, o core detecta e reaproveita o `bridge.js` deste repo em vez de subir a implementação nativa — este `bridge.js` já implementa os dois endpoints (`POST /send-poll`, `POST /send-location`), com os métodos equivalentes `send_poll()`/`send_location()` expostos em `adapter.py`.

---

## 📁 Estrutura do Repositório

```
├── whatsapp_manager.py          # Plugin principal (Hooks Python)
├── adapter.py                   # Adaptador de plataforma nativo (WhatsAppPlatformAdapter)
├── bridge.js                    # Bridge WhatsApp (Node.js + Baileys)
├── plugin.yaml                  # Manifesto do plugin
├── deploy/
│   ├── docker-compose.yml       # Container único + setup automático (SSH, `docker compose up -d`)
│   ├── setup.sh                 # Setup inicial de 1 clique
│   ├── SOUL.md                  # Persona base do dono (Engenheiro/Assistente)
│   ├── SOUL_WHATSAPP.md         # Persona de atendimento aos clientes
│   ├── support_rules.md         # Regras de suporte e FAQs (exemplo)
│   └── personal_contacts.json.example
├── tests/
│   └── plugin_test.py           # 265 testes unitários (100% passing)
└── validate_dedup.py            # Validação de dedup no container
```

---

## ⚡ Instalação e Deploy

**Caminho oficial (próximo cliente):** [deploy/ONBOARDING.md](deploy/ONBOARDING.md) — VPS + Compose + IP, sem domínio obrigatório. Skills no repo: `whatsaya-onboard` (subir) e `whatsaya-diagnose` (bot no ar, comportamento errado).

### Pré-requisitos (caminho oficial)

- Ubuntu 24 com Docker Compose v2
- Número do dono, nome, catálogo e (se vender no chat) chave Pix
- Um provider de modelo (OpenRouter, Gemini ou Codex no dashboard) — não preencha duas chaves da cadeia Google → OpenAI → OpenRouter

Domínio é opcional — detalhes abaixo.

---

### Deploy manual (resumo — o passo a passo completo está em ONBOARDING.md)

1. SSH na VPS, `cd` até onde vai ficar o compose (ex: `/opt/whatsaya`).
2. Copie [`deploy/docker-compose.yml`](deploy/docker-compose.yml) pra lá.
3. Crie um `.env` na mesma pasta com as variáveis essenciais:
   - Um provider de modelo (`OPENROUTER_API_KEY`, `GOOGLE_API_KEY`, ou Codex OAuth)
   - `WHATSAPP_OWNER_NUMBER`: seu número sem `+` (ex: `5511999999999`)
   - `WHATSAPP_OWNER_NAME`: seu nome (ex: `André`)
   - `CONFIG_GITHUB_TOKEN`: opcional, PAT do GitHub para sincronização dos contatos
4. `docker compose up -d`. O container sobe, aplica as configurações de segurança, e inicia a bridge interna.
5. Domínio é opcional — coloque o proxy reverso que preferir (Caddy, Nginx, Traefik) na frente das portas publicadas.

---

## 📲 Conectar o WhatsApp (QR Code)

Após subir o container, acesse os endpoints de pareamento pelo seu navegador:

| URL | Descrição |
|---|---|
| `https://hermes.seu-dominio.com/whatsapp/qr` | Tela de QR Code HTML interativa |
| `https://hermes.seu-dominio.com/whatsapp/qr?format=png` | Imagem direta em PNG |
| `https://hermes.seu-dominio.com/whatsapp/status` | Status JSON da conexão |

No seu celular: **WhatsApp → Aparelhos Conectados → Conectar um aparelho** → Escaneie o QR Code.

Se o dashboard não estiver disponível, o template inclui uma página independente em [`deploy/qr-server.py`](deploy/qr-server.py) e o serviço systemd [`deploy/whatsaya-qr.service`](deploy/whatsaya-qr.service). Ela escuta apenas no host local e deve ser publicada por um proxy autenticado. A instalação reproduzível e o cron transacional de follow-up estão documentados em [`deploy/ONBOARDING.md`](deploy/ONBOARDING.md).

---

## 💬 Comandos no WhatsApp (SelfChat)

Envie mensagens para si mesmo no WhatsApp. Todos os comandos de controle funcionam **exclusivamente para o dono**:

| Comando | Descrição |
|---|---|
| `quais comandos` / `ajuda` | Exibe a lista completa de comandos e status do bot |
| `stop_bot` | Pausa o atendimento automático a clientes |
| `start_bot` | Reativa o atendimento automático a clientes |
| `sincronizar contatos` | Força a sincronização de contatos com o GitHub em background |
| `update contact <nome> campo=valor` | Atualiza dados de um contato (ex: `update contact Bebel relationship=Filho`) |

---

## 🔒 Segurança de Ferramentas por Perfil

```yaml
# Perfil "whatsapp" (/opt/data/.hermes/profiles/whatsapp/config.yaml)
agent:
  tool_use_enforcement: disabled

toolsets:
  - whatsaya_calendar # ✅ Apenas consulta/reserva comercial, oculta sem OAuth
disabled_toolsets:   # ❌ Todas as 25 famílias de ferramentas desativadas
  - file_operations
  - code_execution
  - vision
  - image_generation
  - web_search
  - terminal
  - computer_use

tools: []
skills:
  enabled: false
```

- **Clientes:** Só podem consultar e reservar vagas comerciais pelas ferramentas controladas de agenda. Qualquer outra tentativa é abortada pelo hook `pre_tool_call` no backend.
- **Dono (SelfChat):** Pode solicitar qualquer execução de código, leitura de arquivos ou buscas normalmente.

---

## 🧪 Testes Automatizados

Rode a suíte técnica completa:

```bash
npm test
```

Antes da QA Final da AYA, rode também o gate que combina os guards com a matriz dos
21 cenários e gera artefatos redigidos para o card:

```bash
npm run regression:v1
```

Critérios, staging e interpretação do resultado:
[`deploy/AYA_V1_DEFINITION_OF_DONE.md`](deploy/AYA_V1_DEFINITION_OF_DONE.md).

---

*Desenvolvido e mantido por [André Alencar](https://aalencar.com.br) / Empreendedor Serial.*
