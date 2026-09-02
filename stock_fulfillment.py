"""
stock_fulfillment.py — Lectura del stock almacenado en Fulfillment de cada
marketplace (solo lectura) y cruce con el stock interno de Lusync.

Canales soportados (verificados en vivo):
  - MercadoLibre (Full): GET /inventories/{inventory_id}/stock/fulfillment
  - Walmart (WFS):        GET /v3/fulfillment/inventory
  - Falabella:            GetProducts → BusinessUnits.Stock (stock publicado total)

Paris y Ripley quedan PENDIENTES (aún sin stock Full allá).

IMPORTANTE: este módulo NO modifica stock ni toca los syncs existentes.
Solo consulta las APIs y arma un cruce informativo.
"""

import requests


# ─────────────────────────────────────────────────────────────────────────────
# MERCADOLIBRE (Full)
# ─────────────────────────────────────────────────────────────────────────────
def leer_stock_full_meli(max_items=200):
    """Lee el stock Full de ML. Devuelve dict: {sku_o_itemid: {...}}.
    El SKU se resuelve luego contra el mapeo de Lusync (aquí guardamos item_id).
    """
    from mercadolibre import get_meli_token, MELI_API_URL, meli_headers
    resultado = {}
    try:
        get_meli_token()
        H = meli_headers()
        r = requests.get(f"{MELI_API_URL}/users/me", headers=H, timeout=15)
        uid = r.json().get("id")
        offset = 0
        vistos = 0
        while vistos < max_items:
            rs = requests.get(f"{MELI_API_URL}/users/{uid}/items/search",
                              headers=H, params={"limit": 50, "offset": offset}, timeout=20)
            if rs.status_code != 200:
                break
            items = rs.json().get("results", [])
            if not items:
                break
            for item_id in items:
                vistos += 1
                try:
                    ri = requests.get(f"{MELI_API_URL}/items/{item_id}",
                                      headers=H, params={"attributes": "id,seller_custom_field,inventory_id,variations"},
                                      timeout=15)
                    d = ri.json()
                    # Recolectar (inventory_id, sku_canal, item_id, variation_id)
                    invs = []
                    if d.get("inventory_id"):
                        invs.append((d["inventory_id"], d.get("seller_custom_field"), item_id, None))
                    for v in d.get("variations", []):
                        if v.get("inventory_id"):
                            invs.append((v["inventory_id"], v.get("seller_custom_field"), item_id, v.get("id")))
                    for inv_id, sku_canal, iid, vid in invs:
                        rf = requests.get(f"{MELI_API_URL}/inventories/{inv_id}/stock/fulfillment",
                                          headers=H, timeout=15)
                        if rf.status_code != 200:
                            continue
                        f = rf.json()
                        clave = sku_canal or iid
                        resultado[str(clave)] = {
                            "sku_canal": sku_canal,
                            "item_id": iid,
                            "variation_id": vid,
                            "inventory_id": inv_id,
                            "total": f.get("total", 0),
                            "disponible": f.get("available_quantity", 0),
                            "no_disponible": f.get("not_available_quantity", 0),
                        }
                except Exception:
                    continue
            offset += 50
    except Exception as e:
        print(f"[StockFull ML] error: {e}")
    return resultado


# ─────────────────────────────────────────────────────────────────────────────
# WALMART (WFS)
# ─────────────────────────────────────────────────────────────────────────────
def leer_stock_wfs_walmart():
    """Lee el stock WFS de Walmart. Devuelve dict: {sku: {...}}."""
    from walmart import walmart_headers, WALMART_BASE_URL
    resultado = {}
    try:
        offset = 0
        while True:
            r = requests.get(f"{WALMART_BASE_URL}/v3/fulfillment/inventory",
                             headers=walmart_headers(), params={"limit": 50, "offset": offset}, timeout=25)
            if r.status_code not in (200, 202):
                print(f"[StockFull Walmart] status {r.status_code}: {r.text[:120]}")
                break
            data = r.json()
            payload = data.get("payload", {}) or {}
            inv = payload.get("inventory", []) or []
            if not inv:
                break
            for it in inv:
                sku = it.get("sku")
                nodes = it.get("shipNodes", []) or []
                on_hand = 0
                avail = 0
                for n in nodes:
                    if n.get("shipNodeType") == "WFSFulfilled" or n.get("shipNodeType") is None:
                        on_hand += int(n.get("onHandQty") or 0)
                        avail += int(n.get("availToSellQty") or 0)
                if sku:
                    resultado[str(sku)] = {
                        "sku_canal": sku,
                        "total": on_hand,
                        "disponible": avail,
                        "no_disponible": max(0, on_hand - avail),
                    }
            meta = data.get("headers", {}) or {}
            total = int(meta.get("totalCount") or 0)
            offset += 50
            if offset >= total:
                break
    except Exception as e:
        print(f"[StockFull Walmart] error: {e}")
    return resultado


# ─────────────────────────────────────────────────────────────────────────────
# FALABELLA (stock publicado total — no separa FBF por API)
# ─────────────────────────────────────────────────────────────────────────────
def leer_stock_falabella():
    """Lee el stock FBF (Fulfillment by Falabella) real vía GetStock.
    El stock FBF viene en FulfillmentWarehouses (bodegas de Falabella: San
    Bernardo, Cerrillos, etc.). El FBF de un SKU es la SUMA en todas esas
    bodegas. Devuelve dict: {sku: {...}} solo con SKU que tienen FBF.
    """
    import urllib.parse
    from datetime import datetime
    from hashlib import sha256
    from hmac import HMAC
    from falabella import FALABELLA_USER_ID, FALABELLA_API_KEY, FALABELLA_BASE_URL

    def _call(offset):
        params = {
            "Action": "GetStock", "Format": "JSON", "UserID": FALABELLA_USER_ID,
            "Version": "1.0",
            "Timestamp": datetime.now().astimezone().replace(microsecond=0).isoformat(),
            "Limit": 1000, "Offset": offset,
        }
        q = "&".join("%s=%s" % (k, urllib.parse.quote(str(params[k]), safe=""))
                     for k in sorted(params))
        params["Signature"] = HMAC(FALABELLA_API_KEY.encode(), q.encode(), sha256).hexdigest()
        return requests.get(FALABELLA_BASE_URL, params=params, timeout=30).json()

    resultado = {}
    try:
        offset = 0
        while offset < 5000:
            d = _call(offset)
            stocks = d.get("SuccessResponse", {}).get("Body", {}).get("Stocks", {}) or {}
            sw = stocks.get("SellerWarehouses", [])
            if isinstance(sw, dict):
                sw = [sw]
            fw = stocks.get("FulfillmentWarehouses", [])
            if isinstance(fw, dict):
                fw = [fw]
            # Sumar FBF por SKU (across bodegas), ignorando SKU "_DELETED_"
            for w in (fw or []):
                sku = w.get("Sku")
                if not sku or "_DELETED_" in sku:
                    continue
                try:
                    qty = int(w.get("Quantity") or 0)
                except Exception:
                    qty = 0
                if sku not in resultado:
                    resultado[sku] = {"sku_canal": sku, "total": 0, "disponible": 0, "no_disponible": 0}
                resultado[sku]["total"] += qty
                resultado[sku]["disponible"] += qty
            if len(sw) < 1000 and len(fw) < 1000:
                break
            offset += 1000
    except Exception as e:
        print(f"[StockFull Falabella FBF] error: {e}")
    return resultado


# ─────────────────────────────────────────────────────────────────────────────
# CRUCE con el stock interno de Lusync
# ─────────────────────────────────────────────────────────────────────────────
def cruzar_stock_fulfillment(canales=None):
    """Consulta el stock Full de los canales indicados y lo cruza con el stock
    interno de Lusync (por SKU). Devuelve una lista de filas para la vista.
    """
    canales = canales or ["mercadolibre", "walmart", "falabella"]
    from inventario import cargar_productos, obtener_sku_lusync_por_canal

    # Stock interno Lusync (por SKU)
    productos = cargar_productos()
    lusync = {}
    for p in productos:
        lusync[str(p.get("sku"))] = {
            "nombre": p.get("nombre"),
            "stock": p.get("stock", 0),
        }

    # Leer cada canal
    data_canal = {}
    if "mercadolibre" in canales:
        data_canal["mercadolibre"] = leer_stock_full_meli()
    if "walmart" in canales:
        data_canal["walmart"] = leer_stock_wfs_walmart()
    if "falabella" in canales:
        data_canal["falabella"] = leer_stock_falabella()

    # Construir el índice de SKU Lusync por cada entrada de canal
    # (para ML el SKU puede venir None → resolver por item_id)
    filas = {}  # sku_lusync -> fila

    def _fila(sku_lusync):
        if sku_lusync not in filas:
            info = lusync.get(sku_lusync, {})
            filas[sku_lusync] = {
                "sku": sku_lusync,
                "nombre": info.get("nombre"),
                "stock_lusync": info.get("stock"),
                "mercadolibre": None,
                "walmart": None,
                "falabella": None,
            }
        return filas[sku_lusync]

    for canal, data in data_canal.items():
        for clave, val in data.items():
            # Resolver SKU Lusync
            sku_lusync = val.get("sku_canal")
            if not sku_lusync and val.get("item_id"):
                try:
                    sku_lusync = obtener_sku_lusync_por_canal(
                        canal, sku_canal=None, item_id_canal=val.get("item_id"))
                except Exception:
                    sku_lusync = None
            sku_lusync = sku_lusync or clave
            # Si el SKU del canal existe en Lusync, usar ese; si no, igual mostrarlo
            fila = _fila(str(sku_lusync))
            fila[canal] = {
                "total": val.get("total", 0),
                "disponible": val.get("disponible", 0),
                "no_disponible": val.get("no_disponible", 0),
            }

    # Solo devolver filas que tengan stock en algún canal Full (para no listar todo)
    salida = []
    for f in filas.values():
        tiene_full = any(f.get(c) and f[c].get("total", 0) > 0
                         for c in ("mercadolibre", "walmart", "falabella"))
        # incluir también los que tienen registro en algún canal aunque sea 0
        tiene_registro = any(f.get(c) is not None for c in ("mercadolibre", "walmart", "falabella"))
        if tiene_registro:
            salida.append(f)
    # Ordenar: primero los que tienen más stock Full
    salida.sort(key=lambda x: -sum((x.get(c) or {}).get("total", 0)
                                   for c in ("mercadolibre", "walmart", "falabella")))
    return salida


# ─────────────────────────────────────────────────────────────────────────────
# RECONCILIACIÓN: detectar descuadres entre el stock Full de Lusync y el canal
# ─────────────────────────────────────────────────────────────────────────────
# Filosofía: SOLO detecta y alerta. NUNCA ajusta automáticamente. El usuario
# decide si corregir. No escribe nada en los canales (solo lee).

# Bodega Full de Lusync por canal (debe coincidir con determinar_bodega_para_canal)
BODEGA_FULL_POR_CANAL = {
    "mercadolibre": "MELI_FULL",
    "walmart": "WALMART_FBM",
    "falabella": "FALABELLA_FBM",
}


def reconciliar_stock_full(canal):
    """Compara, por SKU, el stock Full que Lusync tiene registrado en su bodega
    interna vs. el que el canal reporta realmente. Devuelve las diferencias.

    NO modifica nada. Solo detecta descuadres para que el usuario decida.

    Devuelve: {
      "canal": ...,
      "bodega": ...,
      "filas": [{sku, nombre, lusync, canal, diferencia, estado}],
      "resumen": {cuadran, descuadran, solo_en_canal, solo_en_lusync}
    }
    """
    canal = (canal or "").lower()
    bodega = BODEGA_FULL_POR_CANAL.get(canal)
    if not bodega:
        return {"error": f"Canal {canal} no soportado para reconciliación Full"}

    from inventario import get_stock_bodega, cargar_productos, obtener_sku_lusync_por_canal

    # 1. Stock real del canal
    if canal == "mercadolibre":
        data_canal = leer_stock_full_meli()
    elif canal == "walmart":
        data_canal = leer_stock_wfs_walmart()
    elif canal == "falabella":
        data_canal = leer_stock_falabella()
    else:
        data_canal = {}

    # Normalizar a {sku_lusync: cantidad_canal}
    canal_por_sku = {}
    for clave, val in data_canal.items():
        sku_lusync = val.get("sku_canal")
        if not sku_lusync and val.get("item_id"):
            try:
                sku_lusync = obtener_sku_lusync_por_canal(canal, sku_canal=None, item_id_canal=val.get("item_id"))
            except Exception:
                sku_lusync = None
        sku_lusync = sku_lusync or clave
        canal_por_sku[str(sku_lusync)] = canal_por_sku.get(str(sku_lusync), 0) + int(val.get("disponible", 0) or 0)

    # 2. Nombres de producto
    nombres = {str(p.get("sku")): p.get("nombre") for p in cargar_productos()}

    # 3. Comparar. Unir SKU que están en el canal o que Lusync tiene en la bodega Full.
    skus = set(canal_por_sku.keys())
    # Agregar los que Lusync tiene en la bodega Full aunque el canal reporte 0
    try:
        from inventario import get_conn, release_conn
        conn = get_conn(tenant_id=1, is_admin=True)
        cur = conn.cursor()
        cur.execute("SELECT sku, cantidad FROM stock_bodega WHERE bodega_codigo=%s AND cantidad<>0", (bodega,))
        lusync_bodega = {str(r[0]): int(r[1] or 0) for r in cur.fetchall()}
        cur.close(); release_conn(conn)
    except Exception as e:
        print(f"[Reconciliar] error leyendo bodega: {e}")
        lusync_bodega = {}
    skus |= set(lusync_bodega.keys())

    filas = []
    cuadran = descuadran = solo_canal = solo_lusync = 0
    for sku in sorted(skus):
        c = canal_por_sku.get(sku, 0)
        l = lusync_bodega.get(sku, 0)
        dif = l - c  # positivo = Lusync tiene de más; negativo = Lusync tiene de menos
        if dif == 0:
            estado = "cuadra"; cuadran += 1
        else:
            estado = "descuadre"; descuadran += 1
            if l == 0:
                solo_canal += 1
            elif c == 0:
                solo_lusync += 1
        filas.append({
            "sku": sku,
            "nombre": nombres.get(sku),
            "lusync": l,
            "canal": c,
            "diferencia": dif,
            "estado": estado,
        })
    # Ordenar: primero los descuadres más grandes
    filas.sort(key=lambda x: -abs(x["diferencia"]))
    return {
        "canal": canal,
        "bodega": bodega,
        "filas": filas,
        "resumen": {
            "total": len(filas),
            "cuadran": cuadran,
            "descuadran": descuadran,
            "solo_en_canal": solo_canal,
            "solo_en_lusync": solo_lusync,
        },
    }
