# -*- coding: utf-8 -*-
"""
Ordenes Full: Lusync NUNCA emite ni anula documentos de venta por ellas.

Regla del usuario (18/10/2026): en las ventas Full de cualquier marketplace
(Falabella Full, MercadoLibre Full, Walmart WFS, Paris, Ripley...) la boleta
la emite el marketplace por nosotros, y la anula con liquidacion factura. Si
Lusync emitiera o anulara tambien, el documento quedaria duplicado ante el SII.

Se decide por la bodega de la que salio la venta (movimientos + bodegas.tipo):
una bodega que no es 'propia' es del marketplace. Falabella Full
(FALABELLA_FBM) cuenta como Full para esto aunque la devolucion llegue a
nuestra bodega: el documento igual lo emite Falabella.

Si no se puede saber (la venta no esta en Lusync, o falla la consulta), en un
marketplace se BLOQUEA: es preferible que una persona lo revise a emitir un
documento duplicado.
"""

import re

CANALES_MARKETPLACE = ("falabella", "mercadolibre", "meli", "ml", "paris", "ripley", "walmart")


def _variantes_orden(orden_id):
    o = str(orden_id or "").strip()
    if not o:
        return []
    vs = {o}
    # Paris guarda a veces la sub-orden con sufijo "-1"
    m = re.match(r"^(.+)-\d+$", o)
    if m:
        vs.add(m.group(1))
    return list(vs)


def estado_full_orden(tenant_id, orden_id, canal=None):
    """(es_full, detalle). es_full: True, False, o None si no se puede saber."""
    canal = (canal or "").strip().lower()
    ordenes = _variantes_orden(orden_id)
    if not ordenes:
        return False, "sin orden de marketplace"
    from inventario import get_conn, release_conn
    conn = None
    try:
        conn = get_conn(tenant_id=tenant_id, is_admin=True)
        with conn.cursor() as cur:
            cur.execute("""SELECT m.bodega_codigo, COALESCE(b.tipo, 'propia')
                             FROM movimientos m
                        LEFT JOIN bodegas b ON b.codigo = m.bodega_codigo AND b.tenant_id = m.tenant_id
                            WHERE m.tenant_id = %s AND m.tipo = 'salida'
                              AND (m.orden_id = ANY(%s) OR m.numero_orden = ANY(%s))""",
                        (tenant_id, ordenes, ordenes))
            filas = cur.fetchall()
            cur.execute("""SELECT 1 FROM devoluciones_marketplace
                            WHERE tenant_id = %s AND order_id = ANY(%s)
                              AND COALESCE(tipo, '') ILIKE '%%fulfillment%%' LIMIT 1""",
                        (tenant_id, ordenes))
            dev_full = cur.fetchone() is not None
        conn.commit()
    except Exception as e:  # noqa: BLE001
        return None, "no pude revisar la venta: " + str(e)[:120]
    finally:
        if conn is not None:
            release_conn(conn)

    full = sorted({f[0] for f in filas if f[0] and (f[1] or "propia") != "propia"})
    if full or dev_full:
        return True, "la venta salió de " + (", ".join(full) or "un fulfillment del marketplace")
    if filas:
        return False, "la venta salió de bodega propia"
    if canal and canal not in CANALES_MARKETPLACE:
        return False, "canal sin fulfillment"
    return None, "no encontré la venta en Lusync"


def confirmacion_no_full(canal, orden_cruda):
    """Texto si el MARKETPLACE dice explicitamente que esa orden la despacha
    el vendedor; None si no lo dice. Hoy solo Walmart lo informa por parte de
    la compra: shipNodeType = SellerFulfilled en el purchaseOrder. Hace falta
    porque Walmart parte una compra (customerOrderId) en una parte propia y
    otra Full, y la venta en Lusync queda con el numero de la compra: mirando
    solo la bodega, la parte propia pareceria Full o desconocida."""
    if (canal or "").lower() != "walmart" or not isinstance(orden_cruda, dict):
        return None
    if str(orden_cruda.get("shipNodeType") or "").strip().upper() != "SELLERFULFILLED":
        return None
    lineas = (orden_cruda.get("orderLines") or {}).get("orderLine") or []
    lineas = lineas if isinstance(lineas, list) else [lineas]
    for ln in lineas:
        fi = ln.get("fulfillment") or {}
        opt = str(fi.get("fulfillmentOption") or "").strip().upper()
        snt = str(fi.get("shipNodeType") or "").strip().upper()
        # fulfillmentOption es el tipo de entrega (S2H, despacho a domicilio...),
        # no quien despacha: solo descarta si una linea dice WFS o 3PL.
        if "WFS" in snt or "3PL" in snt or "WFS" in opt or "3PL" in opt:
            return None
    return "Walmart informa shipNodeType SellerFulfilled en la orden %s" % (
        orden_cruda.get("purchaseOrderId") or "")


def bloqueo_full(tenant_id, orden_id, canal=None, no_full_por=None):
    """Mensaje de error si NO se puede emitir/anular un documento para esa
    orden; None si se puede.

    no_full_por: la confirmacion explicita del marketplace de que la orden la
    despacha el vendedor (confirmacion_no_full). Solo vale para Walmart."""
    if not str(orden_id or "").strip():
        return None
    if no_full_por and (canal or "").lower() == "walmart":
        return None
    es_full, detalle = estado_full_orden(tenant_id, orden_id, canal)
    if es_full is False:
        return None
    if es_full:
        return ("La orden %s es Full (%s). En las ventas Full la boleta la emite el "
                "marketplace y la anula con liquidación factura: Lusync no emite ni "
                "anula documentos por ella." % (orden_id, detalle))
    return ("No pude confirmar que la orden %s no sea Full (%s). Por seguridad no se "
            "emite ni se anula: en las ventas Full el documento lo maneja el marketplace."
            % (orden_id, detalle))


def bloqueo_full_por_documento(tenant_id, tipo_dte, folio):
    """Para NC/ND: si el documento referenciado es de una orden de marketplace,
    aplica la misma regla. Documentos sin orden (ventas propias) pasan."""
    from inventario import get_conn, release_conn
    conn = None
    try:
        conn = get_conn(tenant_id=tenant_id, is_admin=True)
        with conn.cursor() as cur:
            cur.execute("""SELECT orden_id, canal FROM facturacion_dtes
                            WHERE tenant_id = %s AND tipo_dte = %s AND folio = %s
                            ORDER BY id DESC LIMIT 1""", (tenant_id, int(tipo_dte), int(folio)))
            fila = cur.fetchone()
        conn.commit()
    except Exception as e:  # noqa: BLE001
        return "No pude revisar el documento referenciado; no se emite: " + str(e)[:120]
    finally:
        if conn is not None:
            release_conn(conn)
    if not fila or not fila[0]:
        return None
    return bloqueo_full(tenant_id, fila[0], fila[1])
