# chatbot-organizacion

Bot de gestión de tareas para grupo de Telegram. Escucha todos los mensajes del grupo, clasifica automáticamente tareas y actualizaciones con IA, mantiene un registro en Postgres y envía resúmenes automáticos de apertura y cierre de lunes a viernes.

## Qué hace

- **Clasificación automática** de mensajes de texto y notas de voz: detecta tareas nuevas, actualizaciones, problemas de contexto y mensajes de tarea completada. Reacciona con 👍 si registra algo; sin reacción si es ruido.
- **Soporte de audio**: transcribe notas de voz (Gemini) y las clasifica (Claude), siguiendo el mismo pipeline que el texto. Audios mayores de 5 MB o inaudibles reciben 👀 y un aviso para usar `/nueva`.
- **Fotos y vídeos**: solo reacción 👀, sin clasificación.
- **Resúmenes automáticos**: apertura a las 8:30 (lista de tareas abiertas) y cierre a las 17:00 (repaso + verificación), de lunes a viernes.
- **Flujo de verificación de cierre**: si hay tareas marcadas como "por verificar", el bot las lista con botones inline para confirmar si están hechas o siguen abiertas. El modo expira a medianoche si no se resuelve.

## Arquitectura

```
main.py       Handlers de Telegram, jobs diarios, arranque
llm.py        classify(), interpretar_correccion(), transcribe_audio()
db.py         Pool psycopg3, init de tablas, CRUD
config.py     Variables de entorno
utils.py      es_dia_laborable(), ahora_madrid()
```

### Proveedores LLM

| Función | Proveedor | Cómo cambiar |
|---|---|---|
| Clasificación de texto | Claude (Anthropic) | Variable `LLM_PROVIDER` |
| Transcripción de audio | Gemini (Google) | Función `transcribe_audio()` en `llm.py` |

> **Deuda técnica — privacidad/RGPD**: hay dos puntos de proceso LLM independientes. Cambiar `LLM_PROVIDER=claude` solo migra la clasificación de texto; la transcripción de audio sigue pasando por Google hasta que se migre `transcribe_audio()`. Ver comentarios en `llm.py` y `main.py`.

## Comandos

| Comando | Descripción |
|---|---|
| `/start` | Mensaje de bienvenida con descripción del bot. Solo responde desde `GROUP_CHAT_ID`. |
| `/tareas` | Lista las tareas abiertas. |
| `/nueva <título>` | Crea una tarea manualmente. |
| `/hecha <id>` | Marca una tarea como hecha. |
| `/estado <id> <estado>` | Cambia el estado (`pendiente`, `en_curso`, `bloqueada`, `hecha`). |
| `/forzar_cierre` | Dispara el resumen de cierre bajo demanda (pruebas/piloto). Solo desde `GROUP_CHAT_ID`. Bloqueado si ya hay un cierre abierto. |

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

| Variable | Requerida | Descripción |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Sí | Token de BotFather |
| `DATABASE_URL` | Sí | `postgresql://user:pass@host:port/db` |
| `GROUP_CHAT_ID` | Sí | ID del grupo Telegram (número negativo, ej. `-1001234567890`) |
| `ANTHROPIC_API_KEY` | Sí | Clave de Anthropic — clasificación de texto en producción |
| `LLM_PROVIDER` | Sí | `claude` (producción) o `gemini` (desarrollo) |
| `GEMINI_API_KEY` | Sí | Clave de Google AI Studio — transcripción de audio siempre; clasificación si `LLM_PROVIDER=gemini` |

> Para obtener `GROUP_CHAT_ID`: arranca el bot, envía un mensaje al grupo y busca en los logs la línea `Mensaje recibido | chat_id=...`. Ese es el ID.

### 4. Levantar Postgres local

```bash
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

En la pestaña **Variables** del servicio worker:

```
TELEGRAM_BOT_TOKEN=...
GROUP_CHAT_ID=...
LLM_PROVIDER=claude
ANTHROPIC_API_KEY=...
GEMINI_API_KEY=...
```

`DATABASE_URL` viene del plugin de Postgres — no hace falta definirla manualmente.

### 4. Deploy

Cualquier push a `main` dispara un deploy automático. Para el primer deploy manual:

```bash
railway up
```

### 5. Verificar logs

```bash
railway logs
```

Líneas clave al arrancar:

```
Bot arrancado con long-polling.
```

Al recibir el primer mensaje de texto:

```
Mensaje recibido | chat_id=... | autor=... | texto=...
Clasificado como 'X' | origen=texto | ...
```

Al recibir una nota de voz:

```
Audio recibido | chat_id=... | autor=... | file_id=...
Transcripción Gemini (audio/ogg): '...'
Clasificado como 'X' | origen=audio | ...
```
