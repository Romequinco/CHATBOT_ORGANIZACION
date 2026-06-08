"""
Capa de persistencia con psycopg v3 + ConnectionPool.
"""

import json
import logging
from typing import Any

from psycopg_pool import ConnectionPool

from config import DATABASE_URL

logger = logging.getLogger(__name__)

pool: ConnectionPool = ConnectionPool(DATABASE_URL, min_size=1, max_size=5, open=False)

# Tablas base — sin CHECK inline en estado (la migración lo gestiona)
_SCHEMA = """
CREATE TABLE IF NOT EXISTS tareas (
    id             SERIAL PRIMARY KEY,
    titulo         TEXT        NOT NULL,
    detalle        TEXT,
    prioridad      VARCHAR(10) NOT NULL DEFAULT 'media'
                   CHECK (prioridad IN ('alta', 'media', 'baja')),
    estado         VARCHAR(20) NOT NULL DEFAULT 'pendiente',
    autor          TEXT        NOT NULL,
    creada_en      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    actualizada_en TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS mensajes (
    id                  SERIAL PRIMARY KEY,
    telegram_message_id BIGINT      NOT NULL,
    chat_id             BIGINT      NOT NULL,
    autor               TEXT        NOT NULL,
    texto               TEXT,
    recibido_en         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    clasificacion       JSONB
);

CREATE TABLE IF NOT EXISTS verificacion_estado (
    id         INTEGER PRIMARY KEY DEFAULT 1,
    activo     BOOLEAN     NOT NULL DEFAULT false,
    abierta_en TIMESTAMPTZ,
    datos      JSONB
);
"""

# Migración idempotente: añade 'por_verificar' al CHECK de estado
_MIGRATION = """
DO $$
BEGIN
    ALTER TABLE tareas DROP CONSTRAINT IF EXISTS tareas_estado_check;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'tareas_estado_check_v2'
          AND conrelid = 'tareas'::regclass
    ) THEN
        ALTER TABLE tareas ADD CONSTRAINT tareas_estado_check_v2
            CHECK (estado IN ('pendiente', 'en_curso', 'bloqueada', 'hecha', 'por_verificar'));
    END IF;
END$$;
"""


def init_db() -> None:
    """Abre el pool, crea tablas y aplica migraciones idempotentes."""
    pool.open()
    with pool.connection() as conn:
        conn.execute(_SCHEMA)
        conn.execute(_MIGRATION)
        conn.execute(
            "INSERT INTO verificacion_estado (id, activo) VALUES (1, false) ON CONFLICT (id) DO NOTHING"
        )
        conn.commit()
    logger.info("Base de datos inicializada correctamente.")


def close_db() -> None:
    pool.close()


# ---------------------------------------------------------------------------
# CRUD — tareas
# ---------------------------------------------------------------------------

def crear_tarea(titulo: str, autor: str, detalle: str = "", prioridad: str = "media") -> int:
    with pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO tareas (titulo, detalle, prioridad, autor) VALUES (%s, %s, %s, %s) RETURNING id",
            (titulo, detalle, prioridad, autor),
        ).fetchone()
        conn.commit()
    return row[0]


def actualizar_estado_tarea(tarea_id: int, nuevo_estado: str) -> bool:
    with pool.connection() as conn:
        result = conn.execute(
            "UPDATE tareas SET estado = %s, actualizada_en = NOW() WHERE id = %s",
            (nuevo_estado, tarea_id),
        )
        conn.commit()
    return result.rowcount > 0


def listar_tareas_abiertas() -> list[dict[str, Any]]:
    """Tareas activamente abiertas — excluye 'hecha' y 'por_verificar'."""
    with pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT id, titulo, detalle, prioridad, estado, autor, creada_en, actualizada_en
            FROM tareas
            WHERE estado NOT IN ('hecha', 'por_verificar')
            ORDER BY CASE prioridad WHEN 'alta' THEN 0 WHEN 'media' THEN 1 ELSE 2 END, creada_en
            """,
        ).fetchall()
    cols = ["id", "titulo", "detalle", "prioridad", "estado", "autor", "creada_en", "actualizada_en"]
    return [dict(zip(cols, row)) for row in rows]


def listar_tareas_por_verificar() -> list[dict[str, Any]]:
    """Tareas en estado 'por_verificar', ordenadas por prioridad."""
    with pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT id, titulo, detalle, prioridad, estado, autor, creada_en, actualizada_en
            FROM tareas
            WHERE estado = 'por_verificar'
            ORDER BY CASE prioridad WHEN 'alta' THEN 0 WHEN 'media' THEN 1 ELSE 2 END, creada_en
            """,
        ).fetchall()
    cols = ["id", "titulo", "detalle", "prioridad", "estado", "autor", "creada_en", "actualizada_en"]
    return [dict(zip(cols, row)) for row in rows]


def obtener_tarea(tarea_id: int) -> dict[str, Any] | None:
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT id, titulo, detalle, prioridad, estado, autor FROM tareas WHERE id = %s",
            (tarea_id,),
        ).fetchone()
    if row is None:
        return None
    return dict(zip(["id", "titulo", "detalle", "prioridad", "estado", "autor"], row))


def revertir_tareas_por_verificar() -> int:
    """Devuelve a 'pendiente' todas las tareas en 'por_verificar'. Retorna filas afectadas."""
    with pool.connection() as conn:
        result = conn.execute(
            "UPDATE tareas SET estado = 'pendiente', actualizada_en = NOW() WHERE estado = 'por_verificar'"
        )
        conn.commit()
    return result.rowcount


# ---------------------------------------------------------------------------
# Modo verificación
# ---------------------------------------------------------------------------

def activar_modo_verificacion(datos: dict) -> None:
    with pool.connection() as conn:
        conn.execute(
            "UPDATE verificacion_estado SET activo = true, abierta_en = NOW(), datos = %s WHERE id = 1",
            (json.dumps(datos),),
        )
        conn.commit()


def desactivar_modo_verificacion() -> None:
    with pool.connection() as conn:
        conn.execute("UPDATE verificacion_estado SET activo = false, datos = NULL WHERE id = 1")
        conn.commit()


def obtener_modo_verificacion() -> dict | None:
    """Devuelve el registro si activo=True, None si no."""
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT activo, abierta_en, datos FROM verificacion_estado WHERE id = 1"
        ).fetchone()
    if row is None or not row[0]:
        return None
    datos = row[2]
    if isinstance(datos, str):
        datos = json.loads(datos) if datos else {}
    return {"activo": row[0], "abierta_en": row[1], "datos": datos or {}}


def actualizar_datos_verificacion(datos: dict) -> None:
    with pool.connection() as conn:
        conn.execute(
            "UPDATE verificacion_estado SET datos = %s WHERE id = 1",
            (json.dumps(datos),),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Log de mensajes
# ---------------------------------------------------------------------------

def insertar_mensaje_log(
    telegram_message_id: int,
    chat_id: int,
    autor: str,
    texto: str,
    clasificacion: dict | None = None,
) -> int:
    clasificacion_json = json.dumps(clasificacion) if clasificacion else None
    with pool.connection() as conn:
        row = conn.execute(
            """
            INSERT INTO mensajes (telegram_message_id, chat_id, autor, texto, clasificacion)
            VALUES (%s, %s, %s, %s, %s) RETURNING id
            """,
            (telegram_message_id, chat_id, autor, texto, clasificacion_json),
        ).fetchone()
        conn.commit()
    return row[0]
