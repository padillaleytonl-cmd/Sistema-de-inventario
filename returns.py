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
        fecha_desde = (datetime.utcnow() - timedelta(days=dias)).strftime("%Y-%m-%dT00:00:00.000-00:00")
        offset = 0
        while True:
            params = {
                "date_created_from": fecha_desde,
                "limit": 50,
                "offset": offset,
            }
            r = requests.get(f"{MELI_API_URL}/post-purchase/v1/claims/search",
                             headers=headers, params=params, timeout=25)
            if r.status_code != 200:
                print(f"[Returns ML] claims/search status {r.status_code}: {r.text[:150]}")
                break
            data = r.json()
            claims = data.get("data") or data.get("results") or []
            if not claims:
                break
            for c in claims:
                claim_id = str(c.get("id") or c.get("claim_id") or "")
                if not claim_id:
                    continue
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
                        "fecha_limite": None,
                        "fecha_resolucion": None,
                        "fecha_actualizacion_canal": _parse_fecha(robj.get("last_updated") or c.get("last_updated")),
                        "acciones_disponibles": [],
                        "raw": robj,
                    })
            offset += 50
            if offset >= (data.get("paging", {}) or {}).get("total", offset):
                break
            if offset > 500:
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
                # tracking desde returnOrderShipments o labels
                tracking = None
                transportista = None
                shipments = o.get("returnOrderShipments") or []
                if shipments and isinstance(shipments, list):
                    tr = shipments[0].get("trackingNumber") or shipments[0].get("trackingNo")
                    tracking = tr
                # una fila por línea (o una sola si no hay líneas)
                if not lineas:
                    lineas = [{}]
                for ln in lineas:
                    sku_canal = ln.get("sku") or ln.get("itemId")
                    order_id = str(ln.get("purchaseOrderId") or o.get("customerOrderId") or "")
                    monto = None
                    tra = o.get("totalRefundAmount") or {}
                    if isinstance(tra, dict):
                        monto = tra.get("currencyAmount")
                    salida.append({
                        "canal": "walmart",
                        "return_id": return_id,
                        "claim_id": None,
                        "order_id": order_id,
                        "sku": None,
                        "sku_canal": sku_canal,
                        "producto_nombre": ln.get("itemDescription") or ln.get("productName"),
                        "cantidad": int(ln.get("returnQuantity") or ln.get("quantity") or 1),
                        "estado": _norm_estado("walmart", ln.get("status") or o.get("status")),
                        "estado_canal": str(ln.get("status") or o.get("status") or ""),
                        "motivo": ln.get("returnReason") or ln.get("returnDescription"),
                        "tipo": o.get("returnType") or "return",
                        "monto_reembolso": monto,
                        "moneda": (tra.get("currencyUnit") if isinstance(tra, dict) else None) or "CLP",
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
                 primera_deteccion, ultima_sincronizacion, tenant_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                    NOW(), NOW(), %s)
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
                ultima_sincronizacion = NOW()
        """, (
            dev["canal"], dev["return_id"], dev.get("claim_id"), dev.get("order_id"),
            sku_lusync, dev.get("sku_canal"), dev.get("producto_nombre"),
            dev.get("cantidad") or 1, estado, dev.get("estado_canal"),
            dev.get("motivo"), dev.get("tipo"), dev.get("monto_reembolso"),
            dev.get("moneda") or "CLP", dev.get("tracking_number"), dev.get("transportista"),
            dev.get("fecha_solicitud"), dev.get("fecha_limite"), dev.get("fecha_resolucion"),
            dev.get("fecha_actualizacion_canal"), dias_restantes, requiere_accion,
            json.dumps(acciones, ensure_ascii=False), raw_json, tenant_id
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
