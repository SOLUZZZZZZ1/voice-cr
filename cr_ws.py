import json
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

app = FastAPI(title="SpainRoom Voice — CR WS")

@app.get("/health")
async def health():
    return JSONResponse({"ok": True, "service": "voice-cr-ws"})

@app.websocket("/cr")
async def cr_socket(ws: WebSocket):
    await ws.accept()
    await ws.send_json({"type":"text","token":"Conectado. ¿En qué puedo ayudarle?","last":True,"interruptible":True})
    try:
        while True:
            msg = await ws.receive_text()
            try: data = json.loads(msg)
            except: data = {"raw": msg}
            user_text = None
            if isinstance(data, dict):
                user_text = data.get("input_transcript") or data.get("user") or None
            if user_text:
                reply = f"Ha dicho: {user_text}. ¿Desea que tome sus datos para contactarle?"
                await ws.send_json({"type":"text","token":reply,"last":True,"interruptible":True})
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        try: await ws.close()
        except: pass
