# chatbot-organizacion

Bot de gestión de tareas para grupo de Telegram. Lee todos los mensajes, mantiene un registro en Postgres y envía resúmenes automáticos de apertura (8:30) y cierre (17:00) de lunes a viernes.

## Arquitectura

```
main.py       Arranque, handlers de Telegram, jobs diarios
llm.py        Switch Gemini / Claude — función classify()
db.py         Pool de conexiones, tablas, CRUD
config.py     Variables de entorno
utils.py      es_dia_laborable(), ahora_madrid()
```

## Setup local

### 1. Clonar y crear entorno virtual

```bash
git clone <repo>
cd chatbot-organizacion
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate
```

### 2. Instalar dependencias

```bash
pip install -r requirements.txt
```

### 3. Configurar variables de entorno

```bash
cp .env.example .env
# Editar .env con tus valores reales
```

Variables necesarias:

| Variable | Descripción |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Token de BotFather |
| `DATABASE_URL` | `postgresql://user:pass@host:port/db` |
| `LLM_PROVIDER` | `gemini` (dev) o `claude` (prod) |
| `GEMINI_API_KEY` | Solo si `LLM_PROVIDER=gemini` |
| `ANTHROPIC_API_KEY` | Solo si `LLM_PROVIDER=claude` |

### 4. Levantar Postgres local

```bash
# Con Docker (recomendado para desarrollo)
docker run --name chatbot-pg \
  -e POSTGRES_USER=chatbot \
  -e POSTGRES_PASSWORD=chatbot \
  -e POSTGRES_DB=chatbot_organizacion \
  -p 5432:5432 \
  -d postgres:16

# DATABASE_URL resultante:
# postgresql://chatbot:chatbot@localhost:5432/chatbot_organizacion
```

### 5. Ejecutar

```bash
python main.py
```

El bot imprime el `chat_id` del grupo en cada mensaje recibido — úsalo para configurar los resúmenes automáticos en fases siguientes.

---

## Despliegue en Railway

### 1. Crear proyecto y servicio

1. Nueva cuenta/proyecto en [railway.app](https://railway.app).
2. **New Service → Deploy from GitHub** (conectar el repo).
3. En la pestaña **Settings** del servicio → **Service Type: Worker** (no expone puerto HTTP).

### 2. Provisionar Postgres

1. En el mismo proyecto: **New → Database → Postgres**.
2. Railway inyecta `DATABASE_URL` automáticamente en todos los servicios del proyecto.

### 3. Configurar variables de entorno

En la pestaña **Variables** del servicio worker, añadir:

```
TELEGRAM_BOT_TOKEN=...
LLM_PROVIDER=claude
ANTHROPIC_API_KEY=...
```

`DATABASE_URL` ya viene del plugin de Postgres — no hace falta definirla manualmente.

### 4. Deploy

Cualquier push a `main` dispara un deploy automático. Para el primer deploy manual:

```bash
railway up
```

### 5. Verificar logs

```bash
railway logs
```

Deberías ver `Bot arrancado con long-polling.` y la línea `[DEBUG] chat_id del grupo: ...` al recibir el primer mensaje.

---

## Fases de desarrollo

| Fase | Descripción |
|---|---|
| ✅ 1 | Estructura, persistencia, switch LLM |
| ⬜ 2 | Clasificador LLM + reacción inteligente |
| ⬜ 3 | Comandos /tareas /nueva /hecha /estado |
| ⬜ 4 | Resúmenes automáticos con LLM |
