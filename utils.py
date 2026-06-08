from datetime import datetime
from zoneinfo import ZoneInfo

from config import TIMEZONE

_TZ = ZoneInfo(TIMEZONE)


def es_dia_laborable() -> bool:
    """True de lunes (0) a viernes (4). Festivos incluidos — no se filtra por calendario."""
    return datetime.now(_TZ).weekday() < 5


def ahora_madrid() -> datetime:
    return datetime.now(_TZ)
