"""
ripley.py — Integración con Ripley (vía Mirakl)

Ripley en Chile usa la plataforma Mirakl para sellers.
Documentación: https://help.mirakl.net/

Endpoints principales (Mirakl Operator API):
  - PUT /api/offers     → Actualizar stock, precio, oferta
  - GET /api/orders     → Listar órdenes
  - PUT /api/orders/{id}/ship → Marcar despachada
  - GET /api/products   → Catálogo

Modalidades soportadas:
  - Seller (logistic_class='') → descuenta de bodega CENTRAL
  - FBR Fulfillment (logistic_class='FBR') → descuenta de RIPLEY_FBM
"""
import os
import requests
from datetime import datetime, timedelta
from flask import Blueprint, jsonify, request, session

# ═══════════════════════════════════════════════════════════════════════════
# CONFIGURACIÓN
# ═══════════════════════════════════════════════════════════════════════════
# IMPORTANTE: La API Key SIEMPRE debe venir de variables de entorno.
# NUNCA hardcodearla aquí - exponer credenciales en GitHub es un riesgo de seguridad.
RIPLEY_API_KEY = os.environ.get("RIPLEY_API_KEY", "")
RIPLEY_BASE_URL = os.environ.get("RIPLEY_BASE_URL", "https://ripley-prod.mirakl.net")


def ripley_headers():
    """Headers estándar para llamadas a la API Mirakl de Ripley."""
    return {
        "Authorization": RIPLEY_API_KEY,
        "Accept": "application/json",
        "Content-Type": "application/json"
    }


def verificar_conexion_ripley():
    """Hace un ping a la API para confirmar que la conexión y el API Key funcionan."""
    if not RIPLEY_API_KEY:
        return {
            "ok": False,
            "error": "RIPLEY_API_KEY no configurada en variables de entorno",
            "mensaje": "Agrega RIPLEY_API_KEY en Render → Environment"
        }
    try:
        # Endpoint liviano: 1 sola orden
        res = requests.get(
            f"{RIPLEY_BASE_URL}/api/orders",
            headers=ripley_headers(),
            params={"max": 1},
            timeout=10
        )
        return {
            "ok": res.status_code == 200,
            "status_code": res.status_code,
            "mensaje": "Conexión exitosa" if res.status_code == 200 else f"Error {res.status_code}"
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ═══════════════════════════════════════════════════════════════════════════
# STOCK Y PRECIOS - Actualizar ofertas
# ═══════════════════════════════════════════════════════════════════════════

def actualizar_stock_ripley(sku, cantidad):
    """Actualiza el stock de un SKU usando OF24 con todos los campos requeridos.

    OF24 (POST /api/offers) requiere price, state_code y logistic_class además
    de quantity. Si solo enviamos quantity, Mirakl rechaza con 'lines_in_error'.

    Para no perder los otros valores, primero consultamos la oferta actual,
    cambiamos solo el quantity y reenviamos todos los campos.
    """
    try:
        # 1) Obtener oferta actual para preservar precio, state_code y logistic_class
        res_get = requests.get(
            f"{RIPLEY_BASE_URL}/api/offers",
            headers={**ripley_headers(), "Content-Type": "application/json"},
            params={"sku": sku, "max": 1},
            timeout=15
        )
        if res_get.status_code != 200:
            print(f"[Ripley] No se pudo obtener oferta actual {sku}: {res_get.status_code}")
            return False
        ofertas = res_get.json().get("offers", [])
        if not ofertas:
            print(f"[Ripley] SKU {sku} no encontrado en Ripley")
            return False

        offer_actual = ofertas[0]

        # 2) Construir payload con todos los campos requeridos
        offer_payload = {
            "shop_sku": sku,
            "update_delete": "update",
            "quantity": int(cantidad),
            "price": offer_actual.get("price"),
            "state_code": str(offer_actual.get("state_code", "11")),
            "logistic_class": (offer_actual.get("logistic_class") or {}).get("code", "")
        }
        # Si tenía discount_price activo, conservarlo
        if offer_actual.get("discount_price"):
            offer_payload["discount_price"] = offer_actual["discount_price"]
            if offer_actual.get("discount_start_date"):
                offer_payload["discount_start_date"] = offer_actual["discount_start_date"]
            if offer_actual.get("discount_end_date"):
                offer_payload["discount_end_date"] = offer_actual["discount_end_date"]

        # 3) POST a OF24
        res = requests.post(
            f"{RIPLEY_BASE_URL}/api/offers",
            headers={**ripley_headers(), "Content-Type": "application/json"},
            json={"offers": [offer_payload]},
            timeout=20
        )
        if res.status_code in (200, 201, 202):
            try:
                data = res.json()
                import_id = data.get("import_id", "?")
                print(f"[Ripley] Stock {sku}={cantidad} OK (import_id={import_id})")
            except:
                pass
            return True
        print(f"[Ripley] Stock {sku} ERROR {res.status_code}: {res.text[:300]}")
        return False
    except Exception as e:
        print(f"[Ripley] actualizar_stock {sku} error: {e}")
        return False


def actualizar_stock_ripley_lusync(sku_lusync, cantidad):
    """Actualiza stock en Ripley para TODAS las publicaciones de un SKU Lusync.

    Returns:
        dict: {ok, total_publicaciones, exitosas, fallidas, log}
    """
    from inventario import obtener_publicaciones_canal
    publicaciones = obtener_publicaciones_canal(sku_lusync, "ripley")
    if not publicaciones:
        try:
            from inventario import get_sku_canal
            sku_legacy = get_sku_canal(sku_lusync, "ripley")
            if sku_legacy:
                publicaciones = [{"id": None, "sku_canal": sku_legacy, "item_id_canal": None}]
        except: pass
        if not publicaciones:
            publicaciones = [{"id": None, "sku_canal": sku_lusync, "item_id_canal": None}]

    exitosas, fallidas = 0, 0
    log = []
    for pub in publicaciones:
        sku_rp = (pub.get("sku_canal") or "").strip()
        if not sku_rp:
            fallidas += 1
            continue
        ok = actualizar_stock_ripley(sku_rp, cantidad)
        if ok: exitosas += 1
        else: fallidas += 1
        log.append(f"  {sku_rp}: {'OK' if ok else 'FAIL'}")

    return {"ok": exitosas > 0, "total_publicaciones": len(publicaciones),
            "exitosas": exitosas, "fallidas": fallidas, "log": log}


def actualizar_stocks_ripley_lote(skus_cantidades):
    """Actualiza múltiples SKUs en una sola llamada OF24.

    Args:
        skus_cantidades: dict {sku: cantidad} o lista [(sku, cantidad), ...]
    """
    try:
        if isinstance(skus_cantidades, dict):
            items = list(skus_cantidades.items())
        else:
            items = list(skus_cantidades)
        if not items:
            return False, "Lista vacía"

        # 1) Obtener TODAS las ofertas del seller en una sola llamada
        ofertas_dict = {}
        try:
            offset = 0
            while True:
                res_get = requests.get(
                    f"{RIPLEY_BASE_URL}/api/offers",
                    headers={**ripley_headers(), "Content-Type": "application/json"},
                    params={"max": 100, "offset": offset},
                    timeout=20
                )
                if res_get.status_code != 200:
                    break
                ofertas = res_get.json().get("offers", [])
                if not ofertas:
                    break
                for o in ofertas:
                    if o.get("shop_sku"):
                        ofertas_dict[o["shop_sku"]] = o
                if len(ofertas) < 100:
                    break
                offset += 100
                if offset > 500:
                    break
        except Exception as e:
            return False, f"Error obteniendo ofertas: {e}"

        # 2) Construir payload para todos los SKUs encontrados
        offers_payload = []
        no_encontrados = []
        for sku, cantidad in items:
            offer_actual = ofertas_dict.get(sku)
            if not offer_actual:
                no_encontrados.append(sku)
                continue
            payload = {
                "shop_sku": sku,
                "update_delete": "update",
                "quantity": int(cantidad),
                "price": offer_actual.get("price"),
                "state_code": str(offer_actual.get("state_code", "11")),
                "logistic_class": (offer_actual.get("logistic_class") or {}).get("code", "")
            }
            if offer_actual.get("discount_price"):
                payload["discount_price"] = offer_actual["discount_price"]
                if offer_actual.get("discount_start_date"):
                    payload["discount_start_date"] = offer_actual["discount_start_date"]
                if offer_actual.get("discount_end_date"):
                    payload["discount_end_date"] = offer_actual["discount_end_date"]
            offers_payload.append(payload)

        if not offers_payload:
            return False, f"Ninguno de los {len(items)} SKUs existe en Ripley. No encontrados: {no_encontrados[:10]}"

        # 3) POST OF24 con todos los offers
        res = requests.post(
            f"{RIPLEY_BASE_URL}/api/offers",
            headers={**ripley_headers(), "Content-Type": "application/json"},
            json={"offers": offers_payload},
            timeout=30
        )
        if res.status_code in (200, 201, 202):
            try:
                data = res.json()
                import_id = data.get("import_id", "?")
                print(f"[Ripley] Lote {len(offers_payload)} ofertas OK (import_id={import_id}, no_encontrados={len(no_encontrados)})")
                return True, {"import_id": import_id, "enviados": len(offers_payload), "no_encontrados": no_encontrados}
            except:
                return True, None
        print(f"[Ripley] Lote ERROR {res.status_code}: {res.text[:300]}")
        return False, f"Status {res.status_code}: {res.text[:200]}"
    except Exception as e:
        print(f"[Ripley] lote error: {e}")
        return False, str(e)


def consultar_estado_import_stock(import_id):
    """Consulta el estado de un import en Mirakl. Funciona tanto para STO02 como OF02.
    Estados: WAITING, RUNNING, COMPLETE, FAILED, INTERRUPTED."""
    try:
        # OF02 = consultar estado de import OF24
        res = requests.get(
            f"{RIPLEY_BASE_URL}/api/offers/imports/{import_id}",
            headers=ripley_headers(),
            timeout=10
        )
        if res.status_code == 200:
            return res.json()
        return None
    except Exception as e:
        print(f"[Ripley] consultar import error: {e}")
        return None


def actualizar_precio_ripley(sku, precio_normal, precio_oferta=None):
    """Actualiza el precio de un SKU usando OF24 con todos los campos requeridos.

    Misma lógica que actualizar_stock_ripley: obtenemos la oferta actual,
    cambiamos solo el precio (manteniendo quantity y demás) y reenviamos.

    Args:
        sku: shop_sku del seller
        precio_normal: precio principal (CLP, sin decimales)
        precio_oferta: precio rebajado opcional. Si se pasa, se publica como
                       discount_price con vigencia de 30 días.
    """
    try:
        # 1) Obtener oferta actual
        res_get = requests.get(
            f"{RIPLEY_BASE_URL}/api/offers",
            headers={**ripley_headers(), "Content-Type": "application/json"},
            params={"sku": sku, "max": 1},
            timeout=15
        )
        if res_get.status_code != 200:
            print(f"[Ripley] No se pudo obtener oferta {sku}: {res_get.status_code}")
            return False
        ofertas = res_get.json().get("offers", [])
        if not ofertas:
            print(f"[Ripley] SKU {sku} no encontrado en Ripley")
            return False
        offer_actual = ofertas[0]

        # 2) Construir payload conservando quantity y otros campos
        offer_payload = {
            "shop_sku": sku,
            "update_delete": "update",
            "quantity": int(offer_actual.get("quantity", 0)),
            "price": float(precio_normal),
            "state_code": str(offer_actual.get("state_code", "11")),
            "logistic_class": (offer_actual.get("logistic_class") or {}).get("code", "")
        }

        # Manejo de precio de oferta
        if precio_oferta and float(precio_oferta) > 0 and float(precio_oferta) < float(precio_normal):
            hoy = datetime.utcnow()
            offer_payload["discount_price"] = float(precio_oferta)
            offer_payload["discount_start_date"] = hoy.strftime("%Y-%m-%dT00:00:00Z")
            offer_payload["discount_end_date"] = (hoy + timedelta(days=30)).strftime("%Y-%m-%dT23:59:59Z")
        # Si no se pasa precio_oferta pero la oferta actual tenía discount, lo dejamos eliminado
        # (porque OF24 espera todos los campos: si no se mandan, quedan vacíos)

        # 3) POST a OF24
        res = requests.post(
            f"{RIPLEY_BASE_URL}/api/offers",
            headers={**ripley_headers(), "Content-Type": "application/json"},
            json={"offers": [offer_payload]},
            timeout=20
        )
        if res.status_code in (200, 201, 202):
            try:
                data = res.json()
                import_id = data.get("import_id", "?")
                print(f"[Ripley] Precio {sku}={precio_normal} OK (import_id={import_id})")
            except:
                pass
            return True
        print(f"[Ripley] Precio {sku} ERROR {res.status_code}: {res.text[:300]}")
        return False
    except Exception as e:
        print(f"[Ripley] actualizar_precio {sku} error: {e}")
        return False


def actualizar_oferta_completa_ripley(sku, cantidad=None, precio_normal=None,
                                       precio_oferta=None, activo=True):
    """Actualiza varios campos de una oferta a la vez (más eficiente).
    Solo se mandan los campos que no son None."""
    try:
        offer = {
            "shop_sku": sku,
            "update_delete": "update",
            "state_code": 11  # 11 = Nuevo
        }
        if cantidad is not None: offer["quantity"] = int(cantidad)
        if precio_normal is not None: offer["price"] = float(precio_normal)
        if precio_oferta is not None and float(precio_oferta) > 0:
            offer["discount_price"] = float(precio_oferta)
            hoy = datetime.utcnow()
            offer["discount_start_date"] = hoy.strftime("%Y-%m-%dT00:00:00Z")
            offer["discount_end_date"] = (hoy + timedelta(days=30)).strftime("%Y-%m-%dT23:59:59Z")
        if not activo:
            offer["update_delete"] = "delete"

        payload = {"offers": [offer]}
        res = requests.post(
            f"{RIPLEY_BASE_URL}/api/offers",
            headers=ripley_headers(),
            json=payload,
            timeout=20
        )
        return res.status_code in (200, 201, 202), res.text[:300] if res.status_code >= 400 else None
    except Exception as e:
        return False, str(e)


# ═══════════════════════════════════════════════════════════════════════════
# PRODUCTOS / OFERTAS (para auto-mapeo de SKUs)
# ═══════════════════════════════════════════════════════════════════════════

def obtener_productos_ripley(max_paginas=10, page_size=100):
    """Lista las ofertas (productos) del seller en Ripley con paginación.

    Returns:
        list de dicts con: shop_sku, product_title, price, quantity, state_code
    """
    try:
        todas = []
        offset = 0
        pagina = 0

        while pagina < max_paginas:
            pagina += 1
            res = requests.get(
                f"{RIPLEY_BASE_URL}/api/offers",
                headers={**ripley_headers(), "Content-Type": "application/json"},
                params={"max": page_size, "offset": offset},
                timeout=30
            )
            print(f"[Ripley Items] Página:{pagina} Offset:{offset} Status:{res.status_code}")

            if res.status_code != 200:
                print(f"[Ripley Items] Error: {res.text[:200]}")
                break

            data = res.json()
            ofertas = data.get("offers", [])
            if not ofertas:
                break

            for o in ofertas:
                product = o.get("product") or {}
                producto = {
                    "shop_sku":       o.get("shop_sku", ""),
                    "product_sku":    o.get("product_sku", ""),
                    "product_title":  product.get("title") or o.get("product_title", ""),
                    "price":          o.get("price"),
                    "quantity":       o.get("quantity", 0),
                    "state_code":     o.get("state_code", ""),
                    "active":         o.get("active", True)
                }
                todas.append(producto)

            print(f"[Ripley Items] Página:{pagina} +{len(ofertas)} Total:{len(todas)}")

            # Si trajo menos de page_size, fin
            if len(ofertas) < page_size:
                break
            offset += page_size

        return todas
    except Exception as e:
        print(f"[Ripley] Error productos: {e}")
        return []


# ═══════════════════════════════════════════════════════════════════════════
# ÓRDENES
# ═══════════════════════════════════════════════════════════════════════════

def obtener_ordenes_ripley(estado=None, dias=30, max_resultados=50):
    """Obtiene órdenes recientes de Ripley.

    Args:
        estado: 'WAITING_ACCEPTANCE', 'WAITING_DEBIT', 'SHIPPING', 'SHIPPED', 'CLOSED', etc.
        dias: traer órdenes desde X días atrás
        max_resultados: máximo a retornar (Mirakl pagina de 100 en 100)
    """
    try:
        fecha_desde = (datetime.utcnow() - timedelta(days=dias)).strftime("%Y-%m-%dT00:00:00Z")
        params = {
            "start_date": fecha_desde,
            "max": min(max_resultados, 100)
        }
        if estado:
            params["order_state_codes"] = estado

        res = requests.get(
            f"{RIPLEY_BASE_URL}/api/orders",
            headers=ripley_headers(),
            params=params,
            timeout=20
        )
        if res.status_code != 200:
            print(f"[Ripley] Órdenes error {res.status_code}: {res.text[:200]}")
            return []
        data = res.json()
        return data.get("orders", [])
    except Exception as e:
        print(f"[Ripley] obtener_ordenes error: {e}")
        return []


def obtener_orden_ripley(order_id):
    """Detalle de una orden específica."""
    try:
        res = requests.get(
            f"{RIPLEY_BASE_URL}/api/orders/{order_id}",
            headers=ripley_headers(),
            timeout=15
        )
        if res.status_code == 200:
            return res.json()
        return None
    except Exception as e:
        print(f"[Ripley] orden {order_id} error: {e}")
        return None


def aceptar_orden_ripley(order_id, order_lines):
    """Acepta una orden (workflow Mirakl). order_lines: lista de {order_line_id, accepted: True}."""
    try:
        payload = {
            "order_lines": [
                {"id": l["order_line_id"], "accepted": True}
                for l in order_lines
            ]
        }
        res = requests.put(
            f"{RIPLEY_BASE_URL}/api/orders/{order_id}/accept",
            headers=ripley_headers(),
            json=payload,
            timeout=15
        )
        return res.status_code in (200, 204)
    except Exception as e:
        print(f"[Ripley] aceptar orden error: {e}")
        return False


def marcar_despachada_ripley(order_id, tracking_number=None, carrier_code="OTHER"):
    """Marca una orden como despachada e incluye tracking."""
    try:
        payload = {
            "carrier_code": carrier_code,
            "tracking_number": tracking_number or "",
            "carrier_name": carrier_code
        }
        res = requests.put(
            f"{RIPLEY_BASE_URL}/api/orders/{order_id}/tracking",
            headers=ripley_headers(),
            json=payload,
            timeout=15
        )
        if res.status_code in (200, 204):
            # Marcar como ship
            res2 = requests.put(
                f"{RIPLEY_BASE_URL}/api/orders/{order_id}/ship",
                headers=ripley_headers(),
                timeout=15
            )
            return res2.status_code in (200, 204)
        return False
    except Exception as e:
        print(f"[Ripley] despachar error: {e}")
        return False


# ═══════════════════════════════════════════════════════════════════════════
# CATÁLOGO - Ofertas existentes del seller
# ═══════════════════════════════════════════════════════════════════════════

def obtener_ofertas_ripley(max_resultados=100):
    """Obtiene todas las ofertas del seller (productos publicados)."""
    try:
        params = {"max": min(max_resultados, 100), "paginate": "true"}
        res = requests.get(
            f"{RIPLEY_BASE_URL}/api/offers",
            headers=ripley_headers(),
            params=params,
            timeout=20
        )
        if res.status_code != 200:
            return []
        data = res.json()
        return data.get("offers", [])
    except Exception as e:
        print(f"[Ripley] ofertas error: {e}")
        return []


# ═══════════════════════════════════════════════════════════════════════════
# BLUEPRINT - Endpoints HTTP
# ═══════════════════════════════════════════════════════════════════════════
ripley_bp = Blueprint("ripley", __name__)


@ripley_bp.route("/ripley/test")
def ripley_test():
    """Endpoint de prueba para verificar conexión con Ripley."""
    if not session.get("logged"): return jsonify({"error": "no autorizado"}), 401
    return jsonify(verificar_conexion_ripley())


@ripley_bp.route("/ripley/sync_stock", methods=["POST"])
def ripley_sync_stock():
    """Envía a Ripley el stock actual (de bodega CENTRAL) de todos los SKUs mapeados.
    Usa el endpoint STO01 con un único CSV (más eficiente que mandar 1 por 1)."""
    if not session.get("logged"): return jsonify({"ok": False}), 401
    try:
        from inventario import listar_sku_mapeo, get_stock_bodega, registrar_audit
        registrar_audit(session.get("usuario","Sistema"), request.remote_addr,
                        "sync_ripley_stock", detalle="Sync masivo stock Ripley")

        productos_mapeo = listar_sku_mapeo()
        skus_a_enviar = {}
        log = []
        for fila in productos_mapeo:
            sku_lusync = fila.get("sku_lusync", "")
            sku_ripley = (fila.get("sku_ripley", "") or "").strip()
            if not sku_ripley or not sku_lusync:
                continue
            stock = get_stock_bodega(sku_lusync, "CENTRAL")
            skus_a_enviar[sku_ripley] = stock
            log.append(f"→ {sku_ripley}={stock}u")

        if not skus_a_enviar:
            return jsonify({"ok": True, "enviados": 0, "fallidos": 0,
                            "log": ["Sin SKUs mapeados a Ripley"]})

        # Enviar TODOS en una sola llamada
        ok, info = actualizar_stocks_ripley_lote(skus_a_enviar)
        if ok:
            return jsonify({
                "ok": True,
                "enviados": len(skus_a_enviar),
                "fallidos": 0,
                "import_id": info,
                "nota": "Envío encolado. Mirakl tarda 5-15min en procesar y reflejar.",
                "log": log[:30]
            })
        return jsonify({"ok": False, "enviados": 0, "fallidos": len(skus_a_enviar),
                        "error": str(info), "log": log[:30]})
    except Exception as e:
        import traceback
        return jsonify({"ok": False, "error": str(e), "trace": traceback.format_exc()}), 500


@ripley_bp.route("/ripley/sync_precios", methods=["POST"])
def ripley_sync_precios():
    """Envía a Ripley los precios de todos los SKUs mapeados (precio normal + oferta)."""
    if not session.get("logged"): return jsonify({"ok": False}), 401
    try:
        from inventario import listar_sku_mapeo, get_conn, registrar_audit
        registrar_audit(session.get("usuario","Sistema"), request.remote_addr,
                        "sync_ripley_precios", detalle="Sync masivo precios Ripley")

        # Obtener precios de productos
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
            sku_ripley = (fila.get("sku_ripley", "") or "").strip()
            if not sku_ripley or not sku_lusync:
                continue
            p = precios.get(sku_lusync, {})
            precio = p.get("normal") or 0
            if not precio:
                log.append(f"⚠ {sku_lusync} sin precio_normal")
                continue
            ok = actualizar_precio_ripley(sku_ripley, precio, p.get("oferta"))
            if ok:
                enviados += 1
                log.append(f"✓ {sku_ripley} → ${precio}" + (f" (oferta ${p['oferta']})" if p.get("oferta") else ""))
            else:
                fallidos += 1
                log.append(f"× {sku_ripley} falló")
        return jsonify({"ok": True, "enviados": enviados, "fallidos": fallidos, "log": log[:30]})
    except Exception as e:
        import traceback
        return jsonify({"ok": False, "error": str(e), "trace": traceback.format_exc()}), 500


@ripley_bp.route("/ripley/sync_ordenes")
def ripley_sync_ordenes():
    """Sincroniza órdenes históricas de Ripley descontando del stock por bodega correcta."""
    if not session.get("logged"): return jsonify({"ok": False}), 401
    try:
        from inventario import (cargar_productos, orden_ya_procesada_texto,
                                marcar_orden_procesada_texto, registrar_audit,
                                listar_sku_mapeo)
        from bodegas_logic import descontar_venta, sincronizar_stock_a_marketplaces

        registrar_audit(session.get("usuario","Sistema"), request.remote_addr,
                        "sync_ripley", entidad="ordenes",
                        detalle="Sync manual órdenes Ripley")

        dias = int(request.args.get("dias", 30))
        productos_dict = {p["sku"]: p for p in cargar_productos()}
        nuevas = 0
        errores = []
        log = []

        # Estados Mirakl que indican orden activa
        estados = ["WAITING_ACCEPTANCE", "WAITING_DEBIT", "SHIPPING", "SHIPPED"]
        for estado in estados:
            try:
                ordenes = obtener_ordenes_ripley(estado=estado, dias=dias, max_resultados=50)
                log.append(f"Estado {estado}: {len(ordenes)} órdenes")

                for o in ordenes:
                    order_id = str(o.get("order_id", ""))
                    if not order_id:
                        continue
                    ripley_key = f"RIPLEY-{order_id}"
                    if orden_ya_procesada_texto(ripley_key):
                        continue
                    marcar_orden_procesada_texto(ripley_key)

                    # ── Extraer fecha real de compra del marketplace ────────
                    # Mirakl/Ripley devuelve created_date en ISO con timezone UTC
                    # Ej: "2026-05-03T18:32:15Z" o "2026-05-03T18:32:15+00:00"
                    fecha_compra_ripley = None
                    try:
                        date_str = (o.get("created_date") or o.get("createdDate") or "")
                        if date_str:
                            date_str_clean = date_str.replace("Z", "+00:00")
                            fecha_compra_ripley = datetime.fromisoformat(date_str_clean)
                    except Exception as e:
                        log.append(f"  Orden {order_id}: no se pudo parsear created_date: {e}")
                        fecha_compra_ripley = None

                    # Detectar FBR vs Seller
                    from bodegas_logic import detectar_fulfillment_ripley
                    es_fbr = detectar_fulfillment_ripley(o)
                    tipo_str = "FBR" if es_fbr else "Seller"

                    # Iterar order_lines
                    for line in o.get("order_lines", []):
                        sku_ripley = line.get("offer_sku") or line.get("shop_sku") or ""
                        cantidad = int(line.get("quantity", 1) or 1)
                        if not sku_ripley:
                            continue

                        # Buscar SKU Lusync: PRIORIDAD sku_mapeo_canal, fallback legacy
                        sku_lusync = None
                        try:
                            from inventario import obtener_sku_lusync_por_canal
                            sku_lusync = obtener_sku_lusync_por_canal("ripley", sku_canal=sku_ripley)
                        except: pass
                        if not sku_lusync:
                            try:
                                for fila in listar_sku_mapeo():
                                    if fila.get("sku_ripley") == sku_ripley:
                                        sku_lusync = fila.get("sku_lusync")
                                        break
                            except: pass
                        if not sku_lusync:
                            sku_lusync = sku_ripley  # último fallback

                        if sku_lusync not in productos_dict:
                            log.append(f"{order_id}: SKU '{sku_lusync}' no encontrado")
                            continue

                        resultado = descontar_venta(
                            sku=sku_lusync,
                            cantidad=cantidad,
                            canal="Ripley",
                            fulfillment=es_fbr,
                            orden_id=order_id,
                            motivo=f"Venta Ripley {tipo_str}",
                            fecha_compra_marketplace=fecha_compra_ripley,
                            origen_registro="sync_manual"
                        )
                        log.append(f"{order_id} {tipo_str}: {sku_lusync} -{cantidad} desde {resultado['bodega']}")

                        # Sync cruzado SOLO si fue Seller (afectó Central)
                        if not es_fbr:
                            try:
                                sincronizar_stock_a_marketplaces(sku_lusync, excepto=["ripley"])
                            except Exception as e:
                                log.append(f"  Sync cruzado falló: {e}")

                    nuevas += 1
                # Liberar memoria entre estados
                del ordenes
                import gc
                gc.collect()
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


@ripley_bp.route("/ripley/ofertas")
def ripley_ver_ofertas():
    """Lista las ofertas (productos publicados) en Ripley."""
    if not session.get("logged"): return jsonify({"error": "no autorizado"}), 401
    ofertas = obtener_ofertas_ripley(max_resultados=100)
    return jsonify({
        "total": len(ofertas),
        "ofertas": [
            {
                "shop_sku": o.get("shop_sku"),
                "product_sku": o.get("product_sku"),
                "title": o.get("product_title", "")[:80],
                "price": o.get("price"),
                "discount_price": o.get("discount_price"),
                "quantity": o.get("quantity"),
                "active": o.get("active"),
                "logistic_class": o.get("logistic_class", "")
            } for o in ofertas[:50]
        ]
    })


@ripley_bp.route("/ripley/estado")
def ripley_estado():
    """Devuelve el estado de la conexión + resumen de últimas órdenes."""
    if not session.get("logged"): return jsonify({"error": "no autorizado"}), 401
    conn = verificar_conexion_ripley()
    ofertas_count = len(obtener_ofertas_ripley(max_resultados=10))
    return jsonify({
        "conectado": bool(conn.get("ok")),
        "conexion": conn,
        "ofertas_visibles": ofertas_count,
        "api_key_configurada": bool(RIPLEY_API_KEY),
        "base_url": RIPLEY_BASE_URL
    })


@ripley_bp.route("/ripley/sync_estado")
def ripley_sync_estado():
    """Compara stock en Lusync (CENTRAL) vs stock real en Ripley (Mirakl).
    Devuelve la misma estructura que /paris/sync_estado para reutilizar UI."""
    if not session.get("logged"): return jsonify({"error": "no autorizado"}), 401
    try:
        from inventario import listar_sku_mapeo, get_stock_bodega

        conexion = verificar_conexion_ripley()

        # Obtener TODAS las ofertas (necesitamos paginar para más de 100)
        ofertas_dict = {}
        try:
            offset = 0
            while True:
                params = {"max": 100, "offset": offset}
                res = requests.get(f"{RIPLEY_BASE_URL}/api/offers",
                                   headers=ripley_headers(), params=params, timeout=20)
                if res.status_code != 200:
                    break
                data = res.json()
                ofertas = data.get("offers", [])
                if not ofertas:
                    break
                for o in ofertas:
                    shop_sku = o.get("shop_sku", "")
                    if shop_sku:
                        ofertas_dict[shop_sku] = {
                            "quantity": o.get("quantity", 0),
                            "price": o.get("price", 0),
                            "discount_price": o.get("discount_price"),
                            "active": o.get("active", False),
                            "logistic_class": (o.get("logistic_class") or {}).get("code", ""),
                            "title": o.get("product_title", "")
                        }
                if len(ofertas) < 100:
                    break
                offset += 100
                if offset > 500:  # Safety: max 5 páginas
                    break
        except Exception as e:
            return jsonify({"error_ripley": str(e), "conexion": conexion}), 500

        # Comparar stock CENTRAL vs Ripley
        mapeo = listar_sku_mapeo()
        resultados = []
        for fila in mapeo:
            sku_ripley = (fila.get("sku_ripley", "") or "").strip()
            if not sku_ripley:
                continue
            sku_lusync = fila.get("sku_lusync", "")
            stock_central = get_stock_bodega(sku_lusync, "CENTRAL")
            ripley_data = ofertas_dict.get(sku_ripley, {})
            stock_ripley = ripley_data.get("quantity", None) if ripley_data else None

            if stock_ripley is None:
                estado = "no_encontrado"
            elif stock_ripley == stock_central:
                estado = "sincronizado"
            else:
                estado = "desincronizado"

            resultados.append({
                "sku_lusync": sku_lusync,
                "sku_paris": sku_ripley,  # nombre genérico para reutilizar template Paris
                "sku_ripley": sku_ripley,
                "nombre": fila.get("nombre", "") or ripley_data.get("title", ""),
                "stock_lusync": stock_central,
                "stock_paris": stock_ripley,  # nombre genérico
                "stock_ripley": stock_ripley,
                "diferencia": (stock_ripley - stock_central) if stock_ripley is not None else None,
                "ultima_actualizacion_paris": "",  # Ripley no devuelve fecha
                "logistic_class": ripley_data.get("logistic_class", ""),
                "activo_en_ripley": ripley_data.get("active", False),
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


@ripley_bp.route("/ripley/forzar_sync_sku", methods=["POST"])
def ripley_forzar_sync_sku():
    """Fuerza el envío de stock de un SKU específico a Ripley."""
    if not session.get("logged"): return jsonify({"ok": False}), 401
    try:
        from inventario import listar_sku_mapeo, get_stock_bodega
        data = request.json or {}
        sku_lusync = data.get("sku_lusync", "")
        if not sku_lusync:
            return jsonify({"ok": False, "error": "sku_lusync requerido"}), 400

        # Buscar SKU Ripley en mapeo
        sku_ripley = None
        for fila in listar_sku_mapeo():
            if fila.get("sku_lusync") == sku_lusync:
                sku_ripley = (fila.get("sku_ripley", "") or "").strip()
                break
        if not sku_ripley:
            return jsonify({"ok": False, "error": f"SKU {sku_lusync} no tiene mapeo Ripley"}), 400

        stock = get_stock_bodega(sku_lusync, "CENTRAL")

        # Usar el lote para tener el detalle del error
        ok, info = actualizar_stocks_ripley_lote({sku_ripley: stock})
        if ok:
            return jsonify({
                "ok": True,
                "stock_enviado": stock,
                "sku_ripley": sku_ripley,
                "import_id": info,
                "nota": "Mirakl tarda 5-15min en procesar"
            })
        else:
            return jsonify({
                "ok": False,
                "error": f"Mirakl rechazó: {info}",
                "sku_ripley": sku_ripley,
                "stock_intentado": stock
            })
    except Exception as e:
        import traceback
        return jsonify({"ok": False, "error": str(e), "trace": traceback.format_exc()}), 500


@ripley_bp.route("/ripley/debug_stock/<sku>")
def ripley_debug_stock(sku):
    """Endpoint de debug: prueba VARIOS endpoints alternativos de Mirakl para
    actualizar stock y muestra cuál funciona."""
    if not session.get("logged"): return jsonify({"error": "no autorizado"}), 401
    try:
        cantidad_prueba = int(request.args.get("cantidad", 1))
        resultados = []

        headers_auth = {
            "Authorization": RIPLEY_API_KEY,
            "Accept": "application/json"
        }

        # ── Test 1: STO01 con CSV (POST /api/offers/stocks/imports) ──
        csv_content = "sku;quantity\n" + f"{sku};{cantidad_prueba}\n"
        try:
            res = requests.post(
                f"{RIPLEY_BASE_URL}/api/offers/stocks/imports",
                headers=headers_auth,
                files={"file": ("stock.csv", csv_content, "text/csv")},
                timeout=15
            )
            resultados.append({
                "test": "1. STO01 POST /api/offers/stocks/imports (CSV)",
                "status": res.status_code,
                "response": res.text[:300]
            })
        except Exception as e:
            resultados.append({"test": "1. STO01 CSV", "error": str(e)})

        # ── Test 2: OF24 POST /api/offers JSON con campos completos ──
        # Primero obtenemos los datos actuales del SKU para no perderlos
        try:
            res_get = requests.get(
                f"{RIPLEY_BASE_URL}/api/offers",
                headers={**headers_auth, "Content-Type": "application/json"},
                params={"sku": sku, "max": 1},
                timeout=10
            )
            offer_actual = None
            if res_get.status_code == 200:
                ofertas = res_get.json().get("offers", [])
                if ofertas:
                    offer_actual = ofertas[0]

            if offer_actual:
                # Construir payload con TODOS los campos requeridos
                payload = {
                    "offers": [{
                        "shop_sku": sku,
                        "update_delete": "update",
                        "quantity": cantidad_prueba,
                        "price": offer_actual.get("price"),
                        "state_code": offer_actual.get("state_code", "11"),
                        "logistic_class": (offer_actual.get("logistic_class") or {}).get("code", "")
                    }]
                }
                res = requests.post(
                    f"{RIPLEY_BASE_URL}/api/offers",
                    headers={**headers_auth, "Content-Type": "application/json"},
                    json=payload,
                    timeout=15
                )
                resultados.append({
                    "test": "2. OF24 POST /api/offers (JSON con campos completos)",
                    "payload_enviado": payload,
                    "status": res.status_code,
                    "response": res.text[:500]
                })
            else:
                resultados.append({
                    "test": "2. OF24 - no se pudo obtener oferta actual para SKU " + sku,
                    "status": res_get.status_code,
                    "response": res_get.text[:300]
                })
        except Exception as e:
            resultados.append({"test": "2. OF24 JSON", "error": str(e)})

        # ── Test 3: OF01 POST /api/offers/imports (CSV de ofertas) ──
        try:
            csv_content_2 = "sku;product-id;product-id-type;quantity;update-delete\n"
            csv_content_2 += f"{sku};{sku};SHOP_SKU;{cantidad_prueba};update\n"
            res = requests.post(
                f"{RIPLEY_BASE_URL}/api/offers/imports",
                headers=headers_auth,
                files={"file": ("offer.csv", csv_content_2, "text/csv")},
                timeout=15
            )
            resultados.append({
                "test": "3. OF01 POST /api/offers/imports (CSV de oferta completa)",
                "status": res.status_code,
                "response": res.text[:300]
            })
        except Exception as e:
            resultados.append({"test": "3. OF01 CSV", "error": str(e)})

        # ── Test 4: Listar imports recientes para ver qué API funciona ──
        try:
            res = requests.get(
                f"{RIPLEY_BASE_URL}/api/offers/imports",
                headers=headers_auth,
                params={"max": 3},
                timeout=10
            )
            resultados.append({
                "test": "4. GET /api/offers/imports (listar imports recientes)",
                "status": res.status_code,
                "response": res.text[:500]
            })
        except Exception as e:
            resultados.append({"test": "4. Listar imports", "error": str(e)})

        return jsonify({
            "sku_probado": sku,
            "cantidad": cantidad_prueba,
            "base_url": RIPLEY_BASE_URL,
            "tests": resultados
        })
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500
