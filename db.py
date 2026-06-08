"""
Capa de persistencia con psycopg v3 + ConnectionPool.

Uso:
    from db import pool, init_db
    init_db()          # llamar una vez al arrancar
    with pool.connection() as conn:
        ...            # o usar las funciones CRUD directamente
"""

import json
import logging
from datetime import datetime
from typing import Any

import psycopg
from psycopg_pool import ConnectionPool

from config import DATABASE_URL

logger = logging.getLogger(__name__)

pool: ConnectionPool = ConnectionPool(DATABASE_URL, min_size=1, max_size=5, open=False)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tareas (
    id            SERIAL PRIMARY KEY,
    titulo        TEXT        NOT NULL,
    detalle       TEXT,
    prioridad     VARCHAR(10) NOT NULL DEFAULT 'media'
                  CHECK (prioridad IN ('alta', 'media', 'baja')),
    estado        VARCHAR(20) NOT NULL DEFAULT 'pendiente'
                  CHECK (estado IN ('pendiente', 'en_curso', 'bloqueada', 'hecha')),
    autor         TEXT        NOT NULL,
    creada_en     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
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
"""


def init_db() -> None:
    """Abre el pool y crea las tablas si no existen."""
    pool.open()
    with pool.connection() as conn:
        conn.execute(_SCHEMA)
        conn.commit()
    logger.info("Base de datos inicializada correctamente.")


def close_db() -> None:
    pool.close()


# ---------------------------------------------------------------------------
# CRUD — tareas
# ---------------------------------------------------------------------------

def crear_tarea(
    titulo: str,
    autor: str,
    detalle: str = "",
    prioridad: str = "media",
) -> int:
    """Inserta una tarea nueva y devuelve su id."""
    with pool.connection() as conn:
        row = conn.execute(
            """
            INSERT INTO tareas (titulo, detalle, prioridad, autor)
            VALUES (%s, %s, %s, %s)
            RETURNING id
            """,
            (titulo, detalle, prioridad, autor),
        ).fetchone()
        conn.commit()
    return row[0]


def actualizar_estado_tarea(tarea_id: int, nuevo_estado: str) -> bool:
    """Actualiza el estado de una tarea. Devuelve True si la encontró."""
    with pool.connection() as conn:
        result = conn.execute(
            """
            UPDATE tareas
            SET estado = %s, actualizada_en = NOW()
            WHERE id = %s
            """,
            (nuevo_estado, tarea_id),
        )
        conn.commit()
    return result.rowcount > 0


def listar_tareas_abiertas() -> list[dict[str, Any]]:
    """Devuelve las tareas en estado pendiente, en_curso o bloqueada."""
    with pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT id, titulo, detalle, prioridad, estado, autor, creada_en, actualizada_en
            FROM tareas
            WHERE estado != 'hecha'
            ORDER BY
                CASE prioridad WHEN 'alta' THEN 0 WHEN 'media' THEN 1 ELSE 2 END,
                creada_en
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
    """Guarda el mensaje en el log de auditoría. Devuelve el id insertado."""
    clasificacion_json = json.dumps(clasificacion) if clasificacion else None
    with pool.connection() as conn:
        row = conn.execute(
            """
            INSERT INTO mensajes (telegram_message_id, chat_id, autor, texto, clasificacion)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
            """,
            (telegram_message_id, chat_id, autor, texto, clasificacion_json),
        ).fetchone()
        conn.commit()
    return row[0]
