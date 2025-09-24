# SpainRoom Voice CR (ConversationRelay WebSocket)

Servicio WebSocket para Twilio ConversationRelay.

## Endpoints
- `GET /health` → `{"ok":true,"service":"voice-cr-ws"}`
- `WS  /cr`    → WebSocket de conversación

## Despliegue en Render
- Build: `pip install -r requirements.txt`
- Start: `gunicorn "cr_ws:app" -k uvicorn.workers.UvicornWorker`

## Conexión desde el backend VOZ (Flask)
Setear en el servicio VOZ:

