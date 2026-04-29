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

# ── ÓRDENES CON PAGINACIÓN ──

def obtener_ordenes_walmart(estado="Created", fecha_desde=None):
    """Obtiene órdenes de Walmart Chile. Si fecha_desde se pasa, solo trae desde ahí."""
    try:
        if fecha_desde:
            fecha_inicio = fecha_desde
        else:
            fecha_inicio = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%dT00:00:00.000Z")
        todas = []
        next_cursor = None

        while True:
            params = {
                "createdStartDate": fecha_inicio,
                "limit": 100
            }
            if estado:
                params["status"] = estado
            if next_cursor and next_cursor != "-1":
                params["nextCursor"] = next_cursor

            res = requests.get(
                f"{WALMART_BASE_URL}/v3/orders",
                headers=walmart_headers(),
                params=params
            )
            print(f"[Walmart Ordenes] Estado:{estado} Status:{res.status_code}")

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
            print(f"[Walmart Ordenes] Página:{len(ordenes)} Total:{len(todas)}")

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
