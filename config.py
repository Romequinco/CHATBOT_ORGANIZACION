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
