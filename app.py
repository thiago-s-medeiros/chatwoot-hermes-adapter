"""
Adaptador Chatwoot <-> Hermes (atendente Elastok)

Fluxo:
  Cliente WhatsApp -> Chatwoot (inbox) -> [webhook Agent Bot] -> ESTE adaptador
  adaptador: valida HMAC -> filtra (incoming, sem humano assumido) ->
             busca historico no Chatwoot -> chama a api_server do Hermes (Eli) ->
             posta a resposta de volta via Application API do Chatwoot -> WhatsApp

Config por env:
  CHATWOOT_BASE_URL    ex: https://chatwoot.enterpraiz.com
  CHATWOOT_API_TOKEN   access_token do Agent Bot
  CHATWOOT_HMAC_SECRET secret do Agent Bot (valida X-Chatwoot-Signature = HMAC(secret, "{ts}.{body}"))
  HERMES_URL           ex: http://hermes-agent-xxxx:8642
  HERMES_API_KEY       API_SERVER_KEY do Hermes
  HERMES_MODEL         default "hermes-agent"
  HISTORY_LIMIT        nº de mensagens de contexto (default 20)
  FALLBACK_MESSAGE     msg enviada se o Hermes falhar
"""
import hashlib
import hmac
import logging
import os
import re

import httpx
from fastapi import FastAPI, Request, Response

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("adapter")

BASE_URL = os.environ["CHATWOOT_BASE_URL"].rstrip("/")
API_TOKEN = os.environ["CHATWOOT_API_TOKEN"]
HMAC_SECRET = os.environ.get("CHATWOOT_HMAC_SECRET", "")
HERMES_URL = os.environ.get("HERMES_URL", "").rstrip("/")
HERMES_API_KEY = os.environ.get("HERMES_API_KEY", "")
HERMES_MODEL = os.environ.get("HERMES_MODEL", "hermes-agent")
HISTORY_LIMIT = int(os.environ.get("HISTORY_LIMIT", "20"))
FALLBACK_MESSAGE = os.environ.get("FALLBACK_MESSAGE", "Só um instante, já te respondo! 🙏")

app = FastAPI(title="chatwoot-hermes-adapter")


@app.get("/health")
def health():
    return {"ok": True, "hermes_url": HERMES_URL, "base_url": BASE_URL}


def _valid_signature(raw: bytes, sig_header: str, ts_header: str) -> bool:
    """Chatwoot: X-Chatwoot-Signature = "sha256=" + HMAC_SHA256(secret, f"{X-Chatwoot-Timestamp}.{body}")."""
    if not HMAC_SECRET:
        return True
    if not sig_header:
        return False
    signed = ts_header.encode() + b"." + raw
    expected = hmac.new(HMAC_SECRET.encode(), signed, hashlib.sha256).hexdigest()
    got = sig_header.split("=", 1)[-1].strip()
    return hmac.compare_digest(expected, got)


def _is_incoming(mtype) -> bool:
    return mtype == "incoming" or mtype == 0


def _is_outgoing(mtype) -> bool:
    return mtype == "outgoing" or mtype == 1


_THINK_RE = re.compile(r"<(think|thinking|reasoning|scratchpad)>.*?</\1>", re.DOTALL | re.IGNORECASE)


def clean_reply(text: str) -> str:
    """So o texto final vai pro cliente: remove blocos de raciocinio/thinking."""
    text = text or ""
    text = _THINK_RE.sub("", text)
    text = re.sub(r"</?(think|thinking|reasoning|scratchpad)>", "", text, flags=re.IGNORECASE)
    return text.strip()


async def fetch_history(client, account_id, conversation_id):
    """Busca as mensagens da conversa no Chatwoot e monta o array de contexto pro LLM."""
    url = f"{BASE_URL}/api/v1/accounts/{account_id}/conversations/{conversation_id}/messages"
    r = await client.get(url, headers={"api_access_token": API_TOKEN})
    r.raise_for_status()
    data = r.json()
    items = data.get("payload") if isinstance(data, dict) else data
    items = items or []
    msgs = []
    for m in items:
        content = (m.get("content") or "").strip()
        if not content or m.get("private"):
            continue
        mt = m.get("message_type")
        if _is_incoming(mt):
            msgs.append({"role": "user", "content": content})
        elif _is_outgoing(mt):
            msgs.append({"role": "assistant", "content": content})
        # ignora activity/template
    return msgs[-HISTORY_LIMIT:]


async def call_hermes(client, session_key, messages):
    url = f"{HERMES_URL}/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {HERMES_API_KEY}",
        "Content-Type": "application/json",
        "X-Hermes-Session-Key": session_key,  # memoria de longo prazo POR CONTATO
    }
    body = {"model": HERMES_MODEL, "messages": messages}
    r = await client.post(url, headers=headers, json=body)
    r.raise_for_status()
    data = r.json()
    return clean_reply(data["choices"][0]["message"]["content"])


async def send_text(client, account_id, conversation_id, content):
    url = f"{BASE_URL}/api/v1/accounts/{account_id}/conversations/{conversation_id}/messages"
    r = await client.post(
        url,
        headers={"api_access_token": API_TOKEN},
        json={"content": content, "message_type": "outgoing"},
    )
    log.info("send_text -> %s", r.status_code)
    r.raise_for_status()


@app.post("/chatwoot/webhook")
async def webhook(request: Request):
    raw = await request.body()
    if not _valid_signature(raw, request.headers.get("X-Chatwoot-Signature", ""), request.headers.get("X-Chatwoot-Timestamp", "")):
        log.warning("assinatura invalida — rejeitando")
        return Response(status_code=401)

    payload = await request.json() if raw else {}
    event = payload.get("event")
    mtype = payload.get("message_type")
    conv = payload.get("conversation") or {}
    account = payload.get("account") or {}
    account_id = account.get("id") or conv.get("account_id")
    conversation_id = conv.get("id") or payload.get("conversation_id")
    content = (payload.get("content") or "").strip()
    meta = conv.get("meta") or {}
    assignee = meta.get("assignee") or conv.get("assignee_id")
    sender = payload.get("sender") or meta.get("sender") or {}
    contact_id = sender.get("id") or (conv.get("contact_inbox") or {}).get("source_id") or conversation_id
    log.info("event=%s mtype=%s conv=%s contact=%s assignee=%s content=%r", event, mtype, conversation_id, contact_id, bool(assignee), content[:80])

    # so age em mensagem NOVA de CLIENTE (evita loop com as proprias respostas)
    if event != "message_created" or not _is_incoming(mtype):
        return {"ignored": "nao e message_created/incoming"}
    if not (account_id and conversation_id):
        return {"ignored": "sem ids"}
    # HANDOFF: se um humano assumiu a conversa, o bot fica quieto
    if assignee:
        log.info("conversa %s tem humano atribuido -> bot em silencio", conversation_id)
        return {"ignored": "humano no controle"}

    async with httpx.AsyncClient(timeout=180) as client:
        try:
            messages = await fetch_history(client, account_id, conversation_id)
            if not messages:
                messages = [{"role": "user", "content": content or "Olá"}]
            reply = await call_hermes(client, f"chatwoot-contact-{contact_id}", messages)
            if not reply:
                reply = FALLBACK_MESSAGE
        except Exception as e:
            log.exception("falha ao chamar o Hermes: %s", e)
            reply = FALLBACK_MESSAGE
        await send_text(client, account_id, conversation_id, reply)

    return {"ok": True}
