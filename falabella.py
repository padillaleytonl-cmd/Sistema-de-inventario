"""
falabella.py — Integración con Falabella Seller Center API

Falabella usa la plataforma propia de Seller Center (heredada de Linio).
Documentación: https://developers.falabella.com/

Características:
- Autenticación: UserID + API Key + Signature HMAC-SHA256
- Formato: XML para body, parámetros en query string
- Endpoints: /UpdateStock, /Price, /GetOrders, /SetStatusToReadyToShip, etc.

Modalidades soportadas:
- Seller (FBS)  → descuenta de bodega CENTRAL
- Fulfillment (FBF) → descuenta de FALABELLA_FBM
"""
import os
import urllib.parse
from hashlib import sha256
from hmac import HMAC
from datetime import datetime, timedelta
import requests
from flask import Blueprint, jsonify, request, session

# ═══════════════════════════════════════════════════════════════════════════
# CONFIGURACIÓN
# ═══════════════════════════════════════════════════════════════════════════
# IMPORTANTE: Las credenciales SIEMPRE deben venir de variables de entorno.
# NUNCA hardcodearlas - exponer credenciales en GitHub es un riesgo de seguridad.
FALABELLA_USER_ID = os.environ.get("FALABELLA_USER_ID", "")
FALABELLA_API_KEY = os.environ.get("FALABELLA_API_KEY", "")
FALABELLA_BASE_URL = os.environ.get("FALABELLA_BASE_URL", "https://sellercenter-api.falabella.com")
FALABELLA_VERSION = "1.0"


# ═══════════════════════════════════════════════════════════════════════════
# AUTENTICACIÓN - Firma HMAC-SHA256
# ═══════════════════════════════════════════════════════════════════════════

def generar_firma_falabella(parameters, api_key=None):
    """Genera la firma HMAC-SHA256 requerida por Falabella Seller Center API.

    Algoritmo:
    1. Ordenar parámetros alfabéticamente por nombre
    2. URL-encode (RFC 3986) cada nombre y valor
    3. Concatenar como name=value separados por &
    4. HMAC-SHA256 usando api_key como clave secreta
    5. Devolver el hash en hexadecimal
    """
    if api_key is None:
        api_key = FALABELLA_API_KEY
    sorted_params = sorted(parameters.items())
    concatenated = urllib.parse.urlencode(sorted_params, quote_via=urllib.parse.quote)
    signature = HMAC(
        api_key.encode("utf-8"),
        concatenated.encode("utf-8"),
        sha256
    ).hexdigest()
    return signature


def construir_parametros_base(action, formato="JSON"):
    """Construye los parámetros comunes a todas las llamadas.

    IMPORTANTE: El Timestamp debe incluir timezone explícito (formato ISO 8601 con offset).
    Falabella rechaza timestamps sin zona con error E003 'Timestamp has expired'.
    Formato correcto: 2026-05-02T21:30:00+00:00
    """
    from datetime import timezone
    return {
        "UserID": FALABELLA_USER_ID,
        "Version": FALABELLA_VERSION,
        "Action": action,
        "Format": formato,
        "Timestamp": datetime.now(timezone.utc).isoformat()
    }


def llamar_api_falabella(action, params_extra=None, body_xml=None,
                          method="GET", formato="JSON", timeout=20):
    """Función genérica para llamar a la API de Falabella.

    Args:
        action: nombre del Action (ej: 'UpdateStock', 'GetProducts')
        params_extra: dict con parámetros adicionales del query string
        body_xml: string XML opcional para POST requests
        method: 'GET' o 'POST'
        formato: 'JSON' o 'XML'
        timeout: timeout en segundos

    Returns:
        dict con {ok, status_code, data, error}
    """
    if not FALABELLA_USER_ID or not FALABELLA_API_KEY:
        return {
            "ok": False,
            "error": "FALABELLA_USER_ID o FALABELLA_API_KEY no configuradas en variables de entorno"
        }

    parameters = construir_parametros_base(action, formato)
    if params_extra:
        for k, v in params_extra.items():
            if v is not None:
                parameters[k] = str(v)

    parameters["Signature"] = generar_firma_falabella(parameters)

    headers = {
        "Accept": "application/json" if formato == "JSON" else "application/xml",
    }
    if body_xml:
        headers["Content-Type"] = "application/xml"

    try:
        if method == "GET":
            res = requests.get(
                FALABELLA_BASE_URL,
                headers=headers,
                params=parameters,
                timeout=timeout
            )
        else:  # POST
            res = requests.post(
                FALABELLA_BASE_URL,
                headers=headers,
                params=parameters,
                data=body_xml.encode("utf-8") if body_xml else None,
                timeout=timeout
            )

        result = {
            "status_code": res.status_code,
            "raw_text": res.text[:2000]
        }
        try:
            result["data"] = res.json()
        except:
            result["data"] = None

        # Falabella devuelve HTTP 200 incluso con errores. Hay que detectar
        # si el body contiene ErrorResponse en lugar de SuccessResponse.
        es_http_ok = res.status_code in (200, 201, 202)
        tiene_error_body = False
        error_msg = ""
        if result["data"] and isinstance(result["data"], dict):
            if "ErrorResponse" in result["data"]:
                tiene_error_body = True
                head = result["data"]["ErrorResponse"].get("Head", {})
                error_msg = f"{head.get('ErrorCode', '?')}: {head.get('ErrorMessage', '?')}"

        result["ok"] = es_http_ok and not tiene_error_body
        if not result["ok"]:
            if tiene_error_body:
                result["error"] = f"Falabella rechazó: {error_msg}"
            else:
                result["error"] = f"HTTP {res.status_code}: {res.text[:300]}"
        return result
    except Exception as e:
        return {"ok": False, "error": str(e)}


def verificar_conexion_falabella():
    """Hace ping a GetSellerByUser para validar credenciales y conexión."""
    if not FALABELLA_USER_ID or not FALABELLA_API_KEY:
        return {
            "ok": False,
            "error": "Credenciales no configuradas",
            "mensaje": "Configura FALABELLA_USER_ID y FALABELLA_API_KEY en Render → Environment"
        }
    res = llamar_api_falabella("GetSellerByUser", method="GET")
    if res["ok"]:
        return {
            "ok": True,
            "status_code": res["status_code"],
            "mensaje": "Conexión exitosa",
            "data": res.get("data")
        }
    return {
        "ok": False,
        "status_code": res.get("status_code"),
        "error": res.get("error", "Error desconocido"),
        "mensaje": "Error al conectar con Falabella"
    }


# ═══════════════════════════════════════════════════════════════════════════
# STOCK
# ═══════════════════════════════════════════════════════════════════════════

def actualizar_stock_falabella(sku, cantidad):
    """Actualiza el stock de UN SKU en Falabella usando UpdateStock (single-publicación).

    El body es XML con estructura:
    <Request>
      <Product>
        <SellerSku>SKU001</SellerSku>
        <Quantity>10</Quantity>
      </Product>
    </Request>
    """
    body_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Request>
  <Product>
    <SellerSku>{sku}</SellerSku>
    <Quantity>{int(cantidad)}</Quantity>
  </Product>
</Request>"""

    res = llamar_api_falabella(
        "UpdateStock",
        body_xml=body_xml,
        method="POST",
        formato="JSON"
    )
    if res["ok"]:
        print(f"[Falabella] Stock {sku}={cantidad} OK")
        return True
    print(f"[Falabella] Stock {sku} ERROR: {res.get('error')}")
    return False


def actualizar_stock_falabella_lusync(sku_lusync, cantidad):
    """Actualiza stock en Falabella para TODAS las publicaciones de un SKU Lusync.

    Returns:
        dict: {ok, total_publicaciones, exitosas, fallidas, log}
    """
    from inventario import obtener_publicaciones_canal
    publicaciones = obtener_publicaciones_canal(sku_lusync, "falabella")
    if not publicaciones:
        try:
            from inventario import get_sku_canal
            sku_legacy = get_sku_canal(sku_lusync, "falabella")
            if sku_legacy:
                publicaciones = [{"id": None, "sku_canal": sku_legacy, "item_id_canal": None}]
        except: pass
        if not publicaciones:
            publicaciones = [{"id": None, "sku_canal": sku_lusync, "item_id_canal": None}]

    exitosas, fallidas = 0, 0
    log = []
    for pub in publicaciones:
        sku_fa = (pub.get("sku_canal") or "").strip()
        if not sku_fa:
            fallidas += 1
            continue
        ok = actualizar_stock_falabella(sku_fa, cantidad)
        if ok: exitosas += 1
        else: fallidas += 1
        log.append(f"  {sku_fa}: {'OK' if ok else 'FAIL'}")

    return {"ok": exitosas > 0, "total_publicaciones": len(publicaciones),
            "exitosas": exitosas, "fallidas": fallidas, "log": log}


def actualizar_precio_falabella_lusync(sku_lusync, precio_normal, precio_oferta=None):
    """Wrapper precios Falabella loop por publicaciones."""
    from inventario import obtener_publicaciones_canal
    publicaciones = obtener_publicaciones_canal(sku_lusync, "falabella")
    if not publicaciones:
        try:
            from inventario import get_sku_canal
            sku_legacy = get_sku_canal(sku_lusync, "falabella")
            if sku_legacy:
                publicaciones = [{"id": None, "sku_canal": sku_legacy, "item_id_canal": None}]
        except: pass
        if not publicaciones:
            publicaciones = [{"id": None, "sku_canal": sku_lusync, "item_id_canal": None}]

    exitosas, fallidas = 0, 0
    log = []
    for pub in publicaciones:
        sku_fa = (pub.get("sku_canal") or "").strip()
        if not sku_fa:
            fallidas += 1
            continue
        ok = actualizar_precio_falabella(sku_fa, precio_normal, precio_oferta)
        if ok: exitosas += 1
        else: fallidas += 1
        log.append(f"  {sku_fa}: {'OK' if ok else 'FAIL'}")

    return {"ok": exitosas > 0, "total_publicaciones": len(publicaciones),
            "exitosas": exitosas, "fallidas": fallidas, "log": log}


def actualizar_stocks_falabella_lote(skus_cantidades):
    """Actualiza múltiples SKUs en una sola llamada (más eficiente).

    Args:
        skus_cantidades: dict {sku: cantidad} o lista de tuplas [(sku, cantidad), ...]
    """
    if isinstance(skus_cantidades, dict):
        items = list(skus_cantidades.items())
    else:
        items = list(skus_cantidades)
    if not items:
        return False, "Lista vacía"

    productos_xml = ""
    for sku, cantidad in items:
        productos_xml += f"""  <Product>
    <SellerSku>{sku}</SellerSku>
    <Quantity>{int(cantidad)}</Quantity>
  </Product>
"""

    body_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Request>
{productos_xml}</Request>"""

    res = llamar_api_falabella(
        "UpdateStock",
        body_xml=body_xml,
        method="POST",
        formato="JSON",
        timeout=30
    )
    if res["ok"]:
        print(f"[Falabella] Lote {len(items)} stocks OK")
        return True, {"enviados": len(items)}
    print(f"[Falabella] Lote ERROR: {res.get('error')}")
    return False, res.get("error", "Error desconocido")


# ═══════════════════════════════════════════════════════════════════════════
# PRECIOS
# ═══════════════════════════════════════════════════════════════════════════

def actualizar_precio_falabella(sku, precio_normal, precio_oferta=None):
    """Actualiza el precio de un SKU en Falabella usando ProductUpdate.

    El precio se envía a través del endpoint Price (o ProductUpdate dependiendo
    de la versión). Aquí usamos ProductUpdate que es el más estándar.
    """
    sale_price_xml = ""
    if precio_oferta and float(precio_oferta) > 0 and float(precio_oferta) < float(precio_normal):
        hoy = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        fin = (datetime.utcnow() + timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        sale_price_xml = f"""    <SalePrice>{int(precio_oferta)}</SalePrice>
    <SaleStartDate>{hoy}</SaleStartDate>
    <SaleEndDate>{fin}</SaleEndDate>"""

    body_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Request>
  <Product>
    <SellerSku>{sku}</SellerSku>
    <Price>{int(precio_normal)}</Price>
{sale_price_xml}
  </Product>
</Request>"""

    res = llamar_api_falabella(
        "ProductUpdate",
        body_xml=body_xml,
        method="POST",
        formato="JSON"
    )
    if res["ok"]:
        print(f"[Falabella] Precio {sku}={precio_normal} OK")
        return True
    print(f"[Falabella] Precio {sku} ERROR: {res.get('error')}")
    return False


# ═══════════════════════════════════════════════════════════════════════════
# PRODUCTOS / OFERTAS
# ═══════════════════════════════════════════════════════════════════════════

def obtener_productos_falabella(limit=100, offset=0, filter_status="all"):
    """Lista los productos del seller en Falabella.

    filter_status: 'all', 'live', 'inactive', 'deleted', 'image-missing',
                   'pending', 'rejected', 'sold-out'
    """
    res = llamar_api_falabella(
        "GetProducts",
        params_extra={
            "Limit": limit,
            "Offset": offset,
            "Filter": filter_status
        },
        method="GET",
        formato="JSON"
    )
    if not res["ok"]:
        return []
    data = res.get("data", {}) or {}
    body = data.get("SuccessResponse", {}).get("Body", {}) or {}
    products = body.get("Products", {}).get("Product", [])
    if isinstance(products, dict):
        products = [products]
    return products


# ═══════════════════════════════════════════════════════════════════════════
# ÓRDENES
# ═══════════════════════════════════════════════════════════════════════════

def obtener_ordenes_falabella(estado=None, dias=30, limit=50, offset=0):
    """Obtiene órdenes del seller.

    Estados Falabella: 'pending', 'canceled', 'ready_to_ship', 'shipped',
                       'delivered', 'returned', 'failed'
    """
    fecha_desde = (datetime.utcnow() - timedelta(days=dias)).strftime("%Y-%m-%dT00:00:00")
    params = {
        "CreatedAfter": fecha_desde,
        "Limit": limit,
        "Offset": offset
    }
    if estado:
        params["Status"] = estado

    res = llamar_api_falabella(
        "GetOrders",
        params_extra=params,
        method="GET",
        formato="JSON"
    )
    if not res["ok"]:
        return []
    data = res.get("data", {}) or {}
    # Defensa: si data es lista en vez de dict, devolver vacío
    if not isinstance(data, dict):
        print(f"[Falabella] Respuesta inesperada (data no es dict): {type(data).__name__}")
        return []
    success = data.get("SuccessResponse", {}) or {}
    if not isinstance(success, dict):
        return []
    body = success.get("Body", {}) or {}
    if not isinstance(body, dict):
        return []
    orders_container = body.get("Orders", {}) or {}
    if not isinstance(orders_container, dict):
        # A veces "Orders" puede venir como lista directamente
        if isinstance(orders_container, list):
            return orders_container
        return []
    orders = orders_container.get("Order", [])
    if isinstance(orders, dict):
        orders = [orders]
    if not isinstance(orders, list):
        return []
    return orders


def obtener_orden_falabella(order_id):
    """Detalle completo de una orden con sus items."""
    res = llamar_api_falabella(
        "GetOrder",
        params_extra={"OrderId": order_id},
        method="GET",
        formato="JSON"
    )
    if not res["ok"]:
        return None
    return res.get("data")


def obtener_items_orden_falabella(order_id):
    """Items de una orden específica."""
    res = llamar_api_falabella(
        "GetOrderItems",
        params_extra={"OrderId": order_id},
        method="GET",
        formato="JSON"
    )
    if not res["ok"]:
        return []
    data = res.get("data", {}) or {}
    body = data.get("SuccessResponse", {}).get("Body", {}) or {}
    items = body.get("OrderItems", {}).get("OrderItem", [])
    if isinstance(items, dict):
        items = [items]
    return items


# ═══════════════════════════════════════════════════════════════════════════
# BLUEPRINT - Endpoints HTTP
# ═══════════════════════════════════════════════════════════════════════════
falabella_bp = Blueprint("falabella", __name__)


@falabella_bp.route("/falabella/test")
def falabella_test():
    """Verifica conexión con Falabella Seller Center."""
    if not session.get("logged"): return jsonify({"error": "no autorizado"}), 401
    return jsonify(verificar_conexion_falabella())


@falabella_bp.route("/falabella/estado")
def falabella_estado():
    """Estado completo + resumen ofertas."""
    if not session.get("logged"): return jsonify({"error": "no autorizado"}), 401
    conn = verificar_conexion_falabella()
    productos = []
    if conn.get("ok"):
        try:
            productos = obtener_productos_falabella(limit=10)
        except: pass
    return jsonify({
        "conectado": bool(conn.get("ok")),
        "conexion": conn,
        "productos_visibles": len(productos),
        "credenciales_configuradas": bool(FALABELLA_USER_ID and FALABELLA_API_KEY),
        "base_url": FALABELLA_BASE_URL
    })


@falabella_bp.route("/falabella/productos")
def falabella_listar_productos():
    """Lista los productos del seller."""
    if not session.get("logged"): return jsonify({"error": "no autorizado"}), 401
    productos = obtener_productos_falabella(limit=100)
    return jsonify({
        "total": len(productos),
        "productos": [
            {
                "seller_sku": p.get("SellerSku"),
                "shop_sku": p.get("ShopSku"),
                "name": (p.get("Name") or "")[:80],
                "price": p.get("Price"),
                "sale_price": p.get("SalePrice"),
                "quantity": p.get("Available") or p.get("Quantity"),
                "status": p.get("Status")
            } for p in productos[:50]
        ]
    })


@falabella_bp.route("/falabella/sync_stock", methods=["POST"])
def falabella_sync_stock():
    """Envía a Falabella el stock CENTRAL de todos los SKUs mapeados."""
    if not session.get("logged"): return jsonify({"ok": False}), 401
    try:
        from inventario import listar_sku_mapeo, get_stock_bodega, registrar_audit
        registrar_audit(session.get("usuario","Sistema"), request.remote_addr,
                        "sync_falabella_stock", detalle="Sync masivo stock Falabella")

        productos_mapeo = listar_sku_mapeo()
        skus_a_enviar = {}
        log = []
        for fila in productos_mapeo:
            sku_lusync = fila.get("sku_lusync", "")
            sku_falabella = (fila.get("sku_falabella", "") or "").strip()
            if not sku_falabella or not sku_lusync:
                continue
            stock = get_stock_bodega(sku_lusync, "CENTRAL")
            skus_a_enviar[sku_falabella] = stock
            log.append(f"→ {sku_falabella}={stock}u")

        if not skus_a_enviar:
            return jsonify({"ok": True, "enviados": 0, "fallidos": 0,
                            "log": ["Sin SKUs mapeados a Falabella"]})

        ok, info = actualizar_stocks_falabella_lote(skus_a_enviar)
        if ok:
            return jsonify({
                "ok": True,
                "enviados": len(skus_a_enviar),
                "fallidos": 0,
                "nota": "Stock enviado. Falabella tarda 5-15min en reflejar.",
                "log": log[:30]
            })
        return jsonify({"ok": False, "enviados": 0, "fallidos": len(skus_a_enviar),
                        "error": str(info), "log": log[:30]})
    except Exception as e:
        import traceback
        return jsonify({"ok": False, "error": str(e), "trace": traceback.format_exc()}), 500


@falabella_bp.route("/falabella/sync_precios", methods=["POST"])
def falabella_sync_precios():
    """Envía a Falabella los precios de todos los SKUs mapeados."""
    if not session.get("logged"): return jsonify({"ok": False}), 401
    try:
        from inventario import listar_sku_mapeo, get_conn, registrar_audit
        registrar_audit(session.get("usuario","Sistema"), request.remote_addr,
                        "sync_falabella_precios", detalle="Sync masivo precios Falabella")

        conn = get_conn(); cur = conn.cursor()
        cur.execute("SELECT sku, precio_normal, precio_oferta FROM productos")
        precios = {r[0]: {"normal": r[1], "oferta": r[2]} for r in cur.fetchall()}
        cur.close(); conn.close()

        productos_mapeo = listar_sku_mapeo()
        enviados = 0
        fallidos = 0
        log = []
        for fila in productos_mapeo:
            sku_lusync = fila.get("sku_lusync", "")
            sku_falabella = (fila.get("sku_falabella", "") or "").strip()
            if not sku_falabella or not sku_lusync:
                continue
            p = precios.get(sku_lusync, {})
            precio = p.get("normal") or 0
            if not precio:
                log.append(f"⚠ {sku_lusync} sin precio_normal")
                continue
            ok = actualizar_precio_falabella(sku_falabella, precio, p.get("oferta"))
            if ok:
                enviados += 1
                log.append(f"✓ {sku_falabella} → ${precio}")
            else:
                fallidos += 1
                log.append(f"× {sku_falabella} falló")
        return jsonify({"ok": True, "enviados": enviados, "fallidos": fallidos, "log": log[:30]})
    except Exception as e:
        import traceback
        return jsonify({"ok": False, "error": str(e), "trace": traceback.format_exc()}), 500


@falabella_bp.route("/falabella/sync_ordenes")
def falabella_sync_ordenes():
    """Sincroniza órdenes históricas de Falabella descontando del stock por bodega."""
    if not session.get("logged"): return jsonify({"ok": False}), 401
    try:
        from inventario import (cargar_productos, orden_ya_procesada_texto,
                                marcar_orden_procesada_texto, registrar_audit,
                                listar_sku_mapeo)
        from bodegas_logic import descontar_venta, sincronizar_stock_a_marketplaces, detectar_fulfillment_falabella

        registrar_audit(session.get("usuario","Sistema"), request.remote_addr,
                        "sync_falabella", entidad="ordenes",
                        detalle="Sync manual órdenes Falabella")

        dias = int(request.args.get("dias", 30))
        productos_dict = {p["sku"]: p for p in cargar_productos()}
        nuevas = 0
        errores = []
        log = []

        # Estados que indican órdenes activas
        estados = ["pending", "ready_to_ship", "shipped"]
        for estado in estados:
            try:
                ordenes = obtener_ordenes_falabella(estado=estado, dias=dias, limit=50)
                log.append(f"Estado {estado}: {len(ordenes)} órdenes")

                for o in ordenes:
                    order_id = str(o.get("OrderId") or o.get("OrderNumber") or "")
                    if not order_id:
                        continue
                    fb_key = f"FALABELLA-{order_id}"
                    if orden_ya_procesada_texto(fb_key):
                        continue
                    marcar_orden_procesada_texto(fb_key)

                    # ── Extraer fecha real de compra del marketplace ────────
                    # Falabella SellerCenter devuelve CreatedAt en formato:
                    # "2026-05-03 14:32:15" (sin timezone, asume UTC)
                    # o a veces "2026-05-03T14:32:15+0000"
                    fecha_compra_falabella = None
                    try:
                        import pytz as _pytz
                        date_str = (o.get("CreatedAt") or o.get("created_at") or "")
                        if date_str:
                            # Probar formato con T y zona
                            try:
                                date_str_clean = date_str.replace("Z", "+00:00")
                                fecha_compra_falabella = datetime.fromisoformat(date_str_clean)
                            except ValueError:
                                # Fallback: formato "YYYY-MM-DD HH:MM:SS" sin tz, asumir UTC
                                fecha_naive = datetime.strptime(date_str.strip(), "%Y-%m-%d %H:%M:%S")
                                fecha_compra_falabella = _pytz.utc.localize(fecha_naive)
                    except Exception as e:
                        log.append(f"  Orden {order_id}: no se pudo parsear CreatedAt: {e}")
                        fecha_compra_falabella = None

                    es_fbf = detectar_fulfillment_falabella(o)
                    tipo_str = "FBF" if es_fbf else "FBS"

                    items = obtener_items_orden_falabella(order_id)
                    for line in items:
                        sku_falabella = line.get("SellerSku") or ""
                        cantidad = 1  # Falabella separa cada item, generalmente cantidad=1 por línea
                        if not sku_falabella:
                            continue

                        # Buscar SKU Lusync: PRIORIDAD sku_mapeo_canal, fallback legacy
                        sku_lusync = None
                        try:
                            from inventario import obtener_sku_lusync_por_canal
                            sku_lusync = obtener_sku_lusync_por_canal("falabella", sku_canal=sku_falabella)
                        except: pass
                        if not sku_lusync:
                            try:
                                for fila in listar_sku_mapeo():
                                    if fila.get("sku_falabella") == sku_falabella:
                                        sku_lusync = fila.get("sku_lusync")
                                        break
                            except: pass
                        if not sku_lusync:
                            sku_lusync = sku_falabella  # último fallback

                        if sku_lusync not in productos_dict:
                            log.append(f"{order_id}: SKU '{sku_lusync}' no encontrado")
                            continue

                        resultado = descontar_venta(
                            sku=sku_lusync,
                            cantidad=cantidad,
                            canal="Falabella",
                            fulfillment=es_fbf,
                            orden_id=order_id,
                            motivo=f"Venta Falabella {tipo_str}",
                            fecha_compra_marketplace=fecha_compra_falabella,
                            origen_registro="sync_manual"
                        )
                        log.append(f"{order_id} {tipo_str}: {sku_lusync} -{cantidad} desde {resultado['bodega']}")

                        if not es_fbf:
                            try:
                                sincronizar_stock_a_marketplaces(sku_lusync, excepto=["falabella"])
                            except Exception as e:
                                log.append(f"  Sync cruzado falló: {e}")

                    nuevas += 1
                del ordenes
                import gc; gc.collect()
            except Exception as e:
                errores.append(f"{estado}: {str(e)}")
                log.append(f"Estado {estado}: ERROR {str(e)}")

        return jsonify({
            "ok": True,
            "nuevas_ordenes": nuevas,
            "errores": errores,
            "log": log
        })
    except Exception as e:
        import traceback
        return jsonify({"ok": False, "error": str(e), "trace": traceback.format_exc()}), 500


@falabella_bp.route("/falabella/sync_estado")
def falabella_sync_estado():
    """Compara stock CENTRAL vs Falabella. Devuelve mismo formato que /paris/sync_estado."""
    if not session.get("logged"): return jsonify({"error": "no autorizado"}), 401
    try:
        from inventario import listar_sku_mapeo, get_stock_bodega
        conexion = verificar_conexion_falabella()

        # Obtener productos de Falabella
        productos_fb = []
        try:
            productos_fb = obtener_productos_falabella(limit=100)
        except Exception as e:
            return jsonify({"error_falabella": str(e), "conexion": conexion}), 500

        # Indexar por SellerSku
        fb_dict = {}
        for p in productos_fb:
            seller_sku = p.get("SellerSku", "")
            if seller_sku:
                fb_dict[seller_sku] = {
                    "quantity": int(p.get("Available") or p.get("Quantity") or 0),
                    "price": p.get("Price"),
                    "title": p.get("Name", ""),
                    "status": p.get("Status", "")
                }

        # Comparar
        mapeo = listar_sku_mapeo()
        resultados = []
        for fila in mapeo:
            sku_falabella = (fila.get("sku_falabella", "") or "").strip()
            if not sku_falabella:
                continue
            sku_lusync = fila.get("sku_lusync", "")
            stock_central = get_stock_bodega(sku_lusync, "CENTRAL")
            fb_data = fb_dict.get(sku_falabella, {})
            stock_fb = fb_data.get("quantity", None) if fb_data else None

            if stock_fb is None:
                estado = "no_encontrado"
            elif stock_fb == stock_central:
                estado = "sincronizado"
            else:
                estado = "desincronizado"

            resultados.append({
                "sku_lusync": sku_lusync,
                "sku_paris": sku_falabella,  # nombre genérico para template
                "sku_falabella": sku_falabella,
                "nombre": fila.get("nombre", "") or fb_data.get("title", ""),
                "stock_lusync": stock_central,
                "stock_paris": stock_fb,  # nombre genérico
                "stock_falabella": stock_fb,
                "diferencia": (stock_fb - stock_central) if stock_fb is not None else None,
                "ultima_actualizacion_paris": "",
                "status_falabella": fb_data.get("status", ""),
                "estado": estado
            })

        return jsonify({
            "conexion": conexion,
            "total": len(resultados),
            "sincronizados": sum(1 for r in resultados if r["estado"] == "sincronizado"),
            "desincronizados": sum(1 for r in resultados if r["estado"] == "desincronizado"),
            "no_encontrados": sum(1 for r in resultados if r["estado"] == "no_encontrado"),
            "resultados": resultados
        })
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@falabella_bp.route("/falabella/forzar_sync_sku", methods=["POST"])
def falabella_forzar_sync_sku():
    """Reenvía stock de un SKU específico a Falabella."""
    if not session.get("logged"): return jsonify({"ok": False}), 401
    try:
        from inventario import listar_sku_mapeo, get_stock_bodega
        data = request.json or {}
        sku_lusync = data.get("sku_lusync", "")
        if not sku_lusync:
            return jsonify({"ok": False, "error": "sku_lusync requerido"}), 400

        sku_falabella = None
        for fila in listar_sku_mapeo():
            if fila.get("sku_lusync") == sku_lusync:
                sku_falabella = (fila.get("sku_falabella", "") or "").strip()
                break
        if not sku_falabella:
            return jsonify({"ok": False, "error": f"SKU {sku_lusync} no tiene mapeo Falabella"}), 400

        stock = get_stock_bodega(sku_lusync, "CENTRAL")
        ok = actualizar_stock_falabella(sku_falabella, stock)
        if ok:
            return jsonify({
                "ok": True,
                "stock_enviado": stock,
                "sku_falabella": sku_falabella,
                "nota": "Falabella tarda 5-15min en procesar"
            })
        return jsonify({
            "ok": False,
            "error": "Falabella rechazó el envío",
            "sku_falabella": sku_falabella,
            "stock_intentado": stock
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@falabella_bp.route("/falabella/debug_stock/<sku>")
def falabella_debug_stock(sku):
    """Endpoint de debug: intenta enviar stock=1 al SKU dado y muestra la respuesta cruda."""
    if not session.get("logged"): return jsonify({"error": "no autorizado"}), 401
    try:
        cantidad_prueba = int(request.args.get("cantidad", 1))

        body_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Request>
  <Product>
    <SellerSku>{sku}</SellerSku>
    <Quantity>{cantidad_prueba}</Quantity>
  </Product>
</Request>"""

        # Llamada manual para ver TODA la respuesta
        parameters = construir_parametros_base("UpdateStock", "JSON")
        parameters["Signature"] = generar_firma_falabella(parameters)

        res = requests.post(
            FALABELLA_BASE_URL,
            headers={"Accept": "application/json", "Content-Type": "application/xml"},
            params=parameters,
            data=body_xml.encode("utf-8"),
            timeout=20
        )

        return jsonify({
            "url": FALABELLA_BASE_URL,
            "action": "UpdateStock",
            "sku": sku,
            "cantidad": cantidad_prueba,
            "params_enviados": parameters,
            "body_xml": body_xml,
            "status_code": res.status_code,
            "response_text": res.text[:2000],
            "response_headers": dict(res.headers)
        })
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


# ═══════════════════════════════════════════════════════════════════════════
# WEBHOOKS - Receptor de notificaciones de Falabella
# ═══════════════════════════════════════════════════════════════════════════

@falabella_bp.route("/falabella/webhook", methods=["POST", "GET"])
def falabella_webhook():
    """Endpoint receptor de webhooks de Falabella.

    Falabella envía POST con JSON cuando ocurre un evento configurado.
    Ejemplos de payload:
      - order_created: {"event": "onOrderCreated", "data": {"OrderId": "...", "Items": [...]}}
      - order_items_status_changed: {"event": "...", "data": {"OrderId": "...", "Status": "shipped"}}
      - order_canceled: {"event": "onOrderCanceled", "data": {"OrderId": "..."}}

    Falabella espera HTTP 200 para confirmar recepción. Si devolvemos error,
    reintentará automáticamente.
    """
    # Falabella primero hace un GET de prueba al configurar el webhook
    if request.method == "GET":
        return jsonify({
            "ok": True,
            "mensaje": "Webhook receptor Lusync activo. Esperando eventos de Falabella.",
            "service": "Lusync ERP"
        })

    try:
        payload = request.get_json(silent=True) or {}
        evento = payload.get("event", "desconocido")
        data = payload.get("data", {})

        print(f"[Falabella WEBHOOK] Evento: {evento} · payload: {str(payload)[:300]}")

        # Procesar según tipo de evento
        if evento in ("onOrderCreated", "OrderCreated", "order_created"):
            procesar_webhook_orden_creada(data)
        elif evento in ("onOrderItemsStatusChanged", "OrderItemsStatusChanged"):
            procesar_webhook_estado_orden(data)
        elif evento in ("onOrderCanceled", "OrderCanceled"):
            procesar_webhook_orden_cancelada(data)
        elif evento in ("onProductCreated", "ProductCreated"):
            procesar_webhook_producto(data, accion="creado")
        elif evento in ("onProductUpdated", "ProductUpdated"):
            procesar_webhook_producto(data, accion="actualizado")
        elif evento in ("onFeedCompleted", "FeedCompleted"):
            print(f"[Falabella WEBHOOK] Feed completado: {data}")
        else:
            print(f"[Falabella WEBHOOK] Evento sin handler: {evento}")

        # SIEMPRE devolver 200 para que Falabella no reintente
        return jsonify({"ok": True, "evento": evento}), 200
    except Exception as e:
        import traceback
        # Loggeamos el error pero devolvemos 200 para no entrar en loop de reintentos
        print(f"[Falabella WEBHOOK] ERROR: {e}")
        print(traceback.format_exc())
        return jsonify({"ok": False, "error": str(e)}), 200


def procesar_webhook_orden_creada(data):
    """Cuando llega una orden nueva, descontar stock automáticamente."""
    try:
        from inventario import (cargar_productos, orden_ya_procesada_texto,
                                marcar_orden_procesada_texto, listar_sku_mapeo)
        from bodegas_logic import descontar_venta, sincronizar_stock_a_marketplaces, detectar_fulfillment_falabella

        order_id = str(data.get("OrderId") or data.get("OrderNumber") or "")
        if not order_id:
            print("[Falabella WEBHOOK] order_created sin OrderId")
            return

        # Idempotencia
        fb_key = f"FALABELLA-{order_id}"
        if orden_ya_procesada_texto(fb_key):
            print(f"[Falabella WEBHOOK] Orden {order_id} ya procesada, skip")
            return
        marcar_orden_procesada_texto(fb_key)

        # ── Extraer fecha real de compra del marketplace ────────
        fecha_compra_falabella = None
        try:
            import pytz as _pytz
            date_str = (data.get("CreatedAt") or data.get("created_at") or "")
            if date_str:
                try:
                    date_str_clean = date_str.replace("Z", "+00:00")
                    fecha_compra_falabella = datetime.fromisoformat(date_str_clean)
                except ValueError:
                    fecha_naive = datetime.strptime(date_str.strip(), "%Y-%m-%d %H:%M:%S")
                    fecha_compra_falabella = _pytz.utc.localize(fecha_naive)
        except Exception as e:
            print(f"[Falabella WEBHOOK] No se pudo parsear CreatedAt: {e}")

        # Si el payload no trae items, los obtenemos por API
        items = data.get("Items") or data.get("OrderItems")
        if not items:
            items = obtener_items_orden_falabella(order_id)
        if isinstance(items, dict):
            items = [items]

        # Detectar fulfillment (FBF si Falabella maneja la logística)
        es_fbf = detectar_fulfillment_falabella(data)
        tipo_str = "FBF" if es_fbf else "FBS"

        productos_dict = {p["sku"]: p for p in cargar_productos()}
        for item in items or []:
            sku_falabella = item.get("SellerSku") or item.get("ShopSku") or ""
            cantidad = int(item.get("Quantity", 1) or 1)
            if not sku_falabella:
                continue

            # Buscar SKU Lusync en mapeo
            sku_lusync = sku_falabella
            for fila in listar_sku_mapeo():
                if fila.get("sku_falabella") == sku_falabella:
                    sku_lusync = fila.get("sku_lusync")
                    break

            if sku_lusync not in productos_dict:
                print(f"[Falabella WEBHOOK] {order_id}: SKU '{sku_lusync}' no encontrado")
                continue

            resultado = descontar_venta(
                sku=sku_lusync,
                cantidad=cantidad,
                canal="Falabella",
                fulfillment=es_fbf,
                orden_id=order_id,
                motivo=f"Webhook Falabella {tipo_str}",
                fecha_compra_marketplace=fecha_compra_falabella,
                origen_registro="webhook"
            )
            print(f"[Falabella WEBHOOK] {order_id} {tipo_str}: {sku_lusync} -{cantidad} desde {resultado.get('bodega')}")

            # Sync cruzado solo si fue Seller (afectó CENTRAL)
            if not es_fbf:
                try:
                    sincronizar_stock_a_marketplaces(sku_lusync, excepto=["falabella"])
                except Exception as e:
                    print(f"[Falabella WEBHOOK] Sync cruzado falló: {e}")
    except Exception as e:
        import traceback
        print(f"[Falabella WEBHOOK] procesar_orden_creada ERROR: {e}")
        print(traceback.format_exc())


def procesar_webhook_estado_orden(data):
    """Cuando cambia el estado de items de una orden (shipped, delivered, etc.)."""
    try:
        order_id = str(data.get("OrderId") or "")
        nuevo_estado = data.get("Status") or data.get("NewStatus") or ""
        print(f"[Falabella WEBHOOK] Orden {order_id} cambió a estado: {nuevo_estado}")
        # Por ahora solo loggeamos. A futuro podemos:
        # - Si pasa a 'delivered': marcar orden como completada
        # - Si pasa a 'returned': reintegrar stock con reintegrar_venta()
    except Exception as e:
        print(f"[Falabella WEBHOOK] procesar_estado ERROR: {e}")


def procesar_webhook_orden_cancelada(data):
    """Cuando se cancela una orden, reintegrar el stock que se había descontado."""
    try:
        from inventario import listar_sku_mapeo
        from bodegas_logic import reintegrar_venta

        order_id = str(data.get("OrderId") or "")
        if not order_id:
            return

        items = data.get("Items") or data.get("OrderItems") or []
        if not items:
            items = obtener_items_orden_falabella(order_id)
        if isinstance(items, dict):
            items = [items]

        for item in items:
            sku_falabella = item.get("SellerSku") or ""
            cantidad = int(item.get("Quantity", 1) or 1)
            if not sku_falabella:
                continue

            # Mapear a SKU Lusync
            sku_lusync = sku_falabella
            for fila in listar_sku_mapeo():
                if fila.get("sku_falabella") == sku_falabella:
                    sku_lusync = fila.get("sku_lusync")
                    break

            try:
                reintegrar_venta(
                    sku=sku_lusync,
                    cantidad=cantidad,
                    canal="Falabella",
                    orden_id=order_id,
                    motivo="Cancelación Falabella (webhook)"
                )
                print(f"[Falabella WEBHOOK] Reintegrado {sku_lusync} +{cantidad} de orden {order_id}")
            except Exception as e:
                print(f"[Falabella WEBHOOK] Reintegro falló: {e}")
    except Exception as e:
        print(f"[Falabella WEBHOOK] procesar_cancelacion ERROR: {e}")


def procesar_webhook_producto(data, accion="creado"):
    """Cuando se crea o actualiza un producto en Falabella."""
    try:
        seller_sku = data.get("SellerSku") or ""
        print(f"[Falabella WEBHOOK] Producto {accion}: {seller_sku}")
        # Aquí se podría auto-crear el producto en Lusync si no existe,
        # o actualizar nombre/precio. Por ahora solo loggeamos.
    except Exception as e:
        print(f"[Falabella WEBHOOK] procesar_producto ERROR: {e}")


# ═══════════════════════════════════════════════════════════════════════════
# WEBHOOKS - Gestión (crear, listar, eliminar)
# ═══════════════════════════════════════════════════════════════════════════

@falabella_bp.route("/falabella/webhooks/listar")
def falabella_listar_webhooks():
    """Lista los webhooks que tienes configurados en Falabella."""
    if not session.get("logged"): return jsonify({"error": "no autorizado"}), 401
    res = llamar_api_falabella("GetWebhooks", method="GET", formato="JSON")
    return jsonify(res)


@falabella_bp.route("/falabella/webhooks/crear", methods=["POST"])
def falabella_crear_webhook():
    """Crea un webhook desde Lusync (alternativa al panel manual de Falabella).

    Body JSON: {"callback_url": "...", "events": ["onOrderCreated", ...]}
    """
    if not session.get("logged"): return jsonify({"error": "no autorizado"}), 401
    try:
        body = request.json or {}
        callback_url = body.get("callback_url", f"{request.host_url.rstrip('/')}/falabella/webhook")
        events = body.get("events", ["onOrderCreated", "onOrderItemsStatusChanged", "onOrderCanceled"])

        # CreateWebhook usa body XML
        events_xml = "".join(f"<Event>{e}</Event>" for e in events)
        body_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Request>
  <Webhook>
    <CallbackUrl>{callback_url}</CallbackUrl>
    <Events>{events_xml}</Events>
  </Webhook>
</Request>"""

        res = llamar_api_falabella(
            "CreateWebhook",
            body_xml=body_xml,
            method="POST",
            formato="JSON"
        )
        return jsonify(res)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@falabella_bp.route("/falabella/webhooks/eliminar/<webhook_id>", methods=["POST", "DELETE"])
def falabella_eliminar_webhook(webhook_id):
    """Elimina un webhook por ID."""
    if not session.get("logged"): return jsonify({"error": "no autorizado"}), 401
    res = llamar_api_falabella(
        "DeleteWebhook",
        params_extra={"WebhookId": webhook_id},
        method="POST",
        formato="JSON"
    )
    return jsonify(res)


@falabella_bp.route("/falabella/webhooks/eventos_disponibles")
def falabella_eventos_disponibles():
    """Lista los eventos a los que se puede suscribir."""
    if not session.get("logged"): return jsonify({"error": "no autorizado"}), 401
    res = llamar_api_falabella("GetWebhookEntities", method="GET", formato="JSON")
    return jsonify(res)
