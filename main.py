"""
Bot de gestión de tareas para grupo de Telegram.

Fase actual: captura y persiste todos los mensajes. Clasificación pendiente.
"""

import logging
from datetime import time
from zoneinfo import ZoneInfo

from telegram import Update
from telegram import ReactionTypeEmoji
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import db
import llm
from config import GROUP_CHAT_ID, TELEGRAM_BOT_TOKEN, TIMEZONE
from utils import es_dia_laborable

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TZ = ZoneInfo(TIMEZONE)

_ESTADOS_VALIDOS = {"pendiente", "en_curso", "bloqueada", "hecha"}
_ICONOS_PRIORIDAD = {"alta": "🔴", "media": "🟡", "baja": "🟢"}
_ICONOS_ESTADO = {"pendiente": "⏳", "en_curso": "🔄", "bloqueada": "🚫", "hecha": "✅"}


def _formatear_lista_tareas(tareas: list) -> str:
    """Una línea por tarea. La BD ya las devuelve ordenadas por prioridad (alta→media→baja)."""
    lineas = []
    for t in tareas:
        ip = _ICONOS_PRIORIDAD.get(t["prioridad"], "")
        ie = _ICONOS_ESTADO.get(t["estado"], "")
        lineas.append(f"{ip} [{t['id']}] {t['titulo']} — {t['estado']} {ie}")
    return "\n".join(lineas)


# ---------------------------------------------------------------------------
# Comando /start
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Bot de tareas activo.\n"
        "Estoy escuchando todos los mensajes del grupo y los registro en la base de datos."
    )


# ---------------------------------------------------------------------------
# Handler principal: todos los mensajes del grupo
# ---------------------------------------------------------------------------

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None or msg.text is None:
        return

    chat = update.effective_chat
    user = update.effective_user

    autor = user.username or user.full_name or str(user.id)
    logger.info("Mensaje recibido | chat_id=%s | autor=%s | texto=%r", chat.id, autor, msg.text[:80])

    # Imprime el chat_id en logs (útil para configurar los resúmenes automáticos)
    print(f"[DEBUG] chat_id del grupo: {chat.id}")

    # Clasificar con LLM antes de persistir
    tareas_abiertas = db.listar_tareas_abiertas()
    clasificacion = llm.classify(msg.text, tareas_abiertas)
    logger.info(
        "Clasificado como '%s' | %s",
        clasificacion["categoria"],
        clasificacion["razonamiento"],
    )

    # Persistir en log de auditoría con clasificación incluida
    db.insertar_mensaje_log(
        telegram_message_id=msg.message_id,
        chat_id=chat.id,
        autor=autor,
        texto=msg.text,
        clasificacion=clasificacion,
    )

    # Ejecutar acción según categoría
    categoria = clasificacion["categoria"]
    reaccionar = False

    if categoria == "tarea_nueva":
        try:
            tarea_id = db.crear_tarea(
                titulo=clasificacion["titulo"] or "Sin título",
                autor=autor,
                prioridad=clasificacion["prioridad"] or "media",
            )
            logger.info(
                "Tarea %d creada: '%s' (prioridad: %s)",
                tarea_id, clasificacion["titulo"], clasificacion["prioridad"] or "media",
            )
            reaccionar = True
        except Exception as exc:
            logger.error("Error al crear tarea: %s", exc)

    elif categoria == "completada":
        tid = clasificacion.get("tarea_id_relacionada")
        if tid is not None and db.obtener_tarea(tid) is not None:
            try:
                db.actualizar_estado_tarea(tid, "hecha")
                logger.info("Tarea %d marcada como hecha", tid)
                reaccionar = True
            except Exception as exc:
                logger.error("Error al marcar tarea %d como hecha: %s", tid, exc)
        else:
            logger.info("completada sin tarea enlazada válida, ignorada")

    elif categoria == "actualizacion":
        tid = clasificacion.get("tarea_id_relacionada")
        logger.info("actualizacion sobre tarea %s (no se aplica cambio en esta fase)", tid)
        reaccionar = True

    elif categoria == "problema_contexto":
        logger.info("problema_contexto registrado: %s", clasificacion["razonamiento"])
        reaccionar = True

    # ruido → no hace nada, no reacciona

    if reaccionar:
        try:
            await context.bot.set_message_reaction(
                chat_id=chat.id,
                message_id=msg.message_id,
                reaction=[ReactionTypeEmoji(emoji="👍")],
            )
        except Exception as exc:
            logger.warning("No se pudo reaccionar al mensaje: %s", exc)

# ---------------------------------------------------------------------------
# Jobs programados
# ---------------------------------------------------------------------------

async def job_resumen_apertura(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Resumen de apertura — 8:30 lunes a viernes."""
    if not es_dia_laborable():
        return
    if GROUP_CHAT_ID is None:
        logger.warning("[JOB apertura] GROUP_CHAT_ID no configurado, resumen no enviado.")
        return
    try:
        tareas = db.listar_tareas_abiertas()
        if tareas:
            texto = "☀️ Buenos días. Tareas abiertas hoy:\n\n" + _formatear_lista_tareas(tareas)
        else:
            texto = "☀️ Buenos días. No hay tareas abiertas ahora mismo."
        await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=texto)
        logger.info("[JOB apertura] Resumen enviado (%d tareas).", len(tareas))
    except Exception as exc:
        logger.error("[JOB apertura] Error al enviar resumen: %s", exc)


async def job_resumen_cierre(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Resumen de cierre — 17:00 lunes a viernes."""
    if not es_dia_laborable():
        return
    if GROUP_CHAT_ID is None:
        logger.warning("[JOB cierre] GROUP_CHAT_ID no configurado, resumen no enviado.")
        return
    try:
        tareas = db.listar_tareas_abiertas()
        if tareas:
            cuerpo = _formatear_lista_tareas(tareas)
            texto = (
                "🌙 Cierre del día. Tareas que siguen abiertas:\n\n"
                f"{cuerpo}\n\n"
                "Revisad que esté todo correcto. Mañana más."
            )
        else:
            texto = "🌙 Cierre del día. Todas las tareas están cerradas. ¡Buen trabajo!"
        await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=texto)
        logger.info("[JOB cierre] Resumen enviado (%d tareas pendientes).", len(tareas))
    except Exception as exc:
        logger.error("[JOB cierre] Error al enviar resumen: %s", exc)


# ---------------------------------------------------------------------------
# Comandos
# ---------------------------------------------------------------------------

async def cmd_tareas(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Lista las tareas abiertas ordenadas por prioridad."""
    try:
        tareas = db.listar_tareas_abiertas()
        if not tareas:
            await update.message.reply_text("No hay tareas abiertas.")
            return
        texto = "📋 Tareas abiertas:\n\n" + _formatear_lista_tareas(tareas)
        await update.message.reply_text(texto)
    except Exception as exc:
        logger.error("Error en /tareas: %s", exc)
        await update.message.reply_text("Error al obtener las tareas.")


async def cmd_nueva(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Crea una tarea nueva: /nueva <título>"""
    titulo = " ".join(context.args) if context.args else ""
    if not titulo:
        await update.message.reply_text("Uso: /nueva <título de la tarea>")
        return
    try:
        autor = update.effective_user.username or update.effective_user.full_name or "desconocido"
        tarea_id = db.crear_tarea(titulo=titulo, autor=autor, prioridad="media")
        await update.message.reply_text(f"✅ Tarea #{tarea_id} creada: {titulo}")
        logger.info("Tarea %d creada via /nueva por %s: '%s'", tarea_id, autor, titulo)
    except Exception as exc:
        logger.error("Error en /nueva: %s", exc)
        await update.message.reply_text("Error al crear la tarea.")


async def cmd_hecha(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Marca una tarea como hecha: /hecha <id>"""
    raw = context.args[0] if context.args else ""
    if not raw.isdigit():
        await update.message.reply_text("Uso: /hecha <id>  (el id es un número entero)")
        return
    tarea_id = int(raw)
    try:
        tarea = db.obtener_tarea(tarea_id)
        if tarea is None:
            await update.message.reply_text(f"No existe la tarea #{tarea_id}.")
            return
        db.actualizar_estado_tarea(tarea_id, "hecha")
        await update.message.reply_text(f"✅ Tarea #{tarea_id} marcada como hecha: {tarea['titulo']}")
        logger.info("Tarea %d marcada como hecha via /hecha", tarea_id)
    except Exception as exc:
        logger.error("Error en /hecha: %s", exc)
        await update.message.reply_text("Error al actualizar la tarea.")


async def cmd_estado(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cambia el estado de una tarea: /estado <id> <nuevo_estado>"""
    estados_str = ", ".join(sorted(_ESTADOS_VALIDOS))
    if len(context.args) < 2 or not context.args[0].isdigit():
        await update.message.reply_text(
            f"Uso: /estado <id> <estado>\nEstados válidos: {estados_str}"
        )
        return
    tarea_id = int(context.args[0])
    nuevo_estado = context.args[1].lower()
    if nuevo_estado not in _ESTADOS_VALIDOS:
        await update.message.reply_text(
            f"Estado '{nuevo_estado}' no válido.\nEstados válidos: {estados_str}"
        )
        return
    try:
        tarea = db.obtener_tarea(tarea_id)
        if tarea is None:
            await update.message.reply_text(f"No existe la tarea #{tarea_id}.")
            return
        db.actualizar_estado_tarea(tarea_id, nuevo_estado)
        await update.message.reply_text(f"✅ Tarea #{tarea_id} actualizada a '{nuevo_estado}'.")
        logger.info("Tarea %d → '%s' via /estado", tarea_id, nuevo_estado)
    except Exception as exc:
        logger.error("Error en /estado: %s", exc)
        await update.message.reply_text("Error al actualizar la tarea.")


# ---------------------------------------------------------------------------
# Arranque
# ---------------------------------------------------------------------------

def main() -> None:
    db.init_db()

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Comandos
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("tareas", cmd_tareas))
    app.add_handler(CommandHandler("nueva", cmd_nueva))
    app.add_handler(CommandHandler("hecha", cmd_hecha))
    app.add_handler(CommandHandler("estado", cmd_estado))

    # Mensajes normales (no comandos)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Jobs diarios (lunes=0 … viernes=4)
    jq = app.job_queue
    jq.run_daily(
        job_resumen_apertura,
        time=time(8, 30, tzinfo=TZ),
        days=(0, 1, 2, 3, 4),
        name="apertura",
    )
    jq.run_daily(
        job_resumen_cierre,
        time=time(17, 0, tzinfo=TZ),
        days=(0, 1, 2, 3, 4),
        name="cierre",
    )

    logger.info("Bot arrancado con long-polling.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
