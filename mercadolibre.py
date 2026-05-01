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

def actualizar_stock_meli(sku_seller, cantidad):
    """
    Actualiza stock en MercadoLibre. Busca el ITEM_ID asociado al SKU usando user_products.
    Para apps simples, suele ser más directo guardar el ITEM_ID junto al SKU en el mapeo.
    """
    try:
        from inventario import get_sku_canal
        sku_meli = get_sku_canal(sku_seller, "mercadolibre")
    except Exception:
        sku_meli = None

    if not sku_meli or str(sku_meli).strip() == "":
        sku_meli = sku_seller

    sku_meli = str(sku_meli).strip()

    # En MELI cada publicación tiene un MLC item_id (no usa SKU directo).
    # El sku_meli en nuestro mapeo debe ser el item_id (ej: MLC1234567890)
    if not sku_meli.startswith("MLC"):
        print(f"[MELI Stock] SKU '{sku_meli}' no parece un item_id válido (debe empezar con MLC)")
        return False

    try:
        res = requests.put(
            f"{MELI_API_URL}/items/{sku_meli}",
            headers=meli_headers(),
            json={"available_quantity": int(cantidad)},
            timeout=15
        )
        print(f"[MELI Stock] Item:{sku_meli} Qty:{cantidad} Status:{res.status_code}")
        return res.status_code in (200, 201)
    except Exception as e:
        print(f"[MELI Stock] Error {sku_meli}: {e}")
        return False


def obtener_publicaciones_meli(limite=50, offset=0):
    """Lista las publicaciones del seller para que pueda mapearlas a SKUs Lusync."""
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

        # Detalle de cada item (lote de hasta 20 por llamada)
        items = []
        for i in range(0, len(item_ids), 20):
            batch = item_ids[i:i+20]
            ids_str = ",".join(batch)
            res2 = requests.get(
                f"{MELI_API_URL}/items",
                headers=meli_headers(),
                params={"ids": ids_str, "attributes": "id,title,available_quantity,price,status,seller_custom_field"},
                timeout=20
            )
            if res2.status_code == 200:
                for r in res2.json():
                    body = r.get("body", {})
                    items.append({
                        "item_id":       body.get("id"),
                        "title":         body.get("title"),
                        "stock":         body.get("available_quantity", 0),
                        "price":         body.get("price"),
                        "status":        body.get("status"),
                        "sku_seller":    body.get("seller_custom_field", "")
                    })
        return {"items": items, "total": data.get("paging", {}).get("total", len(item_ids))}
    except Exception as e:
        print(f"[MELI] Error obteniendo publicaciones: {e}")
        return None


# ── ÓRDENES ─────────────────────────────────────────────────────────────────

def obtener_ordenes_meli(limit=50, offset=0, estado=None):
    """Lista órdenes recientes del seller."""
    try:
        from inventario import get_meli_auth
        auth = get_meli_auth()
        if not auth or not auth.get("user_id"):
            return []

        params = {"seller": auth["user_id"], "limit": limit, "offset": offset, "sort": "date_desc"}
        if estado:
            params["order.status"] = estado

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


def obtener_orden_meli(order_id):
    """Detalle de una orden específica.
    Si recibe un pack_id en vez de un order_id, lo resuelve automáticamente."""
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
                # El pack contiene una lista de órdenes internas; tomamos la primera
                ordenes = pack.get("orders", [])
                if ordenes:
                    primer_order_id = ordenes[0].get("id")
                    if primer_order_id:
                        print(f"[MELI] Pack {order_id} → orden interna {primer_order_id}")
                        # Recursivamente traer la orden real
                        res3 = requests.get(
                            f"{MELI_API_URL}/orders/{primer_order_id}",
                            headers=meli_headers(),
                            timeout=15
                        )
                        if res3.status_code == 200:
                            return res3.json()
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
    """Actualiza el precio de una publicación."""
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
    """Cuando llega una orden nueva o cambia de estado, descontamos stock si es venta confirmada.
    Detecta automáticamente si es Full o Seller envía y descuenta de la bodega correcta."""
    try:
        order_id = resource.split("/")[-1]
        orden = obtener_orden_meli(order_id)
        if not orden:
            return False

        estado = orden.get("status", "")
        # Solo procesar órdenes pagadas/confirmadas, no canceladas
        if estado not in ("paid", "confirmed", "payment_required"):
            print(f"[MELI Webhook] Orden {order_id} en estado {estado}, no se procesa")
            return True

        from inventario import (orden_ya_procesada_texto, marcar_orden_procesada_texto,
                                descontar_venta_inteligente, detectar_fulfillment_meli,
                                listar_sku_mapeo, cargar_productos)
        from woo import actualizar_stock_woo
        try:
            from walmart import actualizar_stock_walmart
        except: actualizar_stock_walmart = None
        try:
            from paris import actualizar_stock_paris
        except: actualizar_stock_paris = None

        meli_key = f"MELI-{order_id}"
        if orden_ya_procesada_texto(meli_key):
            print(f"[MELI Webhook] Orden {order_id} ya procesada")
            return True

        marcar_orden_procesada_texto(meli_key)

        # ── Detectar si es venta Full o Seller ──
        es_full = detectar_fulfillment_meli(orden)
        tipo_str = "FULL" if es_full else "Seller"
        print(f"[MELI Webhook] Orden {order_id} tipo: {tipo_str}")

        productos = cargar_productos()
        productos_dict = {p["sku"]: p for p in productos}

        for item in orden.get("order_items", []):
            item_data = item.get("item", {})
            item_id = item_data.get("id", "")

            # MELI puede tener el SKU en varios campos. Probamos por orden de confiabilidad:
            #   1. item.seller_sku        → el campo directo más nuevo del API
            #   2. item.seller_custom_field → campo legacy
            #   3. CONSULTAR el detalle del item (cuando los anteriores vienen vacíos)
            #   4. item.id                 → fallback al item_id MLC...
            sku_meli = (
                (item_data.get("seller_sku") or "").strip()
                or (item_data.get("seller_custom_field") or "").strip()
            )

            # Si los campos directos están vacíos, consultar el detalle del item
            if not sku_meli and item_id:
                print(f"[MELI Webhook] SKU vacío en orden, consultando /items/{item_id}...")
                sku_resuelto = obtener_sku_de_item_meli(item_id)
                if sku_resuelto:
                    sku_meli = sku_resuelto
                    print(f"[MELI Webhook] SKU resuelto desde item detail: {sku_meli}")

            # Último fallback: usar el item_id como SKU
            if not sku_meli:
                sku_meli = item_id

            qty = int(item.get("quantity", 1))

            # Buscar SKU Lusync correspondiente vía mapeo
            # Buscar por sku_meli O por item_id (cualquiera que esté en el mapeo)
            sku_lusync = sku_meli
            try:
                for fila in listar_sku_mapeo():
                    sku_mapped = (fila.get("sku_mercadolibre") or "").strip()
                    if sku_mapped and (sku_mapped == sku_meli or sku_mapped == item_id):
                        sku_lusync = fila.get("sku_lusync")
                        break
            except: pass

            if sku_lusync not in productos_dict:
                print(f"[MELI Webhook] SKU '{sku_lusync}' no encontrado en inventario")
                continue

            # ── Descontar de la bodega correcta ──
            resultado = descontar_venta_inteligente(
                sku=sku_lusync,
                cantidad=qty,
                canal="MercadoLibre",
                fulfillment=es_full,
                orden_id=order_id,
                motivo=f"Venta MercadoLibre{' Full' if es_full else ''}",
                usuario="Sistema"
            )
            print(f"[MELI Webhook] {sku_lusync} -{qty} desde {resultado['bodega']} → {resultado['stock_despues']}")

            # Sync stock a otros canales SOLO si fue venta Seller (Central afectada)
            # Si fue Full, la bodega central no cambia, así que no hay que sincronizar
            if not es_full:
                p = productos_dict[sku_lusync]
                # Recargar stock total después del descuento
                from inventario import cargar_productos as _cp
                productos_actualizados = _cp()
                stock_total = next((pp["stock"] for pp in productos_actualizados if pp["sku"] == sku_lusync), 0)
                try: actualizar_stock_woo(sku_lusync, stock_total)
                except: pass
                if actualizar_stock_walmart:
                    try: actualizar_stock_walmart(sku_lusync, stock_total)
                    except: pass
                if actualizar_stock_paris:
                    try: actualizar_stock_paris(sku_lusync, stock_total)
                    except: pass
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
