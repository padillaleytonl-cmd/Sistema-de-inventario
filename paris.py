import requests
import os
import time
import base64
import json
from datetime import datetime, timedelta

PARIS_API_KEY = os.environ.get("PARIS_API_KEY")
PARIS_BASE_URL = "https://api-developers.ecomm.cencosud.com"

_paris_cache = {"token": None, "expires_at": 0, "seller_id": None, "seller_name": None}


def get_paris_token():
    """Autentica con API Key y obtiene JWT. Cachea por 3.5 horas (token dura 4h)."""
    now = time.time()
    if _paris_cache["token"] and now < _paris_cache["expires_at"] - 300:
        return _paris_cache["token"]

    if not PARIS_API_KEY:
        raise Exception("PARIS_API_KEY no configurada")

    res = requests.post(
        f"{PARIS_BASE_URL}/v1/auth/apiKey",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {PARIS_API_KEY}"
        },
        timeout=15
    )

    if res.status_code != 200:
        raise Exception(f"Paris auth error: {res.status_code} {res.text[:200]}")

    data = res.json()
    token = data.get("accessToken")
    expires_in = int(data.get("expiresIn", 14400))

    _paris_cache["token"] = token
    _paris_cache["expires_at"] = now + expires_in

    # Extraer seller_id del JWT automáticamente
    payload = data.get("jwtPayload", {})
    if payload.get("seller_id"):
        _paris_cache["seller_id"] = payload["seller_id"]
        _paris_cache["seller_name"] = payload.get("seller_name")
        print(f"[Paris] Auth OK · Seller: {_paris_cache['seller_name']} ({_paris_cache['seller_id']})")
    else:
        # Decodificar JWT si jwtPayload no viene en la respuesta
        try:
            jwt_parts = token.split(".")
            jwt_payload = jwt_parts[1]
            jwt_payload += "=" * (4 - len(jwt_payload) % 4)
            decoded = json.loads(base64.b64decode(jwt_payload))
            _paris_cache["seller_id"] = decoded.get("seller_id")
            _paris_cache["seller_name"] = decoded.get("seller_name")
            print(f"[Paris] Auth OK (JWT decode) · Seller: {_paris_cache['seller_name']} ({_paris_cache['seller_id']})")
        except Exception as e:
            print(f"[Paris] Auth OK pero no se pudo extraer seller_id: {e}")

    return token


def get_seller_id():
    """Retorna el seller_id, autenticando si es necesario."""
    if not _paris_cache["seller_id"]:
        get_paris_token()
    return _paris_cache["seller_id"]


def paris_headers():
    """Headers estándar con JWT para todas las llamadas."""
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {get_paris_token()}"
    }


# ── STOCK ──

def actualizar_stock_paris(sku_lusync, cantidad):
    """Actualiza stock en París para TODAS las publicaciones de un SKU Lusync.

    Usa sku_mapeo_canal (multi-publicación). Si no hay mapeos, fallback legacy.

    Returns:
        dict: {ok, total_publicaciones, exitosas, fallidas, log}
    """
    from inventario import obtener_publicaciones_canal

    publicaciones = obtener_publicaciones_canal(sku_lusync, "paris")

    # Fallback legacy si no hay mapeo nuevo
    if not publicaciones:
        try:
            from inventario import get_sku_canal
            sku_paris_legacy = get_sku_canal(sku_lusync, "paris")
            if sku_paris_legacy:
                publicaciones = [{"id": None, "sku_canal": sku_paris_legacy, "item_id_canal": None}]
        except: pass
        if not publicaciones:
            # Último fallback: usar el mismo SKU
            publicaciones = [{"id": None, "sku_canal": sku_lusync, "item_id_canal": None}]

    exitosas = 0
    fallidas = 0
    log = []

    for pub in publicaciones:
        sku_paris = (pub.get("sku_canal") or "").strip()  # ej: PBEAMG001 (sku_seller)
        item_id_paris = (pub.get("item_id_canal") or "").strip()  # ej: MK30WHVEX8-1 (sku Paris real)
        if not sku_paris and not item_id_paris:
            fallidas += 1
            log.append(f"  Publicación sin sku_canal ni item_id, skip")
            continue

        # Estrategia: usar /v2/stock con el item_id de Paris (sku Marketplace)
        # Si no tenemos item_id, fallback al endpoint viejo v1 con sku_seller
        try:
            if item_id_paris:
                # Endpoint v2 con sku Marketplace de Paris
                payload = {
                    "skus": [{
                        "sku": item_id_paris,
                        "quantity": int(cantidad)
                    }]
                }
                res = requests.post(
                    f"{PARIS_BASE_URL}/v2/stock",
                    headers=paris_headers(),
                    json=payload,
                    timeout=15
                )
                ok = res.status_code in [200, 201]
                print(f"[Paris Stock v2] item_id:{item_id_paris} sku_seller:{sku_paris} Qty:{cantidad} Status:{res.status_code}")
                log.append(f"  {item_id_paris} (sku_seller={sku_paris}): v2 status {res.status_code} {'OK' if ok else 'FAIL'}")
                if not ok:
                    log.append(f"    body: {res.text[:200]}")
                # Si v2 falla, intentar v1 con sku_seller como fallback
                if not ok and sku_paris:
                    payload_v1 = {
                        "skus": [{
                            "sku_seller": sku_paris,
                            "quantity": int(cantidad)
                        }]
                    }
                    res2 = requests.post(
                        f"{PARIS_BASE_URL}/v1/stock/sku-seller",
                        headers=paris_headers(),
                        json=payload_v1,
                        timeout=15
                    )
                    ok = res2.status_code in [200, 201]
                    print(f"[Paris Stock v1 fallback] sku_seller:{sku_paris} Qty:{cantidad} Status:{res2.status_code}")
                    log.append(f"    v1 fallback status {res2.status_code} {'OK' if ok else 'FAIL'}")
            else:
                # Sin item_id, usar v1 con sku_seller
                payload = {
                    "skus": [{
                        "sku_seller": sku_paris,
                        "quantity": int(cantidad)
                    }]
                }
                res = requests.post(
                    f"{PARIS_BASE_URL}/v1/stock/sku-seller",
                    headers=paris_headers(),
                    json=payload,
                    timeout=15
                )
                ok = res.status_code in [200, 201]
                print(f"[Paris Stock] SKU:{sku_paris} Qty:{cantidad} Status:{res.status_code}")
                log.append(f"  {sku_paris}: status {res.status_code} {'OK' if ok else 'FAIL'}")
                if not ok:
                    log.append(f"    body: {res.text[:200]}")

            if ok:
                exitosas += 1
            else:
                fallidas += 1
        except Exception as e:
            fallidas += 1
            log.append(f"  {sku_paris}: error {e}")
            print(f"[Paris] Error stock {sku_paris}: {e}")

    return {
        "ok": exitosas > 0,
        "total_publicaciones": len(publicaciones),
        "exitosas": exitosas,
        "fallidas": fallidas,
        "log": log
    }


def actualizar_stock_paris_v2(sku_marketplace, cantidad):
    """Actualiza stock en París usando SKU Marketplace (v2)."""
    try:
        payload = {
            "skus": [{
                "sku": sku_marketplace,
                "quantity": int(cantidad)
            }]
        }
        res = requests.post(
            f"{PARIS_BASE_URL}/v2/stock",
            headers=paris_headers(),
            json=payload,
            timeout=15
        )
        print(f"[Paris Stock v2] SKU:{sku_marketplace} Qty:{cantidad} Status:{res.status_code}")
        return res.status_code in [200, 201]
    except Exception as e:
        print(f"[Paris] Error stock v2 {sku_marketplace}: {e}")
        return False


def obtener_stock_paris(limite=100, offset=0):
    """Obtiene todo el stock del seller."""
    try:
        res = requests.get(
            f"{PARIS_BASE_URL}/v2/stock",
            headers=paris_headers(),
            params={"limit": limite, "offset": offset},
            timeout=15
        )
        if res.status_code == 200:
            return res.json()
        print(f"[Paris] Error obteniendo stock: {res.status_code}")
        return None
    except Exception as e:
        print(f"[Paris] Error stock: {e}")
        return None


# ── ÓRDENES ──

def obtener_ordenes_paris(dias=30, estado=None, limite=50, offset=0):
    """Obtiene órdenes/sub-órdenes de París con filtros."""
    try:
        fecha_desde = (datetime.utcnow() - timedelta(days=dias)).strftime("%Y-%m-%d")
        params = {
            "gteCreatedAt": fecha_desde,
            "limit": limite,
            "offset": offset
        }
        if estado:
            params["itemStatus"] = estado

        seller_id = get_seller_id()
        if seller_id:
            params["sellerId"] = seller_id

        res = requests.get(
            f"{PARIS_BASE_URL}/v2/sub-orders",
            headers=paris_headers(),
            params=params,
            timeout=20
        )

        if res.status_code != 200:
            print(f"[Paris Ordenes] Error: {res.status_code} {res.text[:200]}")
            return []

        data = res.json()
        ordenes = data.get("data", [])
        total = data.get("count", 0)
        print(f"[Paris Ordenes] Obtenidas:{len(ordenes)} Total:{total}")
        return ordenes

    except Exception as e:
        print(f"[Paris] Error órdenes: {e}")
        return []


def obtener_orden_paris(sub_order_number):
    """Obtiene una sub-orden específica por número."""
    try:
        res = requests.get(
            f"{PARIS_BASE_URL}/v2/sub-orders/{sub_order_number}",
            headers=paris_headers(),
            timeout=15
        )
        if res.status_code == 200:
            return res.json()
        return None
    except Exception as e:
        print(f"[Paris] Error orden {sub_order_number}: {e}")
        return None


def obtener_ordenes_paris_todas(dias=30, estado=None):
    """Obtiene TODAS las órdenes con paginación automática."""
    todas = []
    offset = 0
    limite = 50

    while True:
        batch = obtener_ordenes_paris(dias=dias, estado=estado, limite=limite, offset=offset)
        if not batch:
            break
        todas.extend(batch)
        if len(batch) < limite:
            break
        offset += limite

    return todas


# ── PRECIOS ──

def actualizar_precio_paris(sku_marketplace, precio_lista, precio_oferta=None,
                            fecha_desde=None, fecha_hasta=None):
    """Actualiza precio de un producto en París (v2)."""
    try:
        precio_lista_int = int(round(precio_lista))
        prices = [{
            "priceTypeId": "list",
            "value": precio_lista_int
        }]

        if precio_oferta and precio_oferta < precio_lista:
            precio_oferta_int = int(round(precio_oferta))
            oferta = {
                "priceTypeId": "offer",
                "value": precio_oferta_int
            }
            if fecha_desde:
                oferta["showFrom"] = fecha_desde
            if fecha_hasta:
                oferta["showTo"] = fecha_hasta
            prices.append(oferta)

        payload = {"prices": prices}
        res = requests.post(
            f"{PARIS_BASE_URL}/v2/prices/product/{sku_marketplace}",
            headers=paris_headers(),
            json=payload,
            timeout=15
        )
        print(f"[Paris Precio] SKU:{sku_marketplace} Lista:{precio_lista_int} Oferta:{precio_oferta} Status:{res.status_code}")
        return res.status_code in [200, 201]
    except Exception as e:
        print(f"[Paris] Error precio {sku_marketplace}: {e}")
        return False


# ── CANCELACIÓN ──

def cancelar_item_paris(sub_order_number, sku, cantidad=1, razon_id=None):
    """Cancela un artículo dentro de una sub-orden."""
    try:
        payload = {
            "status": "unable_to_fulfill",
            "skus": [{"sku": sku, "quantity": int(cantidad)}]
        }
        if razon_id:
            payload["cancellationReasonId"] = razon_id

        res = requests.put(
            f"{PARIS_BASE_URL}/v1/sub-orders/cancel/{sub_order_number}",
            headers=paris_headers(),
            json=payload,
            timeout=15
        )
        print(f"[Paris Cancel] SubOrder:{sub_order_number} SKU:{sku} Status:{res.status_code}")
        return res.status_code in [200, 201]
    except Exception as e:
        print(f"[Paris] Error cancelando {sub_order_number}: {e}")
        return False


def obtener_razones_cancelacion():
    """Obtiene las razones de cancelación disponibles."""
    try:
        res = requests.get(
            f"{PARIS_BASE_URL}/v1/order-item/cancellation-reason",
            headers=paris_headers(),
            timeout=10
        )
        if res.status_code == 200:
            return res.json()
        return []
    except Exception as e:
        print(f"[Paris] Error razones cancelación: {e}")
        return []


# ── PRODUCTOS ──

def obtener_productos_paris(limite=25, offset=0, sku_seller=None):
    """Obtiene productos publicados en París."""
    try:
        params = {"limit": limite, "offset": offset}
        if sku_seller:
            params["identifier"] = sku_seller
            params["typeFilter"] = "REF_ID"

        res = requests.get(
            f"{PARIS_BASE_URL}/v2/products/search",
            headers=paris_headers(),
            params=params,
            timeout=15
        )
        if res.status_code == 200:
            return res.json()
        return None
    except Exception as e:
        print(f"[Paris] Error productos: {e}")
        return None


# ── ETIQUETAS ──

def imprimir_etiqueta_paris(label_id):
    """Genera/imprime la etiqueta de despacho."""
    try:
        res = requests.get(
            f"{PARIS_BASE_URL}/v2/label/print-label/{label_id}",
            headers=paris_headers(),
            timeout=15
        )
        print(f"[Paris Etiqueta] LabelID:{label_id} Status:{res.status_code}")
        return res.status_code == 200
    except Exception as e:
        print(f"[Paris] Error etiqueta {label_id}: {e}")
        return False


# ── VERIFICACIÓN ──

def verificar_conexion_paris():
    """Verifica que la API Key funciona y retorna datos del seller."""
    try:
        token = get_paris_token()
        return {
            "conectado": token is not None,
            "seller_id": _paris_cache.get("seller_id"),
            "seller_name": _paris_cache.get("seller_name")
        }
    except Exception as e:
        return {"conectado": False, "error": str(e)}


# ═══════════════════════════════════════════════════════════════════════════
# BLUEPRINT - Endpoints HTTP de París
# ═══════════════════════════════════════════════════════════════════════════
# Se registra desde app.py con:
#   from paris import paris_bp
#   app.register_blueprint(paris_bp)

from flask import Blueprint, jsonify, request, session

paris_bp = Blueprint('paris', __name__)


@paris_bp.route("/paris/sync_ordenes")
def paris_sync_ordenes():
    """Sincroniza órdenes históricas de París, detectando CD vs Seller automáticamente
    y descontando de la bodega correcta (CENTRAL o PARIS_CD)."""
    if not session.get("logged"):
        return jsonify({"error": "no autorizado"}), 401

    from inventario import (cargar_productos, orden_ya_procesada_texto,
                            marcar_orden_procesada_texto, registrar_audit,
                            listar_sku_mapeo)
    from bodegas_logic import descontar_venta, sincronizar_stock_a_marketplaces

    registrar_audit(session.get("usuario","Sistema"), request.remote_addr,
                    "sync_paris", entidad="ordenes",
                    detalle="Sync manual órdenes Paris")

    dias = int(request.args.get("dias", 30))
    productos_dict = {p["sku"]: p for p in cargar_productos()}
    nuevas = 0
    errores = []
    log = []

    # CAMBIO: NO filtramos por itemStatus porque el campo viene vacío en muchas órdenes
    # En su lugar, traemos TODAS las órdenes y procesamos las que no estén marcadas
    try:
        ordenes = obtener_ordenes_paris_todas(dias=dias, estado=None)
        log.append(f"Órdenes encontradas (sin filtro): {len(ordenes)}")

        for so in ordenes:
            sub_order_num = str(so.get("subOrderNumber", ""))
            if not sub_order_num:
                continue
            paris_key = f"PARIS-{sub_order_num}"
            if orden_ya_procesada_texto(paris_key):
                continue
            marcar_orden_procesada_texto(paris_key)

            # ── Extraer fecha real de compra del marketplace ────────
            # París devuelve createdAt en ISO con timezone (ej: "2026-05-03T05:02:00.000Z" UTC)
            fecha_compra_paris = None
            try:
                date_str = (so.get("createdAt") or so.get("created_at") or "")
                if date_str:
                    # Normalizar 'Z' a '+00:00' para fromisoformat
                    date_str_clean = date_str.replace("Z", "+00:00")
                    # Si el formato tiene milisegundos con '.', fromisoformat los soporta
                    fecha_compra_paris = datetime.fromisoformat(date_str_clean)
            except Exception as e:
                log.append(f"  Sub-orden {sub_order_num}: no se pudo parsear createdAt: {e}")
                fecha_compra_paris = None

            # Detectar Fulfillment vs Seller
            from bodegas_logic import detectar_fulfillment_paris
            es_cd = detectar_fulfillment_paris(so)
            tipo_str = "Fulfillment" if es_cd else "Seller"

            shipments = so.get("shipments", [])
            for ship in shipments:
                items = ship.get("items", [])
                for item in items:
                    sku_paris = item.get("seller_sku") or item.get("sellerSku") or ""
                    cantidad = int(item.get("quantity", 1) or 1)
                    if not sku_paris:
                        continue

                    # Buscar SKU Lusync: PRIORIDAD sku_mapeo_canal, fallback legacy
                    sku_lusync = None
                    try:
                        from inventario import obtener_sku_lusync_por_canal
                        sku_lusync = obtener_sku_lusync_por_canal("paris", sku_canal=sku_paris)
                    except: pass
                    if not sku_lusync:
                        try:
                            for fila in listar_sku_mapeo():
                                if fila.get("sku_paris") == sku_paris:
                                    sku_lusync = fila.get("sku_lusync")
                                    break
                        except: pass
                    if not sku_lusync:
                        sku_lusync = sku_paris  # último fallback

                    if sku_lusync not in productos_dict:
                        log.append(f"{sub_order_num}: SKU '{sku_lusync}' no encontrado")
                        continue

                    # Descontar de bodega correcta vía bodegas_logic
                    resultado = descontar_venta(
                        sku=sku_lusync,
                        cantidad=cantidad,
                        canal="Paris",
                        fulfillment=es_cd,
                        orden_id=sub_order_num,
                        motivo=f"Venta Paris {tipo_str}",
                        fecha_compra_marketplace=fecha_compra_paris,
                        origen_registro="sync_manual"
                    )
                    log.append(f"{sub_order_num} {tipo_str}: {sku_lusync} -{cantidad} desde {resultado['bodega']}")

                    # Sync cruzado SOLO si fue Seller (afectó Central)
                    if not es_cd:
                        try:
                            sincronizar_stock_a_marketplaces(sku_lusync, excepto=["paris"])
                        except Exception as e:
                            log.append(f"  Sync cruzado falló: {e}")

            nuevas += 1

        # Liberar memoria
        del ordenes
        import gc
        gc.collect()
    except Exception as e:
        errores.append(str(e))
        log.append(f"ERROR general: {str(e)}")

    return jsonify({
        "ok": True,
        "nuevas_ordenes": nuevas,
        "errores": errores,
        "log": log
    })


@paris_bp.route("/paris/forzar_sync_todos", methods=["POST"])
def paris_forzar_sync_todos():
    """Re-envía el stock de todos los SKUs mapeados a París (desde bodega CENTRAL)."""
    if not session.get("logged"):
        return jsonify({"ok": False}), 401
    try:
        from inventario import listar_sku_mapeo, get_stock_bodega
        productos_mapeo = listar_sku_mapeo()
        enviados = 0
        fallidos = 0
        for fila in productos_mapeo:
            sku_lusync = fila.get("sku_lusync", "")
            sku_paris = (fila.get("sku_paris", "") or "").strip()
            if not sku_paris or not sku_lusync:
                continue
            # Usar el stock de bodega CENTRAL (el que vendemos como Seller)
            stock_central = get_stock_bodega(sku_lusync, "CENTRAL")
            if actualizar_stock_paris(sku_lusync, stock_central):
                enviados += 1
            else:
                fallidos += 1
        return jsonify({"ok": True, "enviados": enviados, "fallidos": fallidos})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@paris_bp.route("/paris/sync_estado")
def paris_sync_estado():
    """Compara stock en Lusync (bodega CENTRAL) vs stock real en Paris API."""
    if not session.get("logged"):
        return jsonify({"error": "no autorizado"}), 401
    try:
        import requests as req
        from inventario import listar_sku_mapeo, cargar_productos, get_stock_bodega

        conexion = verificar_conexion_paris()
        productos_dict = {p["sku"]: p for p in cargar_productos()}
        mapeo = listar_sku_mapeo()

        # Obtener todo el stock de Paris
        stock_paris_dict = {}
        try:
            res = req.get(f"{PARIS_BASE_URL}/v2/stock", headers=paris_headers(),
                          params={"limit": 200, "offset": 0}, timeout=20)
            if res.status_code == 200:
                data = res.json()
                for s in data.get("skus", []):
                    sku_seller = s.get("sku_seller", "")
                    if sku_seller:
                        stock_paris_dict[sku_seller] = {
                            "quantity": s.get("quantity", 0),
                            "availableStock": s.get("availableStock", 0),
                            "updatedAt": s.get("updatedAt", ""),
                            "warehouseName": s.get("warehouseName", ""),
                            "active": s.get("active", False),
                            "title": s.get("title", "")
                        }
        except Exception as e:
            return jsonify({"error_paris": str(e), "conexion": conexion}), 500

        # Comparar stock CENTRAL vs Paris
        resultados = []
        for fila in mapeo:
            sku_paris = (fila.get("sku_paris", "") or "").strip()
            if not sku_paris:
                continue
            sku_lusync = fila.get("sku_lusync", "")
            stock_central = get_stock_bodega(sku_lusync, "CENTRAL")
            paris_data = stock_paris_dict.get(sku_paris, {})
            stock_paris = paris_data.get("quantity", None)

            if stock_paris is None:
                estado = "no_encontrado"
            elif stock_paris == stock_central:
                estado = "sincronizado"
            else:
                estado = "desincronizado"

            resultados.append({
                "sku_lusync": sku_lusync,
                "sku_paris": sku_paris,
                "nombre": fila.get("nombre", "") or paris_data.get("title", ""),
                "stock_lusync": stock_central,
                "stock_paris": stock_paris,
                "diferencia": (stock_paris - stock_central) if stock_paris is not None else None,
                "ultima_actualizacion_paris": paris_data.get("updatedAt", ""),
                "warehouse_paris": paris_data.get("warehouseName", ""),
                "activo_en_paris": paris_data.get("active", False),
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
