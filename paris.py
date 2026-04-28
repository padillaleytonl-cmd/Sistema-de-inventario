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

def actualizar_stock_paris(sku_seller, cantidad):
    """Actualiza stock en París usando SKU Seller."""
    try:
        payload = {
            "skus": [{
                "skuSeller": sku_seller,
                "quantity": int(cantidad)
            }]
        }
        res = requests.post(
            f"{PARIS_BASE_URL}/v1/stock/sku-seller",
            headers=paris_headers(),
            json=payload,
            timeout=15
        )
        print(f"[Paris Stock] SKU:{sku_seller} Qty:{cantidad} Status:{res.status_code}")
        return res.status_code in [200, 201]
    except Exception as e:
        print(f"[Paris] Error stock {sku_seller}: {e}")
        return False


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
