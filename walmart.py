import requests
import os
import time

WALMART_CLIENT_ID = os.environ.get("WALMART_CLIENT_ID")
WALMART_CLIENT_SECRET = os.environ.get("WALMART_CLIENT_SECRET")
WALMART_BASE_URL = "https://marketplace.walmartapis.com"

_token_cache = {"token": None, "expires_at": 0}

def get_token():
    """Obtiene y cachea el access token de Walmart"""
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
    """Actualiza el stock de un producto en Walmart Chile"""
    try:
        # Walmart Chile usa endpoint directo por SKU con query param
        headers = walmart_headers()
        headers["Content-Type"] = "application/json"

        # Método 1: endpoint directo por SKU
        payload = {"quantity": {"unit": "EACH", "amount": int(cantidad)}}
        res = requests.put(
            f"{WALMART_BASE_URL}/v3/inventory",
            headers=headers,
            json=payload,
            params={"sku": sku}
        )
        print(f"[Walmart Stock] SKU:{sku} Status:{res.status_code} Response:{res.text[:300]}")
        return res.status_code in [200, 201, 202]
    except Exception as e:
        print(f"[Walmart] Error actualizando stock {sku}: {e}")
        return False

# ── PRECIOS ──

def actualizar_precio_walmart(sku, precio):
    """Actualiza el precio de un producto en Walmart Chile (sin decimales)"""
    try:
        precio_int = int(round(precio))  # Walmart Chile no acepta decimales
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
        print(f"[Walmart] Error actualizando precio {sku}: {e}")
        return False

# ── ÓRDENES ──

def obtener_ordenes_walmart(estado="Created"):
    """Obtiene órdenes de Walmart con el estado indicado"""
    try:
        res = requests.get(
            f"{WALMART_BASE_URL}/v3/orders",
            headers=walmart_headers(),
            params={
                "status": estado,
                "limit": 100
            }
        )
        if res.status_code != 200:
            return []

        data = res.json()
        ordenes = data.get("list", {}).get("elements", {}).get("order", [])
        if isinstance(ordenes, dict):
            ordenes = [ordenes]
        return ordenes
    except Exception as e:
        print(f"[Walmart] Error obteniendo órdenes: {e}")
        return []

def confirmar_orden_walmart(purchase_order_id):
    """Confirma una orden en Walmart"""
    try:
        res = requests.post(
            f"{WALMART_BASE_URL}/v3/orders/{purchase_order_id}/acknowledge",
            headers=walmart_headers()
        )
        return res.status_code in [200, 201, 202]
    except Exception as e:
        print(f"[Walmart] Error confirmando orden {purchase_order_id}: {e}")
        return False

def verificar_conexion_walmart():
    """Verifica que las credenciales de Walmart sean válidas"""
    try:
        token = get_token()
        return token is not None
    except:
        return False
