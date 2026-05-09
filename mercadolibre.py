"""
Módulo de integración con MercadoLibre (Chile).

Flujo OAuth2 estándar:
  1. Usuario clic en "Conectar" → redirige a /mercadolibre/conectar
  2. /conectar arma URL de auth de MELI y redirige al usuario allá
  3. Usuario acepta en MELI → MELI redirige a /mercadolibre/callback?code=XXX
  4. /callback intercambia code por access_token + refresh_token y los guarda en BD
  5. Las funciones de sync usan get_meli_token() que refresca automáticamente si expiró

Documentación oficial:
  https://developers.mercadolibre.cl/es_ar/autenticacion-y-autorizacion
"""

import os
import time
import requests
from datetime import datetime, timedelta

MELI_APP_ID         = os.environ.get("MERCADOLIBRE_APP_ID", "")
MELI_CLIENT_SECRET  = os.environ.get("MERCADOLIBRE_CLIENT_SECRET", "")
MELI_REDIRECT_URI   = os.environ.get("MERCADOLIBRE_REDIRECT_URI",
                                     "https://sistema-de-inventario-pymes.onrender.com/mercadolibre/callback")
MELI_AUTH_URL       = "https://auth.mercadolibre.cl/authorization"
MELI_TOKEN_URL      = "https://api.mercadolibre.com/oauth/token"
MELI_API_URL        = "https://api.mercadolibre.com"


# ── AUTENTICACIÓN OAuth2 ────────────────────────────────────────────────────

def construir_url_autorizacion(state=""):
    """Arma la URL de auth de MELI a la que el usuario debe ser redirigido."""
    params = {
        "response_type": "code",
        "client_id": MELI_APP_ID,
        "redirect_uri": MELI_REDIRECT_URI,
    }
    if state:
        params["state"] = state
    query = "&".join(f"{k}={requests.utils.quote(str(v), safe='')}" for k, v in params.items())
    return f"{MELI_AUTH_URL}?{query}"


def intercambiar_codigo_por_token(code):
    """Cambia el `code` recibido en /callback por access_token + refresh_token."""
    if not MELI_APP_ID or not MELI_CLIENT_SECRET:
        raise Exception("MERCADOLIBRE_APP_ID o CLIENT_SECRET no configurados en variables de entorno")

    res = requests.post(
        MELI_TOKEN_URL,
        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "authorization_code",
            "client_id": MELI_APP_ID,
            "client_secret": MELI_CLIENT_SECRET,
            "code": code,
            "redirect_uri": MELI_REDIRECT_URI,
        },
        timeout=20
    )
    if res.status_code != 200:
        raise Exception(f"MELI token error: {res.status_code} {res.text[:300]}")
    return res.json()


def refrescar_token(refresh_token):
    """Cuando el access_token vence (cada 6h), usa el refresh_token para obtener uno nuevo."""
    res = requests.post(
        MELI_TOKEN_URL,
        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "refresh_token",
            "client_id": MELI_APP_ID,
            "client_secret": MELI_CLIENT_SECRET,
            "refresh_token": refresh_token,
        },
        timeout=20
    )
    if res.status_code != 200:
        raise Exception(f"MELI refresh error: {res.status_code} {res.text[:300]}")
    return res.json()


def get_meli_token():
    """
    Devuelve un access_token válido. Si el guardado expiró, lo refresca automáticamente.
    Lee/guarda en la tabla mercadolibre_auth de inventario.py.
    """
    from inventario import get_meli_auth, set_meli_auth

    auth = get_meli_auth()
    if not auth or not auth.get("access_token"):
        raise Exception("MercadoLibre no está conectado. El usuario debe ir a /mercadolibre/conectar")

    # Si el token está por vencer en menos de 5 minutos, refrescamos
    expires_at = auth.get("expires_at", 0) or 0
    if expires_at - time.time() < 300:
        try:
            data = refrescar_token(auth["refresh_token"])
            set_meli_auth({
                "access_token":  data["access_token"],
                "refresh_token": data.get("refresh_token", auth["refresh_token"]),
                "user_id":       auth.get("user_id"),
                "expires_at":    int(time.time()) + int(data.get("expires_in", 21600))
            })
            print(f"[MELI] Token refrescado, expira en {data.get('expires_in', 21600)}s")
            return data["access_token"]
        except Exception as e:
            print(f"[MELI] Error refrescando token: {e}")
            raise
    return auth["access_token"]


def meli_headers():
    """Headers estándar con Bearer token."""
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {get_meli_token()}"
    }


def verificar_conexion_meli():
    """Verifica que la conexión funcione y retorna info del seller."""
    try:
        from inventario import get_meli_auth
        auth = get_meli_auth()
        if not auth or not auth.get("access_token"):
            return {"conectado": False, "razon": "No hay token guardado"}

        res = requests.get(
            f"{MELI_API_URL}/users/me",
            headers=meli_headers(),
            timeout=15
        )
        if res.status_code == 200:
            data = res.json()
            return {
                "conectado": True,
                "user_id":   data.get("id"),
                "nickname":  data.get("nickname"),
                "email":     data.get("email"),
                "site_id":   data.get("site_id"),
                "country":   data.get("country_id")
            }
        return {"conectado": False, "razon": f"API error {res.status_code}: {res.text[:200]}"}
    except Exception as e:
        return {"conectado": False, "error": str(e)}


# ── STOCK ───────────────────────────────────────────────────────────────────

def actualizar_stock_meli(sku_lusync, cantidad):
    """
    Actualiza stock en MercadoLibre para TODAS las publicaciones de un SKU Lusync.

    Si tienes 1 SKU Lusync con N publicaciones MELI (ej: ODJM001 con 2 MLC),
    actualiza el stock en LAS N publicaciones.

    Returns:
        dict: {ok, total_publicaciones, exitosas, fallidas, log}
    """
    from inventario import obtener_publicaciones_canal

    publicaciones = obtener_publicaciones_canal(sku_lusync, "mercadolibre")

    if not publicaciones:
        # Fallback legacy: si no hay mapeo nuevo, intentar con el sistema viejo
        try:
            from inventario import get_sku_canal
            sku_meli_legacy = get_sku_canal(sku_lusync, "mercadolibre")
            if sku_meli_legacy and str(sku_meli_legacy).strip().startswith("MLC"):
                publicaciones = [{
                    "id": None,
                    "sku_canal": sku_lusync,
                    "item_id_canal": str(sku_meli_legacy).strip()
                }]
        except: pass

    if not publicaciones:
        print(f"[MELI Stock] SKU '{sku_lusync}' sin publicaciones mapeadas, no se actualiza")
        return {"ok": False, "total_publicaciones": 0, "exitosas": 0, "fallidas": 0,
                "log": [f"Sin publicaciones mapeadas para {sku_lusync}"]}

    exitosas = 0
    fallidas = 0
    log = []

    for pub in publicaciones:
        item_id = pub.get("item_id_canal") or pub.get("sku_canal")
        sku_canal = pub.get("sku_canal", "")
        if not item_id or not str(item_id).strip().upper().startswith("MLC"):
            log.append(f"  Publicación {item_id}: no es item_id MELI válido (debe empezar con MLC)")
            fallidas += 1
            continue

        try:
            # Primero obtener el item con TODOS los atributos (necesario para ver SELLER_SKU)
            r_get = requests.get(
                f"{MELI_API_URL}/items/{item_id}?include_attributes=all",
                headers=meli_headers(),
                timeout=15
            )
            if r_get.status_code != 200:
                log.append(f"  {item_id}: error obteniendo item, status {r_get.status_code}")
                print(f"[MELI Stock] No pude leer item {item_id}: {r_get.status_code}")
                fallidas += 1
                continue

            item_data = r_get.json()

            # ── EXCLUIR publicaciones de catálogo (catalog_listing) ──
            if item_data.get("catalog_listing"):
                log.append(f"  {item_id}: ⏭️ catalog_listing — MELI lo gestiona solo, omitido")
                print(f"[MELI Stock] Item {item_id} es catalog_listing, omitido")
                continue

            variations = item_data.get("variations", []) or []
            # NOTA: La presencia de inventory_id NO indica "solo Full".
            # Una publicación puede tener stock Full + stock propio (Flex / Centro de Envío).
            # El available_quantity que enviamos por API actualiza el stock propio (no el Full).

            if variations:
                # Publicación con variantes — actualizar la variante específica que matchea con sku_canal
                # o todas si no se puede identificar
                target_variation = None
                if sku_canal:
                    for v in variations:
                        # Buscar SELLER_SKU en attributes de la variante
                        v_attrs = v.get("attributes", []) or []
                        v_sku = next((a.get("value_name","") for a in v_attrs 
                                     if a.get("id") == "SELLER_SKU"), "")
                        # Fallback: seller_custom_field
                        if not v_sku:
                            v_sku = v.get("seller_custom_field") or ""
                        if v_sku and v_sku.upper().strip() == sku_canal.upper().strip():
                            target_variation = v
                            break

                if target_variation:
                    var_id = target_variation.get("id")
                    user_product_id = target_variation.get("user_product_id")

                    # Estrategia 1: PUT /items con variations (para publicaciones sin Full)
                    payload = {"variations": [{"id": var_id, "available_quantity": int(cantidad)}]}
                    res = requests.put(
                        f"{MELI_API_URL}/items/{item_id}",
                        headers=meli_headers(),
                        json=payload,
                        timeout=15
                    )
                    ok = res.status_code in (200, 201)
                    es_full = "not_modifiable" in res.text or "fulfillment" in res.text.lower()

                    if ok:
                        log.append(f"  {item_id} var {var_id} (sku {sku_canal}): status {res.status_code} OK")
                        print(f"[MELI Stock] Item:{item_id} Var:{var_id} Sku:{sku_canal} Qty:{cantidad} Status:{res.status_code}")
                        exitosas += 1
                    elif es_full and user_product_id:
                        # Estrategia 2: bi-modal — usar /user-products/{id}/stock/type/selling_address
                        # Esto actualiza SOLO el stock propio (deja Full intacto)
                        try:
                            # Primero GET para obtener x-version actual
                            r_get_stock = requests.get(
                                f"{MELI_API_URL}/user-products/{user_product_id}/stock",
                                headers=meli_headers(),
                                timeout=15
                            )
                            x_version = r_get_stock.headers.get("x-version", "1")

                            # PUT al endpoint de selling_address
                            headers_put = dict(meli_headers())
                            headers_put["x-version"] = str(x_version)
                            headers_put["Content-Type"] = "application/json"
                            r_sa = requests.put(
                                f"{MELI_API_URL}/user-products/{user_product_id}/stock/type/selling_address",
                                headers=headers_put,
                                json={"quantity": int(cantidad)},
                                timeout=15
                            )
                            ok_sa = r_sa.status_code in (200, 201, 204)
                            if ok_sa:
                                log.append(f"  {item_id} var {var_id} (sku {sku_canal}): bi-modal selling_address OK (qty={cantidad})")
                                print(f"[MELI Stock] Item:{item_id} Var:{var_id} Sku:{sku_canal} bi-modal selling_address Qty:{cantidad} Status:{r_sa.status_code}")
                                exitosas += 1
                            else:
                                log.append(f"  {item_id} var {var_id} (sku {sku_canal}): selling_address FAIL status {r_sa.status_code}")
                                log.append(f"    body: {r_sa.text[:300]}")
                                print(f"[MELI Stock] Item:{item_id} Var:{var_id} selling_address Status:{r_sa.status_code} body:{r_sa.text[:200]}")
                                fallidas += 1
                        except Exception as e_sa:
                            log.append(f"  {item_id} var {var_id} (sku {sku_canal}): error selling_address: {e_sa}")
                            print(f"[MELI Stock] Item:{item_id} Var:{var_id} error selling_address: {e_sa}")
                            fallidas += 1
                    elif es_full:
                        log.append(f"  {item_id} var {var_id} (sku {sku_canal}): ⏭️ Full sin user_product_id — omitido")
                        print(f"[MELI Stock] Item:{item_id} Var:{var_id} Full sin user_product_id, omitido")
                    else:
                        log.append(f"  {item_id} var {var_id} (sku {sku_canal}): status {res.status_code} FAIL")
                        log.append(f"    body: {res.text[:300]}")
                        print(f"[MELI Stock] Item:{item_id} Var:{var_id} Sku:{sku_canal} Qty:{cantidad} Status:{res.status_code} FAIL")
                        fallidas += 1
                else:
                    # No matcheamos por SKU, pero si solo hay 1 variante, actualizarla igual
                    # (caso común: publicación con atributos de color pero un solo SKU físico)
                    if len(variations) == 1:
                        var_id = variations[0].get("id")
                        payload = {"variations": [{"id": var_id, "available_quantity": int(cantidad)}]}
                        res = requests.put(
                            f"{MELI_API_URL}/items/{item_id}",
                            headers=meli_headers(),
                            json=payload,
                            timeout=15
                        )
                        ok = res.status_code in (200, 201)
                        log.append(f"  {item_id} variante única {var_id}: status {res.status_code} {'OK' if ok else 'FAIL'}")
                        print(f"[MELI Stock] Item:{item_id} Variante única Var:{var_id} Qty:{cantidad} Status:{res.status_code}")
                        if not ok: log.append(f"    body: {res.text[:300]}")
                        if ok: exitosas += 1
                        else: fallidas += 1
                    else:
                        # Multiples variantes y no matchea — actualizar TODAS con el mismo stock
                        # (escenario raro pero práctico: si Lusync solo tiene 1 SKU para publicación con N variantes)
                        var_ids = [v.get("id") for v in variations]
                        payload = {"variations": [{"id": vid, "available_quantity": int(cantidad)} for vid in var_ids]}
                        res = requests.put(
                            f"{MELI_API_URL}/items/{item_id}",
                            headers=meli_headers(),
                            json=payload,
                            timeout=15
                        )
                        ok = res.status_code in (200, 201)
                        log.append(f"  {item_id} {len(var_ids)} variantes: status {res.status_code} {'OK' if ok else 'FAIL'}")
                        print(f"[MELI Stock] Item:{item_id} {len(var_ids)} variantes (sku no matchea) Qty:{cantidad} Status:{res.status_code}")
                        if not ok: log.append(f"    body: {res.text[:300]}")
                        if ok: exitosas += 1
                        else: fallidas += 1
            else:
                # Publicación simple (sin variantes) — actualizar available_quantity directo
                res = requests.put(
                    f"{MELI_API_URL}/items/{item_id}",
                    headers=meli_headers(),
                    json={"available_quantity": int(cantidad)},
                    timeout=15
                )
                ok = res.status_code in (200, 201)
                log.append(f"  {item_id}: status {res.status_code} {'OK' if ok else 'FAIL'}")
                print(f"[MELI Stock] Item:{item_id} Qty:{cantidad} Status:{res.status_code}")
                if ok:
                    exitosas += 1
                else:
                    fallidas += 1
                    log.append(f"    body: {res.text[:300]}")
        except Exception as e:
            fallidas += 1
            log.append(f"  {item_id}: error {e}")
            print(f"[MELI Stock] Error {item_id}: {e}")

    omitidas = len(publicaciones) - exitosas - fallidas
    return {
        "ok": fallidas == 0,
        "total_publicaciones": len(publicaciones),
        "exitosas": exitosas,
        "fallidas": fallidas,
        "omitidas": omitidas,
        "log": log
    }


def obtener_publicaciones_meli(limite=50, offset=0):
    """Lista las publicaciones del seller para que pueda mapearlas a SKUs Lusync.

    El SKU del seller en MELI puede estar en 3 lugares (en orden de prioridad moderna):
      1. attributes[id="SELLER_SKU"].value_name  (forma actual, MELI 2023+)
      2. variations[].attributes[id="SELLER_SKU"]  (si tiene variantes)
      3. seller_custom_field  (legacy)
      4. variations[].seller_custom_field (legacy con variantes)
    """
    try:
        from inventario import get_meli_auth
        auth = get_meli_auth()
        if not auth or not auth.get("user_id"):
            return None

        res = requests.get(
            f"{MELI_API_URL}/users/{auth['user_id']}/items/search",
            headers=meli_headers(),
            params={"limit": limite, "offset": offset},
            timeout=20
        )
        if res.status_code != 200:
            return None
        data = res.json()
        item_ids = data.get("results", [])
        if not item_ids:
            return {"items": [], "total": 0}

        def _extraer_sku_de_item(body):
            """Recorre los 4 lugares posibles donde puede estar el SKU del seller."""
            # 1. SELLER_SKU en attributes del item principal
            for attr in body.get("attributes", []) or []:
                if attr.get("id") == "SELLER_SKU":
                    val = (attr.get("value_name") or attr.get("value_id") or "").strip()
                    if val:
                        return val, "attributes.SELLER_SKU"
            # 2. seller_custom_field (legacy) del item principal
            scf = (body.get("seller_custom_field") or "").strip()
            if scf:
                return scf, "seller_custom_field"
            # 3. SELLER_SKU en la PRIMERA variación con stock (la más representativa)
            variations = body.get("variations", []) or []
            # Priorizar variation con available_quantity > 0
            variations_ord = sorted(variations, key=lambda v: -(v.get("available_quantity") or 0))
            for var in variations_ord:
                for attr in var.get("attributes", []) or []:
                    if attr.get("id") == "SELLER_SKU":
                        val = (attr.get("value_name") or attr.get("value_id") or "").strip()
                        if val:
                            return val, f"variation.attributes.SELLER_SKU"
                scf_var = (var.get("seller_custom_field") or "").strip()
                if scf_var:
                    return scf_var, "variation.seller_custom_field"
            return "", ""

        # Detalle de cada item (lote de hasta 20 por llamada)
        items = []
        for i in range(0, len(item_ids), 20):
            batch = item_ids[i:i+20]
            ids_str = ",".join(batch)
            # IMPORTANTE: NO filtrar attributes para que vengan las variantes con sus atributos completos
            # Si se filtra con "attributes=...,variations", MELI devuelve variantes sin sus atributos internos
            res2 = requests.get(
                f"{MELI_API_URL}/items",
                headers=meli_headers(),
                params={"ids": ids_str},
                timeout=25
            )
            if res2.status_code == 200:
                for r in res2.json():
                    body = r.get("body", {}) or {}
                    sku_seller, sku_origen = _extraer_sku_de_item(body)
                    # Lista de TODAS las variantes (por si hay multi-SKU)
                    variantes_skus = []
                    for var in body.get("variations", []) or []:
                        for attr in var.get("attributes", []) or []:
                            if attr.get("id") == "SELLER_SKU":
                                v = (attr.get("value_name") or "").strip()
                                if v:
                                    variantes_skus.append(v)
                                    break
                        else:
                            scf_var = (var.get("seller_custom_field") or "").strip()
                            if scf_var:
                                variantes_skus.append(scf_var)
                    items.append({
                        "item_id":          body.get("id"),
                        "title":            body.get("title"),
                        "stock":            body.get("available_quantity", 0),
                        "price":            body.get("price"),
                        "status":           body.get("status"),
                        "sku_seller":       sku_seller,
                        "sku_origen":       sku_origen,
                        "variantes_skus":   variantes_skus,
                        "variantes_raw":    body.get("variations", []) or []
                    })
        return {"items": items, "total": data.get("paging", {}).get("total", len(item_ids))}
    except Exception as e:
        print(f"[MELI] Error obteniendo publicaciones: {e}")
        return None


# ── ÓRDENES ─────────────────────────────────────────────────────────────────

def obtener_ordenes_meli(limit=50, offset=0, estado=None, date_from=None, date_to=None):
    """Lista órdenes recientes del seller.
    
    Args:
        limit: máx órdenes por página (50 max recomendado)
        offset: paginación
        estado: 'paid', 'cancelled', etc.
        date_from: fecha desde 'YYYY-MM-DDTHH:MM:SS.000-00:00' (formato ISO MELI)
        date_to: fecha hasta (mismo formato)
    """
    try:
        from inventario import get_meli_auth
        auth = get_meli_auth()
        if not auth or not auth.get("user_id"):
            return []

        params = {"seller": auth["user_id"], "limit": limit, "offset": offset, "sort": "date_desc"}
        if estado:
            params["order.status"] = estado
        if date_from:
            params["order.date_created.from"] = date_from
        if date_to:
            params["order.date_created.to"] = date_to

        res = requests.get(
            f"{MELI_API_URL}/orders/search",
            headers=meli_headers(),
            params=params,
            timeout=20
        )
        if res.status_code != 200:
            print(f"[MELI Ordenes] Error {res.status_code}: {res.text[:200]}")
            return []
        return res.json().get("results", [])
    except Exception as e:
        print(f"[MELI] Error órdenes: {e}")
        return []


def obtener_todas_ordenes_meli_rango(date_from, date_to, max_paginas=10):
    """Trae TODAS las órdenes MELI en un rango de fechas (con paginación).
    
    Itera sobre páginas hasta agotar resultados o llegar a max_paginas.
    Devuelve lista combinada de órdenes.
    
    Args:
        date_from, date_to: ISO format con timezone (ej: '2026-05-01T00:00:00.000-04:00')
        max_paginas: límite de páginas para evitar loops infinitos (10 = 500 órdenes max)
    """
    todas = []
    offset = 0
    limit = 50
    for pagina in range(max_paginas):
        ordenes = obtener_ordenes_meli(
            limit=limit, offset=offset,
            date_from=date_from, date_to=date_to
        )
        if not ordenes:
            break
        todas.extend(ordenes)
        print(f"[MELI Rango] Página {pagina+1}: +{len(ordenes)} órdenes (total acumulado: {len(todas)})")
        if len(ordenes) < limit:
            # Última página
            break
        offset += limit
    return todas


def obtener_orden_meli(order_id):
    """Detalle de una orden específica.
    Si recibe un pack_id en vez de un order_id, devuelve la orden con TODOS los items
    del pack consolidados (no solo la primera orden interna).
    
    BUG FIX (2026-05-06): antes solo procesaba la primera orden del pack, los demás
    productos del carrito quedaban sin descontar stock.
    """
    try:
        # Intento 1: tratar como order_id directo
        res = requests.get(
            f"{MELI_API_URL}/orders/{order_id}",
            headers=meli_headers(),
            timeout=15
        )
        if res.status_code == 200:
            return res.json()

        # Intento 2: si dió 404, puede ser un pack_id (agrupa varias órdenes)
        if res.status_code == 404:
            print(f"[MELI] {order_id} no es order_id, probando como pack_id...")
            res2 = requests.get(
                f"{MELI_API_URL}/packs/{order_id}",
                headers=meli_headers(),
                timeout=15
            )
            if res2.status_code == 200:
                pack = res2.json()
                ordenes_internas = pack.get("orders", []) or []
                if not ordenes_internas:
                    print(f"[MELI] Pack {order_id} sin órdenes internas")
                    return None
                
                print(f"[MELI] Pack {order_id} tiene {len(ordenes_internas)} órdenes internas")
                
                # Traer detalle de TODAS las órdenes del pack
                ordenes_completas = []
                for orden_interna in ordenes_internas:
                    int_order_id = orden_interna.get("id")
                    if not int_order_id: continue
                    try:
                        res3 = requests.get(
                            f"{MELI_API_URL}/orders/{int_order_id}",
                            headers=meli_headers(),
                            timeout=15
                        )
                        if res3.status_code == 200:
                            ordenes_completas.append(res3.json())
                    except Exception as e:
                        print(f"[MELI] Error trayendo orden {int_order_id} del pack: {e}")
                        continue
                
                if not ordenes_completas:
                    return None
                
                # Consolidar items de TODAS las órdenes en una sola estructura
                # Mantenemos el pack_id como id principal (lo que TÚ ves en MELI)
                primera = ordenes_completas[0]
                consolidada = dict(primera)  # Base con buyer, status, etc.
                
                # Marcar que es pack y guardar pack_id
                consolidada["_is_pack"] = True
                consolidada["_pack_id"] = str(order_id)
                consolidada["id"] = order_id  # Forzar a que el "id" sea el pack_id
                
                # Consolidar TODOS los order_items de TODAS las órdenes
                items_consolidados = []
                for orden in ordenes_completas:
                    for item in orden.get("order_items", []) or []:
                        # Anotar el order_id real interno para trazabilidad
                        item_copy = dict(item)
                        item_copy["_internal_order_id"] = str(orden.get("id", ""))
                        items_consolidados.append(item_copy)
                
                consolidada["order_items"] = items_consolidados
                
                print(f"[MELI] Pack {order_id} consolidado: {len(items_consolidados)} items totales")
                return consolidada
        
        return None
    except Exception as e:
        print(f"[MELI] Error obteniendo orden {order_id}: {e}")
        return None
    except Exception as e:
        print(f"[MELI] Error orden {order_id}: {e}")
        return None


def resolver_pack_a_ordenes(pack_id):
    """Si un webhook llega con pack_id, esta función devuelve las órdenes internas."""
    try:
        res = requests.get(
            f"{MELI_API_URL}/packs/{pack_id}",
            headers=meli_headers(),
            timeout=15
        )
        if res.status_code == 200:
            pack = res.json()
            ordenes_ids = [str(o.get("id")) for o in pack.get("orders", []) if o.get("id")]
            return ordenes_ids
        return []
    except Exception as e:
        print(f"[MELI] Error resolviendo pack {pack_id}: {e}")
        return []


def obtener_sku_de_item_meli(item_id):
    """Consulta el detalle de un item y devuelve el SKU del seller.
    MELI guarda el SKU en varios lugares según la antigüedad de la publicación.
    Esta función prueba todos en orden y devuelve el primero válido."""
    try:
        res = requests.get(
            f"{MELI_API_URL}/items/{item_id}",
            headers=meli_headers(),
            timeout=15
        )
        if res.status_code != 200:
            return None
        item = res.json()

        # Path 1: seller_sku directo (API moderna)
        sku = (item.get("seller_sku") or "").strip()
        if sku:
            return sku

        # Path 2: seller_custom_field (legacy)
        sku = (item.get("seller_custom_field") or "").strip()
        if sku:
            return sku

        # Path 3: en attributes (publicaciones de catálogo)
        for attr in item.get("attributes", []):
            attr_id = (attr.get("id") or "").upper()
            if attr_id == "SELLER_SKU" or "SKU" in attr_id:
                val = (attr.get("value_name") or "").strip()
                if val:
                    return val

        # Path 4: en variations (productos con variantes)
        for var in item.get("variations", []):
            for attr in var.get("attributes", []):
                attr_id = (attr.get("id") or "").upper()
                if attr_id == "SELLER_SKU" or "SKU" in attr_id:
                    val = (attr.get("value_name") or "").strip()
                    if val:
                        return val
            # También seller_custom_field en variation
            sku_var = (var.get("seller_custom_field") or "").strip()
            if sku_var:
                return sku_var

        return None
    except Exception as e:
        print(f"[MELI] Error obteniendo SKU de item {item_id}: {e}")
        return None


# ── PRECIOS ─────────────────────────────────────────────────────────────────

def actualizar_precio_meli(item_id, precio):
    """Actualiza el precio de UNA publicación específica (por item_id MLC)."""
    try:
        res = requests.put(
            f"{MELI_API_URL}/items/{item_id}",
            headers=meli_headers(),
            json={"price": float(precio)},
            timeout=15
        )
        print(f"[MELI Precio] Item:{item_id} Precio:{precio} Status:{res.status_code}")
        return res.status_code in (200, 201)
    except Exception as e:
        print(f"[MELI Precio] Error: {e}")
        return False


def actualizar_precio_meli_lusync(sku_lusync, precio):
    """Actualiza precio en TODAS las publicaciones MELI de un SKU Lusync.

    Returns:
        dict: {ok, total_publicaciones, exitosas, fallidas, log}
    """
    from inventario import obtener_publicaciones_canal
    publicaciones = obtener_publicaciones_canal(sku_lusync, "mercadolibre")
    if not publicaciones:
        return {"ok": False, "total_publicaciones": 0, "exitosas": 0, "fallidas": 0,
                "log": [f"Sin publicaciones mapeadas para {sku_lusync}"]}

    exitosas = 0
    fallidas = 0
    log = []
    for pub in publicaciones:
        item_id = pub.get("item_id_canal") or pub.get("sku_canal")
        if not item_id or not str(item_id).strip().upper().startswith("MLC"):
            log.append(f"  {item_id}: no es item_id MELI válido")
            fallidas += 1
            continue
        ok = actualizar_precio_meli(item_id, precio)
        if ok:
            exitosas += 1
            log.append(f"  {item_id}: OK")
        else:
            fallidas += 1
            log.append(f"  {item_id}: FAIL")
    omitidas = len(publicaciones) - exitosas - fallidas
    return {
        "ok": fallidas == 0,
        "total_publicaciones": len(publicaciones),
        "exitosas": exitosas,
        "fallidas": fallidas,
        "omitidas": omitidas,
        "log": log
    }


# ── WEBHOOKS ────────────────────────────────────────────────────────────────

def procesar_webhook_meli(payload):
    """
    Procesa un webhook entrante. MELI envía notificaciones por tópico.
    Estructura:
      {
        "resource": "/orders/2000003508104291",
        "user_id": 99999999,
        "topic": "orders_v2",
        "application_id": 5651702837523293,
        "attempts": 1,
        "sent": "2026-04-30T20:00:00.000Z",
        "received": "2026-04-30T20:00:00.000Z"
      }
    """
    try:
        topic = payload.get("topic", "")
        resource = payload.get("resource", "")
        user_id = payload.get("user_id")

        print(f"[MELI Webhook] topic={topic} resource={resource} user={user_id}")

        # Despachar según tópico
        if topic == "orders_v2":
            return _procesar_orden_webhook(resource)
        elif topic == "items":
            return _procesar_item_webhook(resource)
        elif topic == "questions":
            return _procesar_pregunta_webhook(resource)
        elif topic == "messages":
            return _procesar_mensaje_webhook(resource)
        elif topic == "shipments":
            return _procesar_envio_webhook(resource)
        else:
            print(f"[MELI Webhook] Tópico no manejado todavía: {topic}")
            return True
    except Exception as e:
        print(f"[MELI Webhook] Error: {e}")
        return False


def _procesar_orden_webhook(resource):
    """Procesa orden o cambio de estado vía webhook.
    Detecta automáticamente si es Full o Seller envía y descuenta de la bodega correcta.
    Maneja TANTO ventas como cancelaciones.
    """
    try:
        order_id = resource.split("/")[-1]
        orden = obtener_orden_meli(order_id)
        if not orden:
            return False

        estado = orden.get("status", "")

        from inventario import (orden_ya_procesada_texto, marcar_orden_procesada_texto,
                                descontar_venta_inteligente, detectar_fulfillment_meli,
                                listar_sku_mapeo, cargar_productos, guardar_producto,
                                registrar_movimiento, crear_alerta)

        meli_key = f"MELI-{order_id}"
        cancel_key = f"MELI-CANCEL-{order_id}"

        # ── ÓRDENES CANCELADAS — reintegrar stock ──
        if estado in ("cancelled", "canceled"):
            # Marcado atómico de cancelación (race-condition safe)
            from inventario import intentar_marcar_orden_atomic
            if not intentar_marcar_orden_atomic(cancel_key):
                print(f"[MELI Webhook] Cancelación {order_id} ya procesada (atomic)")
                return True
            # Si la venta NUNCA se procesó, no hay nada que reintegrar
            if not orden_ya_procesada_texto(meli_key):
                print(f"[MELI Webhook] Cancelación {order_id} sin venta previa, marcada")
                return True

            print(f"[MELI Webhook] Cancelación {order_id} — reintegrando stock")
            es_full = detectar_fulfillment_meli(orden)
            items_reintegrados = []
            ultimo_sku = None

            for item in orden.get("order_items", []):
                item_data = item.get("item", {})
                item_id = item_data.get("id", "")
                sku_meli = (
                    (item_data.get("seller_sku") or "").strip()
                    or (item_data.get("seller_custom_field") or "").strip()
                )
                if not sku_meli and item_id:
                    sku_resuelto = obtener_sku_de_item_meli(item_id)
                    if sku_resuelto:
                        sku_meli = sku_resuelto
                if not sku_meli:
                    sku_meli = item_id

                qty = int(item.get("quantity", 1))

                # Buscar SKU Lusync vía mapeo
                sku_lusync = sku_meli
                # Intento 1: tabla vieja sku_mapeo
                try:
                    for fila in listar_sku_mapeo():
                        sku_mapped = (fila.get("sku_mercadolibre") or "").strip()
                        if sku_mapped and (sku_mapped == sku_meli or sku_mapped == item_id):
                            sku_lusync = fila.get("sku_lusync")
                            break
                except: pass
                # Intento 2: tabla nueva sku_mapeo_canal
                if sku_lusync == sku_meli:
                    try:
                        from inventario import obtener_sku_lusync_por_canal
                        sku_traducido = obtener_sku_lusync_por_canal("mercadolibre", sku_canal=sku_meli, item_id_canal=item_id)
                        if sku_traducido:
                            sku_lusync = sku_traducido
                    except: pass

                # Reintegrar stock
                productos = cargar_productos()
                for p in productos:
                    if p["sku"] == sku_lusync:
                        # Si es Full, el stock se reintegra en bodega FULL_MELI
                        # Si es Seller, en CENTRAL (la "stock" del producto)
                        # Por simplicidad usamos el campo stock principal (CENTRAL)
                        if not es_full:
                            p["stock"] += qty
                            guardar_producto(p)
                        registrar_movimiento(
                            "entrada", p["sku"], p["nombre"], qty,
                            f"Cancelación MELI orden {order_id}",
                            usuario="Sistema", canal="MercadoLibre", orden_id=order_id
                        )
                        ultimo_sku = sku_lusync
                        items_reintegrados.append(f"{p['nombre']} (SKU: {sku_lusync}) x{qty}")

                        # Sync a los 6 marketplaces si fue Seller (Central cambió)
                        if not es_full:
                            try:
                                from app import sincronizar_stock_marketplaces
                                sincronizar_stock_marketplaces(
                                    sku_lusync, p["stock"],
                                    contexto="meli_webhook_cancelacion"
                                )
                            except Exception as e_sync:
                                print(f"[MELI Webhook Cancel] Error sync: {e_sync}")
                        break

            # Crear alerta visible
            if items_reintegrados:
                try:
                    crear_alerta(
                        tipo="cancelacion",
                        titulo=f"Orden cancelada en MercadoLibre: {order_id}",
                        mensaje="Stock reintegrado:<br>" + "<br>".join(f"• {it}" for it in items_reintegrados),
                        sku=ultimo_sku
                    )
                except: pass

            marcar_orden_procesada_texto(cancel_key)
            print(f"[MELI Webhook] Cancelación {order_id} procesada — {len(items_reintegrados)} items reintegrados")
            return True

        # ── ÓRDENES PAGADAS/CONFIRMADAS — descontar stock ──
        if estado not in ("paid", "confirmed", "payment_required"):
            print(f"[MELI Webhook] Orden {order_id} en estado {estado}, no se procesa")
            return True

        # Marcado ATÓMICO contra race conditions (multi-publicación, webhooks simultáneos)
        from inventario import intentar_marcar_orden_atomic
        if not intentar_marcar_orden_atomic(meli_key):
            print(f"[MELI Webhook] Orden {order_id} ya procesada (atomic check)")
            return True

        # ── Extraer fecha real de compra ──
        from datetime import datetime as _dt
        fecha_compra_meli = None
        try:
            date_str = orden.get("date_created", "") or ""
            if date_str:
                date_str_clean = date_str.replace("Z", "+00:00")
                fecha_compra_meli = _dt.fromisoformat(date_str_clean)
        except Exception as e:
            print(f"[MELI Webhook] No se pudo parsear date_created: {e}")
            fecha_compra_meli = None

        # ── Detectar Full vs Seller ──
        es_full = detectar_fulfillment_meli(orden)
        tipo_str = "FULL" if es_full else "Seller"
        print(f"[MELI Webhook] Orden {order_id} tipo: {tipo_str}")

        productos = cargar_productos()
        productos_dict = {p["sku"]: p for p in productos}

        for item in orden.get("order_items", []):
            item_data = item.get("item", {})
            item_id = item_data.get("id", "")

            sku_meli = (
                (item_data.get("seller_sku") or "").strip()
                or (item_data.get("seller_custom_field") or "").strip()
            )
            if not sku_meli and item_id:
                print(f"[MELI Webhook] SKU vacío, consultando /items/{item_id}...")
                sku_resuelto = obtener_sku_de_item_meli(item_id)
                if sku_resuelto:
                    sku_meli = sku_resuelto
                    print(f"[MELI Webhook] SKU resuelto: {sku_meli}")
            if not sku_meli:
                sku_meli = item_id

            qty = int(item.get("quantity", 1))

            sku_lusync = sku_meli
            # Intento 1: tabla vieja sku_mapeo (columna sku_mercadolibre)
            try:
                for fila in listar_sku_mapeo():
                    sku_mapped = (fila.get("sku_mercadolibre") or "").strip()
                    if sku_mapped and (sku_mapped == sku_meli or sku_mapped == item_id):
                        sku_lusync = fila.get("sku_lusync")
                        break
            except: pass
            # Intento 2: tabla nueva sku_mapeo_canal (multi-publicación)
            if sku_lusync == sku_meli:
                try:
                    from inventario import obtener_sku_lusync_por_canal
                    sku_traducido = obtener_sku_lusync_por_canal("mercadolibre", sku_canal=sku_meli, item_id_canal=item_id)
                    if sku_traducido:
                        sku_lusync = sku_traducido
                        print(f"[MELI Webhook] SKU traducido vía sku_mapeo_canal: {sku_meli} → {sku_lusync}")
                except Exception as e:
                    print(f"[MELI Webhook] Error obtener_sku_lusync_por_canal: {e}")

            if sku_lusync not in productos_dict:
                print(f"[MELI Webhook] SKU '{sku_lusync}' no encontrado en inventario")
                continue

            # Descontar de la bodega correcta
            resultado = descontar_venta_inteligente(
                sku=sku_lusync,
                cantidad=qty,
                canal="MercadoLibre",
                fulfillment=es_full,
                orden_id=order_id,
                motivo=f"Venta MercadoLibre{' Full' if es_full else ''}",
                usuario="Sistema",
                fecha_compra_marketplace=fecha_compra_meli,
                origen_registro="webhook"
            )
            print(f"[MELI Webhook] {sku_lusync} -{qty} desde {resultado['bodega']} → {resultado['stock_despues']}")

            # Sync a los 6 marketplaces SOLO si fue Seller (Central afectada)
            if not es_full:
                try:
                    from inventario import cargar_productos as _cp
                    productos_actualizados = _cp()
                    stock_total = next((pp["stock"] for pp in productos_actualizados if pp["sku"] == sku_lusync), 0)
                    # Importar el helper centralizado desde app.py (sync 6 marketplaces resiliente)
                    from app import sincronizar_stock_marketplaces
                    sincronizar_stock_marketplaces(
                        sku_lusync, stock_total,
                        contexto="meli_webhook_venta"
                    )
                except Exception as e_sync:
                    print(f"[MELI Webhook] Error sync 6mkts: {e_sync}")
        return True
    except Exception as e:
        print(f"[MELI Webhook] Error procesando orden: {e}")
        import traceback
        print(traceback.format_exc())
        return False


def _procesar_item_webhook(resource):
    """Cuando cambia el estado de una publicación (pausada, activa, etc.)."""
    print(f"[MELI Webhook] Item event: {resource}")
    return True


def _procesar_pregunta_webhook(resource):
    """Cuando un cliente hace una pregunta. Crea alerta para que el seller responda rápido."""
    try:
        from inventario import crear_alerta
        crear_alerta(
            tipo="pregunta_meli",
            canal="MercadoLibre",
            titulo="Nueva pregunta en MercadoLibre",
            mensaje=f"Tienes una pregunta nueva en una publicación. Respóndela pronto para mejorar tu reputación.<br><br>Recurso: {resource}",
            enviar_email=False  # las preguntas son frecuentes, no spamear email
        )
    except Exception as e:
        print(f"[MELI Webhook] Error creando alerta de pregunta: {e}")
    return True


def _procesar_mensaje_webhook(resource):
    """Cuando llega un mensaje post-venta del comprador."""
    print(f"[MELI Webhook] Mensaje: {resource}")
    return True


def _procesar_envio_webhook(resource):
    """Cuando cambia el estado de un envío."""
    print(f"[MELI Webhook] Envío: {resource}")
    return True


# ════════════════════════════════════════════════════════════════════════════
# CONSULTAR STOCK FULL REAL DESDE LA API DE MELI
# ════════════════════════════════════════════════════════════════════════════
# Usado por el scheduler diario de respaldo para verificar si el stock Full
# en Lusync coincide con el real de MELI. Si hay desfase (webhook perdido),
# Lusync se ajusta automáticamente.
# ════════════════════════════════════════════════════════════════════════════

def obtener_stock_full_real_meli(sku_lusync=None, max_publicaciones=200):
    """Consulta el stock Full REAL de MELI vía API.
    
    Args:
        sku_lusync: si se especifica, solo consulta ese SKU. Si None, todos los Full.
        max_publicaciones: límite para evitar loops infinitos.
    
    Returns:
        dict {sku_lusync: {"available": int, "in_transit": int, "total": int}}
        o None si hay error.
    
    Estrategia:
        1. Lista publicaciones del seller que ofrecen Full (logistic_type="fulfillment")
        2. Para cada una, consulta /inventories/{inv_id}/stock/fulfillment
        3. Devuelve dict por SKU
    """
    try:
        token = get_meli_token()
        if not token:
            print(f"[StockFullAPI] No hay token MELI")
            return None
        
        from inventario import get_meli_auth, listar_sku_mapeo
        auth = get_meli_auth() or {}
        seller_id = auth.get("user_id")
        if not seller_id:
            r_user = requests.get(
                "https://api.mercadolibre.com/users/me",
                headers={"Authorization": f"Bearer {token}"}, timeout=10
            )
            seller_id = r_user.json().get("id") if r_user.status_code == 200 else None
        
        if not seller_id:
            print(f"[StockFullAPI] No se pudo obtener seller_id")
            return None
        
        # Mapeo SKU MELI → SKU Lusync
        sku_meli_to_lusync = {}
        try:
            for fila in listar_sku_mapeo():
                sku_meli = (fila.get("sku_mercadolibre") or "").strip()
                sku_lus = (fila.get("sku_lusync") or "").strip()
                if sku_meli and sku_lus:
                    sku_meli_to_lusync[sku_meli] = sku_lus
        except: pass
        
        # ── 1. Listar publicaciones del seller ──
        item_ids = []
        offset = 0
        while len(item_ids) < max_publicaciones:
            r = requests.get(
                f"https://api.mercadolibre.com/users/{seller_id}/items/search",
                params={"limit": 50, "offset": offset, "logistic_type": "fulfillment"},
                headers={"Authorization": f"Bearer {token}"},
                timeout=15
            )
            if r.status_code != 200:
                print(f"[StockFullAPI] HTTP {r.status_code} listando items Full")
                break
            data = r.json()
            ids_lote = data.get("results", []) or []
            if not ids_lote: break
            item_ids.extend(ids_lote)
            offset += len(ids_lote)
            if offset >= data.get("paging", {}).get("total", 0):
                break
        
        print(f"[StockFullAPI] {len(item_ids)} publicaciones Full encontradas")
        
        # ── 2. Para cada item, obtener inventory_id + SKU + stock fulfillment ──
        resultado = {}
        
        # Procesar items en lotes de 20 (multi-get)
        for i in range(0, len(item_ids), 20):
            ids_param = ",".join(item_ids[i:i+20])
            r_items = requests.get(
                "https://api.mercadolibre.com/items",
                params={"ids": ids_param, "attributes": "id,attributes,inventory_id,seller_custom_field"},
                headers={"Authorization": f"Bearer {token}"},
                timeout=20
            )
            if r_items.status_code != 200: continue
            
            for item_resp in r_items.json():
                if item_resp.get("code") != 200: continue
                body = item_resp.get("body", {})
                inventory_id = body.get("inventory_id", "")
                if not inventory_id: continue
                
                # SKU del seller
                sku_meli = ""
                for attr in body.get("attributes", []) or []:
                    if attr.get("id") == "SELLER_SKU":
                        sku_meli = (attr.get("value_name") or "").strip()
                        break
                if not sku_meli:
                    sku_meli = (body.get("seller_custom_field") or "").strip()
                if not sku_meli: continue
                
                # Mapear a SKU Lusync
                sku_lus = sku_meli_to_lusync.get(sku_meli, sku_meli)
                
                # Si filtramos por un SKU específico
                if sku_lusync and sku_lus != sku_lusync:
                    continue
                
                # ── 3. Consultar stock fulfillment del inventory ──
                try:
                    r_stock = requests.get(
                        f"https://api.mercadolibre.com/inventories/{inventory_id}/stock/fulfillment",
                        headers={"Authorization": f"Bearer {token}"},
                        timeout=10
                    )
                    if r_stock.status_code == 200:
                        stock_data = r_stock.json()
                        # Estructura típica: {"total": N, "available": N, "in_transit": N, ...}
                        available = int(stock_data.get("available_quantity", 0) or stock_data.get("available", 0) or 0)
                        in_transit = int(stock_data.get("in_transit", 0) or stock_data.get("in_transfer", 0) or 0)
                        total = int(stock_data.get("total", available + in_transit) or 0)
                        
                        # Acumular si el SKU tiene multi-publicación
                        if sku_lus not in resultado:
                            resultado[sku_lus] = {"available": 0, "in_transit": 0, "total": 0}
                        resultado[sku_lus]["available"] += available
                        resultado[sku_lus]["in_transit"] += in_transit
                        resultado[sku_lus]["total"] += total
                except Exception as e:
                    print(f"[StockFullAPI] Error consultando inventory {inventory_id}: {e}")
                    continue
        
        return resultado
    except Exception as e:
        import traceback
        print(f"[StockFullAPI] Error general: {e}")
        print(traceback.format_exc())
        return None
