# cr_ws.py — SpainRoom Voice CR (ConversationRelay WebSocket) — SAFE
# Nora · 2025-10-15 (import httpx perezoso + robusto)

import os
import json
import logging
import asyncio
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

logger = logging.getLogger("voice-cr")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ENV
LEADS_ENDPOINT = (os.getenv("VOICE_LEADS_ENDPOINT") or "").strip()
SEND_TTS = (os.getenv("SEND_TTS") or "").lower() in ("1","true","yes")
CENTRAL_PHONE = os.getenv("CENTRAL_PHONE", "")

async def health(request):
    return JSONResponse({"ok": True, "service": "voice-cr-ws"})

def _log(*args):
    try:
        logger.info(*args)
    except Exception:
        pass

async def _post_lead(payload: dict):
    """POST lead al backend si hay endpoint.
       IMPORTANTE: import httpx SOLO si existe para no romper el arranque.
    """
    if not LEADS_ENDPOINT:
        _log("[CR] No VOICE_LEADS_ENDPOINT; skip POST lead %s", payload)
        return {"ok": False, "reason": "no_endpoint"}

    try:
        import httpx  # import perezoso
    except Exception as e:
        _log("[CR] httpx no instalado; lead en log. err=%s payload=%s", e, payload)
        return {"ok": False, "reason": "httpx_missing"}

    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(LEADS_ENDPOINT, json=payload)
            try:
                j = r.json()
            except Exception:
                j = {"status": r.status_code, "text": r.text}
            _log("[CR] lead POST resp=%s", j)
            return j
    except Exception as e:
        _log("[CR] lead POST error=%s", e)
        return {"ok": False, "error": str(e)}

async def _send_tts(ws: WebSocket, text: str):
    """Envía un mensaje al cliente para que vocalice (si tu CR lo soporta).
       Lo dejamos opcional para no provocar bucles.
    """
    if not SEND_TTS:
        return
    try:
        await ws.send_text(json.dumps({"event": "message", "type": "tts", "text": text}))
        _log("[CR] >>> TTS: %s", text)
    except Exception as e:
        _log("[CR] TTS send failed: %s", e)

def _extract_text(ev: dict) -> str:
    """Intenta extraer el texto transcrito de distintos formatos."""
    cand = []
    if isinstance(ev.get("text"), str):
        cand.append(ev["text"])
    if ev.get("event") in ("transcription", "transcribed", "speech_final"):
        t = ev.get("transcript") or (ev.get("speech") or {}).get("transcript") or ev.get("text")
        if t:
            cand.append(t)
    # Fallback: primeros strings cortos
    for k, v in ev.items():
        if isinstance(v, str) and 1 < len(v) < 400:
            cand.append(v)
    return max(cand, key=len).strip() if cand else ""

class DialogState:
    def __init__(self):
        self.step = "start"
        self.role = None
        self.zone = None
        self.name = None
        self.phone = None

    def to_payload(self, call_sid="", from_num="", to_num=""):
        return {
            "role": self.role,
            "zone": self.zone,
            "name": self.name,
            "phone": self.phone,
            "call_sid": call_sid,
            "from": from_num,
            "to": to_num,
            "source": "voice",
        }

async def cr_ws(websocket: WebSocket):
    await websocket.accept()
    qp = websocket.query_params
    call_sid = qp.get("callSid") or ""
    from_num = qp.get("from") or ""
    to_num   = qp.get("to") or ""
    _log("[CR] WS connected callSid=%s from=%s to=%s", call_sid, from_num, to_num)

    state = DialogState()

    try:
        while True:
            msg = await websocket.receive()

            if "text" in msg and msg["text"] is not None:
                raw = msg["text"]
                try:
                    ev = json.loads(raw)
                except Exception:
                    ev = {"text": raw}
                speech = _extract_text(ev).lower()
                _log("[CR] recv callSid=%s text=%s", call_sid, repr(speech[:100]))

                # diálogo simple
                if state.step == "start":
                    # espera rol
                    if any(k in speech for k in ("propiet", "inquil", "franquic")):
                        if "propiet" in speech: state.role = "propietario"
                        elif "inquil" in speech: state.role = "inquilino"
                        else: state.role = "franquiciado"
                        state.step = "zone"
                        await _send_tts(websocket, "¿En qué provincia o zona está interesado?")
                    else:
                        state.step = "role"
                        await _send_tts(websocket, "¿Es usted propietario, inquilino o franquiciado?")

                elif state.step == "role":
                    if any(k in speech for k in ("propiet", "inquil", "franquic")):
                        if "propiet" in speech: state.role = "propietario"
                        elif "inquil" in speech: state.role = "inquilino"
                        else: state.role = "franquiciado"
                        state.step = "zone"
                        await _send_tts(websocket, "Gracias. ¿En qué provincia o zona está interesado?")
                    else:
                        await _send_tts(websocket, "No le he entendido. Propietario, inquilino o franquiciado?")

                elif state.step == "zone":
                    if speech:
                        state.zone = speech.strip()
                        state.step = "name"
                        await _send_tts(websocket, "Perfecto. ¿Cuál es su nombre completo?")
                    else:
                        await _send_tts(websocket, "¿En qué provincia o zona está interesado?")

                elif state.step == "name":
                    if speech:
                        state.name = speech.strip()
                        state.step = "phone"
                        await _send_tts(websocket, "Gracias. ¿Cuál es su teléfono de contacto?")
                    else:
                        await _send_tts(websocket, "Dígame su nombre completo, por favor.")

                elif state.step == "phone":
                    digits = "".join(ch for ch in speech if ch.isdigit() or ch == "+")
                    if len(digits) >= 6:
                        state.phone = digits
                        # cerrar y enviar lead
                        payload = state.to_payload(call_sid, from_num, to_num)
                        _log("[CR] creating lead %s", payload)
                        asyncio.create_task(_post_lead(payload))
                        await _send_tts(websocket, "Gracias. En breve nos pondremos en contacto. Adiós.")
                        state.step = "done"
                    elif speech and any(c.isalpha() for c in speech):
                        state.phone = speech.strip()
                        payload = state.to_payload(call_sid, from_num, to_num)
                        _log("[CR] creating lead (literal phone) %s", payload)
                        asyncio.create_task(_post_lead(payload))
                        await _send_tts(websocket, "Gracias. En breve nos pondremos en contacto. Adiós.")
                        state.step = "done"
                    else:
                        await _send_tts(websocket, "No escuché bien el número. ¿Me lo puede repetir?")

                elif state.step == "done":
                    await _send_tts(websocket, "Gracias, llamada finalizada.")

            elif "bytes" in msg and msg["bytes"] is not None:
                _log("[CR] recv binary len=%d", len(msg["bytes"]))

            elif msg.get("type") == "websocket.disconnect":
                break

    except WebSocketDisconnect:
        _log("[CR] WS disconnected callSid=%s", call_sid)
    except Exception as e:
        _log("[CR] WS error callSid=%s err=%s", call_sid, str(e))
        try:
            await websocket.close(code=1011)
        except Exception:
            pass

routes = [
    Route("/health", health, methods=["GET"]),
    WebSocketRoute("/cr", cr_ws),
]

app = Starlette(debug=False, routes=routes)
