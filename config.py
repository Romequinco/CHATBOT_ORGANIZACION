import os
from dotenv import load_dotenv

load_dotenv()


def _require(var: str) -> str:
    value = os.environ.get(var)
    if not value:
        raise RuntimeError(f"Variable de entorno requerida no definida: {var}")
    return value


TELEGRAM_BOT_TOKEN: str = _require("TELEGRAM_BOT_TOKEN")
DATABASE_URL: str = _require("DATABASE_URL")

LLM_PROVIDER: str = os.environ.get("LLM_PROVIDER", "gemini").lower()
GEMINI_API_KEY: str = os.environ.get("GEMINI_API_KEY", "")
ANTHROPIC_API_KEY: str = os.environ.get("ANTHROPIC_API_KEY", "")

GEMINI_MODEL = "gemini-2.5-flash"
CLAUDE_MODEL = "claude-haiku-4-5-20251001"

TIMEZONE = "Europe/Madrid"

# Chat ID del grupo donde el bot envía los resúmenes automáticos.
# Telegram devuelve IDs negativos para grupos (ej: -1001234567890).
# Si no está definido, los jobs de apertura/cierre loguean un warning y no fallan.
_group_chat_id_raw = os.environ.get("GROUP_CHAT_ID", "")
GROUP_CHAT_ID: int | None = (
    int(_group_chat_id_raw) if _group_chat_id_raw.lstrip("-").isdigit() else None
)
