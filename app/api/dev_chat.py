"""Browser-based chat window for exercising the WhatsApp agent locally.

Talks to the same ``handle_message()`` the real webhook uses, so what you see
here is what a WhatsApp user would get — without a phone, a Meta app, or a
tunnel. Outbound delivery is deliberately NOT exercised: this returns the
reply over HTTP instead of calling Meta, so no message is ever sent.

Never registered when APP_ENV=production (see main.py) — the route does not
exist there rather than merely refusing, since it runs the agent as any phone
number with no signature check.
"""

import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app.api.dev_chat_guard import assert_not_production as _assert_not_production
from app.db.session import async_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dev", tags=["dev"])


class DevChatRequest(BaseModel):
    # Defaults to a test number so the window works with no setup. Any value is
    # accepted: conversation state is keyed by phone, so changing it starts a
    # fresh conversation.
    phone: str = Field(default="919999999999", min_length=10, max_length=15)
    text: str = Field(min_length=1, max_length=4096)


class DevChatResponse(BaseModel):
    reply: str


@router.post("/whatsapp-chat", response_model=DevChatResponse)
async def dev_whatsapp_chat(payload: DevChatRequest) -> DevChatResponse:
    """Run one turn through the real agent and return its reply."""
    _assert_not_production()

    # Imported here rather than at module scope: the agent builds a DB engine on
    # import, which the guard tests must not require.
    from app.bot.agent import handle_message

    async with async_session() as db:
        try:
            reply = await handle_message(db, payload.phone, payload.text)
            await db.commit()
        except Exception:
            await db.rollback()
            # Surface the failure in the window instead of a bare 500 — the
            # traceback is in the server log, which is where you want it.
            logger.exception("dev chat: handler failed")
            raise HTTPException(status_code=500, detail="Handler raised — see server log")

    return DevChatResponse(reply=reply)


# Single-file window so there is no template/static wiring to maintain for a
# dev-only tool.
_PAGE = """
<!doctype html>
<title>Wishkr agent — dev chat</title>
<style>
  :root { color-scheme: light dark; }
  body { margin: 0; font: 15px/1.5 system-ui, sans-serif; display: flex;
         flex-direction: column; height: 100vh; }
  header { padding: 10px 16px; border-bottom: 1px solid #8883;
           display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
  header strong { font-weight: 600; }
  header input { font: inherit; padding: 4px 8px; width: 15ch;
                 border: 1px solid #8886; border-radius: 6px;
                 background: transparent; color: inherit; }
  #log { flex: 1; overflow-y: auto; padding: 16px; display: flex;
         flex-direction: column; gap: 10px; }
  .msg { max-width: min(75ch, 80%); padding: 8px 12px; border-radius: 12px;
         white-space: pre-wrap; overflow-wrap: anywhere; }
  .me { align-self: flex-end; background: #2f6feb; color: #fff; }
  .bot { align-self: flex-start; background: #8882; }
  .err { align-self: center; background: #d33; color: #fff; font-size: 13px; }
  form { display: flex; gap: 8px; padding: 12px 16px; border-top: 1px solid #8883; }
  #text { flex: 1; font: inherit; padding: 9px 12px; border: 1px solid #8886;
          border-radius: 8px; background: transparent; color: inherit; }
  button { font: inherit; padding: 9px 18px; border: 0; border-radius: 8px;
           background: #2f6feb; color: #fff; cursor: pointer; }
  button:disabled { opacity: .5; cursor: default; }
</style>
<header>
  <strong>Agent dev chat</strong>
  <label>phone <input id="phone" value="919999999999"></label>
  <span style="opacity:.6">state is keyed by phone — change it to reset</span>
</header>
<div id="log"></div>
<form id="form">
  <input id="text" placeholder="Message the agent…" autocomplete="off" autofocus>
  <button id="send">Send</button>
</form>
<script>
  const log = document.getElementById('log');
  const form = document.getElementById('form');
  const text = document.getElementById('text');
  const send = document.getElementById('send');
  const phone = document.getElementById('phone');

  function add(cls, body) {
    const el = document.createElement('div');
    el.className = 'msg ' + cls;
    el.textContent = body;
    log.appendChild(el);
    log.scrollTop = log.scrollHeight;
  }

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const body = text.value.trim();
    if (!body) return;
    add('me', body);
    text.value = '';
    send.disabled = true;
    try {
      const res = await fetch('./whatsapp-chat', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ phone: phone.value.trim(), text: body }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) add('err', data.detail || ('HTTP ' + res.status));
      else add('bot', data.reply);
    } catch (err) {
      add('err', String(err));
    } finally {
      send.disabled = false;
      text.focus();
    }
  });
</script>
"""


@router.get("/whatsapp-chat", response_class=HTMLResponse)
async def dev_whatsapp_chat_page() -> HTMLResponse:
    _assert_not_production()
    return HTMLResponse(_PAGE)
