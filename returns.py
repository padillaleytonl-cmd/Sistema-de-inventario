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


def _plazo_reclamo(plazo_del_canal, llegada_a_bodega=None):
    """El plazo para responder una devolucion: el que informa la API del canal.

    Solo ese. Si el canal no lo entrega se devuelve None y la pantalla dice
    que el canal no informa plazo. No se calcula nada de nuestro lado.

    Una version anterior caia a "llegada + 72 horas" cuando el canal no daba
    plazo. Era un tiempo definido por nosotros —y en horas corridas, cuando
    Falabella habla de horas habiles—, y como el upsert lo conservaba con
    COALESCE, una vez guardado sobrevivia a todas las sincronizaciones. Un
    reloj que el canal no respalda da seguridad falsa, que es peor que no
    tener reloj.

    llegada_a_bodega se ignora a proposito; queda en la firma para no romper
    a quien la llame.
    """
    return plazo_del_canal or None


# Plazo que PUBLICA cada canal que no lo entrega por API. Son sus reglas, no
# las nuestras; cada una cita de donde sale. Se cuentan en dias habiles
# (lunes a viernes, sin feriados) desde la llegada real a nuestra bodega.
REGLA_PLAZO_CANAL = {
    "paris": {
        "dias_habiles": 3,
        "fuente": "Paris: 3 días hábiles desde que el producto llega a tu bodega",
    },
    "falabella": {
        "dias_habiles": 3,
        "fuente": "Falabella: 72 horas hábiles desde que recibes el producto",
    },
}


def plazo_efectivo(canal, plazo_api, llegada):
    """Cuando vence el plazo y de donde salio. Devuelve (datetime|None, fuente).

    fuente = "api"    el canal lo informo por su API; manda siempre.
             "regla"  el canal no lo informa, y se aplica la regla que el
                      propio canal publica, desde la llegada real.
             None     no hay con que calcularlo (todavia no llega, o el canal
                      no publica regla). No se inventa.

    El de regla no se guarda: se calcula al leer, para que fecha_limite siga
    siendo solo lo que dice la API.
    """
    if plazo_api:
        return plazo_api, "api"
    regla = REGLA_PLAZO_CANAL.get((canal or "").strip().lower())
    if regla and llegada:
        from feriados import calcular_deadline_habil
        return calcular_deadline_habil(llegada, dias_habiles=regla["dias_habiles"]), "regla"
    return None, None


def _norm_estado(canal, estado_crudo):
    """Mapea el estado crudo de cada canal a un estado normalizado común."""
    e = (str(estado_crudo) or "").lower()
    if any(k in e for k in ("cancel", "rejected", "closed_cancel")):
        return "cancelada"
    if any(k in e for k in ("refund", "completed", "resolved", "closed", "store_received", "delivered_after")):
        return "resuelta"
    # "received" es que el producto volvio, no que el caso se cerro: es justo
    # cuando hay que revisarlo. Antes se contaba como "resuelta" y la
    # devolucion saltaba a Completadas sin que nadie la mirara.
    if "received" in e:
        return "recibida"
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
                        # OJO: en Walmart returnByDate suele ser el plazo del
                        # CLIENTE para devolver, no el del seller para reclamar.
                        # Hay que comprobarlo contra una devolucion real antes
                        # de confiar en este numero.
                        "fecha_limite": _plazo_reclamo(
                            _parse_fecha(o.get("returnByDate")), None),
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
                    "fecha_limite": _plazo_reclamo(
                        _parse_fecha(o.get("deadline") or o.get("expiration_date")), None),
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
def _estado_devolucion_paris(o):
    """Estado de una devolucion de Paris a partir de sus fechas.

    /v2/returns/full no trae campo "status": trae cuatro fechas que se van
    llenando a medida que la devolucion avanza. Se leen de la mas avanzada a
    la menos, que es el orden real del flujo y coincide con las pestañas del
    portal (Por recibir / Recibido / En revision / Finalizado).

    Devuelve (estado_normalizado, etiqueta_legible).
    """
    if o.get("finalStatusDate"):
        return "resuelta", "finalizada"
    if o.get("warehouseArrivalDate"):
        # Llego a NUESTRA bodega: es la que requiere accion, hay que revisarla
        # y decidir si es revendible.
        return "abierta", "en_nuestra_bodega"
    if o.get("storeReturnDate") or o.get("dispatchDate"):
        return "en_transito", "en_camino"
    # Pedida y todavia sin moverse: para nosotros ya esta "En camino". No
    # existe una etapa "solicitada" aparte; el propio portal de Paris agrupa
    # estas como "Solicitudes por recibir: que van en camino a tu bodega".
    return "en_transito", "en_camino"


def obtener_devoluciones_paris(dias=30):
    """Devoluciones de Paris desde /v2/returns/full.

    OJO: NO es /v2/returns. Medido contra la API real el 28-09-2026:

        GET /v2/returns          404  "Cannot GET /v2/returns"
        GET /v2/returns/summary  404
        GET /v2/returns/full     200

    /v2/returns se cayo el 15/09, cuando Paris migro el modulo, y el lector
    siguio devolviendo lista vacia sin avisar —devuelve [] tanto si falla como
    si no hay nada—, asi que las devoluciones dejaron de entrar y nadie se
    entero durante 13 dias.

    El contrato de /full es distinto al de /returns:

        responde un ARRAY plano, no {"data": [...], "count": N}
        no trae "status" ni "returnType"; el estado sale de cuatro fechas
        no trae "createdAt"; la solicitud es "originReturnDate"
        el tracking y el monto viven dentro de items[], no en la raiz
        gteCreatedAt y lteCreatedAt son OBLIGATORIOS

    La ventana filtra por fecha de CREACION de la devolucion. Como una
    devolucion vive semanas —30 dias para pedirla, mas el viaje, mas la
    revision—, una ventana corta dejaria fuera devoluciones todavia en curso.
    Por eso se respeta "dias" pero con piso de 90.
    """
    from paris import PARIS_BASE_URL, paris_headers
    from datetime import datetime as _dt, timedelta as _td

    salida = []
    try:
        ventana = max(int(dias or 30), 90)
        hasta = _dt.utcnow()
        desde = hasta - _td(days=ventana)
        params = {"gteCreatedAt": desde.strftime("%Y-%m-%d"),
                  "lteCreatedAt": hasta.strftime("%Y-%m-%d")}

        r = requests.get(f"{PARIS_BASE_URL}/v2/returns/full", headers=paris_headers(),
                         params=params, timeout=30)
        if r.status_code != 200:
            print(f"[Returns Paris] /v2/returns/full status {r.status_code}: {r.text[:200]}")
            return salida

        cuerpo = r.json()
        # Array plano. Se tolera igual el envoltorio {"data": [...]} por si
        # Paris lo vuelve a cambiar.
        arr = cuerpo if isinstance(cuerpo, list) else (cuerpo.get("data") or [])

        for o in arr:
            if not isinstance(o, dict):
                continue
            # Mismo orden que antes (id primero) para no duplicar las filas ya
            # guardadas: el upsert es ON CONFLICT (canal, return_id).
            return_id = str(o.get("id") or o.get("returnNumber") or "")
            if not return_id:
                continue

            items = [i for i in (o.get("items") or []) if isinstance(i, dict)]
            i0 = items[0] if items else {}
            cant = int(i0.get("quantity") or 1)

            rr = o.get("returnReason") or {}
            motivo = ((rr.get("description") or rr.get("name"))
                      if isinstance(rr, dict) else str(rr or ""))

            estado, etiqueta = _estado_devolucion_paris(o)

            # La fecha mas avanzada que tenga, para saber cuando se movio por
            # ultima vez sin que la API entregue un updatedAt.
            fechas = [o.get("finalStatusDate"), o.get("warehouseArrivalDate"),
                      o.get("dispatchDate"), o.get("storeReturnDate"),
                      o.get("originReturnDate")]
            ultima = next((f for f in fechas if f), None)

            salida.append({
                "canal": "paris",
                "return_id": return_id,
                "claim_id": None,
                "order_id": str(o.get("subOrderNumber") or o.get("orderNumber") or ""),
                "sku": None,
                "sku_canal": i0.get("skuSeller") or i0.get("sku"),  # skuSeller es tu SKU
                "producto_nombre": i0.get("name"),
                "cantidad": cant,
                "estado": estado,
                "estado_canal": etiqueta,
                "motivo": motivo,
                "tipo": "return",
                "monto_reembolso": i0.get("priceAfterDiscounts"),
                "moneda": "CLP",
                "tracking_number": i0.get("trackingNumber"),
                "transportista": o.get("carrier"),
                # originReturnDate viene en null en varias devoluciones (se vio
                # en 3 de las primeras 5 de produccion). Sin fecha no se puede
                # calcular la antiguedad ni priorizar, que es justo para lo que
                # sirve el modulo, asi que se cae a la siguiente fecha conocida.
                # originOrderDate es la de la ORDEN, no la de la devolucion:
                # va ultima y solo para no dejarla vacia.
                "fecha_solicitud": _parse_fecha(
                    o.get("originReturnDate") or o.get("storeReturnDate")
                    or o.get("dispatchDate") or o.get("originOrderDate")),
                # /v2/returns/full no trae ningun plazo, asi que NO hay plazo.
                # Antes se calculaba desde la llegada a bodega; eso era un
                # tiempo nuestro, no de Paris.
                "fecha_limite": None,
                # La llegada si la informa Paris, y es un hecho, no un plazo:
                # decide si la devolucion esta "En nuestra bodega".
                "fecha_llegada_bodega": _parse_fecha(o.get("warehouseArrivalDate")),
                "fecha_resolucion": _parse_fecha(o.get("finalStatusDate")),
                "fecha_actualizacion_canal": _parse_fecha(ultima),
                "acciones_disponibles": [],
                "raw": o,
            })

        multi = [o for o in arr
                 if isinstance(o, dict) and len(o.get("items") or []) > 1]
        if multi:
            # Una fila por devolucion es lo que permite el upsert
            # ON CONFLICT (canal, return_id). Con varios productos solo se
            # guarda el primero, igual que antes; el resto queda en "raw".
            print(f"[Returns Paris] {len(multi)} devoluciones con mas de un "
                  f"producto: solo se guarda el primero (el resto queda en raw)")
    except Exception as e:
        print(f"[Returns Paris] error: {e}")
    return salida


# ─────────────────────────────────────────────────────────────────────────────
# FALABELLA (Seller Center)
# ─────────────────────────────────────────────────────────────────────────────
def obtener_devoluciones_falabella(dias=30):
    """Devoluciones de Falabella. No hay endpoint: son un ESTADO de la orden.

    Los estados son los que documenta GetOrderItems:

        return_shipped_by_customer    el cliente ya la despacho
        return_waiting_for_approval   espera que la aprobemos
        return_rejected               rechazada
        returned                      Falabella recibio el producto

    Una version anterior de este lector traia solo "returned" porque los otros
    dos se probaron con el nombre mal escrito —return_ship_by_customer y
    return_awaiting_for_approval, que no existen— y devolvian cero. Eso dejaba
    fuera justo las que piden accion.

    La orden solo trae cabecera, sin SKU ni motivo ni plazo, asi que los items
    se piden aparte con GetOrderItems. No se usa obtener_items_orden_falabella
    porque agrupa por SKU descartando todo campo que no sea SellerSku, Sku,
    Quantity, Status, Name y CreatedAt — justo lo que aca hace falta. Se llama
    la API directo y se agrupa conservando el item crudo en "raw".

    Falabella no entrega id de devolucion porque para ella no existe como
    objeto; se arma uno estable con orden + SKU, que es lo que el upsert
    necesita para no duplicar.
    """
    from falabella import llamar_api_falabella, obtener_ordenes_falabella

    salida = []
    try:
        # Igual que en Paris: una devolucion vive semanas, asi que la ventana
        # tiene piso de 90 dias para no perder las que siguen en curso.
        ventana = max(int(dias or 30), 90)

        # Los cuatro estados del ciclo de devolucion. Una orden puede aparecer
        # en mas de una consulta si tiene items en estados distintos, asi que
        # se deduplica por OrderId antes de pedir los items.
        ESTADOS_DEV = ["return_shipped_by_customer", "return_waiting_for_approval",
                       "return_rejected", "returned"]
        ordenes, vistas = [], set()
        for est in ESTADOS_DEV:
            try:
                for o in (obtener_ordenes_falabella(estado=est, dias=ventana, limit=100) or []):
                    oid = str(o.get("OrderId") or "")
                    if oid and oid not in vistas:
                        vistas.add(oid)
                        ordenes.append(o)
            except Exception as e:
                print(f"[Returns Falabella] estado {est}: {e}")

        for o in ordenes:
            order_id = str(o.get("OrderId") or "").strip()
            order_num = str(o.get("OrderNumber") or order_id).strip()
            if not order_id:
                continue

            res = llamar_api_falabella("GetOrderItems",
                                       params_extra={"OrderId": order_id},
                                       method="GET", formato="JSON")
            if not res.get("ok"):
                print(f"[Returns Falabella] items de {order_num}: {res.get('error')}")
                continue
            body = ((res.get("data") or {}).get("SuccessResponse") or {}).get("Body") or {}
            crudos = (body.get("OrderItems") or {}).get("OrderItem") or []
            if isinstance(crudos, dict):
                crudos = [crudos]
            if not isinstance(crudos, list):
                continue

            # Cada OrderItem es UNA unidad; se agrupan por SKU para la cantidad,
            # conservando el primero para el resto de los campos.
            por_sku = {}
            for it in crudos:
                if not isinstance(it, dict):
                    continue
                # Solo los items que de verdad estan en devolucion. La orden
                # aparece en la consulta si CUALQUIERA de sus items lo esta,
                # asi que sin este filtro una orden de dos productos donde se
                # devolvio uno generaria tambien una "devolucion" del que el
                # cliente se quedo.
                est_it = (it.get("Status") or "").strip().lower()
                if not est_it.startswith("return"):
                    continue
                sku = (it.get("Sku") or it.get("SellerSku") or "").strip()
                if not sku:
                    continue
                g = por_sku.setdefault(sku, {"cant": 0, "item": it, "monto": 0.0})
                g["cant"] += 1
                # Grand Total segun la documentacion de Falabella: PaidPrice
                # mas ShippingAmount, POR PRODUCTO. Cada OrderItem es una
                # unidad, asi que se acumula: quedarse con el primero
                # valorizaba en 1 una devolucion de 2.
                for campo in ("PaidPrice", "ShippingAmount"):
                    try:
                        g["monto"] += float(it.get(campo) or 0)
                    except (TypeError, ValueError):
                        pass

            for sku, g in por_sku.items():
                it = g["item"]
                # Dos estados distintos: el del item en el flujo de la orden, y
                # el de la solicitud de devolucion en si. La documentacion
                # define ReturnStatus como "Estado de la solicitud de
                # devolucion, si la hay (por ejemplo, Pendiente, Aprobada)".
                # Se prefiere ese cuando viene, porque es el que dice si
                # Falabella ya la aprobo.
                estado_item = str(it.get("Status") or "returned")
                estado_dev = str(it.get("ReturnStatus") or "").strip()
                estado_canal = f"{estado_item} · {estado_dev}" if estado_dev else estado_item
                salida.append({
                    "canal": "falabella",
                    # Falabella no da id de devolucion: se arma uno estable.
                    "return_id": f"{order_id}-{sku}",
                    "claim_id": None,
                    "order_id": order_num,
                    "sku": None,
                    "sku_canal": sku,
                    "producto_nombre": it.get("Name"),
                    "cantidad": g["cant"],
                    # Se normaliza por el estado del ITEM: sus valores estan
                    # documentados y son estables. ReturnStatus trae texto
                    # libre en español ("Pendiente", "Aprobada") y no sirve
                    # para mapear, solo para mostrar.
                    "estado": _norm_estado("falabella", estado_item),
                    "estado_canal": estado_canal,
                    # Reason y ReasonDetail estan documentados como "Motivo de
                    # cancelacion o devolucion" y "Razon detallada". Se
                    # combinan cuando vienen los dos: el primero es la
                    # categoria y el segundo el texto del cliente.
                    "motivo": " — ".join(
                        [x for x in [(it.get("Reason") or "").strip(),
                                     (it.get("ReasonDetail") or "").strip()] if x]
                    ) or (o.get("Remarks") or None),
                    # ShippingType del item dice si el producto salio de la
                    # bodega de Falabella o de la nuestra, y eso decide a que
                    # bodega vuelve la unidad. Es el unico dato util que el
                    # item trae de mas: Reason, ReasonDetail y ReturnStatus
                    # existen como campos pero Falabella los manda vacios
                    # —comprobado volcando el item crudo—, asi que el motivo
                    # de la devolucion simplemente no esta disponible por API.
                    "tipo": ("return_fulfillment"
                             if "fulfillment" in str(it.get("ShippingType") or "").lower()
                             else "return_seller"),
                    # PaidPrice + ShippingAmount, que es el "Grand Total" que
                    # documenta Falabella para documentos tributarios. NO
                    # ItemPrice, que es el precio antes de descuentos, ni el
                    # Price de la orden, que su propia documentacion marca
                    # como el que difiere con promociones y rompe los
                    # documentos tributarios. De aca sale el monto de la nota
                    # de credito.
                    "monto_reembolso": round(g["monto"], 2) or None,
                    "moneda": "CLP",
                    "tracking_number": (it.get("TrackingCode") or it.get("TrackingNumber")),
                    "transportista": it.get("ShipmentProvider"),
                    # UpdatedAt de la orden es cuando paso a "returned", que es
                    # lo mas cercano a la fecha de la solicitud que entrega.
                    "fecha_solicitud": _parse_fecha(o.get("UpdatedAt") or o.get("CreatedAt")),
                    # Falabella no entrega plazo: la lista de campos de
                    # GetOrderItems no trae ninguna fecha limite. Y "returned"
                    # significa, textual, "cuando Falabella recibe el producto
                    # que el cliente ha devuelto": es la llegada a FALABELLA,
                    # no a nuestra bodega. El reloj de 72 horas no se puede
                    # arrancar desde aca — falta la señal de llegada propia,
                    # que esta API no da.
                    "fecha_limite": None,
                    "fecha_resolucion": None,
                    "fecha_actualizacion_canal": _parse_fecha(o.get("UpdatedAt")),
                    "acciones_disponibles": [],
                    "raw": {"orden": o, "item": it},
                })
    except Exception as e:
        print(f"[Returns Falabella] error: {e}")
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
                 primera_deteccion, ultima_sincronizacion, tenant_id, url_gestion,
                 fecha_llegada_bodega)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                    NOW(), NOW(), %s, %s, %s)
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
                -- El plazo es el de la API, sin COALESCE: si el canal deja de
                -- informarlo, se borra. Con COALESCE un plazo calculado por
                -- nosotros sobrevivia a todas las sincronizaciones.
                fecha_limite = EXCLUDED.fecha_limite,
                -- La llegada SI se conserva: puede venir del pistoleo, y la
                -- sincronizacion no debe borrar lo que se registro a mano.
                fecha_llegada_bodega = COALESCE(EXCLUDED.fecha_llegada_bodega,
                                                devoluciones_marketplace.fecha_llegada_bodega),
                fecha_resolucion = COALESCE(EXCLUDED.fecha_resolucion, devoluciones_marketplace.fecha_resolucion),
                fecha_actualizacion_canal = EXCLUDED.fecha_actualizacion_canal,
                dias_restantes = EXCLUDED.dias_restantes,
                requiere_accion = EXCLUDED.requiere_accion,
                acciones_disponibles = EXCLUDED.acciones_disponibles,
                raw_json = EXCLUDED.raw_json,
                url_gestion = COALESCE(EXCLUDED.url_gestion, devoluciones_marketplace.url_gestion),
                ultima_sincronizacion = NOW()
            -- La unicidad es (canal, return_id), sin cliente: nunca se pisa la
            -- fila de otro cliente.
            WHERE devoluciones_marketplace.tenant_id IS NOT DISTINCT FROM EXCLUDED.tenant_id
        """, (
            dev["canal"], dev["return_id"], dev.get("claim_id"), dev.get("order_id"),
            sku_lusync, dev.get("sku_canal"), dev.get("producto_nombre"),
            dev.get("cantidad") or 1, estado, dev.get("estado_canal"),
            dev.get("motivo"), dev.get("tipo"), dev.get("monto_reembolso"),
            dev.get("moneda") or "CLP", dev.get("tracking_number"), dev.get("transportista"),
            dev.get("fecha_solicitud"), dev.get("fecha_limite"), dev.get("fecha_resolucion"),
            dev.get("fecha_actualizacion_canal"), dias_restantes, requiere_accion,
            json.dumps(acciones, ensure_ascii=False), raw_json, tenant_id, url_gestion,
            dev.get("fecha_llegada_bodega")
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
    canales = canales or ["mercadolibre", "walmart", "ripley", "paris", "falabella"]
    funcs = {
        "mercadolibre": obtener_devoluciones_meli,
        "walmart": obtener_devoluciones_walmart,
        "ripley": obtener_devoluciones_ripley,
        "paris": obtener_devoluciones_paris,
        "falabella": obtener_devoluciones_falabella,
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
            "fecha_limite": _plazo_reclamo(
                _parse_fecha(p.get("Deadline") or p.get("deadline")), None),
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
    # Sin cliente no se busca: devoluciones_marketplace no tiene RLS y
    # devolveria las de todos.
    if not tenant_id:
        return []
    conn = get_conn(tenant_id=tenant_id)
    try:
        cur = conn.cursor()
        cols = """id, canal, return_id, claim_id, order_id, sku, sku_canal,
                  producto_nombre, estado, estado_canal, motivo, tipo,
                  monto_reembolso, moneda, tracking_number, fecha_solicitud,
                  fecha_limite, fecha_resolucion, dias_restantes, requiere_accion,
                  acciones_disponibles, url_gestion"""

        def _run(where, params):
            cur.execute(f"SELECT {cols} FROM devoluciones_marketplace "
                        f"WHERE ({where}) AND tenant_id = %s "
                        f"ORDER BY fecha_solicitud DESC LIMIT 10", list(params) + [tenant_id])
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
