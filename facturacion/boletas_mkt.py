# -*- coding: utf-8 -*-
"""
Boletas de las ventas de marketplace: que ordenes hay, cuanto pago el cliente,
si llevan boleta, y la emision (por ahora MANUAL, con un boton).

Reglas del usuario (18/10/2026):
  - El documento va SIEMPRE por lo que pago el cliente (facturacion/montos_orden.py).
  - JAMAS se emite ni se anula un documento de una orden Full: lo emite el
    marketplace y lo anula con liquidacion factura (facturacion/ordenes_full.py).
  - Walmart parte una compra en orden propia + orden Full: boleta solo por la
    parte de nuestra bodega (cada parte es su propio purchaseOrderId).
  - MercadoLibre: una boleta por pack, sumando solo ordenes nuestras y no Full.
  - Si los montos no cuadran al peso con el total del marketplace, la orden
    queda en "revisar" y no se emite.
  - La emision automatica existe como interruptor pero queda APAGADA hasta
    validar la manual.

Una fila de boletas_mkt = un documento a emitir. clave identifica la fila:
  falabella OrderNumber (el 3...), mercadolibre pack_id (o el id de la orden
  sin pack), walmart purchaseOrderId, paris subOrderNumber, ripley order_id,
  web numero del pedido.
"""

import json
import logging
from datetime import datetime, timedelta

from facturacion.montos_orden import POR_CANAL, montos_falabella, montos_meli_pack
from facturacion.ordenes_full import confirmacion_no_full

logger = logging.getLogger("lusync")

CANALES = ("falabella", "mercadolibre", "paris", "ripley", "walmart", "web")

ESTADOS = {
    "por_emitir": "Por emitir",
    "no_emitir": "No se emite",
    "revisar": "Revisar montos",
    "full": "La emite el marketplace",
    "emitida": "Emitida",
    "error": "Error al emitir",
    "faltan_datos": "Faltan datos de factura",
}

CAMPOS_FACTURA = ("rut", "razon_social", "giro", "direccion", "comuna")


def _factura_completa(f):
    from facturacion.utils import validar_rut
    return bool(f) and all(str(f.get(c) or "").strip() for c in CAMPOS_FACTURA) and validar_rut(f.get("rut"))


def factura_falabella(o):
    """Falabella: InvoiceRequired = true y los datos en ExtraBillingAttributes.
    No trae giro: queda para completar."""
    if str(o.get("InvoiceRequired")).lower() != "true":
        return None
    x = o.get("ExtraBillingAttributes") or {}
    rut = str(x.get("LegalId") or "").strip()
    if rut and "-" not in rut and x.get("CustomerVerifierDigit"):
        rut = rut + "-" + str(x.get("CustomerVerifierDigit"))
    return {"rut": _rut_limpio(rut) if rut else "", "razon_social": x.get("ReceiverLegalName") or "",
            "giro": x.get("ReceiverTypeRegimen") or "", "direccion": x.get("ReceiverAddress") or "",
            "comuna": x.get("ReceiverMunicipality") or x.get("ReceiverLocality") or "",
            "ciudad": x.get("ReceiverRegion") or "", "email": x.get("ReceiverEmail") or "",
            "fuente": "Falabella"}


def factura_meli(bi):
    """MercadoLibre: en billing_info, BUSINESS_NAME y ECONOMIC_ACTIVITY (giro)
    marcan que el comprador es una empresa y pidio factura."""
    base = (bi or {}).get("crudo") or {}
    extra = {str(e.get("type") or "").upper(): str(e.get("value") or "").strip()
             for e in (base.get("additional_info") or []) if isinstance(e, dict)}
    if not extra.get("BUSINESS_NAME"):
        return None
    direccion = " ".join(x for x in (extra.get("STREET_NAME"), extra.get("STREET_NUMBER")) if x)
    return {"rut": bi.get("rut") or "", "razon_social": extra.get("BUSINESS_NAME"),
            "giro": extra.get("ECONOMIC_ACTIVITY") or "", "direccion": direccion,
            "comuna": extra.get("CITY_NAME") or extra.get("NEIGHBORHOOD") or "",
            "ciudad": extra.get("STATE_NAME") or "", "email": "", "fuente": "MercadoLibre"}


def factura_paris(o):
    """Paris: originInvoiceType = factura."""
    if str(o.get("originInvoiceType") or "").lower() != "factura":
        return None
    b = o.get("billingAddress") or {}
    cust = o.get("customer") or {}
    return {"rut": buscar_rut(cust) or buscar_rut(o) or "",
            "razon_social": b.get("companyName") or b.get("company") or cust.get("companyName") or "",
            "giro": b.get("businessActivity") or b.get("giro") or "",
            "direccion": " ".join(str(x) for x in (b.get("address1"), b.get("address2")) if x),
            "comuna": b.get("city") or "", "ciudad": b.get("stateCode") or "",
            "email": cust.get("email") or "", "fuente": "Paris"}


def asegurar_tabla(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS boletas_mkt (
            id            SERIAL PRIMARY KEY,
            tenant_id     INTEGER NOT NULL,
            canal         TEXT NOT NULL,
            clave         TEXT NOT NULL,
            orden         TEXT,
            ordenes       JSONB,
            fecha         TIMESTAMP,
            cliente       TEXT,
            email         TEXT,
            productos_txt TEXT,
            productos     INTEGER,
            envio         INTEGER,
            total         INTEGER,
            total_canal   INTEGER,
            cuadra        BOOLEAN,
            nota          TEXT,
            es_full       BOOLEAN DEFAULT FALSE,
            full_detalle  TEXT,
            emitir        BOOLEAN DEFAULT TRUE,
            estado        TEXT,
            lineas        JSONB,
            ref           JSONB,
            dte_id        INTEGER,
            folio         INTEGER,
            error         TEXT,
            emitida_en    TIMESTAMP,
            emitida_por   TEXT,
            actualizado   TIMESTAMP,
            UNIQUE (tenant_id, canal, clave)
        )""")
    cur.execute("CREATE INDEX IF NOT EXISTS boletas_mkt_fecha ON boletas_mkt (tenant_id, fecha DESC)")


def _ahora():
    from inventario import now_chile
    return now_chile().replace(tzinfo=None)


def _fecha(v):
    """Fecha de la orden como datetime sin zona (hora de Chile cuando el canal
    la manda con zona)."""
    if not v:
        return None
    try:
        if isinstance(v, (int, float)) or str(v).isdigit():
            from inventario import now_chile
            import pytz
            tz = now_chile().tzinfo or pytz.timezone("America/Santiago")
            return datetime.fromtimestamp(int(v) / 1000, tz).replace(tzinfo=None)
        s = str(v).replace("Z", "+00:00")
        d = datetime.fromisoformat(s[:32])
        if d.tzinfo is not None:
            import pytz
            d = d.astimezone(pytz.timezone("America/Santiago")).replace(tzinfo=None)
        return d
    except Exception:  # noqa: BLE001
        try:
            return datetime.strptime(str(v)[:19], "%Y-%m-%d %H:%M:%S")
        except Exception:  # noqa: BLE001
            return None


def _txt_productos(lineas):
    partes = []
    for l in lineas:
        n = (l.get("nombre") or l.get("sku") or "Producto").strip()
        partes.append("%s ×%s" % (n[:60], l.get("cantidad") or 1))
    return " · ".join(partes)[:500]


_RE_CLAVE_RUT = None


def buscar_rut(obj, prof=0):
    """El RUT del cliente dentro de la orden, venga en el campo que venga
    (Falabella NationalRegistrationNumber, Paris documentNumber, MercadoLibre
    doc_number, Woo meta billing_rut...). Solo devuelve un RUT con digito
    verificador valido, como 12345678-K. Ignora lo que sea del vendedor."""
    import re
    from facturacion.utils import validar_rut
    global _RE_CLAVE_RUT
    if _RE_CLAVE_RUT is None:
        _RE_CLAVE_RUT = re.compile(r"rut|document_?number|nationalregistration|doc_number|"
                                   r"national_id|tax_?id|identification|dni", re.I)
    if prof > 6:
        return None
    if isinstance(obj, dict):
        # Woo: meta_data [{key: billing_rut, value}]; MercadoLibre: [{type: DOC_NUMBER, value}]
        _etiqueta = obj.get("key") if "key" in obj else obj.get("type")
        if "value" in obj and _etiqueta and _RE_CLAVE_RUT.search(str(_etiqueta)):
            v = str(obj.get("value") or "").strip()
            if v and validar_rut(v) and len(v.replace(".", "").replace("-", "")) >= 8:
                return _rut_limpio(v)
        for k, v in obj.items():
            if re.search(r"seller|store|shop|merchant|warehouse", str(k), re.I):
                continue
            if isinstance(v, (dict, list)):
                r = buscar_rut(v, prof + 1)
                if r:
                    return r
            elif _RE_CLAVE_RUT.search(str(k)) and v:
                s = str(v).strip()
                if validar_rut(s) and len(s.replace(".", "").replace("-", "")) >= 8:
                    return _rut_limpio(s)
    elif isinstance(obj, list):
        for v in obj[:20]:
            r = buscar_rut(v, prof + 1)
            if r:
                return r
    return None


def _rut_limpio(s):
    s = str(s).replace(".", "").replace(" ", "").upper()
    if "-" not in s:
        s = s[:-1] + "-" + s[-1]
    return s


def _billing_meli(order_id):
    """Nombre y RUT del comprador desde /orders/{id}/billing_info."""
    from mercadolibre import MELI_API_URL, meli_headers
    import requests
    r = requests.get("%s/orders/%s/billing_info" % (MELI_API_URL, order_id),
                     headers=meli_headers(), timeout=15)
    if r.status_code != 200:
        return {}
    bd = r.json() or {}
    base = (bd.get("buyer") or {}).get("billing_info") or bd.get("billing_info") or bd
    datos = {"rut": None, "nombre": "", "empresa": "", "crudo": base}
    nombre, apellido = base.get("first_name") or base.get("name") or "", base.get("last_name") or ""
    for extra in base.get("additional_info") or []:
        if not isinstance(extra, dict):
            continue
        t, v = str(extra.get("type") or "").upper(), str(extra.get("value") or "").strip()
        if t in ("FIRST_NAME", "NAME") and not nombre:
            nombre = v
        elif t == "LAST_NAME" and not apellido:
            apellido = v
        elif t in ("BUSINESS_NAME", "SOCIAL_REASON", "RAZON_SOCIAL"):
            datos["empresa"] = v
    datos["nombre"] = " ".join(x for x in (nombre, apellido) if x)
    datos["rut"] = buscar_rut(base)
    return datos


def _cancelada(*estados):
    return any("cancel" in str(e or "").lower() for e in estados)


# ── Traer ordenes de cada canal ─────────────────────────────────────────
# Cada funcion devuelve una lista de dicts:
#   {clave, orden, ordenes, fecha, cliente, email, calculo, crudas, ref}
# "crudas" son las ordenes tal como las da la API, para detectar Full.

def _traer_falabella(dias):
    from falabella import obtener_ordenes_falabella
    lista = []
    for off in range(0, 1000, 100):
        lote = obtener_ordenes_falabella(dias=dias, limit=100, offset=off) or []
        lista.extend(lote)
        if len(lote) < 100:
            break

    vivas = [o for o in lista if not _cancelada(o.get("Statuses"))]
    items = _en_paralelo(_items_falabella, [str(o.get("OrderId")) for o in vivas])
    salida = []
    for o in vivas:
        its = items.get(str(o.get("OrderId"))) or []
        if its and all(_cancelada(i.get("Status")) for i in its):
            continue
        salida.append(_doc_falabella(o, its))
    return salida


def _items_falabella(order_id):
    from falabella import llamar_api_falabella
    r = llamar_api_falabella("GetOrderItems", method="GET", formato="JSON",
                             params_extra={"OrderId": order_id})
    body = ((r.get("data") or {}).get("SuccessResponse") or {}).get("Body") or {}
    its = (body.get("OrderItems") or {}).get("OrderItem") or []
    return its if isinstance(its, list) else [its]


def _doc_falabella(o, its):
    return {
        "clave": str(o.get("OrderNumber")), "orden": str(o.get("OrderNumber")),
        "ordenes": [str(o.get("OrderNumber"))], "fecha": _fecha(o.get("CreatedAt")),
        "cliente": " ".join(x for x in (o.get("CustomerFirstName"), o.get("CustomerLastName")) if x),
        "email": "", "calculo": montos_falabella(o, its or None), "crudas": [o], "rut": buscar_rut(o),
        "factura": factura_falabella(o),
        "ref": {"order_id": str(o.get("OrderId")),
                "order_item_ids": [str(i.get("OrderItemId")) for i in its if i.get("OrderItemId")]},
    }


def _una_falabella(numero):
    """Una orden de Falabella por el numero que ve el cliente (3...) o el
    interno (11...). Lusync guarda los dos en cada venta (movimientos
    numero_orden / orden_id); si no la tiene, se busca en Falabella."""
    from falabella import llamar_api_falabella
    from inventario import get_conn, release_conn
    order_id = None
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT orden_id::text FROM movimientos
                            WHERE LOWER(COALESCE(canal,'')) LIKE 'falabella%%'
                              AND (numero_orden = %s OR orden_id::text = %s)
                              AND COALESCE(orden_id::text,'') <> ''
                            ORDER BY id DESC LIMIT 1""", (numero, numero))
            r = cur.fetchone()
            order_id = r[0] if r else None
        conn.commit()
    finally:
        release_conn(conn)
    if not order_id and numero.startswith("11"):
        order_id = numero
    if not order_id:
        from returns import _ordenes_falabella_por_tramos
        for estado in ("pending", "ready_to_ship", "shipped", "delivered"):
            for o in _ordenes_falabella_por_tramos(estado, 120):
                if str(o.get("OrderNumber")) == numero:
                    order_id = str(o.get("OrderId"))
                    break
            if order_id:
                break
    if not order_id:
        return []
    res = llamar_api_falabella("GetOrder", params_extra={"OrderId": order_id}, method="GET", formato="JSON")
    body = ((res.get("data") or {}).get("SuccessResponse") or {}).get("Body") or {}
    o = (body.get("Orders") or {}).get("Order") or body.get("Order")
    if isinstance(o, list):
        o = o[0] if o else None
    if not o or _cancelada(o.get("Statuses")):
        return []
    its = _items_falabella(order_id)
    if its and all(_cancelada(i.get("Status")) for i in its):
        return []
    return [_doc_falabella(o, its)]


def _traer_mercadolibre(dias):
    from mercadolibre import obtener_todas_ordenes_meli_rango
    desde = (datetime.utcnow() - timedelta(days=dias)).strftime("%Y-%m-%dT00:00:00.000-04:00")
    ordenes = [o for o in (obtener_todas_ordenes_meli_rango(desde, None, max_paginas=20) or [])
               if o.get("status") in ("paid", "confirmed")]
    return _docs_meli(ordenes)


def _docs_meli(ordenes):
    """Agrupa ordenes de MercadoLibre por pack (una boleta por pack)."""
    from inventario import get_meli_auth
    seller = str((get_meli_auth() or {}).get("user_id") or "")
    packs = {}
    for o in ordenes:
        clave = str(o.get("pack_id") or o.get("id"))
        packs.setdefault(clave, []).append(o)
    for grupo in packs.values():
        grupo.sort(key=lambda o: str(o.get("date_created") or ""))
    facturacion = _en_paralelo(_billing_meli, [str(g[0].get("id")) for g in packs.values()])
    salida = []
    for clave, grupo in packs.items():
        buyer = grupo[0].get("buyer") or {}
        bi = facturacion.get(str(grupo[0].get("id"))) or {}
        salida.append({
            "clave": clave, "orden": clave, "ordenes": [str(o.get("id")) for o in grupo],
            "fecha": _fecha(grupo[0].get("date_created")),
            "cliente": bi.get("empresa") or bi.get("nombre") or
                       " ".join(x for x in (buyer.get("first_name"), buyer.get("last_name")) if x)
                       or buyer.get("nickname") or "",
            "rut": bi.get("rut"), "factura": factura_meli(bi),
            "email": "", "calculo": None, "crudas": grupo, "seller": seller,
            "ref": {"pack_id": str(grupo[0].get("pack_id") or ""),
                    "order_ids": [str(o.get("id")) for o in grupo]},
        })
    return salida


def _una_meli(numero):
    """Una orden o un pack de MercadoLibre. Si la orden es parte de un pack se
    trae el pack completo (la boleta va por pack)."""
    import requests
    from mercadolibre import MELI_API_URL, meli_headers
    h = meli_headers()
    ids = []
    r = requests.get("%s/orders/%s" % (MELI_API_URL, numero), headers=h, timeout=20)
    if r.status_code == 200:
        o = r.json() or {}
        pack = o.get("pack_id")
        if not pack:
            ids = [str(o.get("id"))]
        else:
            numero = str(pack)
    if not ids:
        rp = requests.get("%s/packs/%s" % (MELI_API_URL, numero), headers=h, timeout=20)
        if rp.status_code == 200:
            ids = [str(x.get("id")) for x in ((rp.json() or {}).get("orders") or []) if x.get("id")]
    ordenes = []
    for i in ids:
        ro = requests.get("%s/orders/%s" % (MELI_API_URL, i), headers=h, timeout=20)
        if ro.status_code == 200 and (ro.json() or {}).get("status") in ("paid", "confirmed"):
            ordenes.append(ro.json())
    return _docs_meli(ordenes) if ordenes else []


def _traer_paris(dias):
    from paris import obtener_ordenes_paris_todas, obtener_orden_paris
    lista = obtener_ordenes_paris_todas(dias=dias) or []
    por_so = {}
    for o in lista:
        so = str(o.get("subOrderNumber") or o.get("orderNumber") or "")
        if so and so not in por_so:
            por_so[so] = o
    det = _en_paralelo(obtener_orden_paris, list(por_so.keys()))
    salida = []
    for so, o in por_so.items():
        full = {**o, **(det.get(so) or {})}
        if _cancelada(full.get("status"), full.get("statusName")):
            continue
        salida.append(_doc_paris(so, full))
    return salida


def _doc_paris(so, full):
    cust = full.get("customer") or {}
    return {
        "clave": so, "orden": so, "ordenes": [so],
        "fecha": _fecha(full.get("createdAt") or full.get("originOrderDate")),
        "cliente": " ".join(x for x in (cust.get("firstName") or cust.get("name"),) if x),
        "email": cust.get("email") or "", "calculo": POR_CANAL["paris"](full),
        "crudas": [full], "rut": buscar_rut(full), "factura": factura_paris(full),
        "ref": {"sub_order": so, "order": str(full.get("orderNumber") or "")},
    }


def _una_paris(numero):
    """Una sub-orden de Paris por su numero (o la orden madre, que puede
    tener varias sub-ordenes)."""
    from paris import obtener_orden_paris, obtener_ordenes_paris_todas
    det = obtener_orden_paris(numero)
    if det:
        so = str(det.get("subOrderNumber") or numero)
        return [] if _cancelada(det.get("status"), det.get("statusName")) else [_doc_paris(so, det)]
    docs = []
    for o in (obtener_ordenes_paris_todas(dias=90) or []):
        if numero in (str(o.get("orderNumber") or ""), str(o.get("subOrderNumber") or "")):
            so = str(o.get("subOrderNumber") or o.get("orderNumber"))
            full = {**o, **(obtener_orden_paris(so) or {})}
            if not _cancelada(full.get("status"), full.get("statusName")):
                docs.append(_doc_paris(so, full))
    return docs


def _traer_ripley(dias):
    from ripley import obtener_ordenes_ripley
    salida = []
    for o in (obtener_ordenes_ripley(dias=dias, max_resultados=500) or []):
        # Mirakl: CLOSED es una venta terminada (si va); STAGING / WAITING_* aun
        # no estan aceptadas ni pagadas, y REFUSED no se vendio.
        if _cancelada(o.get("order_state")) or str(o.get("order_state")) in (
                "REFUSED", "STAGING", "WAITING_ACCEPTANCE", "WAITING_DEBIT", "WAITING_DEBIT_PAYMENT"):
            continue
        salida.append(_doc_ripley(o))
    return salida


def _doc_ripley(o):
    cli = o.get("customer") or {}
    oid = str(o.get("order_id") or o.get("commercial_id") or "")
    return {
        "clave": oid, "orden": str(o.get("commercial_id") or oid), "ordenes": [oid],
        "fecha": _fecha(o.get("created_date")),
        "cliente": " ".join(x for x in (cli.get("firstname"), cli.get("lastname")) if x),
        "email": cli.get("email") or "", "calculo": POR_CANAL["ripley"](o),
        "crudas": [o], "rut": buscar_rut(o), "ref": {"order_id": oid},
    }


def _una_ripley(numero):
    """Una orden de Ripley por su numero (25356767901-A) o el numero comercial
    (25356767901, que puede traer varias)."""
    import requests
    from ripley import RIPLEY_BASE_URL, ripley_headers
    base = RIPLEY_BASE_URL.rstrip("/") + "/api/orders"
    r = requests.get(base, headers=ripley_headers(), params={"order_ids": numero}, timeout=30)
    ords = (r.json() or {}).get("orders") or [] if r.status_code == 200 else []
    if not ords:
        r = requests.get(base, headers=ripley_headers(), params={"commercial_ids": numero}, timeout=30)
        ords = (r.json() or {}).get("orders") or [] if r.status_code == 200 else []
    return [_doc_ripley(o) for o in ords
            if not _cancelada(o.get("order_state"))
            and str(o.get("order_state")) not in ("REFUSED", "STAGING", "WAITING_ACCEPTANCE",
                                                  "WAITING_DEBIT", "WAITING_DEBIT_PAYMENT")]


def _traer_walmart(dias):
    from walmart import obtener_ordenes_walmart
    salida = []
    for o in (obtener_ordenes_walmart(dias=dias, max_paginas=20) or []):
        lineas = (o.get("orderLines") or {}).get("orderLine") or []
        lineas = lineas if isinstance(lineas, list) else [lineas]
        estados = []
        for ln in lineas:
            sts = (ln.get("orderLineStatuses") or {}).get("orderLineStatus") or []
            sts = sts if isinstance(sts, list) else [sts]
            estados += [s.get("status") for s in sts]
        if estados and all(_cancelada(e) for e in estados):
            continue
        salida.append(_doc_walmart(o))
    return salida


def _walmart_cancelada(o):
    lineas = (o.get("orderLines") or {}).get("orderLine") or []
    lineas = lineas if isinstance(lineas, list) else [lineas]
    estados = []
    for ln in lineas:
        sts = (ln.get("orderLineStatuses") or {}).get("orderLineStatus") or []
        sts = sts if isinstance(sts, list) else [sts]
        estados += [st.get("status") for st in sts]
    return bool(estados) and all(_cancelada(e) for e in estados)


def _doc_walmart(o):
    po = str(o.get("purchaseOrderId") or o.get("customerOrderId") or "")
    nombre = ((o.get("shippingInfo") or {}).get("postalAddress") or {}).get("name") or ""
    return {
        "clave": po, "orden": str(o.get("customerOrderId") or po), "ordenes": [po],
        "fecha": _fecha(o.get("orderDate")), "cliente": nombre,
        "email": o.get("customerEmailId") or "", "calculo": POR_CANAL["walmart"](o),
        "crudas": [o], "ref": {"purchase_order_id": po,
                               "customer_order_id": str(o.get("customerOrderId") or ""),
                               "ship_node_type": o.get("shipNodeType") or "",
                               "no_full_por": confirmacion_no_full("walmart", o)},
    }


def _una_walmart(numero):
    """Las partes de una compra de Walmart por customerOrderId (o una parte por
    purchaseOrderId). Se piden por tipo de despacho y se estampa shipNodeType,
    igual que el listado normal: es lo que dice si una parte es Full."""
    import requests
    from walmart import WALMART_BASE_URL, walmart_headers
    desde = (datetime.utcnow() - timedelta(days=180)).strftime("%Y-%m-%d")
    docs, vistos = [], set()
    for nodo in ("SellerFulfilled", "WFSFulfilled"):
        for campo in ("customerOrderId", "purchaseOrderId"):
            r = requests.get(WALMART_BASE_URL + "/v3/orders", headers=walmart_headers(), timeout=25,
                             params={campo: numero, "shipNodeType": nodo, "createdStartDate": desde,
                                     "limit": 20, "productInfo": "false"})
            if r.status_code != 200:
                continue
            ords = ((r.json() or {}).get("list") or {}).get("elements", {}).get("order") or []
            ords = ords if isinstance(ords, list) else [ords]
            for o in ords:
                # Las partes Full (WFS) vienen sin purchaseOrderId: se identifican
                # por la compra, igual que en el listado normal (_doc_walmart).
                po = str(o.get("purchaseOrderId") or o.get("customerOrderId") or "")
                if not po or po in vistos:
                    continue
                vistos.add(po)
                if not o.get("shipNodeType"):
                    o["shipNodeType"] = nodo
                if not _walmart_cancelada(o):
                    docs.append(_doc_walmart(o))
            if ords:
                break
    # Respaldo: el filtro por customerOrderId no siempre devuelve todas las partes de la
    # compra (8572652000134: devolvia la Full y no la propia). Con la fecha de
    # una parte ya encontrada se piden las ordenes de ese dia y se juntan las
    # de la misma compra.
    if docs:
        compra = (docs[0]["ref"] or {}).get("customer_order_id") or numero
        f = docs[0]["crudas"][0].get("orderDate")
        try:
            dia = datetime.utcfromtimestamp(int(f) / 1000) if str(f).isdigit() else                 datetime.fromisoformat(str(f).replace("Z", "+00:00")[:19])
        except Exception:  # noqa: BLE001
            dia = None
        if dia:
            for nodo in ("SellerFulfilled", "WFSFulfilled"):
                r = requests.get(WALMART_BASE_URL + "/v3/orders", headers=walmart_headers(), timeout=25,
                                 params={"shipNodeType": nodo, "limit": 200, "productInfo": "false",
                                         "createdStartDate": (dia - timedelta(days=1)).strftime("%Y-%m-%d"),
                                         "createdEndDate": (dia + timedelta(days=1)).strftime("%Y-%m-%d")})
                if r.status_code != 200:
                    continue
                ords = ((r.json() or {}).get("list") or {}).get("elements", {}).get("order") or []
                ords = ords if isinstance(ords, list) else [ords]
                for o in ords:
                    po = str(o.get("purchaseOrderId") or o.get("customerOrderId") or "")
                    if str(o.get("customerOrderId") or "") != str(compra) or not po or po in vistos:
                        continue
                    vistos.add(po)
                    if not o.get("shipNodeType"):
                        o["shipNodeType"] = nodo
                    if not _walmart_cancelada(o):
                        docs.append(_doc_walmart(o))
    return docs


def _traer_web(dias):
    import requests
    from woo import _woo_api
    from credenciales_canal import credencial
    salida, pagina = [], 1
    desde = (datetime.utcnow() - timedelta(days=dias)).strftime("%Y-%m-%dT00:00:00")
    while pagina <= 20:
        r = requests.get(_woo_api() + "/orders", timeout=25, params={
            "consumer_key": credencial("web", "consumer_key"),
            "consumer_secret": credencial("web", "consumer_secret"),
            "status": "processing,completed", "per_page": 100, "page": pagina, "after": desde})
        if r.status_code != 200:
            raise RuntimeError("WooCommerce respondio %s" % r.status_code)
        lote = r.json() or []
        for o in lote:
            salida.append(_doc_web(o))
        if len(lote) < 100:
            break
        pagina += 1
    return salida


def _doc_web(o):
    b = o.get("billing") or {}
    num = str(o.get("number") or o.get("id"))
    return {
        "clave": num, "orden": num, "ordenes": [num], "fecha": _fecha(o.get("date_created")),
        "cliente": " ".join(x for x in (b.get("first_name"), b.get("last_name")) if x),
        "email": b.get("email") or "", "calculo": POR_CANAL["web"](o),
        "crudas": [o], "rut": buscar_rut(o), "ref": {"order_id": str(o.get("id"))},
    }


def _una_web(numero):
    import requests
    from woo import _woo_api
    from credenciales_canal import credencial
    auth = {"consumer_key": credencial("web", "consumer_key"),
            "consumer_secret": credencial("web", "consumer_secret")}
    r = requests.get(_woo_api() + "/orders/" + str(numero), params=auth, timeout=25)
    if r.status_code == 200 and (r.json() or {}).get("status") in ("processing", "completed"):
        return [_doc_web(r.json())]
    return []


TRAER_UNA = {"falabella": _una_falabella, "mercadolibre": _una_meli, "paris": _una_paris,
             "ripley": _una_ripley, "walmart": _una_walmart, "web": _una_web}


TRAER = {"falabella": _traer_falabella, "mercadolibre": _traer_mercadolibre,
         "paris": _traer_paris, "ripley": _traer_ripley, "walmart": _traer_walmart,
         "web": _traer_web}


def _en_paralelo(fn, claves, hilos=8):
    if not claves:
        return {}
    from concurrent.futures import ThreadPoolExecutor
    from inventario import con_tenant_del_llamador

    def _seguro(c):
        try:
            return fn(c)
        except Exception as e:  # noqa: BLE001
            logger.warning("boletas_mkt: detalle %s fallo: %s", c, str(e)[:120])
            return None

    with ThreadPoolExecutor(max_workers=min(hilos, len(claves))) as pool:
        res = list(pool.map(con_tenant_del_llamador(_seguro), claves))
    return dict(zip(claves, res))


# ── Full ────────────────────────────────────────────────────────────────

def _full_de(tenant_id, canal, doc):
    """(ids_full, detalle, desconocidas). Dos fuentes: el detector del canal
    sobre la orden cruda y la bodega de la venta en Lusync. Basta una para
    que sea Full. Si ninguna dice nada y la venta no esta en Lusync, no se
    sabe (y no se emite)."""
    from bodegas_logic import detectar_fulfillment
    from facturacion.ordenes_full import estado_full_orden
    ids_full, detalles, desconocidas = [], [], []
    for cruda, oid in zip(doc["crudas"], doc["ordenes"]):
        try:
            if detectar_fulfillment(canal, cruda):
                ids_full.append(oid)
                detalles.append("%s es Full según %s" % (oid, canal))
                continue
        except Exception:  # noqa: BLE001
            pass
        if confirmacion_no_full(canal, cruda):
            continue  # el marketplace dice explicitamente que la despachamos nosotros
        es, det = estado_full_orden(tenant_id, oid, canal)
        if es:
            ids_full.append(oid)
            detalles.append("%s: %s" % (oid, det))
        elif es is None and canal != "web":
            desconocidas.append(oid)
    return ids_full, "; ".join(detalles), desconocidas


# ── Sincronizar ─────────────────────────────────────────────────────────

import threading

_CANDADOS = {}
_CANDADOS_LOCK = threading.Lock()


def sincronizar_canal(tenant_id, canal, dias=7):
    """Trae las ordenes del canal y las guarda/actualiza. Nunca toca una fila
    ya emitida, y respeta lo que la persona decidio (emitir si/no)."""
    with _CANDADOS_LOCK:
        candado = _CANDADOS.setdefault((tenant_id, canal), threading.Lock())
    if not candado.acquire(blocking=False):
        return {"canal": canal, "ordenes": 0, "nuevas": 0, "actualizadas": 0,
                "ocupado": "ya se estaba trayendo"}
    try:
        return _sincronizar_canal(tenant_id, canal, dias)
    finally:
        candado.release()


def traer_orden(tenant_id, canal, numero):
    """Trae UNA orden puntual (por ejemplo, una mas antigua que lo que trae
    el boton) y la deja en la lista con los mismos controles."""
    canal = (canal or "").strip().lower()
    numero = str(numero or "").strip()
    if canal not in TRAER_UNA:
        return {"ok": False, "error": "Canal no válido"}
    if not numero:
        return {"ok": False, "error": "Escribe el número de la orden"}
    docs = TRAER_UNA[canal](numero)
    if not docs:
        return {"ok": False, "error": "No encontré la orden %s en %s (o está cancelada)" % (numero, canal)}
    res = _sincronizar_canal(tenant_id, canal, 0, docs=docs)
    return {"ok": True, **res, "numeros": [d["orden"] for d in docs]}


def _sincronizar_canal(tenant_id, canal, dias, docs=None):
    from inventario import get_conn, release_conn
    if docs is None:
        docs = TRAER[canal](dias)
    nuevos = actualizados = 0
    conn = get_conn(tenant_id=tenant_id, is_admin=True)
    try:
        with conn.cursor() as cur:
            asegurar_tabla(cur)
            from facturacion.carga_mkt import asegurar_columnas
            asegurar_columnas(cur)
            cur.execute("""SELECT clave, estado, emitir, factura FROM boletas_mkt
                            WHERE tenant_id=%s AND canal=%s""", (tenant_id, canal))
            previas = {r[0]: (r[1], r[2], r[3]) for r in cur.fetchall()}
            for d in docs:
                prev = previas.get(d["clave"])
                if prev and prev[0] == "emitida":
                    continue
                ids_full, det_full, desconocidas = _full_de(tenant_id, canal, d)
                es_full = bool(ids_full) and len(ids_full) == len(d["ordenes"])
                if canal == "mercadolibre":
                    # Pack: solo ordenes nuestras y no Full
                    no_full = [c for c, oid in zip(d["crudas"], d["ordenes"]) if oid not in ids_full]
                    calc = montos_meli_pack(no_full or d["crudas"], seller_id=d.get("seller") or None)
                    if no_full and ids_full:
                        calc["nota"] = ((calc.get("nota") or "") +
                                        "; parte del pack es Full: solo se suma lo nuestro").strip("; ")
                    if no_full:
                        d["ordenes"] = calc.get("ordenes") or d["ordenes"]
                    desconocidas = [o for o in desconocidas if o in d["ordenes"]]
                else:
                    calc = d["calculo"]
                nota = calc.get("nota") or ""
                emitir = prev[1] if prev else True
                if es_full:
                    estado = "full"
                elif desconocidas:
                    estado = "revisar"
                    nota = ("La venta %s no está en Lusync: no puedo confirmar que no sea Full. "
                            % ", ".join(desconocidas) + nota).strip()
                elif calc.get("cuadra") is False or (calc.get("total") or 0) <= 0:
                    estado = "revisar"
                    nota = ("El cálculo (%s) no cuadra con el total del marketplace (%s). "
                            % (calc.get("total"), calc.get("total_canal")) + nota).strip()
                else:
                    estado = "por_emitir" if emitir else "no_emitir"
                # Factura: los datos que la persona completo a mano mandan sobre los
                # del marketplace (que suelen venir sin giro).
                factura = d.get("factura")
                if factura and prev and prev[2]:
                    factura = {**factura, **{k: v for k, v in (prev[2] or {}).items() if v}}
                if factura and estado == "por_emitir" and not _factura_completa(factura):
                    estado = "faltan_datos"
                if prev and prev[0] == "error" and estado == "por_emitir":
                    estado = "error"  # sigue a la vista hasta que se reintente
                cur.execute("""
                    INSERT INTO boletas_mkt (tenant_id, canal, clave, orden, ordenes, fecha, cliente,
                        email, productos_txt, productos, envio, total, total_canal, cuadra, nota,
                        es_full, full_detalle, emitir, estado, lineas, ref, actualizado, rut_cliente,
                        factura)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (tenant_id, canal, clave) DO UPDATE SET
                        orden=EXCLUDED.orden, ordenes=EXCLUDED.ordenes, fecha=EXCLUDED.fecha,
                        cliente=EXCLUDED.cliente, email=EXCLUDED.email,
                        productos_txt=EXCLUDED.productos_txt, productos=EXCLUDED.productos,
                        envio=EXCLUDED.envio, total=EXCLUDED.total, total_canal=EXCLUDED.total_canal,
                        cuadra=EXCLUDED.cuadra, nota=EXCLUDED.nota, es_full=EXCLUDED.es_full,
                        full_detalle=EXCLUDED.full_detalle, estado=EXCLUDED.estado,
                        lineas=EXCLUDED.lineas, ref=EXCLUDED.ref, actualizado=EXCLUDED.actualizado,
                        rut_cliente=EXCLUDED.rut_cliente, factura=EXCLUDED.factura
                    WHERE boletas_mkt.estado <> 'emitida'
                """, (tenant_id, canal, d["clave"], d["orden"], json.dumps(d["ordenes"]), d["fecha"],
                      (d.get("cliente") or "")[:120], (d.get("email") or "")[:200],
                      _txt_productos(calc.get("lineas") or []), calc.get("productos"), calc.get("envio"),
                      calc.get("total"), calc.get("total_canal"), calc.get("cuadra"), nota[:500],
                      es_full, det_full[:300], emitir, estado, json.dumps(calc.get("lineas") or []),
                      json.dumps(d.get("ref") or {}), _ahora(), d.get("rut"),
                      json.dumps(factura) if factura else None))
                if prev:
                    actualizados += 1
                else:
                    nuevos += 1
        conn.commit()
    finally:
        release_conn(conn)
    return {"canal": canal, "ordenes": len(docs), "nuevas": nuevos, "actualizadas": actualizados}


# ── Emitir ──────────────────────────────────────────────────────────────

def items_boleta(fila):
    """Las lineas de la boleta: productos (con descuentos ya restados) y el
    despacho como linea aparte. Si un total no se divide exacto por la
    cantidad, la linea va como 1 unidad con la cantidad en el nombre, para que
    la boleta sume al peso lo que pago el cliente."""
    items = []
    for l in fila["lineas"] or []:
        total = int(round(l.get("total") or 0))
        if total <= 0:
            continue
        qty = int(l.get("cantidad") or 1)
        nombre = (l.get("nombre") or l.get("sku") or "Producto").strip()[:70]
        if qty > 1 and total % qty == 0:
            items.append({"nombre": nombre, "cantidad": qty, "precio_unitario": total // qty,
                          "exento": False})
        else:
            items.append({"nombre": nombre + (" (x%s)" % qty if qty > 1 else ""), "cantidad": 1,
                          "precio_unitario": total, "exento": False})
    if (fila["envio"] or 0) > 0:
        items.append({"nombre": "Despacho", "cantidad": 1, "precio_unitario": int(fila["envio"]),
                      "exento": False})
    return items


def items_factura(fila):
    """Las lineas de la factura en NETO, calculadas para que neto + IVA de
    exactamente lo que pago el cliente. La factura se arma en neto y el IVA se
    calcula sobre el total; con precios redondeados linea a linea el total
    podria quedar 1 peso arriba o abajo."""
    from facturacion.dtes.factura import IVA_PORCENTAJE, _redondear_clp
    brutas = items_boleta(fila)
    total = sum(i["precio_unitario"] * i["cantidad"] for i in brutas)
    base = _redondear_clp(total * 100.0 / (100 + IVA_PORCENTAJE))
    neto = None
    for n in sorted(range(base - 4, base + 5), key=lambda x: abs(x - base)):
        if n + _redondear_clp(n * IVA_PORCENTAJE / 100.0) == total:
            neto = n
            break
    if neto is None:
        return None
    items, restante = [], neto
    for idx, it in enumerate(brutas):
        linea_bruta = it["precio_unitario"] * it["cantidad"]
        parte = restante if idx == len(brutas) - 1 else _redondear_clp(neto * linea_bruta / total)
        restante -= parte
        nombre = it["nombre"] + (" (x%s)" % it["cantidad"] if it["cantidad"] > 1 else "")
        items.append({"nombre": nombre[:80], "cantidad": 1, "precio_unitario": parte, "exento": False})
    return items


def guardar_datos_factura(tenant_id, fila_id, datos):
    """Completa a mano los datos de la factura (giro, direccion...)."""
    from inventario import get_conn, release_conn
    limpio = {k: str(datos.get(k) or "").strip()[:100] for k in CAMPOS_FACTURA + ("ciudad", "email")}
    if limpio.get("rut"):
        limpio["rut"] = _rut_limpio(limpio["rut"])
    conn = get_conn(tenant_id=tenant_id, is_admin=True)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT factura, estado FROM boletas_mkt WHERE tenant_id=%s AND id=%s",
                        (tenant_id, fila_id))
            r = cur.fetchone()
            if not r:
                return {"ok": False, "error": "No existe esa orden"}
            if r[1] not in ("faltan_datos", "por_emitir", "error", "no_emitir"):
                return {"ok": False, "error": "Esta orden ya no se puede cambiar (%s)" % r[1]}
            f = {**(r[0] or {}), **{k: v for k, v in limpio.items() if v}}
            completa = _factura_completa(f)
            nuevo = r[1]
            if r[1] == "faltan_datos" and completa:
                nuevo = "por_emitir"
            elif r[1] == "por_emitir" and not completa:
                nuevo = "faltan_datos"
            cur.execute("UPDATE boletas_mkt SET factura=%s, estado=%s, actualizado=%s "
                        "WHERE tenant_id=%s AND id=%s", (json.dumps(f), nuevo, _ahora(), tenant_id, fila_id))
        conn.commit()
    finally:
        release_conn(conn)
    faltan = [c for c in CAMPOS_FACTURA if not str(f.get(c) or "").strip()]
    return {"ok": True, "completa": completa, "faltan": faltan, "estado": nuevo}


def leer_fila(cur, tenant_id, fila_id):
    cur.execute("""SELECT id, canal, clave, orden, ordenes, estado, emitir, es_full, cuadra,
                          lineas, envio, total, dte_id, folio, ref, cliente, rut_cliente, factura
                     FROM boletas_mkt WHERE tenant_id=%s AND id=%s""", (tenant_id, fila_id))
    r = cur.fetchone()
    if not r:
        return None
    return {"id": r[0], "canal": r[1], "clave": r[2], "orden": r[3], "ordenes": r[4] or [],
            "estado": r[5], "emitir": r[6], "es_full": r[7], "cuadra": r[8],
            "lineas": r[9] or [], "envio": r[10] or 0, "total": r[11] or 0,
            "dte_id": r[12], "folio": r[13], "ref": r[14] or {},
            "cliente": r[15] or "", "rut_cliente": r[16] or "", "factura": r[17]}


def emitir_fila(tenant_id, fila_id, usuario, actualizar_estado_fn, emitir_factura_fn=None):
    """Emite la boleta (o la factura, si el cliente la pidio) de UNA fila, con
    todos los controles. emitir_factura_fn(body) -> dict llama a la emision de
    facturas de la app. Devuelve dict."""
    from inventario import get_conn, release_conn
    from facturacion.emision_core import emitir_boleta_core
    from facturacion.ordenes_full import bloqueo_full

    conn = get_conn(tenant_id=tenant_id, is_admin=True)
    try:
        with conn.cursor() as cur:
            asegurar_tabla(cur)
            # Bloqueo de la fila mientras se emite: dos clics no hacen dos boletas
            cur.execute("""UPDATE boletas_mkt SET estado='emitiendo'
                            WHERE tenant_id=%s AND id=%s AND estado IN ('por_emitir','error')
                              AND emitir AND NOT es_full
                        RETURNING id""", (tenant_id, fila_id))
            tomada = cur.fetchone()
            conn.commit()
            fila = leer_fila(cur, tenant_id, fila_id)
        if not fila:
            return {"ok": False, "error": "No existe esa orden"}
        if not tomada:
            motivo = {"emitida": "Ya tiene la boleta N°%s" % fila.get("folio"),
                      "full": "Orden Full: la boleta la emite el marketplace",
                      "revisar": "Los montos no cuadran: revisa la orden antes de emitir",
                      "no_emitir": "Está marcada para no emitir",
                      "emitiendo": "Ya se está emitiendo"}.get(fila["estado"], "No se puede emitir")
            return {"ok": False, "error": motivo}

        def _volver(estado, error=None, **extra):
            with conn.cursor() as cur2:
                sets = ["estado=%s", "error=%s", "actualizado=%s"]
                vals = [estado, (error or "")[:500], _ahora()]
                for k, v in extra.items():
                    sets.append("%s=%%s" % k)
                    vals.append(v)
                cur2.execute("UPDATE boletas_mkt SET " + ", ".join(sets) +
                             " WHERE tenant_id=%s AND id=%s", vals + [tenant_id, fila_id])
            conn.commit()

        # Cada orden del documento se revisa por Full (un pack puede tener varias)
        no_full_por = (fila["ref"] or {}).get("no_full_por") if fila["canal"] == "walmart" else None
        for oid in fila["ordenes"]:
            bloq = bloqueo_full(tenant_id, oid, fila["canal"], no_full_por)
            if bloq:
                _volver("full" if "es Full" in bloq else "revisar", bloq)
                return {"ok": False, "error": bloq}

        items = items_boleta(fila)
        suma = sum(i["precio_unitario"] * i["cantidad"] for i in items)
        if not items or suma != int(fila["total"]):
            err = "Las líneas suman %s y el cliente pagó %s: no se emite" % (suma, fila["total"])
            _volver("revisar", err)
            return {"ok": False, "error": err}

        # El cliente pidio factura: se emite factura con sus datos, nunca boleta
        if fila.get("factura"):
            f = fila["factura"]
            if not _factura_completa(f):
                _volver("faltan_datos", "Faltan: " + ", ".join(
                    c for c in CAMPOS_FACTURA if not str(f.get(c) or "").strip()))
                return {"ok": False, "error": "Faltan datos de la factura"}
            items_f = items_factura(fila)
            if not items_f or not emitir_factura_fn:
                _volver("revisar", "No pude armar una factura que sume al peso %s" % fila["total"])
                return {"ok": False, "error": "No pude armar la factura al peso"}
            orden_doc = fila["ordenes"][0] if fila["ordenes"] else fila["clave"]
            res = emitir_factura_fn({
                "items": items_f, "forma_pago": 1, "orden_id": orden_doc, "canal": fila["canal"],
                "receptor": {k: f.get(k) for k in CAMPOS_FACTURA + ("ciudad", "email")}})
            if res.get("ok") and int(res.get("total") or 0) != int(fila["total"]):
                logger.error("boletas_mkt: factura %s total %s != pagado %s",
                             res.get("folio"), res.get("total"), fila["total"])
            doc_id = res.get("fac_id") or res.get("boleta_id")
            if res.get("ok") or doc_id:
                _volver("emitida", None if res.get("ok") else res.get("error"), dte_id=doc_id,
                        folio=res.get("folio"), emitida_en=_ahora(), emitida_por=(usuario or "")[:80])
                carga_ok, carga_det = False, ""
                if res.get("ok"):
                    try:
                        from facturacion.carga_mkt import cargar_fila
                        carga_ok, carga_det = cargar_fila(tenant_id, fila_id)
                    except Exception as e:  # noqa: BLE001
                        carga_det = str(e)[:200]
                    return {"ok": True, "folio": res.get("folio"), "boleta_id": doc_id, "factura": True,
                            "total": res.get("total"), "carga_ok": carga_ok, "carga": carga_det}
                return {"ok": False, "folio": res.get("folio"),
                        "error": "Factura N°%s generada pero el envío al SII falló: %s. Reintenta el "
                                 "envío desde Facturación; no la emitas de nuevo." % (res.get("folio"),
                                                                                      res.get("error"))}
            _volver("full" if res.get("es_full") else "error", res.get("error"))
            return {"ok": False, "error": res.get("error") or "No se pudo emitir la factura"}

        # La orden que queda en la boleta es la que usan las devoluciones para
        # encontrarla: Walmart guarda la compra (customerOrderId), no la parte.
        orden_doc = fila["ordenes"][0] if fila["ordenes"] else fila["clave"]
        if fila["canal"] == "walmart":
            orden_doc = (fila["ref"] or {}).get("customer_order_id") or orden_doc
        # El cliente real en la boleta: su nombre y, si el canal lo entrega, su RUT.
        # Sin RUT valido va el generico 66.666.666-6, pero con el nombre del cliente.
        receptor = {"rut": fila["rut_cliente"] or "66666666-6",
                    "razon_social": (fila["cliente"] or "Consumidor final").strip()[:100]}
        res = emitir_boleta_core(tenant_id=tenant_id, items=items, receptor=receptor,
                                 actualizar_estado_fn=actualizar_estado_fn,
                                 orden_id=orden_doc, canal=fila["canal"], no_full_por=no_full_por)
        if res.get("ok"):
            _volver("emitida", None, dte_id=res.get("boleta_id"), folio=res.get("folio"),
                    emitida_en=_ahora(), emitida_por=(usuario or "")[:80])
            # Se carga altiro en el marketplace (o se envia por correo). Si falla,
            # el job periodico lo reintenta.
            try:
                from facturacion.carga_mkt import cargar_fila
                carga_ok, carga_det = cargar_fila(tenant_id, fila_id)
            except Exception as e:  # noqa: BLE001
                carga_ok, carga_det = False, str(e)[:200]
            return {"ok": True, "folio": res.get("folio"), "boleta_id": res.get("boleta_id"),
                    "total": res.get("total"), "pdf_url": res.get("pdf_url"),
                    "carga_ok": carga_ok, "carga": carga_det}
        if res.get("boleta_id"):
            # Se reservo folio y se registro el documento, pero el envio fallo: queda
            # anotado para no emitir otra boleta encima. Se reintenta desde Facturacion.
            _volver("emitida", res.get("error"), dte_id=res.get("boleta_id"), folio=res.get("folio"),
                    emitida_en=_ahora(), emitida_por=(usuario or "")[:80])
            return {"ok": False, "folio": res.get("folio"),
                    "error": "Boleta N°%s generada pero el envío al SII falló: %s. Reintenta el "
                             "envío desde Facturación; no la emitas de nuevo." % (res.get("folio"),
                                                                                  res.get("error"))}
        _volver("full" if res.get("es_full") else "error", res.get("error"))
        return {"ok": False, "error": res.get("error") or "No se pudo emitir"}
    finally:
        release_conn(conn)
