# cr_ws.py — SpainRoom ConversationRelay WS (FastAPI)
# Más tolerante con formatos de Twilio CR: procesa frames text/JSON, busca transcript profundo,
# guía el flujo (propietario / inquilino / franquiciado) y evita bucles de "no le he entendido".

import re
import json
import urllib.request
from typing import Any, Dict, Optional, List

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

API_BASE = "https://backend-spainroom.onrender.com"

app = FastAPI(title="SpainRoom Voice — CR WS")

# ----------------------- Utils robustos -----------------------

def norm_text(s: str) -> str:
    return (s or "").strip().lower()

def extract_phone(s: str) -> Optional[str]:
    if not s: return None
    digits = re.sub(r"\D+", "", s)
    if len(digits) >= 9:
        if digits.startswith("34") and len(digits) >= 11:
            return "+" + digits
        if len(digits) == 9:
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

def _collect_strings(obj: Any, out: List[str], depth=0, max_depth=5):
    if depth > max_depth:
        return
    if isinstance(obj, str):
        t = obj.strip()
        if t: out.append(t); return
    if isinstance(obj, dict):
        for v in obj.values():
            _collect_strings(v, out, depth+1, max_depth)
    elif isinstance(obj, list):
        for v in obj:
            _collect_strings(v, out, depth+1, max_depth)

def extract_transcript(payload: Any) -> Optional[str]:
    """
    Estrategia:
    1) Claves típicas directas
    2) speech / asr con alternatives
    3) claves genéricas (text, user, utterance, transcript)
    4) input.text
    5) búsqueda profunda: elegimos la STRING más larga (>= 3 chars)
    """
    if isinstance(payload, str):
        return payload.strip() or None
    if not isinstance(payload, dict):
        return None

    # 1) directas
    t = payload.get("input_transcript")
    if isinstance(t, str) and t.strip(): return t.strip()

    # 2) speech/asr con alternatives
    for k in ("speech", "asr"):
        block = payload.get(k) or {}
        if isinstance(block, dict):
            alts = block.get("alternatives")
            if isinstance(alts, list) and alts:
                tt = alts[0].get("transcript") if isinstance(alts[0], dict) else None
                if isinstance(tt, str) and tt.strip(): return tt.strip()
            st = block.get("text")
            if isinstance(st, str) and st.strip(): return st.strip()

    # 3) genéricas
    for k in ("text", "user", "utterance", "transcript"):
        v = payload.get(k)
        if isinstance(v, str) and v.strip(): return v.strip()

    # 4) input.text
    inp = payload.get("input")
    if isinstance(inp, dict):
        tx = inp.get("text")
        if isinstance(tx, str) and tx.strip(): return tx.strip()

    # 5) búsqueda profunda de strings (elige la más larga razonable)
    strings: List[str] = []
    _collect_strings(payload, strings, 0, 5)
    if strings:
        # quedarse con la cadena más larga con letras
        strings = [s for s in strings if re.search(r"[A-Za-zÁÉÍÓÚáéíóúñÑ]", s)]
        if strings:
            strings.sort(key=lambda s: len(s), reverse=True)
            best = strings[0].strip()
            if len(best) >= 3:
                return best

    return None

async def say(ws: WebSocket, text: str, interruptible: bool = True):
    await ws.send_json({"type": "text", "token": text, "last": True, "interruptible": interruptible})

def post_json(url: str, body: Dict[str, Any], timeout: float = 2.5) -> None:
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

    session: Dict[str, Any] = {
        "step": "role",   # role -> city|zone -> name -> phone -> confirm -> done
        "data": {"role": "", "city": "", "zone": "", "name": "", "phone": ""},
        "no_understood": 0,
    }

    await say(ws, "Bienvenido a SpainRoom. ¿Es usted propietario, inquilino o franquiciado?")

    try:
        while True:
            # Recibir frame (texto o json)
            try:
                raw = await ws.receive_text()
                try:
                    payload = json.loads(raw)
                except Exception:
                    payload = raw
            except Exception:
                # si no es texto, intenta JSON directo
                try:
                    payload = await ws.receive_json()
                except Exception:
                    payload = None

            user_text = extract_transcript(payload) if payload is not None else None

            if not user_text:
                session["no_understood"] += 1
                if session["no_understood"] >= 2:
                    await say(ws, "No le he entendido bien. Diga por favor: ‘propietario’, ‘inquilino’ o ‘franquiciado’.")
                    session["no_understood"] = 0
                else:
                    await say(ws, "No le he entendido. ¿Podría repetirlo, por favor?")
                continue

            session["no_understood"] = 0
            txt = norm_text(user_text)
            step = session["step"]
            data = session["data"]

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

            if step == "name":
                data["name"] = user_text.strip().title()
                session["step"] = "phone"
                await say(ws, "¿Me indica un teléfono de contacto?")
                continue

            if step == "phone":
                ph = extract_phone(user_text)
                if not ph:
                    await say(ws, "No he captado bien el teléfono. Dígamelo dígito a dígito, por favor.")
                    continue
                data["phone"] = ph
                session["step"] = "confirm"
                if data["role"] == "franquiciado":
                    await say(ws, f"Entonces, {data['name']}, interesado en franquicia en {data['zone']}, teléfono {data['phone']}. ¿Es correcto?")
                else:
                    await say(ws, f"Entonces, {data['name']}, {data['role']} en {data['city']}, teléfono {data['phone']}. ¿Es correcto?")
                continue

            if step == "confirm":
                if looks_yes(user_text):
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
                        # Cambia a tus endpoints reales si los tienes separados
                        post_json(f"{API_BASE}/api/contacto", body)
                        await say(ws, "Perfecto. Un asesor de SpainRoom se pondrá en contacto con usted. ¡Gracias!")
                    session["step"] = "done"
                elif looks_no(user_text):
                    session["step"] = "role"
                    session["data"] = {"role": "", "city": "", "zone": "", "name": "", "phone": ""}
                    await say(ws, "De acuerdo, volvemos a empezar. ¿Es usted propietario, inquilino o franquiciado?")
                else:
                    await say(ws, "¿Podría confirmar si es correcto, sí o no?")
                continue

            if session["step"] == "done":
                await say(ws, "¿Desea algo más?")
            else:
                await say(ws, f"Ha dicho: {user_text}. ¿Podría repetirlo o dar más detalles?")

    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        try:
            await ws.close()
        except Exception:
            pass
