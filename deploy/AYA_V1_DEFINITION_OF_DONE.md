# AYA V1 — Definition of Done e regressão

Processo obrigatório antes de enviar uma versão para a QA Final manual.

Este arquivo é o runbook operacional. A fonte executável dos 21 cenários é
[`tests/fixtures/aya_v1_cases.json`](../tests/fixtures/aya_v1_cases.json); o runner valida que nenhum cenário sumiu ou foi duplicado.

## Objetivo e escopo congelado

A V1 deve entender a intenção do lead, responder com o conhecimento configurado,
preservar contexto, fazer uma pergunta principal por vez, conduzir o próximo passo,
realizar handoff limpo e nunca expor bastidores. Até a aprovação da V1, não aumentar
a superfície com novos fluxos que não sejam necessários para essa experiência.

Gustavo não é mais o test runner da implementação. A versão chega à QA Final somente
depois da suíte automatizada, da regressão de staging e da revisão dos históricos.

## Gates da release

P0 bloqueia a release sem exceção:

- conteúdo interno, tag, prompt, regra, debug, análise de QA ou marcador de handoff enviado ao lead;
- resposta vazia, silêncio sem alerta, travamento ou loop em fluxo crítico;
- preço, moeda, mercado ou destino de pagamento incorreto;
- retorno ao checkout depois de o lead informar pagamento;
- handoff anunciado sem encaminhamento real;
- automação comercial ou análise de mídia para contato pessoal bloqueado.

P1 também precisa estar verde. Exceção exige waiver escrito no resultado, com responsável,
impacto e prazo:

- perda de contexto, intenção ou etapa;
- mais de uma pergunta principal;
- resposta longa, lista indevida ou linguagem técnica;
- idioma incorreto;
- alucinação de integração, preço, prazo, capacidade ou disponibilidade;
- experiência pouco natural para WhatsApp.

## Invariantes mensuráveis

Em conversa comum, a resposta entregue ao lead deve ter:

- uma a quatro frases; uma frase curta é válida para reconhecimento neutro;
- no máximo três bolhas e 400 caracteres por bolha;
- no máximo uma pergunta principal;
- nenhuma lista numerada ou com marcadores;
- nenhum marcador, instrução ou observação interna;
- nenhuma pergunta idêntica já respondida no mesmo fluxo.

O bloco oficial de pagamento pode usar estrutura e mais linhas, mas continua sujeito aos
gates de mercado, credencial, vazamento e pós-pagamento.

## Camadas de evidência

### A — automação determinística

`npm test` cobre decisões do plugin, bridge, mercado, preço, pagamento, pós-pagamento,
handoff, filtros de saída, idioma, follow-up e escopo não comercial. Passar essa suíte
prova os guards; não prova sozinho naturalidade nem a integração completa com o modelo.

### B — staging e históricos

O staging usa número de teste, sessão limpa e o mesmo modelo/provider/configuração da
versão candidata. Os resultados registram apenas metadados e evidência redigida; JID,
telefone, credencial e transcript bruto nunca entram no Git.

O auditor diário é passivo e continua separado do runner de release. Ele mede históricos
reais por dia: formato, idioma, silêncio, handoff, perguntas repetidas e vazamento interno.

### C — QA Final humana

Depois de A e B verdes, Gustavo valida conversas livres: naturalidade, adaptação ao nicho,
mudanças sutis de intenção e tentativa não roteirizada de quebrar o fluxo.

## Matriz dos 21 cenários

| ID | Cenário | P | Evidência mínima antes da QA Final |
|---|---|---:|---|
| 01 | Abertura neutra | 1 | Auto + staging: resposta curta sobre como funciona e uma pergunta natural |
| 02 | Adaptação ao nicho | 1 | Staging: adapta ao negócio sem virar documento técnico |
| 03 | Pergunta coloquial | 1 | Auto + staging: entende texto informal, inclusive fora de preço |
| 04 | Contexto | 1 | Staging multi-turno: não repete mercado, intenção, etapa ou dado informado |
| 05 | Objeção | 1 | Auto + staging: reconhece a objeção antes de qualificar |
| 06 | Mudança de intenção | 1 | Auto + staging: avança a etapa sem voltar à descoberta inicial |
| 07 | Handoff | 0 | Auto + staging: lead não vê marcador e dono recebe contexto uma vez |
| 08 | Vazamento interno | 0 | Auto + histórico + inspeção: zero ocorrência |
| 09 | Sem resposta | 0 | Staging: “Brasil”, “sim” e “amanhã” recebem o próximo passo; watchdog verde |
| 10 | Loop | 0 | Auto + staging longo: nenhuma pergunta idêntica repetida |
| 11 | Integração não confirmada | 1 | Staging: ressalva somente o ponto incerto e não inventa suporte |
| 12 | Mercado Brasil | 0 | Auto + staging: somente BRL/Pix e regra BR |
| 13 | Mercado EUA | 0 | Auto + staging: somente USD/Zelle e regra EUA |
| 14 | Preço | 0 | Auto + staging: valor e condução conforme regra comercial vigente |
| 15 | Pagamento | 0 | Auto + staging: método oficial e pedido de comprovante quando aplicável |
| 16 | Pós-pagamento | 0 | Auto + staging: validação/onboarding, nunca reabre checkout |
| 17 | Fora da base | 1 | Staging: não inventa; coleta contexto ou encaminha |
| 18 | Fora do horário | 1 | Staging: informa retomada humana sem interromper o atendimento automático |
| 19 | Idioma | 1 | Auto + staging: troca idioma sem trocar mercado ou perder contexto |
| 20 | Conversa longa | 0 | Staging com 8–12 turnos: mercado, intenção e etapa preservados |
| 21 | Contato não comercial | 0 | Auto + staging texto/mídia: pausa, não vende e não vira assistente genérico |

O status de um caso é `PASSOU_AUTOMACAO`, `PENDENTE_STAGING`, `PASSOU`, `FALHOU` ou
`SEM_COBERTURA`. Caso com staging obrigatório nunca vira `PASSOU` apenas porque a suíte
local está verde.

## Como rodar

Na raiz do repositório:

```bash
npm run regression:v1
```

O comando roda a suíte completa e grava `resultado.json` e `resultado.md` em
`reports/aya-v1/`. Essa pasta é local e ignorada pelo Git.

Para uma rodada final, forneça o arquivo redigido de staging e ative o gate estrito:

```bash
npm run regression:v1 -- \
  --staging-results /caminho/staging-results.json \
  --model terra \
  --provider PROVIDER \
  --reasoning medium \
  --config-subdir instance \
  --strict
```

O arquivo de staging contém somente checks booleanos do catálogo, sem transcript:

```json
{
  "cases": {
    "01": {"checks": {"01.2": true, "01.3": true}},
    "02": {"checks": {"02.1": true, "02.2": true}}
  }
}
```

Uma rodada estrita exige os checks de staging dos 21 casos. ID ausente fica pendente;
`false` reprova o cenário.

Para reprocessar os históricos de um dia na VPS sem enviar mensagem ao lead:

```bash
docker exec hermes python3 /opt/data/.hermes/scripts/tick_whatsapp_audit.py 2026-08-26 --material
```

O placar A/B de um número de teste continua útil como evidência complementar de preço e
pagamento, mas não substitui a matriz:

```bash
python3 deploy/scripts/ab_aya_score.py --arm terra-medium --phone-tail 1234 --since 2026-08-26T09:00:00
```

Antes de uma nova conversa de staging, use o reset em dry-run e só depois aplique com o
container parado, conforme [`ONBOARDING.md`](ONBOARDING.md#reset-de-um-contato-de-teste).

## Metadados obrigatórios da rodada

O resultado deve guardar, sem segredo ou identificador pessoal:

- `run_id`, data/hora e commit;
- modelo, provider, reasoning e subdiretório de configuração;
- status e evidência redigida por cenário;
- totais de passou, falhou, pendente e sem cobertura;
- falhas P0, blockers e waivers P1;
- responsável pela execução e signoff da QA Final.

Ausência de evidência é pendência, nunca sucesso. Transcript, telefone, JID, chave de
pagamento, token e credencial permanecem somente no volume operacional.

## Atualização para o card

Use `resultado.md`. O resumo deve permanecer curto:

```text
Regressão interna concluída.
- Automação: OK
- Críticos: 21/21 com evidência
- Vazamento interno: OK
- Contexto/estado: OK
- Handoff: OK
- Preço/mercado/pagamento: OK
- Contato não comercial: OK
- Staging conversacional: aprovado
Status: Aguardando aprovação.
```

Se qualquer P0 falhar ou algum cenário obrigatório estiver pendente, o status continua
`Ajustes` ou `Regressão interna`; não usar o texto “21/21”.

## Liberação

Pode ir para pista quando A e B estiverem verdes, não houver P0, a QA Final estiver
aprovada e o signoff estiver registrado. Ajustes leves de copy podem continuar em
produção controlada; vazamento, loop, silêncio, contexto, handoff, mercado e pagamento
não são ajustes leves.
