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
from config import TELEGRAM_BOT_TOKEN, TIMEZONE
from utils import es_dia_laborable

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TZ = ZoneInfo(TIMEZONE)


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
# Jobs programados (stubs — implementar en fase siguiente)
# ---------------------------------------------------------------------------

async def job_resumen_apertura(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Resumen de apertura — 8:30 lunes a viernes."""
    if not es_dia_laborable():
        return
    # TODO: obtener tareas abiertas, construir resumen con LLM, enviar al grupo
    logger.info("[JOB] Resumen de apertura (pendiente de implementar)")


async def job_resumen_cierre(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Resumen de cierre — 17:00 lunes a viernes."""
    if not es_dia_laborable():
        return
    # TODO: obtener tareas del día, construir resumen con LLM, enviar al grupo
    logger.info("[JOB] Resumen de cierre (pendiente de implementar)")


# ---------------------------------------------------------------------------
# Comandos (stubs — implementar en fase siguiente)
# ---------------------------------------------------------------------------

async def cmd_tareas(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Lista las tareas abiertas."""
    # TODO: db.listar_tareas_abiertas() → formatear y responder
    await update.message.reply_text("(comando /tareas pendiente de implementar)")


async def cmd_nueva(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Crea una tarea nueva: /nueva <título>"""
    # TODO: parsear args, db.crear_tarea(), confirmar
    await update.message.reply_text("(comando /nueva pendiente de implementar)")


async def cmd_hecha(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Marca una tarea como hecha: /hecha <id>"""
    # TODO: parsear id, db.actualizar_estado_tarea(id, 'hecha'), confirmar
    await update.message.reply_text("(comando /hecha pendiente de implementar)")


async def cmd_estado(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cambia el estado de una tarea: /estado <id> <nuevo_estado>"""
    # TODO: parsear args, validar estado, db.actualizar_estado_tarea(), confirmar
    await update.message.reply_text("(comando /estado pendiente de implementar)")


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
