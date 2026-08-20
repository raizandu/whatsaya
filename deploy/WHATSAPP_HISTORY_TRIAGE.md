# Fullsync, snapshot e triagem de WhatsApp

Este é o procedimento padrão para um onboarding WhatsAYA que precisa ler o
histórico do número recém-pareado, classificar os chats e gerar um documento de
revisão para o dono.

## Garantias

- Mensagens históricas são gravadas no SQLite, mas **não entram na fila do LLM**.
- O pipeline não envia mensagens e não muda roteamento por padrão.
- O relatório de revisão contém apenas resumo, evidências e próxima ação.
- Arquivos do snapshot recebem permissão local restrita (`0600`).
- O envio ao self-chat é uma etapa explícita (`send`).

## Pré-requisitos do bridge

A cópia ativa do `bridge.js` precisa ter todos estes invariantes:

```js
syncFullHistory: true,
shouldSyncHistoryMessage: () => true,
```

E precisa registrar:

```js
sock.ev.on('messaging-history.set', handleMessagingHistorySet)
```

`handleMessagingHistorySet` deve gravar os lotes em:

```text
/opt/data/.hermes/whatsapp_messages.db
```

O banco precisa ter, no mínimo, as colunas:

```text
chat_id, sender_id, sender_name, message_id, message_type,
body, timestamp, from_me, is_historical, has_media, media_type
```

Nunca rode dois bridges usando a mesma sessão Baileys. O sintoma típico é
`440 conflict / replaced`.

## Pareamento

1. Suba o cliente com o plugin e as personas já presentes.
2. Abra `http://IP:9119/whatsapp/qr`.
3. Escaneie em **WhatsApp → Aparelhos conectados → Conectar um aparelho**.
4. Aguarde `connected`.
5. Não envie mensagem de teste antes de conferir o snapshot.

## Pipeline de operador

Instale/copiei estes arquivos para o volume do cliente:

```text
deploy/scripts/whatsapp_history_triage.py
deploy/scripts/whatsapp_history_triage.yaml
deploy/hooks/post_fullsync_triage.sh
```

A configuração principal é o YAML. Ajuste:

- `paths.*` se o volume usar outro caminho;
- `bridge.health_url` se o bridge não estiver em localhost;
- `delivery.owner_chat_id` com o JID do dono, somente se for usar `send`.

### 1. Preparar snapshot e prompt

```bash
python3 deploy/scripts/whatsapp_history_triage.py \
  --config deploy/scripts/whatsapp_history_triage.yaml run
```

O comando aguarda a conexão e a estabilização do lote histórico. Ele gera:

```text
whatsapp_fullsync_YYYY-MM-DD.json
whatsapp_fullsync_chat_index_YYYY-MM-DD.csv
whatsapp_triagem_prompt_YYYY-MM-DD.md
```

### 2. Classificar

Use a skill `whatsapp-client-triage` para ler o JSON em lotes e gerar um
`whatsapp_classification.json` com todos os `chat_id`. O classificador deve:

- distinguir pessoal, lead, cliente, fornecedor/parceiro, spam e revisão;
- extrair empresa, nome, e-mail, CNPJ, cidade e necessidade somente quando
  estiverem explícitos;
- marcar contato pessoal como `automation: bloqueada`;
- não tratar saudação isolada como cliente;
- manter evidências curtas e factuais;
- não enviar mensagens nem editar `personal_contacts.json` nessa etapa.

A classificação só pode ser considerada completa se o número de registros no
JSON for igual ao número de chats do snapshot.

### 3. Gerar o documento para o cliente

```bash
python3 deploy/scripts/whatsapp_history_triage.py \
  --config deploy/scripts/whatsapp_history_triage.yaml report \
  --snapshot /opt/data/.hermes/workspace/whatsapp_fullsync_YYYY-MM-DD.json \
  --classification /opt/data/.hermes/workspace/whatsapp_classification.json \
  --output /opt/data/.hermes/workspace/whatsapp_triagem_revisao_cliente_YYYY-MM-DD.md
```

O Markdown segue o formato operacional:

- resumo da triagem;
- leads e negociações;
- parceiros/fornecedores;
- contatos pessoais ou ambíguos;
- spam/mensagens ilegíveis;
- evidências;
- ação recomendada;
- checkbox de feedback do dono.

### 4. Enviar ao self-chat do dono

Só depois de verificar o documento:

```bash
python3 deploy/scripts/whatsapp_history_triage.py \
  --config deploy/scripts/whatsapp_history_triage.yaml send \
  --report /opt/data/.hermes/workspace/whatsapp_triagem_revisao_cliente_YYYY-MM-DD.md \
  --chat-id 5562936180895@s.whatsapp.net
```

O bridge divide mensagens longas em partes. O script exige um destino explícito
e confirma o `success` retornado pelo bridge.

## Verificação pós-run

```bash
python3 deploy/scripts/whatsapp_history_triage.py \
  --config deploy/scripts/whatsapp_history_triage.yaml snapshot --no-wait

curl -sS http://127.0.0.1:3000/health
python3 /opt/data/.hermes/plugins/whatsapp-manager/history_store.py \
  status /opt/data/.hermes/whatsapp_messages.db
```

Confira:

- bridge `connected`;
- `historical > 0` em um pareamento com histórico disponível;
- snapshot e CSV presentes;
- classificação cobre todos os `chat_id`;
- `messages_sent: false` e `routing_changed: false` no relatório;
- nenhum placeholder ou credencial no Markdown.

## Falhas conhecidas

### Fullsync conecta, mas não grava histórico

- conferir `syncFullHistory: true` na cópia realmente executada pelo gateway;
- conferir `messaging-history.set` e o `history_store.py` ao lado do bridge;
- confirmar que a sessão foi pareada como um novo dispositivo;
- não apagar `creds.json` sem backup;
- verificar se não há segundo bridge usando a mesma sessão.

### O snapshot inclui mensagens novas depois do fullsync

Isso é esperado. O relatório deve registrar o horário do snapshot. Para uma
triagem auditável, não misture dados coletados depois da classificação: gere um
novo snapshot e uma nova data.

### Preços divergentes

O histórico pode conter ofertas antigas ou outro produto. Não corrija mensagens
retroativamente. Separe o catálogo e mantenha a regra atual da WhatsAYA:
R$ 997 de implementação + R$ 397/mês, sem desconto sem autorização.
