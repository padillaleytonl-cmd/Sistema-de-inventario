import requests
import os
import time
from datetime import datetime, timedelta

WALMART_CLIENT_ID = os.environ.get("WALMART_CLIENT_ID")
WALMART_CLIENT_SECRET = os.environ.get("WALMART_CLIENT_SECRET")
WALMART_BASE_URL = "https://marketplace.walmartapis.com"

_token_cache = {"token": None, "expires_at": 0}

def get_token():
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["token"]

    import base64, uuid
    credentials = base64.b64encode(f"{WALMART_CLIENT_ID}:{WALMART_CLIENT_SECRET}".encode()).decode()
    res = requests.post(
        "https://marketplace.walmartapis.com/v3/token",
        headers={
            "Authorization": f"Basic {credentials}",
            "WM_SVC.NAME": "Lusync",
            "WM_QOS.CORRELATION_ID": str(uuid.uuid4()),
            "WM_MARKET": "cl",
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded"
        },
        data={"grant_type": "client_credentials"}
    )

    if res.status_code != 200:
        raise Exception(f"Walmart auth error: {res.status_code} {res.text}")

    data = res.json()
    _token_cache["token"] = data["access_token"]
    _token_cache["expires_at"] = now + int(data.get("expires_in", 900))
    return _token_cache["token"]

def walmart_headers():
    import uuid
    return {
        "WM_SVC.NAME": "Lusync",
        # CORRELATION_ID debe ser ÚNICO por request (lo exige la doc de Walmart).
        # Usar un valor fijo causa rechazos intermitentes por deduplicación.
        "WM_QOS.CORRELATION_ID": str(uuid.uuid4()),
        "WM_SEC.ACCESS_TOKEN": get_token(),
        "WM_MARKET": "cl",
        "Accept": "application/json",
        "Content-Type": "application/json"
    }

# ── INVENTARIO ──

def actualizar_stock_walmart(sku, cantidad):
    """Actualiza stock en Walmart Chile para UN SKU específico (single-publicación)."""
    try:
        headers = walmart_headers()
        headers["Content-Type"] = "application/json"
        payload = {
            "sku": sku,
            "quantity": {"unit": "EACH", "amount": int(cantidad)}
        }
        res = requests.put(
            f"{WALMART_BASE_URL}/v3/inventory",
            headers=headers,
            json=payload
        )
        ok = res.status_code in [200, 201, 202]
        if ok:
            print(f"[Walmart Stock] SKU:{sku} Status:{res.status_code} OK")
        else:
            # Loguear el body del error para diagnosticar (SKU inexistente,
            # publicación inactiva, formato, etc.)
            print(f"[Walmart Stock] SKU:{sku} Status:{res.status_code} FAIL Body:{res.text[:400]}")
        return ok
    except Exception as e:
        print(f"[Walmart] Error stock {sku}: {e}")
        return False


def _actualizar_stock_walmart_detalle(sku, cantidad):
    """Igual que actualizar_stock_walmart pero devuelve (ok, detalle) con el
    body/status del error, para diagnóstico fino en el endpoint de test."""
    try:
        headers = walmart_headers()
        headers["Content-Type"] = "application/json"
        payload = {
            "sku": sku,
            "quantity": {"unit": "EACH", "amount": int(cantidad)}
        }
        res = requests.put(
            f"{WALMART_BASE_URL}/v3/inventory",
            headers=headers,
            json=payload
        )
        ok = res.status_code in [200, 201, 202]
        if ok:
            return True, f"status {res.status_code}"
        return False, f"status {res.status_code}: {res.text[:250]}"
    except Exception as e:
        return False, f"excepción: {str(e)[:200]}"


def actualizar_stock_walmart_lusync(sku_lusync, cantidad):
    """Actualiza stock en Walmart para TODAS las publicaciones de un SKU Lusync.

    Aunque Walmart usualmente tiene 1 publicación por SKU, este wrapper soporta
    múltiples por consistencia con MELI/París.

    Returns:
        dict: {ok, total_publicaciones, exitosas, fallidas, log}
    """
    from inventario import obtener_publicaciones_canal
    publicaciones = obtener_publicaciones_canal(sku_lusync, "walmart")

    # Fallback legacy
    if not publicaciones:
        try:
            from inventario import get_sku_canal
            sku_legacy = get_sku_canal(sku_lusync, "walmart")
            if sku_legacy:
                publicaciones = [{"id": None, "sku_canal": sku_legacy, "item_id_canal": None}]
        except: pass
        if not publicaciones:
            publicaciones = [{"id": None, "sku_canal": sku_lusync, "item_id_canal": None}]

    exitosas, fallidas = 0, 0
    log = []
    for pub in publicaciones:
        sku_wm = (pub.get("sku_canal") or "").strip()
        if not sku_wm:
            fallidas += 1
            log.append(f"  Publicación sin sku_canal")
            continue
        # Llamar capturando el detalle del error para diagnóstico
        ok, detalle = _actualizar_stock_walmart_detalle(sku_wm, cantidad)
        if ok:
            exitosas += 1
            log.append(f"  {sku_wm}: OK")
        else:
            fallidas += 1
            log.append(f"  {sku_wm}: FAIL — {detalle}")

    return {
        "ok": exitosas > 0,
        "total_publicaciones": len(publicaciones),
        "exitosas": exitosas,
        "fallidas": fallidas,
        "log": log
    }


def actualizar_precio_walmart_lusync(sku_lusync, precio):
    """Actualiza precio en TODAS las publicaciones Walmart de un SKU Lusync."""
    from inventario import obtener_publicaciones_canal
    publicaciones = obtener_publicaciones_canal(sku_lusync, "walmart")
    if not publicaciones:
        try:
            from inventario import get_sku_canal
            sku_legacy = get_sku_canal(sku_lusync, "walmart")
            if sku_legacy:
                publicaciones = [{"id": None, "sku_canal": sku_legacy, "item_id_canal": None}]
        except: pass
        if not publicaciones:
            publicaciones = [{"id": None, "sku_canal": sku_lusync, "item_id_canal": None}]

    exitosas, fallidas = 0, 0
    log = []
    for pub in publicaciones:
        sku_wm = (pub.get("sku_canal") or "").strip()
        if not sku_wm:
            fallidas += 1
            continue
        ok = actualizar_precio_walmart(sku_wm, precio)
        if ok: exitosas += 1
        else: fallidas += 1
        log.append(f"  {sku_wm}: {'OK' if ok else 'FAIL'}")

    return {"ok": exitosas > 0, "total_publicaciones": len(publicaciones),
            "exitosas": exitosas, "fallidas": fallidas, "log": log}

# ── PRECIOS ──

def actualizar_precio_walmart(sku, precio):
    try:
        precio_int = int(round(precio))
        payload = {
            "PriceHeader": {"version": "1.7"},
            "Price": [{
                "pricing": [{
                    "currentPriceType": "BASE",
                    "currentPrice": {
                        "currency": "CLP",
                        "amount": precio_int
                    }
                }],
                "sku": sku
            }]
        }
        res = requests.put(
            f"{WALMART_BASE_URL}/v3/price",
            headers=walmart_headers(),
            json=payload,
            params={"feedType": "price"}
        )
        return res.status_code in [200, 201, 202]
    except Exception as e:
        print(f"[Walmart] Error precio {sku}: {e}")
        return False

# ── PRODUCTOS / ITEMS (para auto-mapeo de SKUs) ──

def obtener_productos_walmart(limit=50, max_paginas=20, debug=False):
    """Lista los productos publicados del seller en Walmart Chile.

    Walmart Chile/MX limita a 50 items por página máximo.
    Para traer ~200 productos usar max_paginas=4-5.

    Returns:
        list de dicts con: sku, productName, price, availableInventory, status
    """
    try:
        todas = []
        next_cursor = None
        pagina = 0
        debug_log = []

        while pagina < max_paginas:
            pagina += 1
            # IMPORTANTE: Walmart Chile máximo 50 por página
            params = {"limit": min(limit, 50)}
            if next_cursor and next_cursor != "*":
                params["nextCursor"] = next_cursor

            res = requests.get(
                f"{WALMART_BASE_URL}/v3/items",
                headers=walmart_headers(),
                params=params,
                timeout=30
            )
            print(f"[Walmart Items] Página:{pagina} Status:{res.status_code}")
            debug_log.append(f"Página {pagina}: HTTP {res.status_code}")

            if res.status_code != 200:
                err_msg = res.text[:500]
                print(f"[Walmart Items] Error: {err_msg}")
                debug_log.append(f"Error body: {err_msg}")
                break

            data = res.json()
            # ── Soportar MÚLTIPLES estructuras posibles de respuesta ──
            # Estructura A (Walmart Chile/MX modern):
            #   { "ItemResponse": [...], "totalItems": N, "nextCursor": "..." }
            # Estructura B (Walmart antiguo):
            #   { "items": [...], "totalItems": N }
            # Estructura C (anidado en payload):
            #   { "payload": { "items": [...] } }
            # Estructura D (response anidado):
            #   { "response": { "items": [...] } }

            items = None
            estructura_usada = "?"

            if isinstance(data, dict):
                # Probar todas las estructuras conocidas
                if "ItemResponse" in data and isinstance(data["ItemResponse"], list):
                    items = data["ItemResponse"]
                    estructura_usada = "ItemResponse"
                elif "items" in data and isinstance(data["items"], list):
                    items = data["items"]
                    estructura_usada = "items"
                elif "payload" in data:
                    p = data["payload"]
                    if isinstance(p, dict):
                        items = p.get("items") or p.get("ItemResponse")
                        estructura_usada = "payload.items"
                elif "response" in data:
                    r = data["response"]
                    if isinstance(r, dict):
                        items = r.get("items") or r.get("ItemResponse")
                        estructura_usada = "response.items"
                # Última opción: ver si data es directamente una lista de items en alguna key
                if items is None:
                    debug_log.append(f"Keys disponibles: {list(data.keys())}")
                    # Buscar la primera key que contenga una lista no vacía
                    for k, v in data.items():
                        if isinstance(v, list) and len(v) > 0:
                            items = v
                            estructura_usada = f"auto:{k}"
                            break
            elif isinstance(data, list):
                items = data
                estructura_usada = "lista_directa"

            if items is None:
                debug_log.append(f"No se pudo encontrar items en respuesta. Keys: {list(data.keys()) if isinstance(data, dict) else type(data).__name__}")
                debug_log.append(f"Sample respuesta (primeros 300 chars): {str(data)[:300]}")
                break

            if isinstance(items, dict):
                items = [items]

            debug_log.append(f"Estructura: {estructura_usada}, items en página: {len(items)}")
            print(f"[Walmart Items] Estructura detectada: {estructura_usada}, +{len(items)} items")

            for item in items:
                # Soportar todos los campos posibles según versión del API
                sku = (item.get("sku") or item.get("itemSku") or
                       item.get("SKU") or item.get("seller_sku") or "")
                name = (item.get("productName") or item.get("name") or
                        item.get("title") or item.get("productTitle") or "")
                wpid = (item.get("wpid") or item.get("itemId") or
                        item.get("walmartItemId") or "")

                producto = {
                    "sku":               sku,
                    "productName":       name,
                    "price":             None,
                    "availableInventory": item.get("availableInventory") or item.get("inventory") or item.get("quantity") or 0,
                    "status":            (item.get("publishedStatus") or item.get("status") or
                                          item.get("itemStatus") or item.get("lifecycleStatus") or ""),
                    "wpid":              wpid
                }
                # Extraer precio de distintas estructuras
                price_raw = item.get("price") or item.get("currentPrice") or item.get("listPrice")
                if isinstance(price_raw, dict):
                    producto["price"] = (price_raw.get("amount") or
                                         (price_raw.get("currentPrice") or {}).get("amount") or
                                         price_raw.get("value"))
                elif isinstance(price_raw, (int, float)):
                    producto["price"] = price_raw
                elif isinstance(price_raw, str):
                    try: producto["price"] = float(price_raw)
                    except: pass
                if producto["price"] is None:
                    pricing = item.get("pricing") or {}
                    cp = pricing.get("currentPrice") or {}
                    producto["price"] = cp.get("amount")

                todas.append(producto)

            print(f"[Walmart Items] Total acumulado:{len(todas)}")

            # Ver si hay siguiente página
            if isinstance(data, dict):
                next_cursor = data.get("nextCursor") or data.get("cursor")
                # En algunas versiones el cursor está en metadata
                if not next_cursor:
                    meta = data.get("meta") or data.get("metadata") or {}
                    if isinstance(meta, dict):
                        next_cursor = meta.get("nextCursor")
            if not next_cursor or next_cursor == "*":
                break

        if debug:
            return {"items": todas, "debug_log": debug_log}
        return todas
    except Exception as e:
        import traceback
        print(f"[Walmart] Error productos: {e}")
        print(traceback.format_exc())
        if debug:
            return {"items": [], "debug_log": [f"EXCEPTION: {e}", traceback.format_exc()]}
        return []


# ── ÓRDENES CON PAGINACIÓN ──

def obtener_ordenes_walmart(estado="Created", fecha_desde=None, max_paginas=2, limit=50, dias=30):
    """Obtiene órdenes de Walmart Chile con paginación limitada para no agotar memoria.

    Args:
        estado: 'Created', 'Acknowledged', 'Shipped', 'Delivered'
        fecha_desde: ISO date opcional. Si no se pasa, usa últimos 'dias' días
        max_paginas: máximo de páginas a traer (default 2 = 100 órdenes max si limit=50)
        limit: órdenes por página (Walmart soporta hasta 200)
        dias: días hacia atrás (default 30) si no se pasa fecha_desde
    """
    try:
        if fecha_desde:
            fecha_inicio = fecha_desde
        else:
            fecha_inicio = (datetime.utcnow() - timedelta(days=dias)).strftime("%Y-%m-%dT00:00:00.000Z")
        todas = []
        next_cursor = None
        pagina = 0

        while pagina < max_paginas:
            pagina += 1
            params = {
                "createdStartDate": fecha_inicio,
                "limit": limit,
                # Por defecto Walmart SOLO devuelve órdenes seller-fulfilled.
                # Para incluir también las WFS (Walmart Fulfillment Services) y 3PL,
                # hay que pedir explícitamente shipNodeType. Sin esto, las ventas
                # despachadas por Walmart nunca llegan a Lusync.
                "shipNodeType": "SellerFulfilled,WFSFulfilled,3PLFulfilled",
            }
            if estado:
                params["status"] = estado
            if next_cursor and next_cursor != "-1":
                params["nextCursor"] = next_cursor

            res = requests.get(
                f"{WALMART_BASE_URL}/v3/orders",
                headers=walmart_headers(),
                params=params,
                timeout=20
            )
            print(f"[Walmart Ordenes] Estado:{estado} Página:{pagina} Status:{res.status_code}")

            if res.status_code != 200:
                print(f"[Walmart Ordenes] Error: {res.text[:200]}")
                break

            data = res.json()
            lista = data.get("list", {})
            meta = lista.get("meta", {})
            ordenes = lista.get("elements", {}).get("order", [])

            if isinstance(ordenes, dict):
                ordenes = [ordenes]

            todas.extend(ordenes)
            print(f"[Walmart Ordenes] Página:{pagina} +{len(ordenes)} Total:{len(todas)}")

            next_cursor = meta.get("nextCursor")
            if not next_cursor or next_cursor == "-1":
                break

        return todas
    except Exception as e:
        print(f"[Walmart] Error órdenes: {e}")
        return []

def confirmar_orden_walmart(purchase_order_id):
    try:
        res = requests.post(
            f"{WALMART_BASE_URL}/v3/orders/{purchase_order_id}/acknowledge",
            headers=walmart_headers()
        )
        return res.status_code in [200, 201, 202]
    except Exception as e:
        print(f"[Walmart] Error confirmando orden: {e}")
        return False

def verificar_conexion_walmart():
    try:
        token = get_token()
        return token is not None
    except:
        return False


# ═══════════════════════════════════════════════════════════════════════════
# BLUEPRINT - Endpoints HTTP de Walmart
# ═══════════════════════════════════════════════════════════════════════════
# Este Blueprint se registra desde app.py con:
#   from walmart import walmart_bp
#   app.register_blueprint(walmart_bp)

from flask import Blueprint, jsonify, request, session

walmart_bp = Blueprint('walmart', __name__)


@walmart_bp.route("/walmart/sync_ordenes")
def walmart_sync_ordenes():
    """Sincronización manual de órdenes Walmart desde la UI.
    Usa lógica de bodegas: detecta WFS automáticamente y descuenta
    de la bodega correcta (CENTRAL si es Seller, WALMART_FBM si es WFS)."""
    if not session.get("logged"):
        return jsonify({"error": "no autorizado"}), 401

    # Imports locales para evitar circular imports
    from inventario import (cargar_productos, orden_ya_procesada_texto,
                            marcar_orden_procesada_texto, registrar_audit,
                            listar_sku_mapeo)
    from bodegas_logic import descontar_venta, sincronizar_stock_a_marketplaces

    registrar_audit(session.get("usuario","Sistema"), request.remote_addr,
                    "sync_walmart", entidad="ordenes",
                    detalle="Sync manual órdenes Walmart")

    productos_dict = {p["sku"]: p for p in cargar_productos()}
    nuevas = 0
    errores = []
    log = []

    # Walmart Chile usa Created/Acknowledged para órdenes pendientes
    # ESTADOS PRIORIZADOS: empezar por los nuevos primero (menos órdenes)
    # Solo procesar Delivered si específicamente se pide via ?incluir_delivered=1
    incluir_delivered = request.args.get("incluir_delivered", "0") == "1"
    dias = int(request.args.get("dias", 7))   # default últimos 7 días
    estados = ["Created", "Acknowledged", "Shipped"]
    if incluir_delivered:
        estados.append("Delivered")

    for estado in estados:
        try:
            # Solo 1 página de 50 órdenes por estado para no agotar RAM
            ordenes = obtener_ordenes_walmart(estado, max_paginas=1, limit=50, dias=dias)
            log.append(f"Estado {estado}: {len(ordenes)} órdenes")

            for o in ordenes:
                order_id = o.get("purchaseOrderId")
                if not order_id:
                    continue

                # Usar customerOrderId (número largo) para evitar duplicados
                customer_order_id = str(o.get("customerOrderId", order_id))
                if orden_ya_procesada_texto(customer_order_id):
                    continue

                # Marcar ANTES de procesar para evitar dobles descuentos
                marcar_orden_procesada_texto(customer_order_id)

                # Detectar si es WFS (Walmart Fulfillment Services)
                from bodegas_logic import detectar_fulfillment_walmart
                es_wfs = detectar_fulfillment_walmart(o)
                tipo_str = "WFS" if es_wfs else "Seller"

                lineas = o.get("orderLines", {}).get("orderLine", [])
                if isinstance(lineas, dict):
                    lineas = [lineas]

                for linea in lineas:
                    try:
                        sku_walmart = linea.get("item", {}).get("sku")
                        if not sku_walmart:
                            continue

                        # Determinar cantidad
                        cantidad = 1
                        qty = linea.get("orderLineQuantity", {})
                        if qty and qty.get("amount"):
                            cantidad = int(float(qty.get("amount", 1)))
                        if cantidad == 1:
                            status_qty = linea.get("statusQuantity", {})
                            if status_qty and status_qty.get("amount"):
                                cantidad = int(float(status_qty.get("amount", 1)))

                        # Buscar SKU Lusync: PRIORIDAD sku_mapeo_canal, fallback legacy
                        sku_lusync = None
                        try:
                            from inventario import obtener_sku_lusync_por_canal
                            sku_lusync = obtener_sku_lusync_por_canal("walmart", sku_canal=sku_walmart)
                        except: pass
                        if not sku_lusync:
                            try:
                                for fila in listar_sku_mapeo():
                                    if fila.get("sku_walmart") == sku_walmart:
                                        sku_lusync = fila.get("sku_lusync")
                                        break
                            except: pass
                        if not sku_lusync:
                            sku_lusync = sku_walmart  # último fallback

                        if sku_lusync not in productos_dict:
                            log.append(f"{customer_order_id}: SKU '{sku_lusync}' no encontrado")
                            continue

                        # Descontar usando lógica de bodegas
                        resultado = descontar_venta(
                            sku=sku_lusync,
                            cantidad=cantidad,
                            canal="Walmart",
                            fulfillment=es_wfs,
                            orden_id=customer_order_id,
                            motivo=f"Venta Walmart {tipo_str}"
                        )
                        log.append(f"{customer_order_id} {tipo_str}: {sku_lusync} -{cantidad} desde {resultado['bodega']}")

                        # Sync cruzado a otros canales SOLO si fue Seller (afectó Central)
                        if not es_wfs:
                            try:
                                sincronizar_stock_a_marketplaces(sku_lusync, excepto=["walmart"])
                            except Exception as e:
                                log.append(f"  Sync cruzado falló: {e}")

                    except Exception as e:
                        errores.append(str(e))
                        log.append(f"  Error línea: {e}")

                nuevas += 1
            # Liberar memoria de las órdenes procesadas
            del ordenes
            import gc
            gc.collect()
        except Exception as e:
            errores.append(f"{estado}: {str(e)}")
            log.append(f"Estado {estado}: ERROR {str(e)}")

    return jsonify({
        "ok": True,
        "nuevas_ordenes": nuevas,
        "errores": errores[:5],
        "log": log
    })
