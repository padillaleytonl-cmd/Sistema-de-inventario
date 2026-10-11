# -*- coding: utf-8 -*-
"""Lectura del stock que cada canal tiene publicado, para compararlo con
Lusync. Lusync manda: lo que el canal muestre distinto se corrige.

Solo se lee el stock que publica Lusync (bodega propia del vendedor), nunca el
de Full: ese lo administra el marketplace desde su bodega.

Cada lector devuelve {sku_en_el_canal: cantidad}. Una cantidad None significa
que el canal no la entrego (no se puede comparar).
"""
import requests

_TIMEOUT = 20


def leer_stock_walmart_vendedor(skus_walmart):
    """Stock de vendedor (no WFS) de cada SKU, uno por uno (/v3/inventory)."""
    import time
    from walmart import walmart_headers, WALMART_BASE_URL
    salida = {}
    for i, s in enumerate(sorted(set(skus_walmart))):
        try:
            r = requests.get(f"{WALMART_BASE_URL}/v3/inventory", headers=walmart_headers(),
                             params={"sku": s}, timeout=_TIMEOUT)
            if r.status_code == 200:
                salida[s] = int(((r.json() or {}).get("quantity") or {}).get("amount"))
            else:
                salida[s] = None
        except Exception:
            salida[s] = None
        if (i + 1) % 20 == 0:
            time.sleep(1)  # no saturar la API
    return salida


def leer_stock_woo():
    """Stock de la tienda Web. Un producto o variacion sin "gestionar
    inventario" se informa como "sin_control": la tienda lo vende sin limite."""
    from woo import _woo_api, _woo_auth
    salida = {}
    pagina = 1
    while pagina <= 50:
        r = requests.get(_woo_api() + "/products", timeout=_TIMEOUT,
                         params={**_woo_auth(), "per_page": 100, "page": pagina,
                                 "_fields": "id,sku,type,manage_stock,stock_quantity"})
        if r.status_code != 200:
            break
        productos = r.json() or []
        if not productos:
            break
        for p in productos:
            if p.get("type") == "variable":
                rv = requests.get(f"{_woo_api()}/products/{p['id']}/variations", timeout=_TIMEOUT,
                                  params={**_woo_auth(), "per_page": 100,
                                          "_fields": "sku,manage_stock,stock_quantity"})
                variaciones = rv.json() if rv.status_code == 200 else []
                for v in (variaciones or []):
                    if v.get("sku"):
                        salida[v["sku"]] = (v.get("stock_quantity") if v.get("manage_stock") is True
                                            else "sin_control")
            if p.get("sku") and p.get("type") in ("simple", "variable"):
                if p.get("type") == "simple" or p.get("manage_stock") is True:
                    salida[p["sku"]] = (p.get("stock_quantity") if p.get("manage_stock") is True
                                        else "sin_control")
        if len(productos) < 100:
            break
        pagina += 1
    return salida


def leer_stock_falabella_vendedor():
    """Stock de vendedor de Falabella (GetStock, SellerWarehouses). El de
    Falabella Full viene aparte, en FulfillmentWarehouses, y no se toca."""
    import urllib.parse
    from datetime import datetime
    from hashlib import sha256
    from hmac import HMAC
    from falabella import FALABELLA_BASE_URL, _fal_user_id, _fal_api_key

    def _call(offset):
        params = {"Action": "GetStock", "Format": "JSON", "UserID": _fal_user_id(),
                  "Version": "1.0",
                  "Timestamp": datetime.now().astimezone().replace(microsecond=0).isoformat(),
                  "Limit": 1000, "Offset": offset}
        q = "&".join("%s=%s" % (k, urllib.parse.quote(str(params[k]), safe=""))
                     for k in sorted(params))
        params["Signature"] = HMAC(_fal_api_key().encode(), q.encode(), sha256).hexdigest()
        return requests.get(FALABELLA_BASE_URL, params=params, timeout=30).json()

    salida = {}
    offset = 0
    while offset < 5000:
        d = _call(offset)
        stocks = (d.get("SuccessResponse", {}).get("Body", {}) or {}).get("Stocks", {}) or {}
        sw = stocks.get("SellerWarehouses", []) or []
        if isinstance(sw, dict):
            sw = [sw]
        for w in sw:
            sku = w.get("Sku")
            if not sku or "_DELETED_" in sku:
                continue
            try:
                salida[sku] = salida.get(sku, 0) + int(w.get("Quantity") or 0)
            except (TypeError, ValueError):
                salida.setdefault(sku, None)
        if len(sw) < 1000:
            break
        offset += 1000
    return salida


def leer_stock_ripley():
    """Stock de las ofertas de Ripley (Mirakl /api/offers), todas las paginas.
    ripley.obtener_ofertas_ripley trae solo las primeras 100."""
    from ripley import ripley_headers, RIPLEY_BASE_URL
    salida = {}
    offset = 0
    while offset < 5000:
        r = requests.get(f"{RIPLEY_BASE_URL}/api/offers", headers=ripley_headers(),
                         params={"max": 100, "offset": offset}, timeout=_TIMEOUT)
        if r.status_code != 200:
            break
        ofertas = (r.json() or {}).get("offers", []) or []
        for o in ofertas:
            if o.get("shop_sku"):
                salida[o["shop_sku"]] = o.get("quantity")
        if len(ofertas) < 100:
            break
        offset += 100
    return salida


def leer_stock_meli_propio(publicaciones):
    """Stock PROPIO (bodega del vendedor) de cada publicacion de MercadoLibre.

    publicaciones: lista de (sku_canal, item_id). Devuelve {sku_canal: qty}.
    Una publicacion puede tener stock Full y propio a la vez (bi-modal): su
    available_quantity los suma. Por eso se lee /user-products/{id}/stock y se
    toma solo la ubicacion selling_address. Se saltan las de catalogo (las
    maneja MercadoLibre) y las que son solo Full.
    Si un mismo SKU esta en varias publicaciones, se toma la mayor: es la que
    puede sobrevender.
    """
    import time
    from mercadolibre import meli_headers, MELI_API_URL
    salida = {}
    items_vistos = {}
    for i, (sku_canal, item_id) in enumerate(publicaciones):
        item_id = str(item_id or "").strip()
        if not item_id.upper().startswith("MLC"):
            continue
        try:
            if item_id not in items_vistos:
                r = requests.get(f"{MELI_API_URL}/items/{item_id}?include_attributes=all",
                                 headers=meli_headers(), timeout=_TIMEOUT)
                items_vistos[item_id] = r.json() if r.status_code == 200 else None
            item = items_vistos[item_id]
            if not item or item.get("catalog_listing"):
                continue
            upid = item.get("user_product_id")
            cantidad_item = item.get("available_quantity")
            variaciones = item.get("variations") or []
            if variaciones:
                objetivo = None
                for v in variaciones:
                    v_sku = next((a.get("value_name", "") for a in (v.get("attributes") or [])
                                  if a.get("id") == "SELLER_SKU"), "") or v.get("seller_custom_field") or ""
                    if v_sku and v_sku.strip().upper() == str(sku_canal).strip().upper():
                        objetivo = v
                        break
                if not objetivo:
                    continue
                upid = objetivo.get("user_product_id") or upid
                cantidad_item = objetivo.get("available_quantity")
            qty = None
            if upid:
                rs = requests.get(f"{MELI_API_URL}/user-products/{upid}/stock",
                                  headers=meli_headers(), timeout=_TIMEOUT)
                if rs.status_code == 200:
                    ubicaciones = (rs.json() or {}).get("locations") or []
                    propias = [u for u in ubicaciones if (u.get("type") or "") == "selling_address"]
                    if propias:
                        qty = sum(int(u.get("quantity") or 0) for u in propias)
                    elif ubicaciones:
                        continue   # solo Full: el stock lo maneja MercadoLibre
            if qty is None:
                logistica = ((item.get("shipping") or {}).get("logistic_type") or "").lower()
                if logistica == "fulfillment":
                    continue
                qty = cantidad_item
            if qty is None:
                continue
            salida[sku_canal] = max(int(qty), int(salida.get(sku_canal, 0) or 0))
        except Exception:
            continue
        if (i + 1) % 20 == 0:
            time.sleep(1)
    return salida
