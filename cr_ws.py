# cr_ws.py — SpainRoom ConversationRelay WS (FastAPI)
# Diálogo guiado: rol (propietario/inquilino) → ciudad → nombre → teléfono → confirmación.
# Compatible con varios formatos de eventos CR (intenta leer transcript en varias claves).

import re
import json
from typing import Any, Dict, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

app = FastAPI(title="SpainRoom Voice — CR WS")

# ----------------------- Utils -----------------------

def norm_text(s: str) -> str:
    return (s or "").strip().lower()

def extract_phone(s: str) -> Optional[str]:
    if not s: return None
    digits = re.sub(r"\D+", "", s)
    # números españoles típicos: 9 dígitos (o con +34 prefijo)
    if len(digits) >= 9:
        if digits.startswith("34") and len(digits) >= 11:
            return "+" + digits
        if digits.startswith("0"):
            return digits
        if len(digits) == 9:
            return "+34" + digits
        return "+" + digits
    return None

def looks_yes(s: str) -> bool:
    s = f" {norm_text(s)} "
    return any(x in s for x in [" sí ", " si ", " correcto ", " vale ", " de acuerdo ", " afirmativo "])

def looks_no(s: str) -> bool:
    s = f" {norm_text(s)} "
    return any(x in s for x in [" no ", " negativo ", " no quiero ", " no gracias "])

def role_from_text(s: str) -> Optional[str]:
    s = norm_text(s)
    if re.search(r"\bpropiet", s) or "dueñ" in s:
        return "propietario"
    if "inquil" in s or "alquil" in s:
        return "inquilino"
    return None

def extract_transcript(payload: Any) -> Optional[str]:
    """
    Intenta encontrar texto de usuario en varios formatos que usa Twilio CR o integraciones:
    - {"input_transcript": "..."}
    - {"speech": {"type": "transcript", "alternatives":[{"transcript": "..."}]}}
    - {"text": "..."} o {"user": "..."}
    - plain string
    """
    if isinstance(payload, str):
        return payload.strip() or None
    if not isinstance(payload, dict):
        return None

    # 1) input_transcript directo
    t = payload.get("input_transcript")
    if isinstance(t, str) and t.strip():
        return t.strip()

    # 2) speech.alternatives[0].transcript
    speech = payload.get("speech") or payload.get("asr") or {}
    if isinstance(speech, dict):
        alts = speech.get("alternatives")
        if isinstance(alts, list) and alts:
            tt = alts[0].get("transcript") if isinstance(alts[0], dict) else None
            if isinstance(tt, str) and tt.strip():
                return tt.strip()
        # a veces hay "text" directamente
        st = speech.get("text")
        if isinstance(st, str) and st.strip():
            return st.strip()

    # 3) text / user genéricos
    for k in ["text", "user", "utterance", "transcript"]:
        v = payload.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()

    # 4) nested "input": {"text": "..."}
    inp = payload.get("input")
    if isinstance(inp, dict):
        tx = inp.get("text")
        if isinstance(tx, str) and tx.strip():
            return tx.strip()

    return None

async def say(ws: WebSocket, text: str, interruptible: bool = True):
    await ws.send_json({
        "type": "text",
        "token": text,
        "last": True,
        "interruptible": interruptible
    })

# ----------------------- Conversación -----------------------

@app.get("/health")
async def health():
    return JSONResponse({"ok": True, "service": "voice-cr-ws"})

@app.websocket("/cr")
async def cr_socket(ws: WebSocket):
    await ws.accept()

    session: Dict[str, Any] = {
        "step": "role",    # role -> city -> name -> phone -> confirm
        "data": {"role": "", "city": "", "name": "", "phone": ""},
    }

    # saludo inicial
    await say(ws, "Bienvenido a SpainRoom. ¿Es usted propietario o inquilino?")

    try:
        while True:
            raw = await ws.receive_text()
            try:
                payload = json.loads(raw)
            except Exception:
                payload = raw

            user_text = extract_transcript(payload)

            # Si no hay texto, pide repetir
            if not user_text:
                await say(ws, "No le he entendido. ¿Podría repetirlo, por favor?")
                continue

            txt = norm_text(user_text)

            # Lógica por pasos
            step = session["step"]
            data = session["data"]

            if step == "role":
                r = role_from_text(txt)
                if not r:
                    await say(ws, "¿Es propietario o inquilino?")
                    continue
                data["role"] = r
                session["step"] = "city"
                await say(ws, f"De acuerdo. ¿En qué ciudad o población?")
                continue

            if step == "city":
                # Acepta cualquier string como ciudad
                data["city"] = user_text.strip().title()
                session["step"] = "name"
                await say(ws, "Perfecto. ¿Su nombre y apellidos, por favor?")
                continue

            if step == "name":
                # Acepta cualquier string como nombre
                data["name"] = user_text.strip().title()
                session["step"] = "phone"
                await say(ws, "Gracias. ¿Me indica un teléfono de contacto?")
                continue

            if step == "phone":
                ph = extract_phone(user_text)
                if not ph:
                    await say(ws, "No he captado bien el teléfono. Dígamelo dígito a dígito, por favor.")
                    continue
                data["phone"] = ph
                session["step"] = "confirm"
                await say(ws,
                          f"Entonces, {data['name']}, {data['role']} en {data['city']}, teléfono {data['phone']}. ¿Es correcto?")
                continue

            if step == "confirm":
                if looks_yes(user_text):
                    # Aquí podrías POSTear al backend API para guardar el lead:
                    # try:
                    #   import urllib.request, json as jsonlib
                    #   req = urllib.request.Request(
                    #       "https://backend-spainroom.onrender.com/api/contacto/voice_lead",
                    #       data=jsonlib.dumps(data).encode("utf-8"),
                    #       headers={"Content-Type":"application/json"},
                    #       method="POST"
                    #   )
                    #   urllib.request.urlopen(req, timeout=2.0).read()
                    # except Exception:
                    #   pass
                    await say(ws, "Perfecto. Un asesor de SpainRoom se pondrá en contacto con usted. ¡Gracias!")
                    # Puedes cerrar tras despedida si quieres:
                    # await ws.close()
                    session["step"] = "done"
                elif looks_no(user_text):
                    session["step"] = "role"
                    session["data"] = {"role": "", "city": "", "name": "", "phone": ""}
                    await say(ws, "De acuerdo, volvemos a empezar. ¿Es usted propietario o inquilino?")
                else:
                    await say(ws, "¿Podría confirmar si es correcto, sí o no?")
                continue

            # Si ya está done o no encaja, eco amable
            if session["step"] == "done":
                await say(ws, "¿Desea algo más?")
            else:
                await say(ws, f"Ha dicho: {user_text}. ¿Podría repetirlo o dar más detalles?")
    except WebSocketDisconnect:
        # usuario colgó o Twilio cortó
        pass
    except Exception:
        # fallo general (silenciado para que Twilio reintente)
        pass
    finally:
        try:
            await ws.close()
        except Exception:
            pass
