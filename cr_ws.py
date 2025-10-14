# cr_ws.py — SpainRoom Voice CR (ConversationRelay WebSocket)
# Nora · 2025-10-14
#
# Start (Render):
#   gunicorn "cr_ws:app" -k uvicorn.workers.UvicornWorker
#
# Endpoints:
#   GET /health  -> {"ok": true, "service": "voice-cr-ws"}
#   WS  /cr      -> ConversationRelay WebSocket endpoint (Twilio connects here)
#
# Notas:
# - Este WS no envía prompts ni corta al usuario: se limita a aceptar la conexión y loguear eventos.
# - Deja el saludo/guion en el TwiML (backend-1) con <Say> y/o welcomeGreeting.
# - Si en el futuro quieres que el WS envíe respuestas (TTS), añade send_json() según tu pipeline.

import json
import logging
from starlette.applications import Starlette
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

logger = logging.getLogger("voice-cr")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

async def health(request):
    return JSONResponse({"ok": True, "service": "voice-cr-ws"})

async def cr_ws(websocket: WebSocket):
    # Acepta conexión
    await websocket.accept()
    qp = websocket.query_params
    call_sid = qp.get("callSid") or ""
    from_num = qp.get("from") or ""
    to_num   = qp.get("to") or ""
    logger.info("[CR] WS connected callSid=%s from=%s to=%s", call_sid, from_num, to_num)

    try:
        while True:
            # Recibe texto o binario; Twilio CR envía JSON (text frames)
            message = await websocket.receive()
            if "text" in message and message["text"] is not None:
                txt = message["text"]
                # Intenta parsear JSON (si falla, lo registramos como texto)
                try:
                    data = json.loads(txt)
                    # Ejemplo de log reduciendo tamaño:
                    evt = data.get("event") or data.get("type") or "message"
                    logger.info("[CR] recv event=%s len=%d", evt, len(txt))
                except Exception:
                    logger.info("[CR] recv text len=%d", len(txt))
                # NOTA: no respondemos para evitar bucles / barge-in
                # Si quisieras ack/keepalive, podrías:
                # await websocket.send_text('{"event":"keepalive"}')

            elif "bytes" in message and message["bytes"] is not None:
                logger.info("[CR] recv binary len=%d", len(message["bytes"]))
                # Normalmente CR usa frames de texto; ignoramos binario

            elif message.get("type") == "websocket.disconnect":
                break

    except WebSocketDisconnect:
        logger.info("[CR] WS disconnected callSid=%s", call_sid)
    except Exception as e:
        logger.warning("[CR] WS error callSid=%s err=%s", call_sid, str(e))
        try:
            await websocket.close(code=1011)
        except Exception:
            pass

routes = [
    Route("/health", health, methods=["GET"]),
    WebSocketRoute("/cr", cr_ws),
]

app = Starlette(debug=False, routes=routes)
