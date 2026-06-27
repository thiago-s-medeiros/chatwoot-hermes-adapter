# chatwoot-hermes-adapter (primeiro teste)

Receptor do webhook do Chatwoot que responde via Application API (texto + PDF do catálogo).
Valida o teste: **mensagem chega pelo webhook** + **PDF enviado via API**. O Hermes entra depois.

## Variáveis de ambiente
| Var | Ex | Obrigatória |
|---|---|---|
| `CHATWOOT_BASE_URL` | `https://chatwoot.enterpraiz.com` | sim |
| `CHATWOOT_API_TOKEN` | access_token do Agent Bot | sim |
| `CHATWOOT_HMAC_SECRET` | secret do Agent Bot | recomendada |
| `CATALOG_PDF_PATH` | `/data/CatalogoElastok.pdf` | sim (p/ o teste do PDF) |
| `TEST_SEND_PDF` | `true` | não (default true) |

## Deploy (Coolify)
1. Repo com estes arquivos → recurso Coolify (Dockerfile build).
2. Porta interna 8000. Domínio: ex. `adapter.enterpraiz.com` (ou só rede interna do Coolify).
3. Volume `/data` com o `CatalogoElastok.pdf` dentro (ou bakear no build).
4. Setar as envs acima (token/secret no Infisical).

## Chatwoot (Agent Bot)
1. Super Admin → Agent Bots → criar bot, `outgoing_url` = `https://adapter.enterpraiz.com/chatwoot/webhook`
   (ou URL interna se mesmo projeto/rede). Guardar `access_token` + `secret`.
2. Atribuir o bot ao inbox WhatsApp da Elastok.

## Teste
- `GET /health` → `{ok:true, pdf_present:true}`.
- Mandar mensagem do número de teste no WhatsApp → deve voltar o texto "(teste) recebi: ..." + o PDF.

## Próximos passos (depois do teste)
- Trocar o echo por `call_hermes()` (resposta de verdade, memória por conversa).
- Handoff por reatribuição (assignee humano → bot para).
- Conteúdo rico: botões/listas (`input_select`), templates (`template_params`).
