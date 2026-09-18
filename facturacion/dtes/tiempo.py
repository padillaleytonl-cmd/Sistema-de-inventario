# -*- coding: utf-8 -*-
"""
facturacion/dtes/tiempo.py
─────────────────────────────────────────────────────────────
Hora de Chile para todos los timestamps que leen el SII.

El servidor corre en UTC. La fecha de emisión (FchEmis) ya se calculaba en hora
de Chile, pero el timbre (TSTED) y las firmas (TmstFirma, TmstFirmaEnv) salían
de `datetime.now()`, o sea de la hora del servidor. Entre las 21:00 y la
medianoche de Chile eso deja el timbre fechado un día DESPUÉS que el documento:

    Boleta 25210 · FchEmis 2026-09-17 · TSTED 2026-09-18T01:37:06

Emitida a las 22:37 de Chile. Para el SII, un documento timbrado al día
siguiente de su fecha de emisión.

Todo lo que el SII interpreta como hora local del emisor tiene que salir de acá.
Este módulo no importa nada fuera de la librería estándar a propósito: lo usan
los generadores de DTE, que están pensados para poder importarse aunque falten
dependencias pesadas como cryptography o lxml.
"""
from datetime import datetime, timedelta, timezone

ZONA_CHILE = "America/Santiago"


def _offset_aproximado(utc: datetime) -> timedelta:
    """Respaldo para cuando no hay base de datos de zonas horarias instalada.

    Chile continental usa UTC-3 en horario de verano (de septiembre a abril) y
    UTC-4 el resto del año. Es una aproximación: los fines de semana en que se
    cambia la hora puede errar por una hora. No debería usarse nunca en
    producción — está solo para que un entorno sin tzdata no quede sin fecha.
    """
    return timedelta(hours=3) if utc.month >= 9 or utc.month <= 4 else timedelta(hours=4)


def ahora_chile() -> datetime:
    """Momento actual en hora de Chile, sin tzinfo (el SII no lleva offset)."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(ZONA_CHILE)).replace(tzinfo=None)
    except Exception:
        utc = datetime.now(timezone.utc)
        return (utc - _offset_aproximado(utc)).replace(tzinfo=None)


def timestamp_sii() -> str:
    """'AAAA-MM-DDTHH:MM:SS' — para TSTED, TmstFirma y TmstFirmaEnv."""
    return ahora_chile().strftime("%Y-%m-%dT%H:%M:%S")


def fecha_chile() -> str:
    """'AAAA-MM-DD' — para FchEmis."""
    return ahora_chile().strftime("%Y-%m-%d")


def sello_id() -> str:
    """'AAAAMMDDHHMMSS' — para los IDs de libros y del RCOF."""
    return ahora_chile().strftime("%Y%m%d%H%M%S")
