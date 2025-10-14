# cr_ws.py — SpainRoom Voice CR (ConversationRelay WebSocket)
# Nora · 2025-10-14 (mejora: diálogo guiado + ruteo lead via HTTP)
#
# Start (Render):
#   pip install -r requirements.txt    # añade websockets si falta
#   gunicorn "cr_ws:app" -k uvicorn.workers.UvicornWorker
#
# Endpoints:
#   GET /health  -> {"ok": true, "service": "voice-cr-ws"}
#   WS  /cr      -> ConversationRelay WebSocket endpoint (Twilio connects aquí)

import os
import json
import logging
import asyncio
import httpx
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

logger = logging.getLogger("voice-cr")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# Config desde env (ajusta si hace falta)
LEADS_ENDPOINT = os.getenv("VOICE_LEADS_ENDPOINT", "").rstrip("/")  # e.g. https://backend-spainroom.onrender.com/api/voice/lead
SEND_TTS = os.getenv("SEND_TTS", "false").lower() in ("1","true","yes")
CENTRAL_PHONE = os.getenv("CENTRAL_PHONE", "")

async def health(request):
    return JSONResponse({"ok": True, "service": "voice-cr-ws"})

# ---- helpers ----
def _log_info(*a, **k):
    try:
        logger.info(*a, **k)
    except Exception:
        pass

async def _post_lead(payload):
    """POST simple al backend para crear/guardar/rutar lead (si VOICE_LEADS_ENDPOINT está definido)."""
    if not LEADS_ENDPOINT:
        _log_info("No LEADS_ENDPOINT configurado; skip POST lead:", payload)
        return {"ok": False, "reason": "no_endpoint"}
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(LEADS_ENDPOINT, json=payload)
            try:
                return r.json()
            except Exception:
                return {"ok": False, "status_code": r.status_code, "text": r.text}
    except Exception as e:
        return {"ok": False, "error": str(e)}

async def _send_tts(ws: WebSocket, text: str):
    """Envía TTS al ConversationRelay si quieres que el sistema hable. Experimental: depende del cliente.
       Twilio ConversationRelay acepta mensajes JSON; hay implementaciones que reaccionan a {"event":"message","text":...}
       Ajusta según lo que tu voice-cr/ws espere.
    """
    if not SEND_TTS:
        return
    try:
        # estructura conservadora; adapta si tu CR espera otro formato
        msg = json.dumps({"event":"message", "type":"tts", "text": text})
        await ws.send_text(msg)
        _log_info(">>> TTS sent:", text)
    except Exception as e:
        _log_info("TTS send failed:", e)

# ---- máquina de estados por conexión ----
class DialogState:
    def __init__(self):
        self.step = "start"   # start -> role -> zone -> name -> phone -> done
        self.role = None
        self.zone = None
        self.name = None
        self.phone = None

    def is_complete(self):
        return self.step == "done"

    def to_payload(self, call_sid="", from_num="", to_num=""):
        return {
            "role": self.role,
            "zone": self.zone,
            "name": self.name,
            "phone": self.phone,
            "call_sid": call_sid,
            "from": from_num,
            "to": to_num
        }

# basic heuristics for user replies
def extract_text_from_event(ev: dict):
    """Intenta sacar el texto de distintos eventos que Twilio/ConversationRelay envía."""
    # Twilio CR sends different shapes; try some common ones:
    candidates = []
    if "text" in ev and isinstance(ev["text"], str):
        candidates.append(ev["text"])
    # Twilio MediaStreams / ConversationRelay can send {"speech": {..., "transcript":"..."}} or similar
    if ev.get("event") in ("transcription","transcribed","speech_final"):
        t = ev.get("transcript") or ev.get("text") or (ev.get("speech") or {}).get("transcript")
        if t: candidates.append(t)
    # fallback: flatten first-level string fields
    for k,v in ev.items():
        if isinstance(v, str) and len(v) < 400 and len(v) > 1:
            candidates.append(v)
    # pick longest
    if not candidates:
        return ""
    return max(candidates, key=len).strip()

# ---- websocket handler ----
async def cr_ws(websocket: WebSocket):
    await websocket.accept()
    qp = websocket.query_params
    call_sid = qp.get("callSid") or ""
    from_num = qp.get("from") or ""
    to_num   = qp.get("to") or ""
    _log_info("[CR] WS connected callSid=%s from=%s to=%s", call_sid, from_num, to_num)

    state = DialogState()

    # optionally prompt initial question (if you want the WS to speak after connect)
    # Nota: normalmente el TwiML en /voice/answer_cr envía el saludo; aquí solo un follow-up
    try:
        await asyncio.sleep(0.1)
        # ejemplo: preguntar rol si no hecho (puedes desactivar si TwiML ya lo hace)
        # await _send_tts(websocket, "Para atenderle: ¿Es usted propietario, inquilino o franquiciado?")
    except Exception:
        pass

    try:
        while True:
            message = await websocket.receive()
            if "text" in message and message["text"] is not None:
                txt = message["text"]
                # intenta parse JSON
                try:
                    data = json.loads(txt)
                except Exception:
                    data = {"raw": txt}

                # extraer texto hablado
                speech_txt = extract_text_from_event(data).lower()
                _log_info("[CR] recv event (callSid=%s) text_len=%d speech_txt=%s", call_sid, len(txt), repr(speech_txt[:80]))

                # flujo de diálogo simple: avanzamos según estado
                if state.step == "start":
                    # si TwiML already asked greeting, expect user to answer role
                    # busco keywords
                    if any(k in speech_txt for k in ("propiet", "inquil", "franquic", "admin")):
                        if "propiet" in speech_txt: state.role = "propietario"
                        elif "inquil" in speech_txt: state.role = "inquilino"
                        elif "franquic" in speech_txt: state.role = "franquiciado"
                        else: state.role = "otro"
                        state.step = "zone"
                        await _send_tts(websocket, "Perfecto. ¿En qué provincia o zona está interesado?")
                    else:
                        # no detectado: preguntar rol (si no detectado)
                        state.step = "role"
                        await _send_tts(websocket, "¿Es usted propietario, inquilino o franquiciado?")
                elif state.step in ("role",):
                    # intentar parsear role desde respuesta
                    if any(k in speech_txt for k in ("propiet", "inquil", "franquic", "admin")):
                        if "propiet" in speech_txt: state.role = "propietario"
                        elif "inquil" in speech_txt: state.role = "inquilino"
                        elif "franquic" in speech_txt: state.role = "franquiciado"
                        else: state.role = "otro"
                        state.step = "zone"
                        await _send_tts(websocket, "Entendido. ¿En qué provincia o zona está interesado?")
                    else:
                        # repetir
                        await _send_tts(websocket, "No le he entendido. ¿Propietario, inquilino o franquiciado?")
                elif state.step == "zone":
                    if speech_txt:
                        state.zone = speech_txt.strip()
                        state.step = "name"
                        await _send_tts(websocket, "Perfecto. ¿Cuál es su nombre completo?")
                    else:
                        await _send_tts(websocket, "¿En qué provincia o zona está interesado?")
                elif state.step == "name":
                    if speech_txt:
                        state.name = speech_txt.strip()
                        state.step = "phone"
                        await _send_tts(websocket, "Gracias. ¿Cuál es su teléfono de contacto, por favor?")
                    else:
                        await _send_tts(websocket, "Dígame su nombre completo, por favor.")
                elif state.step == "phone":
                    # intentar extraer dígitos de teléfono
                    digits = "".join(ch for ch in speech_txt if ch.isdigit() or ch in "+")
                    if len(digits) >= 6:
                        state.phone = digits
                        state.step = "done"
                        await _send_tts(websocket, "Perfecto. En breve le pondremos en contacto. Gracias por llamar a SpainRoom. Adiós.")
                        # crear lead (async)
                        payload = state.to_payload(call_sid=call_sid, from_num=from_num, to_num=to_num)
                        _log_info("[CR] creating lead payload=%s", payload)
                        # fire-and-forget
                        asyncio.create_task(_post_lead(payload))
                    else:
                        # si el usuario habló sin dígitos, guardar literal y seguir
                        if speech_txt and len(speech_txt) > 3 and any(c.isalpha() for c in speech_txt):
                            state.phone = speech_txt.strip()
                            state.step = "done"
                            await _send_tts(websocket, "Gracias. Le contactaremos pronto. Adiós.")
                            payload = state.to_payload(call_sid=call_sid, from_num=from_num, to_num=to_num)
                            asyncio.create_task(_post_lead(payload))
                        else:
                            await _send_tts(websocket, "No escuché bien el número. ¿Me lo puede repetir?")
                elif state.step == "done":
                    # ya procesado, ignorar o agradecer
                    await _send_tts(websocket, "Gracias. Hemos tomado nota.")
                else:
                    _log_info("[CR] estado desconocido: %s", state.step)

            elif "bytes" in message and message["bytes"] is not None:
                # ignore binary
                _log_info("[CR] recv binary len=%d", len(message["bytes"]))

            elif message.get("type") == "websocket.disconnect":
                break

    except WebSocketDisconnect:
        _log_info("[CR] WS disconnected callSid=%s", call_sid)
    except Exception as e:
        _log_info("[CR] WS error callSid=%s err=%s", call_sid, str(e))
        try:
            await websocket.close(code=1011)
        except Exception:
            pass

routes = [
    Route("/health", health, methods=["GET"]),
    WebSocketRoute("/cr", cr_ws),
]

app = Starlette(debug=False, routes=routes)
