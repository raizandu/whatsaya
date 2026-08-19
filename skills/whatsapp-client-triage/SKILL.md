---
name: whatsapp-client-triage
description: "Run fullsync, classify chats, and write client review."
version: 1.0.0
author: WhatsAYA
license: Internal
metadata:
  hermes:
    tags: [whatsapp, whatsaya, onboarding, fullsync, triage, crm]
---

# WhatsApp client triage

## When to Use

Use after a new WhatsAYA number is paired, the bridge reports `connected`, and the owner wants a historical snapshot, chat classification, and a client review document.

Use this skill after a new WhatsAYA number is paired and the bridge reports
`connected`. It creates a historical snapshot, classifies every chat in
batches, generates the client-facing review Markdown, and optionally sends only
the review to the owner's self-chat.

## Safety invariants

- Never let historical messages enter the LLM response queue.
- Never send a reply to a client during snapshot or classification.
- Never alter `personal_contacts.json` or routing as part of the first pass.
- Do not classify a relationship, company, email, CNPJ, city, or role unless the
  conversation states it explicitly.
- `Pessoal` always means `automation: bloqueada`.
- Treat `status@broadcast`, the owner's self-chat, and protocol-only records as
  internal and exclude them from CRM actions.
- Do not report success from a child/command claim alone; verify files, counts,
  bridge health, and the send response yourself.

## Source files

The reusable pipeline lives in the plugin deploy tree:

```text
/opt/data/.hermes/plugins/whatsapp-manager/deploy/scripts/whatsapp_history_triage.py
/opt/data/.hermes/plugins/whatsapp-manager/deploy/scripts/whatsapp_history_triage.yaml
/opt/data/.hermes/plugins/whatsapp-manager/deploy/hooks/post_fullsync_triage.sh
/opt/data/.hermes/plugins/whatsapp-manager/deploy/WHATSAPP_HISTORY_TRIAGE.md
```

The client volume normally maps to `/opt/data`; a host path such as
`/opt/whatsaya/data` is the host-side equivalent, not a path to assume inside
the container.

## Procedure

1. Verify the active bridge, not only the plugin source:
   - `curl -sS http://127.0.0.1:3000/health`
   - `syncFullHistory: true`
   - `shouldSyncHistoryMessage: () => true`
   - `messaging-history.set` listener present
   - `/opt/data/.hermes/whatsapp_messages.db` exists after pairing
2. Wait for `status=connected` and a stable historical count:

```bash
python3 /opt/data/.hermes/plugins/whatsapp-manager/deploy/scripts/whatsapp_history_triage.py \
  --config /opt/data/.hermes/plugins/whatsapp-manager/deploy/scripts/whatsapp_history_triage.yaml run
```

   This writes a sanitized snapshot, a chat index CSV, and a classification
   prompt. If the count remains zero after the configured wait, stop and
   diagnose; do not fabricate a result.
3. Read the snapshot JSON. Split records into stable batches of roughly 10–15
   chats. Classify every record with this schema:

```json
{
  "chat_id": "...",
  "display_name": "...",
  "flag": "Pessoal|Lead|Cliente|Fornecedor/parceiro|Spam/irrelevante|Revisar",
  "relationship": "Amigo|AmigoProximo|Parente|Filho|Cliente|Vendedor|Desconhecido",
  "automation": "bloqueada|comercial|revisao",
  "stage": "pessoal|lead_novo|lead_qualificado|proposta|cliente|fornecedor|incerto|spam",
  "confidence": "alta|media|baixa",
  "contact_data": {},
  "summary": "...",
  "evidence": ["..."],
  "next_action": "..."
}
```

   The final classification file must contain exactly the snapshot's chat IDs.
   Aggregate and deduplicate in code, not mentally.
4. Generate the review document:

```bash
python3 /opt/data/.hermes/plugins/whatsapp-manager/deploy/scripts/whatsapp_history_triage.py \
  --config /opt/data/.hermes/plugins/whatsapp-manager/deploy/scripts/whatsapp_history_triage.yaml report \
  --snapshot /opt/data/.hermes/workspace/whatsapp_fullsync_YYYY-MM-DD.json \
  --classification /opt/data/.hermes/workspace/whatsapp_classification.json \
  --output /opt/data/.hermes/workspace/whatsapp_triagem_revisao_cliente_YYYY-MM-DD.md
```

   The Markdown must contain the count, period, pending contacts, evidence,
   recommended action, and feedback checkboxes. Do not include raw message
   bodies or credentials in the client review.
5. Verify the report before delivery:
   - count matches the requested snapshot;
   - no raw `messages` arrays in the review document;
   - no `password`, `senha`, `token`, or API key values;
   - `messages_sent=false` and `routing_changed=false` in the JSON audit;
   - bridge remains connected.
6. Send only after the owner explicitly requests delivery:

```bash
python3 /opt/data/.hermes/plugins/whatsapp-manager/deploy/scripts/whatsapp_history_triage.py \
  --config /opt/data/.hermes/plugins/whatsapp-manager/deploy/scripts/whatsapp_history_triage.yaml send \
  --report /opt/data/.hermes/workspace/whatsapp_triagem_revisao_cliente_YYYY-MM-DD.md \
  --chat-id <OWNER_JID>
```

   Verify the HTTP response contains `success: true`. Never claim delivery from
   a command that failed or timed out.

## Client onboarding handoff

The report is a review artifact, not truth applied to production. After the
owner replies with feedback, apply only approved high-confidence flags. Keep
ambiguous contacts in review. Keep the snapshot, classification JSON, CSV, and
Markdown together under the client's workspace for auditability.

## Troubleshooting

- `440 conflict / replaced`: another bridge is using the same Baileys session.
- connected but `historical=0`: active bridge is old, listener/schema missing, or
  the session was not a new pairing; inspect the actual process path.
- a historical message triggered a reply: stop the bridge, restore silent
  history handling, and inspect the queue before resuming.
- price/catalog mismatch: separate old/other-product offers; do not rewrite
  client messages. Current prices come from this client's `support_rules.md`.
  Do not invent discounts.
