# -*- coding: utf-8 -*-
"""Despachos desde la bodega propia: que tiene que sacar hoy la persona de
bodega y que queda para el siguiente dia habil.

Cada sincronizacion de canal anota aca el estado de envio de las ordenes que
salen de la bodega propia. Las de Full (las despacha el marketplace) no se
cuentan: o no se anotan, o quedan resueltas desde el principio.

La fecha que manda es la de la COMPRA en el canal, no la de importacion. Con el
horario de corte de cada canal (configurable por cliente, 12:00 por defecto):
  - comprada en dia habil antes del corte  -> se despacha ese mismo dia
  - comprada despues del corte, en fin de semana o feriado -> siguiente dia habil
Lo que no se despacho de dias anteriores sigue contando para hoy.

La orden deja de contar cuando el canal la marca como entregada al courier.
"""
from datetime import datetime, date, time, timedelta

import pytz

from inventario import get_conn, release_conn, tenant_actual, es_dia_habil

ZONA_CHILE = pytz.timezone("America/Santiago")

CORTE_POR_DEFECTO = "12:00"

# Canales que despachan desde bodega propia, con el nombre que ve el usuario.
CANALES_DESPACHO = [
    ("mercadolibre", "MercadoLibre"),
    ("falabella", "Falabella"),
    ("paris", "Paris"),
    ("ripley", "Ripley"),
    ("walmart", "Walmart"),
    ("web", "Web"),
]

# Ordenes mas viejas que esto no se cuentan: si un canal dejo de informar una
# orden, no puede quedar inflando el numero para siempre.
DIAS_MAXIMOS = 10


def _tenant(tenant_id=None):
    if tenant_id:
        return int(tenant_id)
    ctx = tenant_actual()
    return int(ctx[0]) if ctx else 1


def init_despachos_bodega():
    conn = get_conn(is_admin=True)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS despachos_bodega (
                    tenant_id    INTEGER NOT NULL,
                    canal        TEXT NOT NULL,
                    orden_id     TEXT NOT NULL,
                    numero       TEXT,
                    fecha_compra TIMESTAMP,
                    estado_canal TEXT,
                    pendiente    BOOLEAN NOT NULL DEFAULT TRUE,
                    ref_envio    TEXT,
                    visto_en     TIMESTAMP NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (tenant_id, canal, orden_id)
                )""")
            cur.execute("""CREATE INDEX IF NOT EXISTS idx_despachos_pendientes
                           ON despachos_bodega (tenant_id) WHERE pendiente""")
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"[Despachos] init: {e}")
    finally:
        release_conn(conn)


def a_hora_chile(fecha):
    """datetime en hora de Chile, sin zona. Uno sin zona se toma como de Chile."""
    if fecha is None:
        return None
    if isinstance(fecha, str):
        try:
            fecha = datetime.fromisoformat(fecha.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    if fecha.tzinfo is not None:
        fecha = fecha.astimezone(ZONA_CHILE).replace(tzinfo=None)
    return fecha


def anotar_despacho(canal, orden_id, fecha_compra, estado_canal, pendiente,
                    numero=None, ref_envio=None, tenant_id=None):
    """Guarda o actualiza el estado de envio de una orden de bodega propia.

    No puede romper la sincronizacion: cualquier error se loguea y se sigue.
    """
    if not orden_id:
        return
    tid = _tenant(tenant_id)
    conn = None
    try:
        conn = get_conn(tenant_id=tid, is_admin=True)
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO despachos_bodega
                    (tenant_id, canal, orden_id, numero, fecha_compra,
                     estado_canal, pendiente, ref_envio, visto_en)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (tenant_id, canal, orden_id) DO UPDATE SET
                    numero       = COALESCE(EXCLUDED.numero, despachos_bodega.numero),
                    fecha_compra = COALESCE(EXCLUDED.fecha_compra, despachos_bodega.fecha_compra),
                    estado_canal = EXCLUDED.estado_canal,
                    pendiente    = EXCLUDED.pendiente,
                    ref_envio    = COALESCE(EXCLUDED.ref_envio, despachos_bodega.ref_envio),
                    visto_en     = NOW()""",
                (tid, canal, str(orden_id), str(numero) if numero else None,
                 a_hora_chile(fecha_compra), (estado_canal or "")[:60],
                 bool(pendiente), str(ref_envio) if ref_envio else None))
        conn.commit()
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        print(f"[Despachos] no pude anotar {canal} {orden_id}: {str(e)[:150]}")
    finally:
        release_conn(conn)


def quitar_despacho(canal, orden_id, tenant_id=None):
    """Orden cancelada: ya no hay nada que despachar."""
    tid = _tenant(tenant_id)
    conn = None
    try:
        conn = get_conn(tenant_id=tid, is_admin=True)
        with conn.cursor() as cur:
            cur.execute("""DELETE FROM despachos_bodega
                            WHERE tenant_id = %s AND canal = %s AND orden_id = %s""",
                        (tid, canal, str(orden_id)))
        conn.commit()
    except Exception as e:
        print(f"[Despachos] no pude quitar {canal} {orden_id}: {str(e)[:150]}")
    finally:
        release_conn(conn)


def despachos_resueltos(canal, tenant_id=None):
    """orden_id de las ordenes de este canal que ya no hay que mirar: Full o
    ya entregadas al courier. Sirve para no volver a consultar su envio."""
    tid = _tenant(tenant_id)
    conn = None
    try:
        conn = get_conn(tenant_id=tid, is_admin=True)
        with conn.cursor() as cur:
            cur.execute("""SELECT orden_id FROM despachos_bodega
                            WHERE tenant_id = %s AND canal = %s AND NOT pendiente""",
                        (tid, canal))
            return {r[0] for r in cur.fetchall()}
    except Exception as e:
        print(f"[Despachos] resueltos {canal}: {str(e)[:150]}")
        return set()
    finally:
        release_conn(conn)


def despachos_por_revisar(canal, minutos=15, limite=25, tenant_id=None):
    """Pendientes que la ultima sincronizacion no trajo (el canal solo devuelve
    las ordenes mas recientes). Hay que preguntarlas una por una."""
    tid = _tenant(tenant_id)
    conn = None
    try:
        conn = get_conn(tenant_id=tid, is_admin=True)
        with conn.cursor() as cur:
            cur.execute("""SELECT orden_id, ref_envio FROM despachos_bodega
                            WHERE tenant_id = %s AND canal = %s AND pendiente
                              AND visto_en < NOW() - (%s * INTERVAL '1 minute')
                              AND fecha_compra > NOW() - (%s * INTERVAL '1 day')
                            ORDER BY visto_en ASC LIMIT %s""",
                        (tid, canal, minutos, DIAS_MAXIMOS, limite))
            return cur.fetchall()
    except Exception as e:
        print(f"[Despachos] por revisar {canal}: {str(e)[:150]}")
        return []
    finally:
        release_conn(conn)


# ── Horario de corte ─────────────────────────────────────────────────────────

def clave_corte(canal):
    return f"corte_despacho_{canal}"


def _parse_corte(valor):
    try:
        h, m = str(valor or CORTE_POR_DEFECTO).strip().split(":")[:2]
        return time(int(h), int(m))
    except Exception:
        return time(12, 0)


def cortes_del_cliente(config):
    """{canal: "HH:MM"} desde la configuracion del cliente."""
    return {c: (config.get(clave_corte(c)) or CORTE_POR_DEFECTO) for c, _ in CANALES_DESPACHO}


class _DiasHabiles:
    """es_dia_habil consulta la base por cada fecha: se recuerda por request."""

    def __init__(self):
        self._memo = {}

    def es_habil(self, d):
        if d not in self._memo:
            self._memo[d] = es_dia_habil(d)
        return self._memo[d]

    def siguiente(self, d):
        d = d + timedelta(days=1)
        for _ in range(15):
            if self.es_habil(d):
                return d
            d += timedelta(days=1)
        return d


def dia_de_despacho(fecha_compra, corte, habiles):
    """Dia habil en que hay que despachar una orden comprada en fecha_compra."""
    d = fecha_compra.date()
    if habiles.es_habil(d) and fecha_compra.time() < corte:
        return d
    return habiles.siguiente(d)


def resumen_despachos(config, tenant_id, ahora=None):
    """Lo que necesita la tarjeta del dashboard y su detalle."""
    tid = _tenant(tenant_id)
    ahora = ahora or datetime.now(ZONA_CHILE).replace(tzinfo=None)
    habiles = _DiasHabiles()
    cortes = cortes_del_cliente(config)
    cortes_t = {c: _parse_corte(v) for c, v in cortes.items()}

    hoy = ahora.date()
    # En fin de semana o feriado, "hoy" es el proximo dia en que se trabaja.
    dia_hoy = hoy if habiles.es_habil(hoy) else habiles.siguiente(hoy)
    dia_siguiente = habiles.siguiente(dia_hoy)

    conn = get_conn(tenant_id=tid, is_admin=True)
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT canal, orden_id, numero, fecha_compra, estado_canal
                             FROM despachos_bodega
                            WHERE tenant_id = %s AND pendiente
                              AND fecha_compra IS NOT NULL
                              AND fecha_compra > NOW() - (%s * INTERVAL '1 day')
                            ORDER BY fecha_compra ASC""", (tid, DIAS_MAXIMOS))
            filas = cur.fetchall()

            # Productos de cada orden, desde la venta registrada.
            ids = list({f[1] for f in filas})
            productos = {}
            if ids:
                cur.execute("""SELECT orden_id, sku, nombre, SUM(cantidad)
                                 FROM movimientos
                                WHERE tenant_id = %s AND tipo = 'salida'
                                  AND orden_id = ANY(%s)
                                GROUP BY orden_id, sku, nombre""", (tid, ids))
                for oid, sku, nombre, cant in cur.fetchall():
                    productos.setdefault(oid, []).append(
                        {"sku": sku, "nombre": nombre, "cantidad": int(cant or 0)})
    finally:
        release_conn(conn)

    nombres = dict(CANALES_DESPACHO)
    por_canal = {c: {"canal": c, "nombre": nombres[c], "corte": cortes[c],
                     "hoy": 0, "siguiente": 0} for c, _ in CANALES_DESPACHO}
    ordenes_hoy, ordenes_siguiente = [], []
    for canal, oid, numero, fecha, estado in filas:
        if canal not in por_canal:
            continue
        dia = dia_de_despacho(fecha, cortes_t[canal], habiles)
        orden = {"canal": canal, "canal_nombre": nombres[canal], "orden_id": oid,
                 "numero": numero or oid, "fecha_compra": fecha.strftime("%d/%m %H:%M"),
                 "estado": estado, "productos": productos.get(oid, [])}
        if dia <= dia_hoy:
            por_canal[canal]["hoy"] += 1
            ordenes_hoy.append(orden)
        elif dia == dia_siguiente:
            por_canal[canal]["siguiente"] += 1
            ordenes_siguiente.append(orden)

    dias = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
    def _etq(d):
        return f"{dias[d.weekday()]} {d.strftime('%d/%m')}"

    return {
        "hoy": len(ordenes_hoy),
        "siguiente": len(ordenes_siguiente),
        "dia_hoy": _etq(dia_hoy),
        "dia_hoy_es_hoy": dia_hoy == hoy,
        "dia_siguiente": _etq(dia_siguiente),
        "por_canal": [por_canal[c] for c, _ in CANALES_DESPACHO],
        "ordenes_hoy": ordenes_hoy,
        "ordenes_siguiente": ordenes_siguiente,
        "cortes": cortes,
        "generado": ahora.strftime("%d/%m %H:%M"),
    }
