"""
Switch de proveedor LLM.

Uso:
    from llm import classify
    resultado = classify(mensaje="...", tareas_abiertas=[...])

Salida garantizada (independiente del proveedor):
    {
        "categoria":           "ruido | tarea_nueva | actualizacion | problema_contexto | completada",
        "titulo":              str | None,   # solo si tarea_nueva
        "prioridad":           "alta | media | baja" | None,  # solo si tarea_nueva
        "tarea_id_relacionada": int | None,  # solo si actualizacion o completada
        "razonamiento":        str           # frase breve para depuración
    }

Selección por variable de entorno LLM_PROVIDER:
    gemini  → Google Gemini 2.5 Flash  (desarrollo)
    claude  → Anthropic claude-haiku   (producción)
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

_FALLBACK: dict = {
    "categoria": "ruido",
    "titulo": None,
    "prioridad": None,
    "tarea_id_relacionada": None,
    "razonamiento": "fallback por error",
}

# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

def _build_prompt(mensaje: str, tareas_abiertas: list) -> str:
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
- tarea_nueva      : el mensaje describe algo pendiente que hay que hacer y que NO existe aún en la lista de tareas
- actualizacion    : el mensaje aporta progreso, cambio o comentario sobre una tarea ya existente
- problema_contexto: información relevante para el equipo (bloqueo, dependencia, aviso) que no es una tarea concreta
- completada       : algo que acaba de terminarse o resolverse

PRINCIPIO RECTOR — ante la duda, usa "ruido".
Es mucho peor registrar basura que perder una tarea: las tareas perdidas se recuperan a mano,
el ruido acumulado ensucia el sistema para siempre. Sé conservador.

REGLAS DE RELLENO:
- titulo y prioridad      → rellena SOLO si categoria == "tarea_nueva"; en el resto pon null
- tarea_id_relacionada    → rellena SOLO si categoria == "actualizacion" o "completada" Y hay una tarea
                            clara en la lista que encaje; si no hay coincidencia obvia, pon null
- razonamiento            → una frase breve explicando la decisión (sirve para depuración, sé directo)

TAREAS ABIERTAS AHORA MISMO:
{tareas_str}

MENSAJE A CLASIFICAR:
"{mensaje}"

Devuelve ÚNICAMENTE este JSON (sin ```, sin texto antes ni después del JSON):
{{
  "categoria": "<ruido|tarea_nueva|actualizacion|problema_contexto|completada>",
  "titulo": "<string si tarea_nueva, null en el resto>",
  "prioridad": "<alta|media|baja si tarea_nueva, null en el resto>",
  "tarea_id_relacionada": <entero si actualizacion/completada con tarea clara, null si no>,
  "razonamiento": "<frase breve>"
}}"""


# ---------------------------------------------------------------------------
# Parser de respuesta
# ---------------------------------------------------------------------------

def _parse_response(text: str) -> dict:
    """Limpia fences markdown, parsea el JSON y normaliza todos los campos."""
    text = text.strip()

    # Quitar fences del tipo ```json ... ``` o ``` ... ```
    if text.startswith("```"):
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1:]
        if text.endswith("```"):
            text = text[:-3].rstrip()

    result = json.loads(text)

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

    # titulo solo es relevante en tarea_nueva
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


# ---------------------------------------------------------------------------
# Reintento con backoff exponencial
# ---------------------------------------------------------------------------

def _classify_with_retry(fn, prompt: str, intentos: int = 3) -> dict:
    """Llama fn(prompt) hasta `intentos` veces con backoff 1s → 2s → 4s entre fallos."""
    ultimo_error: Exception | None = None
    for intento in range(intentos):
        try:
            return fn(prompt)
        except Exception as exc:
            ultimo_error = exc
            if intento < intentos - 1:
                espera = 2 ** intento  # 0→1s, 1→2s
                logger.warning(
                    "Reintento LLM %d/%d tras error: %s. Esperando %ds.",
                    intento + 1, intentos - 1, exc, espera,
                )
                time.sleep(espera)
    raise ultimo_error  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Backend Gemini
# ---------------------------------------------------------------------------

def _classify_gemini(prompt: str) -> dict:
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
    return _parse_response(response.text)


# ---------------------------------------------------------------------------
# Backend Claude (Anthropic)
# ---------------------------------------------------------------------------

def _classify_claude(prompt: str) -> dict:
    import anthropic

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    message = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    return _parse_response(message.content[0].text)


# ---------------------------------------------------------------------------
# Punto de entrada público
# ---------------------------------------------------------------------------

def classify(mensaje: str, tareas_abiertas: list) -> dict:
    """
    Clasifica un mensaje de Telegram en el contexto de las tareas abiertas.
    El caller no necesita saber qué proveedor se usa.
    Nunca lanza excepciones — devuelve _FALLBACK si se agotan todos los reintentos.
    """
    prompt = _build_prompt(mensaje, tareas_abiertas)
    fn = _classify_claude if LLM_PROVIDER == "claude" else _classify_gemini

    try:
        return _classify_with_retry(fn, prompt)
    except Exception as exc:
        logger.error("LLM agotó todos los reintentos (%s): %s", LLM_PROVIDER, exc)
        return _FALLBACK.copy()
