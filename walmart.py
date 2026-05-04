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

    import base64
    credentials = base64.b64encode(f"{WALMART_CLIENT_ID}:{WALMART_CLIENT_SECRET}".encode()).decode()
    res = requests.post(
        "https://marketplace.walmartapis.com/v3/token",
        headers={
            "Authorization": f"Basic {credentials}",
            "WM_SVC.NAME": "Lusync",
            "WM_QOS.CORRELATION_ID": "lusync-auth",
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
    return {
        "WM_SVC.NAME": "Lusync",
        "WM_QOS.CORRELATION_ID": "lusync-sync",
        "WM_SEC.ACCESS_TOKEN": get_token(),
        "WM_MARKET": "cl",
        "Accept": "application/json",
        "Content-Type": "application/json"
    }

# ── INVENTARIO ──

def actualizar_stock_walmart(sku, cantidad):
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
        print(f"[Walmart Stock] SKU:{sku} Status:{res.status_code}")
        return res.status_code in [200, 201, 202]
    except Exception as e:
        print(f"[Walmart] Error stock {sku}: {e}")
        return False

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

def obtener_productos_walmart(limit=200, max_paginas=5):
    """Lista los productos publicados del seller en Walmart Chile.

    Returns:
        list de dicts con: sku, productName, price, availableInventory, status
    """
    try:
        todas = []
        next_cursor = None
        pagina = 0

        while pagina < max_paginas:
            pagina += 1
            params = {"limit": min(limit, 200)}
            if next_cursor and next_cursor != "*":
                params["nextCursor"] = next_cursor

            res = requests.get(
                f"{WALMART_BASE_URL}/v3/items",
                headers=walmart_headers(),
                params=params,
                timeout=20
            )
            print(f"[Walmart Items] Página:{pagina} Status:{res.status_code}")

            if res.status_code != 200:
                print(f"[Walmart Items] Error: {res.text[:200]}")
                break

            data = res.json()
            # La estructura suele ser:
            # { "ItemResponse": [...], "totalItems": N, "nextCursor": "..." }
            items = data.get("ItemResponse", []) or data.get("items", [])
            if isinstance(items, dict):
                items = [items]

            for item in items:
                # Walmart Chile devuelve estructura variada según versión
                producto = {
                    "sku":               item.get("sku") or item.get("itemSku") or "",
                    "productName":       item.get("productName") or item.get("name") or "",
                    "price":             None,
                    "availableInventory": item.get("availableInventory") or 0,
                    "status":            item.get("publishedStatus") or item.get("status") or "",
                    "wpid":              item.get("wpid") or item.get("itemId") or ""
                }
                # Intentar extraer precio de distintas estructuras posibles
                price_raw = item.get("price")
                if isinstance(price_raw, dict):
                    producto["price"] = price_raw.get("amount") or price_raw.get("currentPrice", {}).get("amount")
                elif isinstance(price_raw, (int, float, str)):
                    try:
                        producto["price"] = float(price_raw)
                    except: pass
                # Algunas versiones lo ponen en .pricing
                if producto["price"] is None:
                    pricing = item.get("pricing") or {}
                    cp = pricing.get("currentPrice") or {}
                    producto["price"] = cp.get("amount")

                todas.append(producto)

            print(f"[Walmart Items] Página:{pagina} +{len(items)} Total:{len(todas)}")

            next_cursor = data.get("nextCursor")
            if not next_cursor or next_cursor == "*":
                break

        return todas
    except Exception as e:
        print(f"[Walmart] Error productos: {e}")
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
                "limit": limit
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

                        # Buscar SKU Lusync vía mapeo
                        sku_lusync = sku_walmart
                        try:
                            for fila in listar_sku_mapeo():
                                if fila.get("sku_walmart") == sku_walmart:
                                    sku_lusync = fila.get("sku_lusync")
                                    break
                        except: pass

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
