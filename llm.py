"""
Switch de proveedor LLM.

Funciones públicas:
  classify(mensaje, tareas_abiertas) -> dict
  interpretar_correccion(mensaje, tareas_en_verificacion) -> dict

LLM_PROVIDER=gemini → Gemini 2.5 Flash (desarrollo)
LLM_PROVIDER=claude → claude-haiku (producción)
"""

import json
import logging
import time

from config import (
    ANTHROPIC_API_KEY,
    CLAUDE_MODEL,
    GEMINI_API_KEY,
    GEMINI_MODEL,
    LLM_PROVIDER,
)

logger = logging.getLogger(__name__)

_VALID_CATEGORIAS = {"ruido", "tarea_nueva", "actualizacion", "problema_contexto", "completada"}
_VALID_PRIORIDADES = {"alta", "media", "baja", None}
_VALID_ESTADOS_CORR = {"pendiente", "en_curso", "bloqueada", "hecha"}

_FALLBACK: dict = {
    "categoria": "ruido",
    "titulo": None,
    "prioridad": None,
    "tarea_id_relacionada": None,
    "razonamiento": "fallback por error",
}

_FALLBACK_CORRECCION: dict = {
    "correcciones": [],
    "interpretacion": "No se detectaron cambios para aplicar",
    "hay_cambios": False,
}

# ---------------------------------------------------------------------------
# Reintento con backoff exponencial (compartido por ambas funciones públicas)
# ---------------------------------------------------------------------------

def _call_with_retry(fn, prompt: str, intentos: int = 3) -> dict:
    """Llama fn(prompt) hasta `intentos` veces con backoff 1s → 2s entre fallos."""
    ultimo_error: Exception | None = None
    for intento in range(intentos):
        try:
            return fn(prompt)
        except Exception as exc:
            ultimo_error = exc
            if intento < intentos - 1:
                espera = 2 ** intento
                logger.warning(
                    "Reintento LLM %d/%d tras error: %s. Esperando %ds.",
                    intento + 1, intentos - 1, exc, espera,
                )
                time.sleep(espera)
    raise ultimo_error  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Llamadas raw a las APIs (devuelven texto sin parsear)
# ---------------------------------------------------------------------------

def _call_gemini_raw(prompt: str) -> str:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=GEMINI_API_KEY)
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.0,
        ),
    )
    return response.text


def _call_claude_raw(prompt: str) -> str:
    import anthropic

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    message = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text


# ---------------------------------------------------------------------------
# Utilidad: limpia fences markdown antes de parsear JSON
# ---------------------------------------------------------------------------

def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1:]
        if text.endswith("```"):
            text = text[:-3].rstrip()
    return text


# ---------------------------------------------------------------------------
# CLASIFICACIÓN (classify)
# ---------------------------------------------------------------------------

def _build_prompt_classify(mensaje: str, tareas_abiertas: list) -> str:
    if tareas_abiertas:
        tareas_str = "\n".join(
            f'  - [ID {t["id"]}] "{t["titulo"]}" (estado: {t["estado"]})'
            for t in tareas_abiertas
        )
    else:
        tareas_str = "  (no hay tareas abiertas en este momento)"

    return f"""Eres el clasificador de un bot de gestión de tareas para un equipo de trabajo español.
Tu única función es categorizar mensajes de Telegram del grupo de trabajo.

CATEGORÍAS — elige UNA:
- ruido            : bromas, saludos, conversación casual, emojis sueltos, off-topic, cualquier cosa no laboral
- tarea_nueva      : el mensaje describe algo pendiente que hay que hacer y que NO existe aún en la lista
- actualizacion    : el mensaje aporta progreso, cambio o comentario sobre una tarea ya existente
- problema_contexto: información relevante para el equipo (bloqueo, dependencia, aviso) que no es una tarea
- completada       : algo que acaba de terminarse o resolverse

PRINCIPIO RECTOR — ante la duda, usa "ruido".
Es peor registrar basura que perder una tarea: las tareas se recuperan a mano, el ruido ensucia para siempre.

REGLAS:
- titulo y prioridad      → SOLO si categoria == "tarea_nueva"; en el resto, null
- tarea_id_relacionada    → SOLO si categoria == "actualizacion" o "completada" Y hay coincidencia clara
- razonamiento            → frase breve para depuración

TAREAS ABIERTAS:
{tareas_str}

MENSAJE:
"{mensaje}"

Devuelve ÚNICAMENTE este JSON (sin ```, sin texto fuera del JSON):
{{
  "categoria": "<ruido|tarea_nueva|actualizacion|problema_contexto|completada>",
  "titulo": "<string si tarea_nueva, null si no>",
  "prioridad": "<alta|media|baja si tarea_nueva, null si no>",
  "tarea_id_relacionada": <entero o null>,
  "razonamiento": "<frase breve>"
}}"""


def _parse_classify(text: str) -> dict:
    result = json.loads(_strip_fences(text))

    categoria = result.get("categoria", "ruido")
    if categoria not in _VALID_CATEGORIAS:
        categoria = "ruido"

    prioridad = result.get("prioridad") or None
    if prioridad not in _VALID_PRIORIDADES:
        prioridad = None

    tarea_id = result.get("tarea_id_relacionada")
    if tarea_id is not None:
        try:
            tarea_id = int(tarea_id)
        except (TypeError, ValueError):
            tarea_id = None

    titulo = result.get("titulo") or None
    if categoria != "tarea_nueva":
        titulo = None
        prioridad = None

    return {
        "categoria": categoria,
        "titulo": titulo,
        "prioridad": prioridad,
        "tarea_id_relacionada": tarea_id,
        "razonamiento": str(result.get("razonamiento", "")),
    }


def _classify_gemini(prompt: str) -> dict:
    return _parse_classify(_call_gemini_raw(prompt))


def _classify_claude(prompt: str) -> dict:
    return _parse_classify(_call_claude_raw(prompt))


def classify(mensaje: str, tareas_abiertas: list) -> dict:
    """Clasifica un mensaje. Nunca lanza — devuelve _FALLBACK si se agotan los reintentos."""
    prompt = _build_prompt_classify(mensaje, tareas_abiertas)
    fn = _classify_claude if LLM_PROVIDER == "claude" else _classify_gemini
    try:
        return _call_with_retry(fn, prompt)
    except Exception as exc:
        logger.error("LLM agotó reintentos en classify (%s): %s", LLM_PROVIDER, exc)
        return _FALLBACK.copy()


# ---------------------------------------------------------------------------
# CORRECCIÓN (interpretar_correccion)
# ---------------------------------------------------------------------------

def _build_prompt_correccion(mensaje: str, tareas: list) -> str:
    if tareas:
        tareas_str = "\n".join(
            f'  - [ID {t["id"]}] "{t["titulo"]}" (estado: {t["estado"]})'
            for t in tareas
        )
    else:
        tareas_str = "  (no hay tareas relevantes)"

    return f"""Eres el intérprete de correcciones de un bot de gestión de tareas.
El equipo está en la ventana de verificación del cierre del día.
Tu función es detectar si el mensaje describe cambios de estado para tareas específicas.

ESTADOS VÁLIDOS: pendiente, en_curso, bloqueada, hecha

TAREAS RELEVANTES:
{tareas_str}

MENSAJE:
"{mensaje}"

Si el mensaje describe cambios de estado para alguna tarea: extrae los cambios.
Si NO parece una corrección (ruido, charla, etc.): hay_cambios = false.
Sé conservador: solo extrae si estás razonablemente seguro de la intención.

Devuelve ÚNICAMENTE este JSON (sin ```, sin texto fuera del JSON):
{{
  "correcciones": [
    {{"tarea_id": <entero>, "nuevo_estado": "<pendiente|en_curso|bloqueada|hecha>"}},
    ...
  ],
  "interpretacion": "<descripción legible de los cambios a aplicar>",
  "hay_cambios": <true|false>
}}

Si hay_cambios es false: correcciones = [] e interpretacion puede estar vacía."""


def _parse_correccion(text: str) -> dict:
    result = json.loads(_strip_fences(text))

    correcciones = []
    for c in result.get("correcciones", []):
        tid = c.get("tarea_id")
        estado = c.get("nuevo_estado", "")
        if tid is not None and estado in _VALID_ESTADOS_CORR:
            try:
                correcciones.append({"tarea_id": int(tid), "nuevo_estado": estado})
            except (TypeError, ValueError):
                pass

    hay_cambios = bool(result.get("hay_cambios", False)) and bool(correcciones)
    return {
        "correcciones": correcciones,
        "interpretacion": str(result.get("interpretacion", "")),
        "hay_cambios": hay_cambios,
    }


def _correccion_gemini(prompt: str) -> dict:
    return _parse_correccion(_call_gemini_raw(prompt))


def _correccion_claude(prompt: str) -> dict:
    return _parse_correccion(_call_claude_raw(prompt))


def interpretar_correccion(mensaje: str, tareas_en_verificacion: list) -> dict:
    """Interpreta un mensaje como corrección de estado. Nunca lanza — devuelve _FALLBACK_CORRECCION."""
    prompt = _build_prompt_correccion(mensaje, tareas_en_verificacion)
    fn = _correccion_claude if LLM_PROVIDER == "claude" else _correccion_gemini
    try:
        return _call_with_retry(fn, prompt)
    except Exception as exc:
        logger.error("LLM agotó reintentos en interpretar_correccion (%s): %s", LLM_PROVIDER, exc)
        return _FALLBACK_CORRECCION.copy()
