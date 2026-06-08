"""
Bot de gestión de tareas para grupo de Telegram.
"""

import logging
from datetime import time
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReactionTypeEmoji, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
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

# por_verificar es estado gestionado por el bot; no lo exponemos en /estado
_ESTADOS_VALIDOS = {"pendiente", "en_curso", "bloqueada", "hecha"}
_ICONOS_PRIORIDAD = {"alta": "🔴", "media": "🟡", "baja": "🟢"}
_ICONOS_ESTADO = {
    "pendiente": "⏳", "en_curso": "🔄", "bloqueada": "🚫",
    "hecha": "✅", "por_verificar": "🔍",
}


def _formatear_lista_tareas(tareas: list) -> str:
    """Una línea por tarea. La BD ya las devuelve ordenadas por prioridad."""
    lineas = []
    for t in tareas:
        ip = _ICONOS_PRIORIDAD.get(t["prioridad"], "")
        ie = _ICONOS_ESTADO.get(t["estado"], "")
        lineas.append(f"{ip} [{t['id']}] {t['titulo']} — {t['estado']} {ie}")
    return "\n".join(lineas)


def _teclado_verificacion(tareas: list) -> InlineKeyboardMarkup:
    """Par de botones por tarea + 'Confirmar todo'."""
    botones = []
    for t in tareas:
        botones.append([
            InlineKeyboardButton(f"✅ Hecha [{t['id']}]", callback_data=f"verif:hecha:{t['id']}"),
            InlineKeyboardButton(f"↩️ Sigue abierta [{t['id']}]", callback_data=f"verif:abierta:{t['id']}"),
        ])
    botones.append([InlineKeyboardButton("✅ Confirmar todo", callback_data="verif:confirmar_todo")])
    return InlineKeyboardMarkup(botones)


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Bot de tareas activo.\n"
        "Estoy escuchando todos los mensajes del grupo y los registro en la base de datos."
    )


# ---------------------------------------------------------------------------
# Handler principal
# ---------------------------------------------------------------------------

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None or msg.text is None:
        return

    chat = update.effective_chat
    user = update.effective_user
    autor = user.username or user.full_name or str(user.id)

    logger.info("Mensaje recibido | chat_id=%s | autor=%s | texto=%r", chat.id, autor, msg.text[:80])
    print(f"[DEBUG] chat_id del grupo: {chat.id}")

    # --- MODO VERIFICACIÓN: capa adicional antes del flujo normal ---
    try:
        modo = db.obtener_modo_verificacion()
    except Exception as exc:
        logger.error("Error al consultar modo verificación: %s", exc)
        modo = None

    if modo is not None:
        try:
            tareas_pv = db.listar_tareas_por_verificar()
            tareas_ab = db.listar_tareas_abiertas()
            propuesta = llm.interpretar_correccion(msg.text, tareas_pv + tareas_ab)
        except Exception as exc:
            logger.error("Error en interpretar_correccion: %s", exc)
            propuesta = {"correcciones": [], "interpretacion": "", "hay_cambios": False}

        if propuesta.get("hay_cambios"):
            try:
                datos = modo["datos"]
                datos["propuesta_pendiente"] = {
                    "correcciones": propuesta["correcciones"],
                    "interpretacion": propuesta["interpretacion"],
                }
                db.actualizar_datos_verificacion(datos)
                teclado = InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Sí, aplicar", callback_data="corr:si"),
                    InlineKeyboardButton("❌ No", callback_data="corr:no"),
                ]])
                await msg.reply_text(
                    f"Voy a aplicar: {propuesta['interpretacion']}",
                    reply_markup=teclado,
                )
                db.insertar_mensaje_log(
                    telegram_message_id=msg.message_id,
                    chat_id=chat.id,
                    autor=autor,
                    texto=msg.text,
                    clasificacion={"modo": "verificacion_correccion", "propuesta": propuesta},
                )
            except Exception as exc:
                logger.error("Error al procesar corrección en modo verificación: %s", exc)
            return  # No continuar al flujo normal

        logger.info("Modo verificación activo pero mensaje no es corrección; procesando normalmente.")

    # --- FLUJO NORMAL ---
    tareas_abiertas = db.listar_tareas_abiertas()
    clasificacion = llm.classify(msg.text, tareas_abiertas)
    logger.info("Clasificado como '%s' | %s", clasificacion["categoria"], clasificacion["razonamiento"])

    db.insertar_mensaje_log(
        telegram_message_id=msg.message_id,
        chat_id=chat.id,
        autor=autor,
        texto=msg.text,
        clasificacion=clasificacion,
    )

    categoria = clasificacion["categoria"]
    reaccionar = False

    if categoria == "tarea_nueva":
        try:
            tarea_id = db.crear_tarea(
                titulo=clasificacion["titulo"] or "Sin título",
                autor=autor,
                prioridad=clasificacion["prioridad"] or "media",
            )
            logger.info("Tarea %d creada: '%s' (prioridad: %s)",
                        tarea_id, clasificacion["titulo"], clasificacion["prioridad"] or "media")
            reaccionar = True
        except Exception as exc:
            logger.error("Error al crear tarea: %s", exc)

    elif categoria == "completada":
        tid = clasificacion.get("tarea_id_relacionada")
        if tid is not None and db.obtener_tarea(tid) is not None:
            try:
                db.actualizar_estado_tarea(tid, "por_verificar")
                logger.info("Tarea %d marcada como por_verificar (pendiente de confirmación en el cierre)", tid)
                reaccionar = True
            except Exception as exc:
                logger.error("Error al marcar tarea %d como por_verificar: %s", tid, exc)
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
# Callbacks: verificación (verif:*) y correcciones (corr:*)
# ---------------------------------------------------------------------------

async def handle_verif_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Botones del resumen de cierre: hecha, sigue abierta, confirmar todo."""
    query = update.callback_query
    await query.answer()
    data = query.data

    try:
        if data.startswith("verif:hecha:"):
            tarea_id = int(data.split(":")[2])
            db.actualizar_estado_tarea(tarea_id, "hecha")
            logger.info("[VERIF] Tarea %d → hecha via botón", tarea_id)
            restantes = db.listar_tareas_por_verificar()
            if restantes:
                await query.edit_message_reply_markup(reply_markup=_teclado_verificacion(restantes))
            else:
                db.desactivar_modo_verificacion()
                await query.edit_message_reply_markup(reply_markup=None)
                if GROUP_CHAT_ID:
                    await context.bot.send_message(GROUP_CHAT_ID, "✅ Todas las tareas verificadas. ¡Buen trabajo!")

        elif data.startswith("verif:abierta:"):
            tarea_id = int(data.split(":")[2])
            db.actualizar_estado_tarea(tarea_id, "pendiente")
            logger.info("[VERIF] Tarea %d → pendiente via botón", tarea_id)
            restantes = db.listar_tareas_por_verificar()
            if restantes:
                await query.edit_message_reply_markup(reply_markup=_teclado_verificacion(restantes))
            else:
                db.desactivar_modo_verificacion()
                await query.edit_message_reply_markup(reply_markup=None)

        elif data == "verif:confirmar_todo":
            tareas_pv = db.listar_tareas_por_verificar()
            n = len(tareas_pv)
            for t in tareas_pv:
                db.actualizar_estado_tarea(t["id"], "hecha")
            db.desactivar_modo_verificacion()
            await query.edit_message_reply_markup(reply_markup=None)
            if GROUP_CHAT_ID:
                await context.bot.send_message(
                    GROUP_CHAT_ID,
                    f"✅ Verificación completa. {n} tarea(s) marcada(s) como hechas.",
                )
            logger.info("[VERIF] Confirmar todo: %d tarea(s) cerrada(s).", n)

    except Exception as exc:
        logger.error("Error en handle_verif_callback (%s): %s", data, exc)


async def handle_corr_callback(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Botones de confirmación de correcciones textuales (corr:si / corr:no)."""
    query = update.callback_query
    await query.answer()
    data = query.data

    try:
        modo = db.obtener_modo_verificacion()
        if modo is None:
            await query.edit_message_text("⚠️ El modo verificación ya no está activo.")
            return

        datos = modo["datos"]
        propuesta = datos.get("propuesta_pendiente")
        if not propuesta:
            await query.edit_message_text("⚠️ No hay corrección pendiente de confirmar.")
            return

        if data == "corr:si":
            for c in propuesta.get("correcciones", []):
                db.actualizar_estado_tarea(c["tarea_id"], c["nuevo_estado"])
            logger.info("[VERIF] Corrección aplicada: %s", propuesta["interpretacion"])
            datos["propuesta_pendiente"] = None
            db.actualizar_datos_verificacion(datos)
            await query.edit_message_text(f"✅ Aplicado: {propuesta['interpretacion']}")

        elif data == "corr:no":
            datos["propuesta_pendiente"] = None
            db.actualizar_datos_verificacion(datos)
            await query.edit_message_text("❌ Corrección descartada.")

    except Exception as exc:
        logger.error("Error en handle_corr_callback (%s): %s", data, exc)


# ---------------------------------------------------------------------------
# Jobs programados
# ---------------------------------------------------------------------------

async def job_resumen_apertura(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Resumen de apertura — 8:30 lunes a viernes."""
    if not es_dia_laborable():
        return
    if GROUP_CHAT_ID is None:
        logger.warning("[JOB apertura] GROUP_CHAT_ID no configurado.")
        return
    try:
        tareas = db.listar_tareas_abiertas()
        texto = (
            "☀️ Buenos días. Tareas abiertas hoy:\n\n" + _formatear_lista_tareas(tareas)
            if tareas else
            "☀️ Buenos días. No hay tareas abiertas ahora mismo."
        )
        await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=texto)
        logger.info("[JOB apertura] Resumen enviado (%d tareas).", len(tareas))
    except Exception as exc:
        logger.error("[JOB apertura] Error: %s", exc)


async def job_resumen_cierre(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Resumen de cierre — 17:00. Activa modo verificación si hay tareas por_verificar."""
    if not es_dia_laborable():
        return
    if GROUP_CHAT_ID is None:
        logger.warning("[JOB cierre] GROUP_CHAT_ID no configurado.")
        return
    try:
        tareas_ab = db.listar_tareas_abiertas()
        tareas_pv = db.listar_tareas_por_verificar()

        partes = ["🌙 Cierre del día."]
        if tareas_ab:
            partes.append("\n📋 Siguen abiertas:\n" + _formatear_lista_tareas(tareas_ab))
        else:
            partes.append("\nNo hay tareas activamente abiertas.")
        if tareas_pv:
            partes.append("\n🔍 Pendientes de verificar:\n" + _formatear_lista_tareas(tareas_pv))

        texto = "\n".join(partes)

        if tareas_pv:
            sent = await context.bot.send_message(
                chat_id=GROUP_CHAT_ID, text=texto, reply_markup=_teclado_verificacion(tareas_pv)
            )
            db.activar_modo_verificacion({
                "tareas_ids": [t["id"] for t in tareas_pv],
                "resumen_message_id": sent.message_id,
                "propuesta_pendiente": None,
            })
            logger.info("[JOB cierre] Modo verificación activado con %d tarea(s).", len(tareas_pv))
        else:
            await context.bot.send_message(
                chat_id=GROUP_CHAT_ID, text=texto + "\n\nTodas las tareas están en orden. Mañana más."
            )
            logger.info("[JOB cierre] Resumen enviado sin modo verificación.")

    except Exception as exc:
        logger.error("[JOB cierre] Error: %s", exc)


async def job_expiracion_verificacion(context: ContextTypes.DEFAULT_TYPE) -> None:
    """00:00 todos los días — revierte por_verificar a pendiente si el modo sigue activo."""
    try:
        if db.obtener_modo_verificacion() is None:
            return
        n = db.revertir_tareas_por_verificar()
        db.desactivar_modo_verificacion()
        logger.info("[JOB expiración] %d tarea(s) devuelta(s) a pendiente.", n)
        if GROUP_CHAT_ID:
            await context.bot.send_message(
                GROUP_CHAT_ID,
                f"⏰ Ventana de verificación cerrada. "
                f"{n} tarea(s) no confirmada(s) vuelve(n) a pendiente.",
            )
    except Exception as exc:
        logger.error("[JOB expiración] Error: %s", exc)


# ---------------------------------------------------------------------------
# Comandos
# ---------------------------------------------------------------------------

async def cmd_tareas(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        tareas = db.listar_tareas_abiertas()
        if not tareas:
            await update.message.reply_text("No hay tareas abiertas.")
            return
        await update.message.reply_text("📋 Tareas abiertas:\n\n" + _formatear_lista_tareas(tareas))
    except Exception as exc:
        logger.error("Error en /tareas: %s", exc)
        await update.message.reply_text("Error al obtener las tareas.")


async def cmd_nueva(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    titulo = " ".join(context.args) if context.args else ""
    if not titulo:
        await update.message.reply_text("Uso: /nueva <título de la tarea>")
        return
    try:
        autor = update.effective_user.username or update.effective_user.full_name or "desconocido"
        tarea_id = db.crear_tarea(titulo=titulo, autor=autor, prioridad="media")
        await update.message.reply_text(f"✅ Tarea #{tarea_id} creada: {titulo}")
        logger.info("Tarea %d creada via /nueva por %s", tarea_id, autor)
    except Exception as exc:
        logger.error("Error en /nueva: %s", exc)
        await update.message.reply_text("Error al crear la tarea.")


async def cmd_hecha(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    raw = context.args[0] if context.args else ""
    if not raw.isdigit():
        await update.message.reply_text("Uso: /hecha <id>")
        return
    tarea_id = int(raw)
    try:
        tarea = db.obtener_tarea(tarea_id)
        if tarea is None:
            await update.message.reply_text(f"No existe la tarea #{tarea_id}.")
            return
        db.actualizar_estado_tarea(tarea_id, "hecha")
        await update.message.reply_text(f"✅ Tarea #{tarea_id} marcada como hecha: {tarea['titulo']}")
        logger.info("Tarea %d → hecha via /hecha", tarea_id)
    except Exception as exc:
        logger.error("Error en /hecha: %s", exc)
        await update.message.reply_text("Error al actualizar la tarea.")


async def cmd_estado(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    estados_str = ", ".join(sorted(_ESTADOS_VALIDOS))
    if len(context.args) < 2 or not context.args[0].isdigit():
        await update.message.reply_text(f"Uso: /estado <id> <estado>\nEstados válidos: {estados_str}")
        return
    tarea_id = int(context.args[0])
    nuevo_estado = context.args[1].lower()
    if nuevo_estado not in _ESTADOS_VALIDOS:
        await update.message.reply_text(f"Estado '{nuevo_estado}' no válido.\nVálidos: {estados_str}")
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

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("tareas", cmd_tareas))
    app.add_handler(CommandHandler("nueva", cmd_nueva))
    app.add_handler(CommandHandler("hecha", cmd_hecha))
    app.add_handler(CommandHandler("estado", cmd_estado))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.add_handler(CallbackQueryHandler(handle_verif_callback, pattern="^verif:"))
    app.add_handler(CallbackQueryHandler(handle_corr_callback, pattern="^corr:"))

    jq = app.job_queue
    jq.run_daily(job_resumen_apertura, time=time(8, 30, tzinfo=TZ), days=(0, 1, 2, 3, 4), name="apertura")
    jq.run_daily(job_resumen_cierre, time=time(17, 0, tzinfo=TZ), days=(0, 1, 2, 3, 4), name="cierre")
    jq.run_daily(job_expiracion_verificacion, time=time(0, 0, tzinfo=TZ), name="expiracion_verificacion")

    logger.info("Bot arrancado con long-polling.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
