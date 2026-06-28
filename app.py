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
import asyncio
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
READ_TOKEN = os.environ.get("CHATWOOT_READ_TOKEN") or API_TOKEN  # token de AGENTE p/ ler historico (GET); bot token so posta
HMAC_SECRET = os.environ.get("CHATWOOT_HMAC_SECRET", "")
HERMES_URL = os.environ.get("HERMES_URL", "").rstrip("/")
HERMES_API_KEY = os.environ.get("HERMES_API_KEY", "")
HERMES_MODEL = os.environ.get("HERMES_MODEL", "hermes-agent")
HISTORY_LIMIT = int(os.environ.get("HISTORY_LIMIT", "20"))
FALLBACK_MESSAGE = os.environ.get("FALLBACK_MESSAGE", "Um instante! Vou te transferir para um atendente do nosso time. 🙏")
DEBOUNCE_SECONDS = float(os.environ.get("DEBOUNCE_SECONDS", "6"))
# WhatsApp Cloud API direto: typing indicator NATIVO pro cliente (o Chatwoot nao repassa)
WHATSAPP_PHONE_NUMBER_ID = os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "")
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN", "")
WHATSAPP_API_VERSION = os.environ.get("WHATSAPP_API_VERSION", "v22.0")
# PDF do catalogo (embutido na imagem); enviado quando a Eli emite o marcador [[CATALOGO]].
# Tenta o env, depois caminhos embutidos — robusto a env desatualizado.
CATALOG_PDF_PATHS = [p for p in [os.environ.get("CATALOG_PDF_PATH"), "/app/CatalogoElastok.pdf", "/data/CatalogoElastok.pdf"] if p]

# estado em memoria (1 processo uvicorn): debounce por conversa
_pending: dict = {}        # conversation_id -> asyncio.Task em andamento
_last_content: dict = {}   # conversation_id -> ultimo texto recebido (fallback)

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
    """Historico da conversa no Chatwoot (NAO-FATAL: se falhar, retorna [] e segue so com a msg atual)."""
    url = f"{BASE_URL}/api/v1/accounts/{account_id}/conversations/{conversation_id}/messages"
    try:
        r = await client.get(url, headers={"api_access_token": READ_TOKEN})
        r.raise_for_status()
        data = r.json()
        items = data.get("payload") if isinstance(data, dict) else data
        msgs = []
        for m in (items or []):
            content = (m.get("content") or "").strip()
            if not content or m.get("private"):
                continue
            mt = m.get("message_type")
            if _is_incoming(mt):
                msgs.append({"role": "user", "content": content})
            elif _is_outgoing(mt):
                msgs.append({"role": "assistant", "content": content})
        return msgs[-HISTORY_LIMIT:]
    except Exception as e:
        log.warning("fetch_history falhou (%s) — seguindo so com a mensagem atual", e)
        return []


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


async def send_note(client, account_id, conversation_id, content):
    """Nota PRIVADA: so o atendente humano ve no Chatwoot; NAO vai pro cliente (WhatsApp)."""
    url = f"{BASE_URL}/api/v1/accounts/{account_id}/conversations/{conversation_id}/messages"
    try:
        r = await client.post(url, headers={"api_access_token": API_TOKEN},
                              json={"content": content, "message_type": "outgoing", "private": True})
        log.info("send_note -> %s", r.status_code)
    except Exception as e:
        log.warning("send_note falhou: %s", e)


async def send_catalog(client, account_id, conversation_id):
    """Envia o PDF do catalogo como ANEXO (Application API, multipart) -> vai pro cliente no WhatsApp."""
    path = next((p for p in CATALOG_PDF_PATHS if os.path.exists(p)), None)
    if not path:
        log.warning("catalogo nao encontrado em %s — pulando envio", CATALOG_PDF_PATHS)
        return False
    url = f"{BASE_URL}/api/v1/accounts/{account_id}/conversations/{conversation_id}/messages"
    try:
        with open(path, "rb") as fh:
            files = {"attachments[]": (os.path.basename(path), fh, "application/pdf")}
            r = await client.post(url, headers={"api_access_token": API_TOKEN},
                                  data={"message_type": "outgoing"}, files=files)
        log.info("send_catalog -> %s", r.status_code)
        r.raise_for_status()
        return True
    except Exception as e:
        log.warning("send_catalog falhou: %s", e)
        return False


async def escalate_to_human(client, account_id, conversation_id):
    """Handoff: marca a conversa como 'open' pra entrar na fila dos atendentes humanos (sai do controle do bot)."""
    url = f"{BASE_URL}/api/v1/accounts/{account_id}/conversations/{conversation_id}/toggle_status"
    try:
        r = await client.post(url, headers={"api_access_token": READ_TOKEN}, json={"status": "open"})
        log.info("escalate_to_human (open) -> %s", r.status_code)
    except Exception as e:
        log.warning("escalate_to_human falhou: %s", e)


async def set_typing(client, account_id, conversation_id, on):
    """Liga/desliga o 'digitando...' na conversa enquanto a Eli processa."""
    url = f"{BASE_URL}/api/v1/accounts/{account_id}/conversations/{conversation_id}/toggle_typing_status"
    try:
        await client.post(url, headers={"api_access_token": READ_TOKEN}, json={"typing_status": "on" if on else "off"})
    except Exception as e:
        log.warning("toggle_typing falhou: %s", e)


async def send_whatsapp_typing(wamid):
    """Typing indicator NATIVO do WhatsApp (Meta) — Chatwoot nao repassa, entao chamamos a Graph API direto.
    Tambem marca a msg como lida. Dura ~25s ou ate a resposta sair."""
    if not (WHATSAPP_PHONE_NUMBER_ID and WHATSAPP_TOKEN and wamid):
        return
    url = f"https://graph.facebook.com/{WHATSAPP_API_VERSION}/{WHATSAPP_PHONE_NUMBER_ID}/messages"
    body = {"messaging_product": "whatsapp", "status": "read", "message_id": wamid, "typing_indicator": {"type": "text"}}
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(url, headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"}, json=body)
            log.info("wa_typing -> %s %s", r.status_code, r.text[:150])
    except Exception as e:
        log.warning("wa_typing falhou: %s", e)


async def process_conversation(account_id, conversation_id, contact_id):
    """Roda apos o debounce: 1 resposta por rajada, com o historico completo. Cancelavel
    (uma msg nova cancela este task e reagenda, juntando tudo)."""
    try:
        await asyncio.sleep(DEBOUNCE_SECONDS)
        async with httpx.AsyncClient(timeout=180) as client:
            await set_typing(client, account_id, conversation_id, True)  # "digitando..."
            escalate = False
            try:
                messages = await fetch_history(client, account_id, conversation_id)
                if not messages:
                    messages = [{"role": "user", "content": _last_content.get(conversation_id) or "Olá"}]
                reply = await call_hermes(client, f"chatwoot-contact-{contact_id}", messages)
                if not reply:
                    reply, escalate = FALLBACK_MESSAGE, True
            except Exception as e:
                log.exception("falha ao chamar o Hermes: %s", e)
                reply, escalate = FALLBACK_MESSAGE, True
            await set_typing(client, account_id, conversation_id, False)
            # separa: marcadores internos (NOTA/HANDOFF) NUNCA vao pro cliente
            notes = re.findall(r"\[\[NOTA:\s*(.*?)\]\]", reply, re.DOTALL | re.IGNORECASE)
            handoff = re.findall(r"\[\[HANDOFF:\s*(.*?)\]\]", reply, re.DOTALL | re.IGNORECASE)
            wants_catalog = bool(re.search(r"\[\[CATALOGO\]\]", reply, re.IGNORECASE))
            customer_text = re.sub(r"\[\[.*?\]\]", "", reply, flags=re.DOTALL).strip()
            # notas internas (lead etc.) -> nota privada
            for n in notes:
                if n.strip():
                    await send_note(client, account_id, conversation_id, "🧾 " + n.strip())
            # resposta pro cliente (sem marcadores)
            if customer_text:
                await send_text(client, account_id, conversation_id, customer_text)
            # catalogo: a Eli pediu p/ enviar o PDF -> anexa na conversa (vai pro WhatsApp)
            if wants_catalog:
                await send_catalog(client, account_id, conversation_id)
            # handoff: falha tecnica (escalate) OU decisao da Eli ([[HANDOFF]])
            if escalate:
                await escalate_to_human(client, account_id, conversation_id)
            elif handoff:
                await send_note(client, account_id, conversation_id, "🤝 Handoff p/ humano: " + (handoff[0].strip() or "motivo nao informado"))
                await escalate_to_human(client, account_id, conversation_id)
    except asyncio.CancelledError:
        log.info("conv %s: processamento cancelado (mensagem nova na rajada)", conversation_id)
        raise
    finally:
        if _pending.get(conversation_id) is asyncio.current_task():
            _pending.pop(conversation_id, None)


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
    status = conv.get("status")
    sender = payload.get("sender") or meta.get("sender") or {}
    contact_id = sender.get("id") or (conv.get("contact_inbox") or {}).get("source_id") or conversation_id
    log.info("event=%s mtype=%s status=%s conv=%s contact=%s assignee=%s content=%r", event, mtype, status, conversation_id, contact_id, bool(assignee), content[:80])

    # so age em mensagem NOVA de CLIENTE (evita loop com as proprias respostas)
    if event != "message_created" or not _is_incoming(mtype):
        return {"ignored": "nao e message_created/incoming"}
    if not (account_id and conversation_id):
        return {"ignored": "sem ids"}
    # HANDOFF: o bot so responde quando a conversa esta COM ELE (pending) e sem humano atribuido.
    # Se um humano assumiu (assignee) ou ja foi pra fila humana (status != pending), fica quieto.
    if assignee:
        return {"ignored": "humano atribuido"}
    if status and status != "pending":
        return {"ignored": f"status={status} (fila humana)"}

    # typing NATIVO do WhatsApp (imediato, pro cliente ver "digitando..." durante o debounce+processamento)
    wamid = payload.get("source_id")
    if wamid:
        asyncio.create_task(send_whatsapp_typing(wamid))

    # DEBOUNCE: agrupa rajadas. Cada msg nova cancela o processamento anterior
    # (inclusive uma chamada a Eli em andamento) e reagenda; ao fim da janela,
    # responde 1x com o historico completo (buscado do Chatwoot na hora).
    _last_content[conversation_id] = content
    old = _pending.get(conversation_id)
    if old and not old.done():
        old.cancel()
    _pending[conversation_id] = asyncio.create_task(
        process_conversation(account_id, conversation_id, contact_id)
    )
    return {"ok": True, "scheduled": True}
