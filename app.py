"""
Adaptador Chatwoot -> (Hermes) -> Chatwoot  [PRIMEIRO TESTE]

Objetivo deste primeiro corte:
  1) RECEBER a mensagem do cliente pelo WEBHOOK do Chatwoot (Agent Bot).
  2) RESPONDER via Application API — com um texto e o PDF do catalogo (anexo),
     pra validar o envio de attachment via API.

Fluxo:
  Cliente WhatsApp -> Chatwoot (inbox WhatsApp) -> [webhook Agent Bot] -> ESTE adaptador
  ESTE adaptador -> POST /api/v1/accounts/{aid}/conversations/{cid}/messages -> Chatwoot -> WhatsApp

O Hermes ainda NAO entra aqui (proximo passo: a funcao call_hermes()).

Config por variaveis de ambiente:
  CHATWOOT_BASE_URL    ex: https://chatwoot.enterpraiz.com  (ou http://chatwoot:3000 interno)
  CHATWOOT_API_TOKEN   access_token do Agent Bot (ou de um agente)
  CHATWOOT_HMAC_SECRET (opcional) secret do Agent Bot p/ validar X-Chatwoot-Signature
  CATALOG_PDF_PATH     caminho do PDF dentro do container (ex: /data/CatalogoElastok.pdf)
  TEST_SEND_PDF        "true" = envia o PDF em toda msg recebida (so p/ teste)
"""
import hashlib
import hmac
import logging
import os

import httpx
from fastapi import FastAPI, Request, Response

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("adapter")

BASE_URL = os.environ["CHATWOOT_BASE_URL"].rstrip("/")
API_TOKEN = os.environ["CHATWOOT_API_TOKEN"]
HMAC_SECRET = os.environ.get("CHATWOOT_HMAC_SECRET", "")
CATALOG_PDF_PATH = os.environ.get("CATALOG_PDF_PATH", "/data/CatalogoElastok.pdf")
TEST_SEND_PDF = os.environ.get("TEST_SEND_PDF", "true").lower() == "true"

app = FastAPI(title="chatwoot-hermes-adapter")


@app.get("/health")
def health():
    return {"ok": True, "pdf_present": os.path.exists(CATALOG_PDF_PATH), "base_url": BASE_URL}


def _valid_signature(raw: bytes, header: str) -> bool:
    """Valida HMAC-SHA256 do corpo cru com o secret do Agent Bot (X-Chatwoot-Signature)."""
    if not HMAC_SECRET:
        return True  # validacao desativada enquanto nao houver secret configurado
    if not header:
        return False
    expected = hmac.new(HMAC_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    got = header.split("=", 1)[-1].strip()  # Chatwoot manda "sha256=<hex>"
    return hmac.compare_digest(expected, got)


async def send_text(client: httpx.AsyncClient, account_id, conversation_id, content: str):
    url = f"{BASE_URL}/api/v1/accounts/{account_id}/conversations/{conversation_id}/messages"
    r = await client.post(
        url,
        headers={"api_access_token": API_TOKEN},
        json={"content": content, "message_type": "outgoing"},
    )
    log.info("send_text -> %s %s", r.status_code, r.text[:300])
    r.raise_for_status()


async def send_pdf(client: httpx.AsyncClient, account_id, conversation_id, content: str):
    url = f"{BASE_URL}/api/v1/accounts/{account_id}/conversations/{conversation_id}/messages"
    with open(CATALOG_PDF_PATH, "rb") as fh:
        files = {"attachments[]": (os.path.basename(CATALOG_PDF_PATH), fh, "application/pdf")}
        data = {"content": content, "message_type": "outgoing"}
        r = await client.post(url, headers={"api_access_token": API_TOKEN}, data=data, files=files)
    log.info("send_pdf -> %s %s", r.status_code, r.text[:300])
    r.raise_for_status()


# TODO (proximo passo): chamar o Hermes p/ gerar a resposta de verdade.
# async def call_hermes(content: str, conversation_id) -> str: ...


@app.post("/chatwoot/webhook")
async def webhook(request: Request):
    raw = await request.body()
    if not _valid_signature(raw, request.headers.get("X-Chatwoot-Signature", "")):
        log.warning("assinatura invalida — rejeitando")
        return Response(status_code=401)

    payload = await request.json() if raw else {}
    event = payload.get("event")
    mtype = payload.get("message_type")
    conv = payload.get("conversation") or {}
    account = payload.get("account") or {}
    account_id = account.get("id") or conv.get("account_id")
    conversation_id = conv.get("id") or payload.get("conversation_id")
    content = payload.get("content", "") or ""
    status = conv.get("status")
    log.info(
        "event=%s mtype=%s status=%s conv=%s acc=%s content=%r",
        event, mtype, status, conversation_id, account_id, content[:120],
    )

    # so age em mensagem NOVA de CLIENTE (evita loop com as proprias respostas outgoing)
    if event != "message_created" or mtype != "incoming":
        return {"ignored": True, "reason": "nao e message_created/incoming"}
    if not (account_id and conversation_id):
        log.warning("payload sem account/conversation id")
        return {"ignored": True, "reason": "sem ids"}

    async with httpx.AsyncClient(timeout=180) as client:
        # TODO: trocar por call_hermes(content, conversation_id)
        await send_text(client, account_id, conversation_id, f"(teste) recebi: {content!r}")
        if TEST_SEND_PDF and os.path.exists(CATALOG_PDF_PATH):
            await send_pdf(client, account_id, conversation_id, "Segue nosso catalogo 📄")

    return {"ok": True}
