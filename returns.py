"""
returns.py — Trazabilidad automática de devoluciones desde las APIs de los marketplaces.

Cada canal expone datos distintos; aquí se traen y se NORMALIZAN a un formato común
que se guarda en la tabla devoluciones_marketplace (ver init_devoluciones_mkt en inventario.py).

Canales por PULL (se consultan periódicamente):
  - MercadoLibre : /post-purchase/v1/claims/search + /v2/claims/{id}/returns
  - Walmart      : GET /v3/returns (Global API, funciona en Chile)
  - Ripley       : Mirakl /api/returns
  - Paris        : Cencosud /v2/returns

Canal por PUSH (llega por webhook, se procesa en el endpoint /falabella/webhook):
  - Falabella    : evento onReturnStatusChanged

Formato normalizado (dict) que produce cada función de canal:
  canal, return_id, claim_id, order_id, sku, sku_canal, producto_nombre, cantidad,
  estado, estado_canal, motivo, tipo, monto_reembolso, moneda,
  tracking_number, transportista, fecha_solicitud, fecha_limite,
  fecha_resolucion, fecha_actualizacion_canal, acciones_disponibles (list), raw
"""

import json
import requests
from datetime import datetime, timedelta

from inventario import (
    get_conn, release_conn, now_chile, obtener_sku_lusync_por_canal
)


# ─────────────────────────────────────────────────────────────────────────────
# Utilidades de parseo de fechas (cada API usa formatos distintos)
# ─────────────────────────────────────────────────────────────────────────────
def _parse_fecha(valor):
    """Convierte distintos formatos ISO a datetime naive (sin tz). None si no se puede."""
    if not valor:
        return None
    if isinstance(valor, (int, float)):
        # epoch ms o s
        try:
            v = float(valor)
            if v > 1e12:  # ms
                v = v / 1000.0
            return datetime.utcfromtimestamp(v)
        except Exception:
            return None
    s = str(valor).strip()
    if not s:
        return None
    # Normalizar Z y offsets
    s = s.replace("Z", "+0000")
    formatos = [
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ]
    for fmt in formatos:
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is not None:
                dt = dt.replace(tzinfo=None) - dt.utcoffset()
            return dt
        except Exception:
            continue
    return None


def _link_gestion(canal, order_id, return_id, claim_id=None):
    """Genera el link directo al panel del marketplace para gestionar la devolución.
    Lleva al vendedor a la pantalla donde puede responder/apelar en cada MKT.
    """
    try:
        if canal == "mercadolibre" and claim_id:
            # Centro de reclamos/ventas de ML
            return f"https://www.mercadolibre.cl/ventas/{order_id}/detalle" if order_id else \
                   f"https://myaccount.mercadolibre.cl/sales/claims/{claim_id}"
        if canal == "walmart":
            return "https://seller.walmart.com/returns-overview"
        if canal == "paris":
            return "https://sellercenter.paris.cl/returns"
        if canal == "ripley":
            return "https://mirakl.ripley.cl/mmp/shop/returns"
        if canal == "falabella":
            return "https://sellercenter.falabella.com/return/index"
    except Exception:
        pass
    return None


# Traducción de estados crudos de cada canal → etiqueta legible en español.
# Se muestra el estado tal cual lo reporta el marketplace, pero legible.
# Basado en la documentación oficial de cada canal + datos reales.
ESTADO_CANAL_LABEL = {
    "mercadolibre": {
        "opened": "Reclamo abierto",
        "closed": "Cerrado",
        "cancelled": "Cancelado",
        "delivered": "Producto entregado",
        "shipped": "En camino",
        "ready_to_ship": "Listo para enviar",
        "in_mediation": "En mediación ML",
        "dispute": "En disputa",
    },
    "walmart": {
        "RETURN_INITIATED": "Devolución iniciada",
        "RETURN_SHIPPED": "En camino a bodega",
        "RETURN_DELIVERED": "Recibido",
        "RETURN_COMPLETED": "Reembolso completado",
        "RETURN_CANCELLED": "Cancelada",
        "INITIATED": "Iniciada",
        "COMPLETED": "Completada",
        "CANCELLED": "Cancelada",
    },
    "paris": {
        "request_accepted": "Solicitud aceptada",
        "auto_accepted": "Aceptada automáticamente",
        "review_accepted": "Revisión aceptada",
        "review_rejected": "Revisión rechazada",
        "return_rejected": "Devolución rechazada",
        "in_review": "En revisión",
        "shipped": "En camino",
        "store_received": "Recibido en tienda",
        "received": "Recibido",
        "finalized": "Finalizada",
        "refunded": "Reembolsada",
    },
    "ripley": {
        "WAITING_ACCEPTANCE": "Esperando aceptación",
        "IN_PROGRESS": "En proceso",
        "RECEIVED": "Recibido",
        "REFUNDED": "Reembolsado",
        "CLOSED": "Cerrada",
        "REFUSED": "Rechazada",
        "OPEN": "Abierta",
    },
    "falabella": {
        "returned": "Devuelto",
        "return_waiting_for_approval": "Esperando aprobación",
        "return_shipped_by_customer": "Enviado por cliente",
        "return_rejected": "Devolución rechazada",
        "return_accepted": "Devolución aceptada",
        "appeal_accepted": "Apelación aceptada",
        "appeal_rejected": "Apelación rechazada",
        "delivered": "Entregado",
        "canceled": "Cancelado",
    },
}


def traducir_estado_canal(canal, estado_crudo):
    """Devuelve la etiqueta legible del estado crudo. Si no está en el diccionario,
    formatea el crudo (snake_case → Título) para que igual sea legible."""
    if not estado_crudo:
        return ""
    mapa = ESTADO_CANAL_LABEL.get((canal or "").lower(), {})
    # Buscar exacto y case-insensitive
    if estado_crudo in mapa:
        return mapa[estado_crudo]
    for k, v in mapa.items():
        if k.lower() == str(estado_crudo).lower():
            return v
    # Fallback: snake_case o MAYUS → Título legible
    return str(estado_crudo).replace("_", " ").strip().capitalize()


def _norm_estado(canal, estado_crudo):
    """Mapea el estado crudo de cada canal a un estado normalizado común."""
    e = (str(estado_crudo) or "").lower()
    if any(k in e for k in ("cancel", "rejected", "closed_cancel")):
        return "cancelada"
    if any(k in e for k in ("refund", "completed", "resolved", "closed", "received", "store_received", "delivered_after")):
        return "resuelta"
    if any(k in e for k in ("transit", "shipped", "on_the_way", "intransit")):
        return "en_transito"
    if any(k in e for k in ("pending", "opened", "open", "created", "requested", "waiting", "review")):
        return "abierta"
    return "abierta"  # por defecto, tratar como abierta (requiere atención)


# ─────────────────────────────────────────────────────────────────────────────
# MERCADOLIBRE
# ─────────────────────────────────────────────────────────────────────────────
def obtener_devoluciones_meli(dias=30):
    """Trae reclamos con devolución de ML y los normaliza.
    Flujo: buscar claims (con devolución) -> por cada uno, traer el/los returns.
    """
    from mercadolibre import get_meli_token, MELI_API_URL
    token = get_meli_token()
    headers = {"Authorization": f"Bearer {token}"}
    salida = []
    try:
        # El claims/search exige al menos un filtro. Usamos 'range' de fecha
        # (formato ML: date_created:after:<ISO>,before:<ISO>). Iteramos por
        # stage para cubrir reclamos con devolución en distintas etapas.
        desde = (datetime.utcnow() - timedelta(days=dias)).strftime("%Y-%m-%dT00:00:00.000-00:00")
        hasta = datetime.utcnow().strftime("%Y-%m-%dT23:59:59.000-00:00")
        rango = f"date_created:after:{desde},before:{hasta}"
        vistos_claims = set()
        for stage in ("claim", "dispute", "recontact", "none"):
            offset = 0
            while True:
                params = {
                    "stage": stage,
                    "range": rango,
                    "limit": 50,
                    "offset": offset,
                }
                r = requests.get(f"{MELI_API_URL}/post-purchase/v1/claims/search",
                                 headers=headers, params=params, timeout=25)
                if r.status_code != 200:
                    # Si un stage no es válido, seguir con el siguiente
                    if r.status_code == 400 and offset == 0:
                        break
                    print(f"[Returns ML] {stage} status {r.status_code}: {r.text[:120]}")
                    break
                data = r.json()
                claims = data.get("data") or data.get("results") or []
                if not claims:
                    break
                for c in claims:
                    claim_id = str(c.get("id") or c.get("claim_id") or "")
                    if not claim_id or claim_id in vistos_claims:
                        continue
                    vistos_claims.add(claim_id)
                    # Traer detalle de la devolución del claim
                    try:
                        rr = requests.get(f"{MELI_API_URL}/post-purchase/v2/claims/{claim_id}/returns",
                                          headers=headers, timeout=20)
                        if rr.status_code != 200:
                            continue
                        ret = rr.json()
                    except Exception:
                        continue
                    if not ret:
                        continue
                    returns_list = ret if isinstance(ret, list) else [ret]
                    for robj in returns_list:
                        return_id = str(robj.get("id") or claim_id)
                        shipments = robj.get("shipments") or []
                        tracking = None
                        estado_env = None
                        if shipments:
                            tracking = shipments[0].get("tracking_number")
                            estado_env = shipments[0].get("status")
                        order_id = str(c.get("resource_id") or c.get("order_id") or "")
                        # Extraer las acciones del vendedor (respondent) y su plazo.
                        # ML entrega en cada available_action un due_date = fecha
                        # límite para ejecutar esa acción. Tomamos la más próxima.
                        acciones = []
                        fecha_limite = None
                        for p in (c.get("players") or []):
                            if p.get("role") != "respondent":
                                continue
                            for a in (p.get("available_actions") or []):
                                nombre_acc = a.get("action")
                                if nombre_acc:
                                    acciones.append(nombre_acc)
                                dd = _parse_fecha(a.get("due_date"))
                                if dd and (fecha_limite is None or dd < fecha_limite):
                                    fecha_limite = dd
                        salida.append({
                            "canal": "mercadolibre",
                            "return_id": return_id,
                            "claim_id": claim_id,
                            "order_id": order_id,
                            "sku": None, "sku_canal": None,
                            "producto_nombre": None,
                            "cantidad": 1,
                            "estado": _norm_estado("mercadolibre", estado_env or c.get("status")),
                            "estado_canal": estado_env or str(c.get("status") or ""),
                            "motivo": c.get("reason_id") or c.get("type"),
                            "tipo": c.get("type") or "return",
                            "monto_reembolso": None, "moneda": "CLP",
                            "tracking_number": tracking, "transportista": None,
                            "fecha_solicitud": _parse_fecha(c.get("date_created")),
                            "fecha_limite": fecha_limite,
                            "fecha_resolucion": None,
                            "fecha_actualizacion_canal": _parse_fecha(robj.get("last_updated") or c.get("last_updated")),
                            "acciones_disponibles": acciones,
                            "raw": robj,
                        })
                offset += 50
                total = (data.get("paging", {}) or {}).get("total", offset)
                if offset >= total or offset > 500:
                    break
    except Exception as e:
        print(f"[Returns ML] error: {e}")
    return salida


# ─────────────────────────────────────────────────────────────────────────────
# WALMART
# ─────────────────────────────────────────────────────────────────────────────
def obtener_devoluciones_walmart(dias=30):
    from walmart import walmart_headers, WALMART_BASE_URL
    salida = []
    try:
        offset = 0
        while True:
            params = {"limit": 50, "offset": offset}
            r = requests.get(f"{WALMART_BASE_URL}/v3/returns",
                             headers=walmart_headers(), params=params, timeout=25)
            if r.status_code not in (200, 202):
                print(f"[Returns Walmart] status {r.status_code}: {r.text[:150]}")
                break
            data = r.json()
            ordenes = data.get("returnOrders") or []
            if not ordenes:
                break
            for o in ordenes:
                return_id = str(o.get("returnOrderId") or "")
                lineas = o.get("returnOrderLines") or []
                if isinstance(lineas, dict):
                    lineas = [lineas]
                # tracking desde returnOrderShipments
                tracking = None
                transportista = None
                shipments = o.get("returnOrderShipments") or []
                if shipments and isinstance(shipments, list):
                    tracking = shipments[0].get("trackingNumber") or shipments[0].get("trackingNo")
                    transportista = shipments[0].get("carrier")
                # una fila por línea (o una sola si no hay líneas)
                if not lineas:
                    lineas = [{}]
                for ln in lineas:
                    item = ln.get("item") or {}
                    nombre = item.get("productName") or item.get("sku")
                    sku_canal = item.get("sku")
                    order_id = str(o.get("customerOrderId") or ln.get("purchaseOrderId") or "")
                    # Monto: buscar charge de tipo PRODUCT en charges[]
                    monto = None
                    moneda = "CLP"
                    for ch in (ln.get("charges") or []):
                        if not isinstance(ch, dict):
                            continue
                        ca = ch.get("chargeAmount") or {}
                        if ch.get("chargeType") == "PRODUCT" and isinstance(ca, dict):
                            try:
                                monto = float(ca.get("amount"))
                                moneda = ca.get("currency") or "CLP"
                            except Exception:
                                pass
                            break
                    # Cantidad: puede venir como número o dict {amount}
                    cant_raw = ln.get("returnQuantity") or ln.get("quantity") or 1
                    if isinstance(cant_raw, dict):
                        cant_raw = cant_raw.get("amount") or 1
                    try:
                        cantidad = int(float(cant_raw))
                    except Exception:
                        cantidad = 1
                    salida.append({
                        "canal": "walmart",
                        "return_id": return_id,
                        "claim_id": None,
                        "order_id": order_id,
                        "sku": None,
                        "sku_canal": sku_canal,
                        "producto_nombre": nombre,
                        "cantidad": cantidad,
                        "estado": _norm_estado("walmart", ln.get("status") or o.get("status")),
                        "estado_canal": str(ln.get("status") or o.get("status") or ""),
                        "motivo": ln.get("returnReason") or ln.get("returnDescription"),
                        "tipo": o.get("returnType") or "return",
                        "monto_reembolso": monto,
                        "moneda": moneda,
                        "tracking_number": tracking,
                        "transportista": transportista,
                        "fecha_solicitud": _parse_fecha(o.get("returnOrderDate")),
                        "fecha_limite": _parse_fecha(o.get("returnByDate")),
                        "fecha_resolucion": None,
                        "fecha_actualizacion_canal": _parse_fecha(o.get("returnOrderDate")),
                        "acciones_disponibles": [],
                        "raw": o,
                    })
            meta = data.get("meta") or {}
            total = meta.get("totalCount") or 0
            offset += 50
            if offset >= total or offset > 1000:
                break
    except Exception as e:
        print(f"[Returns Walmart] error: {e}")
    return salida


# ─────────────────────────────────────────────────────────────────────────────
# RIPLEY (Mirakl)
# ─────────────────────────────────────────────────────────────────────────────
def obtener_devoluciones_ripley(dias=30):
    from ripley import RIPLEY_BASE_URL, RIPLEY_API_KEY
    headers = {"Authorization": RIPLEY_API_KEY, "Accept": "application/json"}
    salida = []
    try:
        page_token = None
        vueltas = 0
        while vueltas < 20:
            vueltas += 1
            params = {"max": 100}
            if page_token:
                params["page_token"] = page_token
            r = requests.get(f"{RIPLEY_BASE_URL}/api/returns", headers=headers, params=params, timeout=25)
            if r.status_code != 200:
                print(f"[Returns Ripley] status {r.status_code}: {r.text[:150]}")
                break
            data = r.json()
            arr = data.get("data") or []
            for o in arr:
                return_id = str(o.get("id") or o.get("return_id") or "")
                # Mirakl: lineas en return_lines o order_lines
                lineas = o.get("return_lines") or o.get("lines") or []
                sku_canal = None
                nombre = None
                cant = 1
                if lineas and isinstance(lineas, list):
                    l0 = lineas[0]
                    sku_canal = l0.get("offer_sku") or l0.get("product_sku") or l0.get("sku")
                    nombre = l0.get("product_title") or l0.get("title")
                    cant = int(l0.get("quantity") or 1)
                salida.append({
                    "canal": "ripley",
                    "return_id": return_id,
                    "claim_id": None,
                    "order_id": str(o.get("order_id") or o.get("commercial_id") or ""),
                    "sku": None,
                    "sku_canal": sku_canal,
                    "producto_nombre": nombre,
                    "cantidad": cant,
                    "estado": _norm_estado("ripley", o.get("state") or o.get("status")),
                    "estado_canal": str(o.get("state") or o.get("status") or ""),
                    "motivo": o.get("reason") or o.get("reason_code"),
                    "tipo": o.get("type") or "return",
                    "monto_reembolso": o.get("amount") or o.get("total_amount"),
                    "moneda": o.get("currency_iso_code") or "CLP",
                    "tracking_number": o.get("tracking_number"),
                    "transportista": o.get("carrier"),
                    "fecha_solicitud": _parse_fecha(o.get("created_date") or o.get("creation_date")),
                    "fecha_limite": _parse_fecha(o.get("deadline") or o.get("expiration_date")),
                    "fecha_resolucion": _parse_fecha(o.get("closed_date")),
                    "fecha_actualizacion_canal": _parse_fecha(o.get("last_updated_date") or o.get("update_date")),
                    "acciones_disponibles": [],
                    "raw": o,
                })
            page_token = data.get("next_page_token")
            if not page_token or not arr:
                break
    except Exception as e:
        print(f"[Returns Ripley] error: {e}")
    return salida


# ─────────────────────────────────────────────────────────────────────────────
# PARIS (Cencosud)
# ─────────────────────────────────────────────────────────────────────────────
def obtener_devoluciones_paris(dias=30):
    from paris import PARIS_BASE_URL, paris_headers
    salida = []
    try:
        offset = 0
        while True:
            params = {"offset": offset, "limit": 50}
            r = requests.get(f"{PARIS_BASE_URL}/v2/returns", headers=paris_headers(),
                             params=params, timeout=25)
            if r.status_code != 200:
                print(f"[Returns Paris] status {r.status_code}: {r.text[:150]}")
                break
            data = r.json()
            arr = data.get("data") or []
            if not arr:
                break
            for o in arr:
                return_id = str(o.get("id") or o.get("returnNumber") or "")
                items = o.get("items") or []
                sku_canal = None
                sku_seller = None
                nombre = None
                cant = 1
                if items and isinstance(items, list):
                    i0 = items[0]
                    sku_canal = i0.get("sku")
                    sku_seller = i0.get("skuSeller")
                    nombre = i0.get("name")
                    cant = int(i0.get("quantity") or 1)
                # status/returnType/returnReason son objetos {id,name,...}
                st = o.get("status") or {}
                estado_canal = st.get("name") if isinstance(st, dict) else str(st)
                rt = o.get("returnType") or {}
                tipo = rt.get("name") if isinstance(rt, dict) else str(rt)
                rr = o.get("returnReason") or {}
                motivo = (rr.get("description") or rr.get("name")) if isinstance(rr, dict) else str(rr)
                salida.append({
                    "canal": "paris",
                    "return_id": return_id,
                    "claim_id": None,
                    "order_id": str(o.get("subOrderNumber") or o.get("orderNumber") or ""),
                    "sku": None,
                    "sku_canal": sku_seller or sku_canal,  # skuSeller es tu SKU
                    "producto_nombre": nombre,
                    "cantidad": cant,
                    "estado": _norm_estado("paris", estado_canal),
                    "estado_canal": str(estado_canal or ""),
                    "motivo": motivo,
                    "tipo": tipo or "return",
                    "monto_reembolso": o.get("refundAmount") or o.get("totalAmount"),
                    "moneda": "CLP",
                    "tracking_number": o.get("trackingNumber"),
                    "transportista": o.get("carrier"),
                    "fecha_solicitud": _parse_fecha(o.get("createdAt")),
                    "fecha_limite": _parse_fecha(o.get("deadline") or o.get("expiresAt")),
                    "fecha_resolucion": _parse_fecha(o.get("closedAt") or o.get("resolvedAt")),
                    "fecha_actualizacion_canal": _parse_fecha(o.get("updatedAt") or o.get("createdAt")),
                    "acciones_disponibles": [],
                    "raw": o,
                })
            count = data.get("count") or 0
            offset += 50
            if offset >= count or offset > 1000:
                break
    except Exception as e:
        print(f"[Returns Paris] error: {e}")
    return salida


# ─────────────────────────────────────────────────────────────────────────────
# UPSERT a la tabla devoluciones_marketplace
# ─────────────────────────────────────────────────────────────────────────────
def upsert_devolucion(dev, tenant_id=None):
    """Inserta o actualiza una devolución normalizada. Calcula dias_restantes y
    requiere_accion. Resuelve el SKU Lusync si es posible. Devuelve 'insert'/'update'/'error'.
    """
    conn = get_conn(tenant_id=tenant_id) if tenant_id else get_conn()
    try:
        cur = conn.cursor()

        # Resolver SKU Lusync desde el sku del canal
        sku_lusync = dev.get("sku")
        if not sku_lusync and dev.get("sku_canal"):
            try:
                sku_lusync = obtener_sku_lusync_por_canal(
                    dev["canal"], sku_canal=dev.get("sku_canal"),
                    item_id_canal=dev.get("sku_canal"))
            except Exception:
                sku_lusync = None
        sku_lusync = sku_lusync or dev.get("sku_canal")

        # Calcular días restantes hasta la fecha límite
        dias_restantes = None
        requiere_accion = False
        fl = dev.get("fecha_limite")
        if fl:
            try:
                dias_restantes = (fl - now_chile().replace(tzinfo=None)).days
            except Exception:
                dias_restantes = None
        estado = dev.get("estado") or "abierta"
        if estado not in ("resuelta", "cancelada"):
            requiere_accion = True

        acciones = dev.get("acciones_disponibles") or []
        url_gestion = _link_gestion(dev["canal"], dev.get("order_id"),
                                    dev.get("return_id"), dev.get("claim_id"))
        raw = dev.get("raw")
        raw_json = None
        try:
            raw_json = json.dumps(raw, ensure_ascii=False, default=str)[:60000] if raw else None
        except Exception:
            raw_json = None

        cur.execute("""
            INSERT INTO devoluciones_marketplace
                (canal, return_id, claim_id, order_id, sku, sku_canal, producto_nombre,
                 cantidad, estado, estado_canal, motivo, tipo, monto_reembolso, moneda,
                 tracking_number, transportista, fecha_solicitud, fecha_limite,
                 fecha_resolucion, fecha_actualizacion_canal, dias_restantes,
                 requiere_accion, acciones_disponibles, raw_json,
                 primera_deteccion, ultima_sincronizacion, tenant_id, url_gestion)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                    NOW(), NOW(), %s, %s)
            ON CONFLICT (canal, return_id) DO UPDATE SET
                order_id = EXCLUDED.order_id,
                sku = COALESCE(EXCLUDED.sku, devoluciones_marketplace.sku),
                sku_canal = EXCLUDED.sku_canal,
                producto_nombre = COALESCE(EXCLUDED.producto_nombre, devoluciones_marketplace.producto_nombre),
                cantidad = EXCLUDED.cantidad,
                estado = EXCLUDED.estado,
                estado_canal = EXCLUDED.estado_canal,
                motivo = COALESCE(EXCLUDED.motivo, devoluciones_marketplace.motivo),
                tipo = EXCLUDED.tipo,
                monto_reembolso = COALESCE(EXCLUDED.monto_reembolso, devoluciones_marketplace.monto_reembolso),
                moneda = EXCLUDED.moneda,
                tracking_number = COALESCE(EXCLUDED.tracking_number, devoluciones_marketplace.tracking_number),
                transportista = COALESCE(EXCLUDED.transportista, devoluciones_marketplace.transportista),
                fecha_limite = COALESCE(EXCLUDED.fecha_limite, devoluciones_marketplace.fecha_limite),
                fecha_resolucion = COALESCE(EXCLUDED.fecha_resolucion, devoluciones_marketplace.fecha_resolucion),
                fecha_actualizacion_canal = EXCLUDED.fecha_actualizacion_canal,
                dias_restantes = EXCLUDED.dias_restantes,
                requiere_accion = EXCLUDED.requiere_accion,
                acciones_disponibles = EXCLUDED.acciones_disponibles,
                raw_json = EXCLUDED.raw_json,
                url_gestion = COALESCE(EXCLUDED.url_gestion, devoluciones_marketplace.url_gestion),
                ultima_sincronizacion = NOW()
        """, (
            dev["canal"], dev["return_id"], dev.get("claim_id"), dev.get("order_id"),
            sku_lusync, dev.get("sku_canal"), dev.get("producto_nombre"),
            dev.get("cantidad") or 1, estado, dev.get("estado_canal"),
            dev.get("motivo"), dev.get("tipo"), dev.get("monto_reembolso"),
            dev.get("moneda") or "CLP", dev.get("tracking_number"), dev.get("transportista"),
            dev.get("fecha_solicitud"), dev.get("fecha_limite"), dev.get("fecha_resolucion"),
            dev.get("fecha_actualizacion_canal"), dias_restantes, requiere_accion,
            json.dumps(acciones, ensure_ascii=False), raw_json, tenant_id, url_gestion
        ))
        conn.commit()
        cur.close()
        return "ok"
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        print(f"[Returns upsert] error {dev.get('canal')}/{dev.get('return_id')}: {e}")
        return "error"
    finally:
        release_conn(conn)


# ─────────────────────────────────────────────────────────────────────────────
# SYNC de todos los canales de pull
# ─────────────────────────────────────────────────────────────────────────────
def sincronizar_devoluciones(tenant_id=None, dias=30, canales=None):
    """Trae devoluciones de todos los canales de pull y las guarda.
    Devuelve un resumen por canal.
    """
    canales = canales or ["mercadolibre", "walmart", "ripley", "paris"]
    funcs = {
        "mercadolibre": obtener_devoluciones_meli,
        "walmart": obtener_devoluciones_walmart,
        "ripley": obtener_devoluciones_ripley,
        "paris": obtener_devoluciones_paris,
    }
    resumen = {}
    for canal in canales:
        fn = funcs.get(canal)
        if not fn:
            continue
        try:
            devs = fn(dias=dias)
            ok = 0
            for d in devs:
                if upsert_devolucion(d, tenant_id=tenant_id) == "ok":
                    ok += 1
            resumen[canal] = {"traidas": len(devs), "guardadas": ok}
            print(f"[Returns sync] {canal}: {len(devs)} traídas, {ok} guardadas")
        except Exception as e:
            resumen[canal] = {"error": str(e)[:120]}
            print(f"[Returns sync] {canal} error: {e}")
    return resumen


# ─────────────────────────────────────────────────────────────────────────────
# FALABELLA — procesar payload del webhook onReturnStatusChanged
# ─────────────────────────────────────────────────────────────────────────────
def procesar_webhook_falabella_return(payload, tenant_id=None):
    """Normaliza y guarda una notificación de devolución de Falabella (webhook).
    El payload de Falabella varía; se extrae lo posible de forma defensiva.
    """
    try:
        # Estructura típica Falabella webhook: {"Entity":"ORDER","EventType":"onReturnStatusChanged","Payload":{...}}
        p = payload.get("Payload") or payload.get("payload") or payload
        return_id = str(p.get("ReturnId") or p.get("returnId") or p.get("OrderId") or p.get("orderId") or "")
        if not return_id:
            return "sin_id"
        estado_canal = p.get("Status") or p.get("status") or p.get("ReturnStatus") or ""
        dev = {
            "canal": "falabella",
            "return_id": return_id,
            "claim_id": None,
            "order_id": str(p.get("OrderId") or p.get("orderId") or ""),
            "sku": None,
            "sku_canal": p.get("Sku") or p.get("sku"),
            "producto_nombre": p.get("ProductName") or p.get("name"),
            "cantidad": int(p.get("Quantity") or 1),
            "estado": _norm_estado("falabella", estado_canal),
            "estado_canal": str(estado_canal),
            "motivo": p.get("Reason") or p.get("reason"),
            "tipo": "return",
            "monto_reembolso": p.get("RefundAmount") or p.get("refundAmount"),
            "moneda": "CLP",
            "tracking_number": p.get("TrackingNumber"),
            "transportista": p.get("Carrier"),
            "fecha_solicitud": _parse_fecha(p.get("CreatedAt") or p.get("createdAt")),
            "fecha_limite": _parse_fecha(p.get("Deadline") or p.get("deadline")),
            "fecha_resolucion": None,
            "fecha_actualizacion_canal": _parse_fecha(p.get("UpdatedAt") or p.get("updatedAt")) or now_chile().replace(tzinfo=None),
            "acciones_disponibles": [],
            "raw": payload,
        }
        return upsert_devolucion(dev, tenant_id=tenant_id)
    except Exception as e:
        print(f"[Returns Falabella webhook] error: {e}")
        return "error"


# ─────────────────────────────────────────────────────────────────────────────
# MATCHING: vincular una devolución física con su devolución de marketplace
# ─────────────────────────────────────────────────────────────────────────────
def buscar_devolucion_mkt(oc_origen=None, sku=None, canal=None, tenant_id=None):
    """Dado lo que se conoce de una devolución física (la OC de origen que trae
    la etiqueta, el SKU, el canal), busca la devolución de marketplace que le
    corresponde. Devuelve una lista de candidatos (dicts), del match más fuerte
    al más débil. Puede haber más de uno (una orden con varias devoluciones).

    Estrategia de match, de más fuerte a más débil:
      1. order_id exacto + canal + sku
      2. order_id exacto + canal
      3. order_id exacto (cualquier canal)
      4. sku + canal (últimos 60 días) — cuando no hay OC clara
    """
    if not any([oc_origen, sku]):
        return []
    conn = get_conn(tenant_id=tenant_id) if tenant_id else get_conn()
    try:
        cur = conn.cursor()
        cols = """id, canal, return_id, claim_id, order_id, sku, sku_canal,
                  producto_nombre, estado, estado_canal, motivo, tipo,
                  monto_reembolso, moneda, tracking_number, fecha_solicitud,
                  fecha_limite, fecha_resolucion, dias_restantes, requiere_accion,
                  acciones_disponibles, url_gestion"""

        def _run(where, params):
            cur.execute(f"SELECT {cols} FROM devoluciones_marketplace WHERE {where} "
                        f"ORDER BY fecha_solicitud DESC LIMIT 10", params)
            rows = cur.fetchall()
            names = [d[0] for d in cur.description]
            return [dict(zip(names, r)) for r in rows]

        canal_norm = (canal or "").lower().strip() or None
        candidatos = []
        vistos = set()

        def _add(lista, fuerza):
            for r in lista:
                if r["id"] in vistos:
                    continue
                vistos.add(r["id"])
                r["_match"] = fuerza
                candidatos.append(r)

        oc = str(oc_origen).strip() if oc_origen else None
        skv = str(sku).strip() if sku else None

        # El matching SIEMPRE exige que la orden (order_id) coincida. Nunca se
        # vincula solo por SKU+canal, porque un mismo producto (SKU) aparece en
        # muchas órdenes distintas y eso pegaría la devolución de otra orden
        # (falso positivo). Mejor no vincular que vincular mal: si la orden no
        # tiene devolución en el canal, el bloque dirá que no hay vínculo.
        if oc and canal_norm and skv:
            _add(_run("order_id=%s AND canal=%s AND (sku=%s OR sku_canal=%s)",
                      (oc, canal_norm, skv, skv)), "exacto_oc_canal_sku")
        if oc and canal_norm:
            _add(_run("order_id=%s AND canal=%s", (oc, canal_norm)), "oc_canal")
        if oc:
            _add(_run("order_id=%s", (oc,)), "solo_oc")

        # Serializar fechas para consumo del frontend
        for r in candidatos:
            for k in ("fecha_solicitud", "fecha_limite", "fecha_resolucion"):
                if r.get(k):
                    r[k] = r[k].isoformat()
            if r.get("monto_reembolso") is not None:
                try:
                    r["monto_reembolso"] = float(r["monto_reembolso"])
                except Exception:
                    pass
            # Etiqueta legible del estado crudo del canal (lo que se muestra)
            r["estado_label"] = traducir_estado_canal(r.get("canal"), r.get("estado_canal"))
        return candidatos
    except Exception as e:
        print(f"[buscar_devolucion_mkt] error: {e}")
        return []
    finally:
        release_conn(conn)


def estado_mkt_de_devolucion(order_id, canal=None, tenant_id=None):
    """Devuelve el estado actual en el marketplace de una devolución ya vinculada,
    para el seguimiento de la resolución final. Se usa al refrescar una ficha.
    """
    cands = buscar_devolucion_mkt(oc_origen=order_id, canal=canal, tenant_id=tenant_id)
    return cands[0] if cands else None
