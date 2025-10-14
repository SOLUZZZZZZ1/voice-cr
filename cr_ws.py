# cr_ws.py — SpainRoom Voice CR (ConversationRelay WebSocket)
import json, logging
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

logger = logging.getLogger("voice-cr")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

async def health(request):
    return JSONResponse({"ok": True, "service": "voice-cr-ws"})

async def cr_ws(websocket: WebSocket):
    await websocket.accept()
    qp = websocket.query_params
    call_sid = qp.get("callSid") or ""
    from_num = qp.get("from") or ""
    to_num   = qp.get("to") or ""
    logger.info("[CR] WS connected callSid=%s from=%s to=%s", call_sid, from_num, to_num)
    try:
        while True:
            msg = await websocket.receive()
            if "text" in msg and msg["text"] is not None:
                txt = msg["text"]
                try:
                    data = json.loads(txt)
                    evt = data.get("event") or data.get("type") or "message"
                    logger.info("[CR] recv event=%s len=%d", evt, len(txt))
                except Exception:
                    logger.info("[CR] recv text len=%d", len(txt))
            elif "bytes" in msg and msg["bytes"] is not None:
                logger.info("[CR] recv binary len=%d", len(msg["bytes"]))
            elif msg.get("type") == "websocket.disconnect":
                break
    except WebSocketDisconnect:
        logger.info("[CR] WS disconnected callSid=%s", call_sid)
    except Exception as e:
        logger.warning("[CR] WS error callSid=%s err=%s", call_sid, str(e))
        try: await websocket.close(code=1011)
        except Exception: pass

routes = [
    Route("/health", health, methods=["GET"]),
    WebSocketRoute("/cr", cr_ws),
]

app = Starlette(debug=False, routes=routes)
