# -*- coding: utf-8 -*-
"""
Mas vendidos por canal y plan de compras.

Todo sale de la base de Lusync (movimientos y stock_bodega), sin llamar a los
marketplaces: la pagina tiene que cargar rapido aunque el periodo sea largo.
El stock Full se actualiza ANTES, con el boton que reconcilia cada bodega Full
contra lo que informa el marketplace (/stock-fulfillment/aplicar).

Reglas del usuario (22/10/2026):
  - Venta diaria con los ultimos 180 dias (se puede elegir 30/90/180).
  - El stock Full cuenta: esas unidades tambien se venden.
  - El plan mira 3 meses hacia adelante.

Calculo por producto:
  venta_dia      = unidades vendidas en el periodo / dias del periodo
  dias_stock     = stock total (propio + Full) / venta_dia
  se_agota       = hoy + dias_stock
  comprar_antes  = se_agota - tiempo de reposicion del producto (lead_time)
  pedir          = venta_dia x meses a cubrir x 30, redondeado hacia arriba
"""

import math
import threading
import time
from datetime import timedelta

# Canal del movimiento -> columna del informe
_CANALES = {"falabella": "falabella", "mercadolibre": "mercadolibre", "mercado libre": "mercadolibre",
            "meli": "mercadolibre", "paris": "paris", "parís": "paris", "ripley": "ripley",
            "walmart": "walmart", "web": "web", "woocommerce": "web", "woo": "web", "pos": "pos"}
ORDEN_CANALES = ("falabella", "mercadolibre", "paris", "ripley", "walmart", "web", "pos")
NO_VENTA = ("manual", "ajuste", "sistema", "")

_CACHE = {}
_CACHE_LOCK = threading.Lock()
_CACHE_SEG = 600


def _canal(c):
    return _CANALES.get((c or "").strip().lower())


def _consultar(tenant_id, dias):
    from inventario import get_conn, release_conn, now_chile
    hoy = now_chile().replace(tzinfo=None)
    desde = hoy - timedelta(days=dias)
    desde_ant = desde - timedelta(days=dias)
    conn = get_conn(tenant_id=tenant_id, is_admin=True)
    try:
        with conn.cursor() as cur:
            # Ventas: salidas con orden de un canal de venta, sin las ordenes que
            # despues se cancelaron (tienen su entrada "Cancelacion ..."). Se cuenta
            # la cantidad pedida, con faltante incluido: es demanda real aunque no
            # hubiera stock.
            cur.execute("""
                SELECT m.sku, LOWER(COALESCE(m.canal, '')),
                       SUM(CASE WHEN m.fecha >= %s THEN m.cantidad ELSE 0 END),
                       SUM(CASE WHEN m.fecha < %s THEN m.cantidad ELSE 0 END)
                  FROM movimientos m
                 WHERE m.tenant_id = %s AND m.tipo = 'salida' AND m.fecha >= %s
                   AND COALESCE(m.orden_id::text, '') <> ''
                   AND LOWER(COALESCE(m.canal, '')) NOT IN ('manual', 'ajuste', 'sistema', '')
                   AND NOT EXISTS (SELECT 1 FROM movimientos c
                                    WHERE c.tenant_id = m.tenant_id AND c.tipo = 'entrada'
                                      AND c.sku = m.sku AND c.orden_id::text = m.orden_id::text
                                      AND COALESCE(c.motivo, '') ILIKE 'cancelaci%%')
                 GROUP BY 1, 2""", (desde, desde, tenant_id, desde_ant))
            ventas = cur.fetchall()
            cancel = []
            cur.execute("""
                SELECT s.sku, s.bodega_codigo, COALESCE(b.tipo, 'propia'), s.cantidad
                  FROM stock_bodega s
             LEFT JOIN bodegas b ON b.codigo = s.bodega_codigo AND b.tenant_id = s.tenant_id
                 WHERE s.tenant_id = %s AND COALESCE(s.cantidad, 0) <> 0""", (tenant_id,))
            stock = cur.fetchall()
            cur.execute("SELECT sku, nombre, COALESCE(lead_time, 45) FROM productos WHERE tenant_id = %s",
                        (tenant_id,))
            productos = {r[0]: {"nombre": r[1] or r[0], "lead_time": int(r[2] or 45)} for r in cur.fetchall()}
        conn.commit()
    finally:
        release_conn(conn)
    return hoy, ventas, cancel, stock, productos


def plan(tenant_id, dias=180, meses=3, usar_cache=True):
    dias = 30 if dias < 45 else (90 if dias < 135 else 180)
    meses = max(1, min(int(meses or 3), 6))
    clave = (tenant_id, dias)
    with _CACHE_LOCK:
        c = _CACHE.get(clave)
        if usar_cache and c and time.time() - c[0] < _CACHE_SEG:
            hoy, ventas, cancel, stock, productos = c[1]
        else:
            c = None
    if not c:
        datos = _consultar(tenant_id, dias)
        with _CACHE_LOCK:
            _CACHE[clave] = (time.time(), datos)
        hoy, ventas, cancel, stock, productos = datos

    por_sku = {}

    def _fila(sku):
        p = productos.get(sku) or {"nombre": sku, "lead_time": 45}
        return por_sku.setdefault(sku, {"sku": sku, "nombre": p["nombre"], "lead_time": p["lead_time"],
                                        "canales": {}, "total": 0, "anterior": 0,
                                        "stock_propio": 0, "stock_full": 0, "full_por_bodega": {}})

    canales_vistos = set()
    for sku, canal, n, n_ant in ventas:
        k = _canal(canal)
        if not k:
            continue
        f = _fila(sku)
        f["canales"][k] = f["canales"].get(k, 0) + int(n or 0)
        f["total"] += int(n or 0)
        f["anterior"] += int(n_ant or 0)
        if n:
            canales_vistos.add(k)
    for sku, canal, n in cancel:
        k = _canal(canal)
        if not k or sku not in por_sku:
            continue
        f = por_sku[sku]
        f["canales"][k] = max(0, f["canales"].get(k, 0) - int(n or 0))
        f["total"] = max(0, f["total"] - int(n or 0))
    for sku, bodega, tipo, cant in stock:
        if sku not in por_sku:
            continue
        f = por_sku[sku]
        if (tipo or "propia") == "propia":
            f["stock_propio"] += int(cant or 0)
        else:
            f["stock_full"] += int(cant or 0)
            f["full_por_bodega"][bodega] = int(cant or 0)

    hoy_d = hoy.date()
    filas = []
    for f in por_sku.values():
        if f["total"] <= 0:
            continue
        f["stock_total"] = max(0, f["stock_propio"]) + max(0, f["stock_full"])
        vd = f["total"] / float(dias)
        f["venta_dia"] = round(vd, 3)
        f["variacion"] = (round((f["total"] - f["anterior"]) * 100.0 / f["anterior"])
                          if f["anterior"] else None)
        dias_stock = f["stock_total"] / vd if vd > 0 else None
        f["dias_stock"] = round(dias_stock) if dias_stock is not None else None
        if dias_stock is not None:
            agota = hoy_d + timedelta(days=int(dias_stock))
            comprar = agota - timedelta(days=f["lead_time"])
            f["se_agota"] = agota.strftime("%d/%m/%Y")
            f["comprar_antes"] = comprar.strftime("%d/%m/%Y")
            f["comprar_iso"] = comprar.isoformat()
            atraso = (hoy_d - comprar).days
            f["atraso_dias"] = atraso if atraso > 0 else 0
        f["pedir"] = int(math.ceil(vd * meses * 30))
        filas.append(f)
    filas.sort(key=lambda x: -x["total"])

    # Plan de compras: comprar ya + los proximos meses calendario
    limite = hoy_d + timedelta(days=meses * 31)
    grupos = {"ya": []}
    meses_lista = []
    m = hoy_d.replace(day=1)
    for i in range(meses + 1):
        clave_m = m.strftime("%Y-%m")
        meses_lista.append(clave_m)
        grupos[clave_m] = []
        m = (m + timedelta(days=32)).replace(day=1)
    for f in filas:
        if not f.get("comprar_iso"):
            continue
        from datetime import date
        d = date.fromisoformat(f["comprar_iso"])
        if d <= hoy_d:
            grupos["ya"].append(f["sku"])
        elif d <= limite:
            k = d.strftime("%Y-%m")
            grupos.setdefault(k, []).append(f["sku"])
    # El mes en curso se junta con "comprar ya" si la fecha ya paso, y queda
    # como su propio grupo si es mas adelante este mes.
    nombres_mes = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
                   "septiembre", "octubre", "noviembre", "diciembre"]
    columnas = [{"clave": "ya", "titulo": "Comprar ya", "skus": grupos["ya"]}]
    for k in meses_lista:
        if grupos.get(k):
            anio, mes = k.split("-")
            columnas.append({"clave": k, "titulo": nombres_mes[int(mes) - 1].capitalize() +
                             ("" if int(anio) == hoy_d.year else " " + anio), "skus": grupos[k]})
    canales = [c for c in ORDEN_CANALES if c in canales_vistos]
    return {"dias": dias, "meses": meses, "hoy": hoy_d.strftime("%d/%m/%Y"),
            "canales": canales, "productos": filas, "plan": columnas,
            "generado": hoy.strftime("%d/%m/%Y %H:%M")}


def borrar_cache(tenant_id=None):
    with _CACHE_LOCK:
        for k in list(_CACHE):
            if tenant_id is None or k[0] == tenant_id:
                _CACHE.pop(k, None)
