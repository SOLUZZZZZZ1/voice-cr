# cr_ws.py — SpainRoom ConversationRelay WS (FastAPI)
# Flujos:
#  - Propietario / Inquilino: ciudad → nombre → teléfono → confirmación
#  - Franquiciado (interesado): zona → nombre → teléfono → confirmación → envía lead a Admin
#
# Compatible con varios formatos de eventos CR (transcripciones en distintas claves).
# Si el POST al backend falla, no rompe la llamada (best-effort).

import re
import json
import urllib.request
from typing import Any, Dict, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

# Ajusta si tu API tiene otro dominio:
API_BASE = "https://backend-spainroom.onrender.com"

app = FastAPI(title="SpainRoom Voice — CR WS")

# ----------------------- Utils -----------------------

def norm_text(s: str) -> str:
    return (s or "").strip().lower()

def extract_phone(s: str) -> Optional[str]:
    if not s: return None
    digits = re.sub(r"\D+", "", s)
    # Acepta números largos + internacional, normaliza España
    if len(digits) >= 9:
        if digits.startswith("34") and len(digits) >= 11:
            return "+" + digits
        if len(digits) == 9:  # típico móvil/ fijo sin prefijo
            return "+34" + digits
        return "+" + digits
    return None

def looks_yes(s: str) -> bool:
    s = f" {norm_text(s)} "
    return any(x in s for x in [" sí ", " si ", " correcto ", " vale ", " de acuerdo ", " afirmativo ", " exacto "])

def looks_no(s: str) -> bool:
    s = f" {norm_text(s)} "
    return any(x in s for x in [" no ", " negativo ", " no quiero ", " no gracias ", " incorrecto "])

def role_from_text(s: str) -> Optional[str]:
    s = norm_text(s)
    if re.search(r"\bpropiet", s) or "dueñ" in s:
        return "propietario"
    if "inquil" in s or "alquil" in s:
        return "inquilino"
    if "franquici" in s:
        return "franquiciado"
    return None

def extract_transcript(payload: Any) -> Optional[str]:
    """
    Intenta encontrar texto de usuario en varios formatos que usa Twilio CR o integraciones:
    - {"input_transcript": "..."}
    - {"speech": {"type": "transcript", "alternatives":[{"transcript": "..."}]}}
    - {"text": "..."} o {"user": "..."} o {"utterance": "..."}
    - {"input": {"text": "..."}}
    - plain string
    """
    if isinstance(payload, str):
        return payload.strip() or None
    if not isinstance(payload, dict):
        return None

    t = payload.get("input_transcript")
    if isinstance(t, str) and t.strip():
        return t.strip()

    speech = payload.get("speech") or payload.get("asr") or {}
    if isinstance(speech, dict):
        alts = speech.get("alternatives")
        if isinstance(alts, list) and alts:
            tt = alts[0].get("transcript") if isinstance(alts[0], dict) else None
            if isinstance(tt, str) and tt.strip():
                return tt.strip()
        st = speech.get("text")
        if isinstance(st, str) and st.strip():
            return st.strip()

    for k in ["text", "user", "utterance", "transcript"]:
        v = payload.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()

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

def post_json(url: str, body: Dict[str, Any], timeout: float = 2.5) -> None:
    """Best-effort POST; si falla no lanzamos excepción (no romper la llamada)."""
    try:
        req = urllib.request.Request(url,
                                     data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        urllib.request.urlopen(req, timeout=timeout).read()
    except Exception:
        pass

# ----------------------- Conversación -----------------------

@app.get("/health")
async def health():
    return JSONResponse({"ok": True, "service": "voice-cr-ws"})

@app.websocket("/cr")
async def cr_socket(ws: WebSocket):
    await ws.accept()

    # Estado de la sesión
    # step: role -> (prop/inq) -> city|zone -> name -> phone -> confirm
    session: Dict[str, Any] = {
        "step": "role",
        "data": {"role": "", "city": "", "zone": "", "name": "", "phone": ""},
    }

    # Saludo inicial
    await say(ws, "Bienvenido a SpainRoom. ¿Es usted propietario, inquilino o franquiciado?")

    try:
        while True:
            raw = await ws.receive_text()
            try:
                payload = json.loads(raw)
            except Exception:
                payload = raw

            user_text = extract_transcript(payload)
            if not user_text:
                await say(ws, "No le he entendido. ¿Podría repetirlo, por favor?")
                continue

            txt = norm_text(user_text)
            step = session["step"]
            data = session["data"]

            # 1) Descubrimos rol
            if step == "role":
                r = role_from_text(txt)
                if not r:
                    await say(ws, "¿Es propietario, inquilino o franquiciado?")
                    continue
                data["role"] = r
                if r == "franquiciado":
                    session["step"] = "zone"
                    await say(ws, "Perfecto. ¿Cuál es su zona de interés?")
                else:
                    session["step"] = "city"
                    await say(ws, "De acuerdo. ¿En qué ciudad o población?")
                continue

            # 2) Ciudad o zona (según rol)
            if step == "city":
                data["city"] = user_text.strip().title()
                session["step"] = "name"
                await say(ws, "Perfecto. ¿Su nombre y apellidos, por favor?")
                continue

            if step == "zone":
                data["zone"] = user_text.strip().title()
                session["step"] = "name"
                await say(ws, "Gracias. ¿Su nombre y apellidos?")
                continue

            # 3) Nombre
            if step == "name":
                data["name"] = user_text.strip().title()
                session["step"] = "phone"
                await say(ws, "¿Me indica un teléfono de contacto?")
                continue

            # 4) Teléfono
            if step == "phone":
                ph = extract_phone(user_text)
                if not ph:
                    await say(ws, "No he captado bien el teléfono. Dígamelo dígito a dígito, por favor.")
                    continue
                data["phone"] = ph
                session["step"] = "confirm"
                if data["role"] == "franquiciado":
                    await say(ws,
                        f"Entonces, {data['name']}, franquiciado interesado en {data['zone']}, teléfono {data['phone']}. ¿Es correcto?")
                else:
                    await say(ws,
                        f"Entonces, {data['name']}, {data['role']} en {data['city']}, teléfono {data['phone']}. ¿Es correcto?")
                continue

            # 5) Confirmación + envío de lead
            if step == "confirm":
                if looks_yes(user_text):
                    # Dispara lead a Admin según rol
                    role = data["role"]
                    if role == "franquiciado":
                        body = {
                            "nombre": data["name"],
                            "telefono": data["phone"],
                            "zona": data["zone"],
                            "via": "voice_franquiciado"
                        }
                        post_json(f"{API_BASE}/api/contacto/franquiciados", body)
                        await say(ws, "Perfecto. Un asesor de SpainRoom se pondrá en contacto con usted para la franquicia. ¡Gracias!")
                    else:
                        body = {
                            "rol": role,
                            "nombre": data["name"],
                            "telefono": data["phone"],
                            "poblacion": data["city"],
                            "via": "voice_web"
                        }
                        # si tienes un endpoint /api/contacto/tenants o /api/contacto/owners cámbialo aquí:
                        post_json(f"{API_BASE}/api/contacto", body)
                        await say(ws, "Perfecto. Un asesor de SpainRoom se pondrá en contacto con usted. ¡Gracias!")
                    session["step"] = "done"
                elif looks_no(user_text):
                    # Reiniciar
                    session["step"] = "role"
                    session["data"] = {"role": "", "city": "", "zone": "", "name": "", "phone": ""}
                    await say(ws, "De acuerdo, volvemos a empezar. ¿Es usted propietario, inquilino o franquiciado?")
                else:
                    await say(ws, "¿Podría confirmar si es correcto, sí o no?")
                continue

            # 6) Fin o eco amable
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
