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
    """Actualiza solo el stock de una oferta usando el endpoint STO01 de Mirakl.

    POST /api/offers/stocks/imports es el endpoint correcto para SOLO modificar
    stock sin tocar otros campos (precio, state_code, etc).

    Es asíncrono: devuelve un import_id. Para verificar éxito hay que consultar
    /api/offers/stocks/imports/{id} con STO02. Para uso simple, asumimos que si
    el import se aceptó (201), el stock se actualizará en pocos segundos.
    """
    try:
        import csv
        import io

        # Mirakl STO01 espera un CSV con formato específico
        # Columnas: sku, quantity
        csv_content = "sku;quantity\n"
        csv_content += f"{sku};{int(cantidad)}\n"

        # multipart/form-data
        files = {
            "file": ("stock.csv", csv_content, "text/csv")
        }
        # Headers SIN Content-Type (requests lo arma para multipart)
        headers = {
            "Authorization": RIPLEY_API_KEY,
            "Accept": "application/json"
        }

        res = requests.post(
            f"{RIPLEY_BASE_URL}/api/offers/stocks/imports",
            headers=headers,
            files=files,
            timeout=20
        )
        if res.status_code in (200, 201, 202):
            try:
                data = res.json()
                import_id = data.get("import_id", "?")
                print(f"[Ripley] Stock {sku} = {cantidad} OK (import_id={import_id})")
            except:
                print(f"[Ripley] Stock {sku} = {cantidad} OK")
            return True
        print(f"[Ripley] Stock {sku} ERROR {res.status_code}: {res.text[:300]}")
        return False
    except Exception as e:
        print(f"[Ripley] actualizar_stock error: {e}")
        return False


def actualizar_stocks_ripley_lote(skus_cantidades):
    """Actualiza el stock de múltiples SKUs en una sola llamada (más eficiente).

    Args:
        skus_cantidades: dict {sku: cantidad} o lista de tuplas [(sku, cantidad), ...]
    """
    try:
        # Aceptar dict o lista
        if isinstance(skus_cantidades, dict):
            items = list(skus_cantidades.items())
        else:
            items = list(skus_cantidades)
        if not items:
            return False, "Lista vacía"

        csv_content = "sku;quantity\n"
        for sku, cantidad in items:
            csv_content += f"{sku};{int(cantidad)}\n"

        files = {
            "file": ("stocks_lote.csv", csv_content, "text/csv")
        }
        headers = {
            "Authorization": RIPLEY_API_KEY,
            "Accept": "application/json"
        }

        res = requests.post(
            f"{RIPLEY_BASE_URL}/api/offers/stocks/imports",
            headers=headers,
            files=files,
            timeout=30
        )
        if res.status_code in (200, 201, 202):
            try:
                data = res.json()
                import_id = data.get("import_id", "?")
                print(f"[Ripley] Lote {len(items)} stocks OK (import_id={import_id})")
                return True, import_id
            except:
                return True, None
        print(f"[Ripley] Lote ERROR {res.status_code}: {res.text[:300]}")
        return False, res.text[:300]
    except Exception as e:
        print(f"[Ripley] lote error: {e}")
        return False, str(e)


def consultar_estado_import_stock(import_id):
    """Consulta el estado de un import de stock (STO02).
    Estados: WAITING, RUNNING, COMPLETE, FAILED, INTERRUPTED."""
    try:
        res = requests.get(
            f"{RIPLEY_BASE_URL}/api/offers/stocks/imports/{import_id}",
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
    """Actualiza el precio (y opcionalmente precio de oferta) usando PRI01 de Mirakl.

    POST /api/offers/pricing/imports es el endpoint específico para SOLO modificar
    precios sin tocar otros campos. Es asíncrono.

    Args:
        sku: shop_sku del seller
        precio_normal: precio principal (CLP, sin decimales)
        precio_oferta: precio rebajado opcional. Si se pasa, se publica como discount-price
                       con vigencia de 30 días.
    """
    try:
        # CSV con formato Mirakl PRI01
        # Columnas: offer-sku;price;discount-price;discount-start-date;discount-end-date
        if precio_oferta and float(precio_oferta) > 0 and float(precio_oferta) < float(precio_normal):
            hoy = datetime.utcnow().strftime("%Y-%m-%d")
            fin = (datetime.utcnow() + timedelta(days=30)).strftime("%Y-%m-%d")
            csv_content = "offer-sku;price;discount-price;discount-start-date;discount-end-date\n"
            csv_content += f"{sku};{int(precio_normal)};{int(precio_oferta)};{hoy};{fin}\n"
        else:
            csv_content = "offer-sku;price\n"
            csv_content += f"{sku};{int(precio_normal)}\n"

        files = {
            "file": ("price.csv", csv_content, "text/csv")
        }
        headers = {
            "Authorization": RIPLEY_API_KEY,
            "Accept": "application/json"
        }

        res = requests.post(
            f"{RIPLEY_BASE_URL}/api/offers/pricing/imports",
            headers=headers,
            files=files,
            timeout=20
        )
        if res.status_code in (200, 201, 202):
            try:
                data = res.json()
                import_id = data.get("import_id", "?")
                print(f"[Ripley] Precio {sku}={precio_normal} OK (import_id={import_id})")
            except:
                print(f"[Ripley] Precio {sku}={precio_normal} OK")
            return True
        print(f"[Ripley] Precio {sku} ERROR {res.status_code}: {res.text[:300]}")
        return False
    except Exception as e:
        print(f"[Ripley] actualizar_precio error: {e}")
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

                        # Buscar SKU Lusync en mapeo
                        sku_lusync = sku_ripley
                        try:
                            for fila in listar_sku_mapeo():
                                if fila.get("sku_ripley") == sku_ripley:
                                    sku_lusync = fila.get("sku_lusync")
                                    break
                        except: pass

                        if sku_lusync not in productos_dict:
                            log.append(f"{order_id}: SKU '{sku_lusync}' no encontrado")
                            continue

                        resultado = descontar_venta(
                            sku=sku_lusync,
                            cantidad=cantidad,
                            canal="Ripley",
                            fulfillment=es_fbr,
                            orden_id=order_id,
                            motivo=f"Venta Ripley {tipo_str}"
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
        ok = actualizar_stock_ripley(sku_ripley, stock)
        return jsonify({"ok": ok, "stock_enviado": stock, "sku_ripley": sku_ripley})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
