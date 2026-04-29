from flask import Flask, request, render_template, session, redirect, jsonify, send_file
import requests
import os
from config import *
from apscheduler.schedulers.background import BackgroundScheduler
import atexit
from walmart import (actualizar_stock_walmart, actualizar_precio_walmart,
                     obtener_ordenes_walmart, confirmar_orden_walmart,
                     verificar_conexion_walmart)
from paris import (verificar_conexion_paris, obtener_ordenes_paris_todas,
                   actualizar_stock_paris, obtener_stock_paris,
                   actualizar_precio_paris, obtener_orden_paris,
                   get_seller_id as get_paris_seller_id)
from woo import actualizar_stock_woo
from inventario import (cargar_productos, guardar_productos, guardar_producto,
                        registrar_movimiento, cargar_movimientos, cargar_movimientos_hoy,
                        init_db, orden_ya_procesada, marcar_orden_procesada, actualizar_precios,
                        get_configuracion, set_configuracion, set_lead_time, eliminar_producto,
                        orden_ya_procesada_texto, marcar_orden_procesada_texto,
                        init_devoluciones, generar_codigo_dev, crear_devolucion,
                        asignar_codigo_dev, actualizar_devolucion, listar_devoluciones,
                        get_devolucion,
                        init_audit, registrar_audit, listar_audit,
                        init_sku_mapeo, listar_sku_mapeo, guardar_sku_mapeo_fila,
                        get_sku_canal, get_plataforma_web, set_plataforma_web,
                        registrar_importacion_mapeo, listar_historial_mapeo)

app = Flask(__name__)
app.secret_key = "clave_super_segura"

init_db()
init_devoluciones()
init_audit()
init_sku_mapeo()

# ── SYNC AUTOMÁTICO WALMART CADA 5 MINUTOS ──
def _sync_walmart_automatico():
    """Tarea de background: sincroniza órdenes Walmart sin requerir sesión"""
    try:
        print("[Scheduler] Iniciando sync automático Walmart...")
        productos = cargar_productos()
        nuevas = 0
        errores = []

        for estado in ["Created", "Acknowledged", "Shipped", "Delivered"]:
            ordenes = obtener_ordenes_walmart(estado)
            for o in ordenes:
                order_id = o.get("purchaseOrderId")
                if not order_id:
                    continue
                customer_order_id = str(o.get("customerOrderId", order_id))
                if orden_ya_procesada_texto(customer_order_id):
                    continue

                lineas = o.get("orderLines", {}).get("orderLine", [])
                if isinstance(lineas, dict):
                    lineas = [lineas]

                for linea in lineas:
                    try:
                        sku = linea.get("item", {}).get("sku")
                        if not sku:
                            continue
                        cantidad = 1
                        qty = linea.get("orderLineQuantity", {})
                        if qty and qty.get("amount"):
                            cantidad = int(float(qty.get("amount", 1)))
                        if cantidad == 1:
                            status_qty = linea.get("statusQuantity", {})
                            if status_qty and status_qty.get("amount"):
                                cantidad = int(float(status_qty.get("amount", 1)))

                        for p in productos:
                            if p["sku"] == sku:
                                p["stock"] = max(0, p["stock"] - cantidad)
                                guardar_producto(p)
                                registrar_movimiento("salida", p["sku"], p["nombre"],
                                                    cantidad, "Venta Walmart",
                                                    usuario="Sistema", canal="Walmart",
                                                    orden_id=customer_order_id)
                                actualizar_stock_woo(p["sku"], p["stock"])
                                actualizar_stock_walmart(p["sku"], p["stock"])
                                actualizar_stock_paris(p["sku"], p["stock"])
                                print(f"[Scheduler] SKU:{sku} Cant:{cantidad} Stock:{p['stock']}")
                    except Exception as e:
                        errores.append(str(e))
                        print(f"[Scheduler] Error linea: {e}")

                marcar_orden_procesada_texto(customer_order_id)
                nuevas += 1

        # ── CANCELACIONES WALMART — devolver stock si se canceló una orden ya procesada
        try:
            canceladas = obtener_ordenes_walmart("Cancelled")
            reingresadas = 0
            for o in canceladas:
                order_id = o.get("purchaseOrderId")
                if not order_id:
                    continue
                customer_order_id = str(o.get("customerOrderId", order_id))
                cancel_key = f"CANCEL-{customer_order_id}"

                # Solo procesar si la orden fue previamente descontada Y no se reingresó antes
                if not orden_ya_procesada_texto(customer_order_id):
                    continue  # nunca se procesó, no hay stock que devolver
                if orden_ya_procesada_texto(cancel_key):
                    continue  # ya se procesó la cancelación

                lineas = o.get("orderLines", {}).get("orderLine", [])
                if isinstance(lineas, dict):
                    lineas = [lineas]

                productos = cargar_productos()
                for linea in lineas:
                    try:
                        sku = linea.get("item", {}).get("sku")
                        if not sku:
                            continue
                        cantidad = 1
                        qty = linea.get("orderLineQuantity", {})
                        if qty and qty.get("amount"):
                            cantidad = int(float(qty.get("amount", 1)))
                        if cantidad == 1:
                            status_qty = linea.get("statusQuantity", {})
                            if status_qty and status_qty.get("amount"):
                                cantidad = int(float(status_qty.get("amount", 1)))

                        for p in productos:
                            if p["sku"] == sku:
                                p["stock"] = p["stock"] + cantidad
                                guardar_producto(p)
                                registrar_movimiento("entrada", p["sku"], p["nombre"],
                                                    cantidad, "Cancelación Walmart",
                                                    usuario="Sistema", canal="Walmart",
                                                    orden_id=customer_order_id)
                                actualizar_stock_woo(p["sku"], p["stock"])
                                actualizar_stock_walmart(p["sku"], p["stock"])
                                actualizar_stock_paris(p["sku"], p["stock"])
                                print(f"[Scheduler] CANCELACIÓN SKU:{sku} +{cantidad} Stock:{p['stock']}")
                    except Exception as e:
                        print(f"[Scheduler] Error cancelación linea: {e}")

                marcar_orden_procesada_texto(cancel_key)
                reingresadas += 1

            if reingresadas:
                print(f"[Scheduler] Cancelaciones procesadas: {reingresadas}")
        except Exception as e:
            print(f"[Scheduler] Error procesando cancelaciones: {e}")

        # ── SYNC PARIS (si está configurado) ──
        try:
            import os as _os
            if _os.environ.get("PARIS_API_KEY"):
                from paris import obtener_ordenes_paris_todas
                ordenes_paris = obtener_ordenes_paris_todas(dias=7, estado="awaiting_fullfillment")
                for so in ordenes_paris:
                    sub_order_num = str(so.get("subOrderNumber", ""))
                    paris_key = f"PARIS-{sub_order_num}"
                    if orden_ya_procesada_texto(paris_key):
                        continue
                    shipments = so.get("shipments", [])
                    for ship in shipments:
                        items = ship.get("items", [])
                        for item in items:
                            sku_seller = item.get("seller_sku") or item.get("sellerSku") or ""
                            cantidad = 1
                            if not sku_seller:
                                continue
                            for p in productos:
                                if p["sku"] == sku_seller:
                                    p["stock"] = max(0, p["stock"] - cantidad)
                                    guardar_producto(p)
                                    registrar_movimiento("salida", p["sku"], p["nombre"],
                                                        cantidad, "Venta Paris",
                                                        usuario="Sistema", canal="Paris",
                                                        orden_id=sub_order_num)
                                    actualizar_stock_woo(p["sku"], p["stock"])
                                    actualizar_stock_walmart(p["sku"], p["stock"])
                                    actualizar_stock_paris(p["sku"], p["stock"])
                                    print(f"[Scheduler] Paris SKU:{sku_seller} -{cantidad} Stock:{p['stock']}")
                    marcar_orden_procesada_texto(paris_key)
                    nuevas += 1
                print(f"[Scheduler] Paris sync OK")
        except Exception as e:
            print(f"[Scheduler] Paris error: {e}")

        print(f"[Scheduler] Sync completado — nuevas:{nuevas} errores:{len(errores)}")
    except Exception as e:
        print(f"[Scheduler] Error general: {e}")

scheduler = BackgroundScheduler(daemon=True)
scheduler.add_job(_sync_walmart_automatico, "interval", minutes=5, id="walmart_sync")
scheduler.start()
atexit.register(lambda: scheduler.shutdown(wait=False))

# ── SYNC DE RECUPERACIÓN AL ARRANCAR ──
# Busca órdenes perdidas durante caídas del servidor
def _sync_recuperacion():
    try:
        print("[Recuperación] Buscando órdenes no procesadas...")
        productos = cargar_productos()
        recuperadas = 0
        for estado in ["Created", "Acknowledged", "Shipped", "Delivered"]:
            ordenes = obtener_ordenes_walmart(estado)
            for o in ordenes:
                order_id = o.get("purchaseOrderId")
                if not order_id:
                    continue
                customer_order_id = str(o.get("customerOrderId", order_id))
                if orden_ya_procesada_texto(customer_order_id):
                    continue

                lineas = o.get("orderLines", {}).get("orderLine", [])
                if isinstance(lineas, dict):
                    lineas = [lineas]

                for linea in lineas:
                    try:
                        sku = linea.get("item", {}).get("sku")
                        if not sku:
                            continue
                        cantidad = 1
                        qty = linea.get("orderLineQuantity", {})
                        if qty and qty.get("amount"):
                            cantidad = int(float(qty.get("amount", 1)))
                        if cantidad == 1:
                            status_qty = linea.get("statusQuantity", {})
                            if status_qty and status_qty.get("amount"):
                                cantidad = int(float(status_qty.get("amount", 1)))

                        for p in productos:
                            if p["sku"] == sku:
                                p["stock"] = max(0, p["stock"] - cantidad)
                                guardar_producto(p)
                                registrar_movimiento("salida", p["sku"], p["nombre"],
                                                    cantidad, "Venta Walmart (recuperada)",
                                                    usuario="Sistema", canal="Walmart",
                                                    orden_id=customer_order_id)
                                actualizar_stock_woo(p["sku"], p["stock"])
                                actualizar_stock_walmart(p["sku"], p["stock"])
                                actualizar_stock_paris(p["sku"], p["stock"])
                                print(f"[Recuperación] SKU:{sku} Cant:{cantidad} OC:{customer_order_id}")
                    except Exception as e:
                        print(f"[Recuperación] Error linea: {e}")

                marcar_orden_procesada_texto(customer_order_id)
                recuperadas += 1

        # También recuperar cancelaciones
        try:
            canceladas = obtener_ordenes_walmart("Cancelled")
            for o in canceladas:
                order_id = o.get("purchaseOrderId")
                if not order_id:
                    continue
                customer_order_id = str(o.get("customerOrderId", order_id))
                cancel_key = f"CANCEL-{customer_order_id}"
                if not orden_ya_procesada_texto(customer_order_id):
                    continue
                if orden_ya_procesada_texto(cancel_key):
                    continue
                lineas = o.get("orderLines", {}).get("orderLine", [])
                if isinstance(lineas, dict):
                    lineas = [lineas]
                for linea in lineas:
                    sku = linea.get("item", {}).get("sku")
                    if not sku:
                        continue
                    cantidad = 1
                    qty = linea.get("orderLineQuantity", {})
                    if qty and qty.get("amount"):
                        cantidad = int(float(qty.get("amount", 1)))
                    for p in productos:
                        if p["sku"] == sku:
                            p["stock"] += cantidad
                            guardar_producto(p)
                            registrar_movimiento("entrada", p["sku"], p["nombre"],
                                                cantidad, "Cancelación Walmart (recuperada)",
                                                usuario="Sistema", canal="Walmart",
                                                orden_id=customer_order_id)
                            actualizar_stock_woo(p["sku"], p["stock"])
                            actualizar_stock_walmart(p["sku"], p["stock"])
                            actualizar_stock_paris(p["sku"], p["stock"])
                marcar_orden_procesada_texto(cancel_key)
        except Exception as e:
            print(f"[Recuperación] Error cancelaciones: {e}")

        print(f"[Recuperación] Completado — {recuperadas} órdenes recuperadas")
    except Exception as e:
        print(f"[Recuperación] Error general: {e}")

# Ejecutar recuperación 10 segundos después del arranque
scheduler.add_job(_sync_recuperacion, "date", 
                  run_date=__import__("datetime").datetime.now() + __import__("datetime").timedelta(seconds=10),
                  id="recovery_sync")

@app.route("/agregar", methods=["POST"])
def agregar():
    data = request.json
    p = {
        "sku": data["sku"],
        "nombre": data["nombre"],
        "stock": int(data["stock"]),
        "precio_normal": float(data.get("precio_normal", 0)),
        "precio_oferta": float(data.get("precio_oferta", 0))
    }
    guardar_producto(p)
    return {"ok": True}

@app.route("/importar_woo")
def importar():
    registrar_audit(session.get("usuario","Sistema"), request.remote_addr, "importar_woo", entidad="productos", detalle="Importación desde WooCommerce")
    nuevos = 0
    productos = cargar_productos()
    skus_existentes = {p["sku"] for p in productos}

    res = requests.get(
        "https://www.babymine.cl/wp-json/wc/v3/products",
        params={"consumer_key": WC_KEY, "consumer_secret": WC_SECRET, "per_page": 100}
    )
    if res.status_code != 200:
        return {"error": "Woo error"}

    for p in res.json():
        if p["type"] == "simple":
            sku = p.get("sku") or str(p.get("id"))
            if sku not in skus_existentes:
                pn = p.get("regular_price") or "0"
                po = p.get("sale_price") or "0"
                guardar_producto({
                    "sku": sku,
                    "nombre": p["name"],
                    "stock": p.get("stock_quantity") or 0,
                    "precio_normal": float(pn) if pn else 0,
                    "precio_oferta": float(po) if po else 0
                })
                nuevos += 1

        if p["type"] == "variable":
            res_var = requests.get(
                f"https://www.babymine.cl/wp-json/wc/v3/products/{p['id']}/variations",
                params={"consumer_key": WC_KEY, "consumer_secret": WC_SECRET, "per_page": 100}
            )
            if res_var.status_code != 200:
                continue
            for v in res_var.json():
                sku = v.get("sku") or str(v.get("id"))
                if sku not in skus_existentes:
                    vn = v.get("regular_price") or "0"
                    vo = v.get("sale_price") or "0"
                    guardar_producto({
                        "sku": sku,
                        "nombre": f"{p['name']} - {sku}",
                        "stock": v.get("stock_quantity") or 0,
                        "precio_normal": float(vn) if vn else 0,
                        "precio_oferta": float(vo) if vo else 0
                    })
                    nuevos += 1

    return {"mensaje": f"{nuevos} productos importados"}

@app.route("/sincronizar_precios_woo")
def sincronizar_precios_woo():
    registrar_audit(session.get("usuario","Sistema"), request.remote_addr, "sincronizar_precios", entidad="productos", detalle="Sincronización de precios WooCommerce")
    actualizados = 0
    res = requests.get(
        "https://www.babymine.cl/wp-json/wc/v3/products",
        params={"consumer_key": WC_KEY, "consumer_secret": WC_SECRET, "per_page": 100}
    )
    if res.status_code != 200:
        return {"error": "Woo error"}

    for p in res.json():
        if p["type"] == "simple":
            sku = p.get("sku") or str(p.get("id"))
            pn = p.get("regular_price") or "0"
            po = p.get("sale_price") or "0"
            actualizar_precios(sku,
                float(pn) if pn else 0,
                float(po) if po else 0)
            actualizados += 1

        if p["type"] == "variable":
            res_var = requests.get(
                f"https://www.babymine.cl/wp-json/wc/v3/products/{p['id']}/variations",
                params={"consumer_key": WC_KEY, "consumer_secret": WC_SECRET, "per_page": 100}
            )
            if res_var.status_code != 200:
                continue
            for v in res_var.json():
                sku = v.get("sku") or str(v.get("id"))
                vn = v.get("regular_price") or "0"
                vo = v.get("sale_price") or "0"
                actualizar_precios(sku,
                    float(vn) if vn else 0,
                    float(vo) if vo else 0)
                actualizados += 1

    return {"mensaje": f"{actualizados} precios sincronizados"}

@app.route("/actualizar_precios", methods=["POST"])
def actualizar_precios_route():
    registrar_audit(session.get("usuario","Sistema"), request.remote_addr, "actualizar_precios", entidad="productos", detalle="Actualización manual de precios")
    data = request.json
    sku = data.get("sku")
    precio_normal = float(data.get("precio_normal", 0))
    precio_oferta = float(data.get("precio_oferta", 0))

    # Guardar en BD
    actualizar_precios(sku, precio_normal, precio_oferta)

    # Buscar el producto en WooCommerce por SKU
    try:
        res = requests.get(
            "https://www.babymine.cl/wp-json/wc/v3/products",
            params={"consumer_key": WC_KEY, "consumer_secret": WC_SECRET, "sku": sku}
        )
        if res.status_code == 200 and res.json():
            producto = res.json()[0]
            payload = {
                "regular_price": str(precio_normal),
                "sale_price": str(precio_oferta) if precio_oferta > 0 else ""
            }
            if producto["type"] == "simple":
                requests.put(
                    f"https://www.babymine.cl/wp-json/wc/v3/products/{producto['id']}",
                    params={"consumer_key": WC_KEY, "consumer_secret": WC_SECRET},
                    json=payload
                )
            elif producto["type"] == "variation":
                requests.put(
                    f"https://www.babymine.cl/wp-json/wc/v3/products/{producto['parent_id']}/variations/{producto['id']}",
                    params={"consumer_key": WC_KEY, "consumer_secret": WC_SECRET},
                    json=payload
                )
    except:
        pass

    return {"ok": True}

@app.route("/entrada", methods=["POST"])
def entrada():
    registrar_audit(session.get("usuario","Sistema"), request.remote_addr, "entrada_manual", entidad="productos", detalle="Entrada manual de stock")
    data = request.json
    productos = cargar_productos()
    for p in productos:
        if p["sku"] == data["sku"]:
            p["stock"] += int(data["cantidad"])
            guardar_producto(p)
            registrar_movimiento("entrada", p["sku"], p["nombre"], int(data["cantidad"]), data.get("motivo"), usuario="Luis Padilla", canal="Manual")
            actualizar_stock_woo(p["sku"], p["stock"])
            actualizar_stock_walmart(p["sku"], p["stock"])
            actualizar_stock_paris(p["sku"], p["stock"])
            return {"ok": True}
    return {"error": "no encontrado"}

@app.route("/salida", methods=["POST"])
def salida():
    registrar_audit(session.get("usuario","Sistema"), request.remote_addr, "salida_manual", entidad="productos", detalle="Salida manual de stock")
    data = request.json
    productos = cargar_productos()
    for p in productos:
        if p["sku"] == data["sku"]:
            if p["stock"] < int(data["cantidad"]):
                return {"error": "Stock insuficiente"}
            p["stock"] -= int(data["cantidad"])
            guardar_producto(p)
            registrar_movimiento("salida", p["sku"], p["nombre"], int(data["cantidad"]), data.get("motivo"), usuario="Luis Padilla", canal="Manual")
            actualizar_stock_woo(p["sku"], p["stock"])
            actualizar_stock_walmart(p["sku"], p["stock"])
            actualizar_stock_paris(p["sku"], p["stock"])
            return {"ok": True}
    return {"error": "no encontrado"}

@app.route("/sync_ordenes")
def sync_ordenes():
    try:
        res = requests.get(
            "https://www.babymine.cl/wp-json/wc/v3/orders",
            params={"consumer_key": WC_KEY, "consumer_secret": WC_SECRET, "status": "processing"},
            timeout=15
        )
    except requests.exceptions.Timeout:
        print("[WooCommerce] Timeout en sync_ordenes")
        return {"ok": True, "nuevas_ordenes": 0, "warn": "timeout"}
    except Exception as e:
        print(f"[WooCommerce] Error en sync_ordenes: {e}")
        return {"ok": True, "nuevas_ordenes": 0, "warn": str(e)}
    if res.status_code != 200:
        return {"error": "Woo error", "status": res.status_code}

    productos = cargar_productos()
    nuevas = 0

    for o in res.json():
        if orden_ya_procesada(o["id"]):
            continue

        # WooCommerce ya guarda en hora Chile — usar directamente sin convertir
        from datetime import datetime
        try:
            fecha_real = datetime.strptime(o.get("date_created",""), "%Y-%m-%dT%H:%M:%S")
        except:
            fecha_real = None

        for item in o["line_items"]:
            sku = item.get("sku")
            cantidad = item.get("quantity")
            for p in productos:
                if p["sku"] == sku:
                    p["stock"] -= cantidad
                    guardar_producto(p)
                    registrar_movimiento("salida", p["sku"], p["nombre"], cantidad, "Venta Web",
                                        usuario="Sistema", canal="WooCommerce",
                                        orden_id=str(o["id"]), fecha_override=fecha_real)
                    actualizar_stock_woo(p["sku"], p["stock"])
                    actualizar_stock_walmart(p["sku"], p["stock"])
                    actualizar_stock_paris(p["sku"], p["stock"])
        marcar_orden_procesada(o["id"])
        nuevas += 1

    return {"ok": True, "nuevas_ordenes": nuevas}

@app.route("/movimientos_hoy")
def movimientos_hoy():
    return {"ventas": cargar_movimientos_hoy()}

@app.route("/productos")
def ver_productos():
    if not session.get("logged"):
        return {"productos": [], "error": "no autorizado"}, 401
    try:
        return {"productos": cargar_productos()}
    except Exception as e:
        print(f"[/productos] Error: {e}")
        return {"productos": [], "error": str(e)}, 500

@app.route("/movimientos")
def ver_movimientos():
    if not session.get("logged"):
        return {"movimientos": [], "error": "no autorizado"}, 401
    try:
        limite = int(request.args.get("limite", 20))
        return {"movimientos": cargar_movimientos(limite)}
    except Exception as e:
        print(f"[/movimientos] Error: {e}")
        return {"movimientos": [], "error": str(e)}, 500

# ── WALMART ──

@app.route("/walmart/test")
def walmart_test():
    if not session.get("logged"):
        return {"error": "no autorizado"}, 401
    try:
        from walmart import get_token, WALMART_CLIENT_ID
        token = get_token()
        return {"conectado": True, "client_id": WALMART_CLIENT_ID[:8]+"..."}
    except Exception as e:
        return {"conectado": False, "error": str(e)}

@app.route("/walmart/diagnostico")
def walmart_diagnostico():
    """Diagnóstico completo de Walmart — items, SKUs y test de inventory"""
    if not session.get("logged"):
        return {"error": "no autorizado"}, 401
    import requests as req
    from walmart import walmart_headers, WALMART_BASE_URL

    resultado = {}

    # 1. Traer items publicados en Walmart
    try:
        res = req.get(
            f"{WALMART_BASE_URL}/v3/items",
            headers=walmart_headers(),
            params={"limit": 5}
        )
        resultado["items_status"] = res.status_code
        resultado["items_respuesta"] = res.text[:800]
    except Exception as e:
        resultado["items_error"] = str(e)

    # 2. Buscar el producto por SKU específico
    try:
        res2 = req.get(
            f"{WALMART_BASE_URL}/v3/items/CBSNCPB001",
            headers=walmart_headers(),
            params={"productIdType": "SKU"}
        )
        resultado["busqueda_sku_status"] = res2.status_code
        resultado["busqueda_sku_respuesta"] = res2.text[:500]
    except Exception as e:
        resultado["busqueda_sku_error"] = str(e)

    # 3. Probar inventory con cantidad fija
    try:
        headers = walmart_headers()
        headers["Content-Type"] = "application/json"
        payload = {"quantity": {"unit": "EACH", "amount": 10}}
        res3 = req.put(
            f"{WALMART_BASE_URL}/v3/inventory",
            headers=headers,
            json=payload,
            params={"sku": "CBSNCPB001"}
        )
        resultado["inventory_sin_param_status"] = res3.status_code
        resultado["inventory_sin_param_respuesta"] = res3.text[:500]
    except Exception as e:
        resultado["inventory_error"] = str(e)

    return resultado

@app.route("/walmart/test_stock_one")
def walmart_test_stock_one():
    """Prueba actualizar stock de UN solo producto para debug"""
    if not session.get("logged"):
        return {"error": "no autorizado"}, 401
    productos = cargar_productos()
    if not productos:
        return {"error": "sin productos"}
    p = productos[0]
    try:
        import requests as req
        from walmart import get_token, WALMART_BASE_URL, walmart_headers
        headers = walmart_headers()
        headers["Content-Type"] = "application/json"
        payload = {
            "sku": p["sku"],
            "quantity": {"unit": "EACH", "amount": int(p["stock"])}
        }
        res = req.put(
            f"{WALMART_BASE_URL}/v3/inventory",
            headers=headers,
            json=payload
        )
        return {
            "sku": p["sku"],
            "stock": p["stock"],
            "status": res.status_code,
            "respuesta": res.text[:500]
        }
    except Exception as e:
        return {"error": str(e)}

@app.route("/walmart/sync_stock", methods=["POST"])
def walmart_sync_stock():
    if not session.get("logged"):
        return {"error": "no autorizado"}, 401
    productos = cargar_productos()
    ok = 0
    error = 0
    errores_detalle = []
    print(f"[Walmart] Iniciando sync stock — {len(productos)} productos")
    for p in productos:
        if p.get("sku"):
            resultado = actualizar_stock_walmart(p["sku"], p["stock"])
            actualizar_stock_paris(p["sku"], p["stock"])
            if resultado:
                ok += 1
            else:
                error += 1
                errores_detalle.append(p["sku"])
    print(f"[Walmart] Sync completado — OK:{ok} Error:{error}")
    return {"ok": ok, "error": error, "total": len(productos), "errores": errores_detalle[:5]}

@app.route("/walmart/sync_precios", methods=["POST"])
def walmart_sync_precios():
    if not session.get("logged"):
        return {"error": "no autorizado"}, 401
    from inventario import get_configuracion
    cfg = get_configuracion()
    comision = float(cfg.get("walmart_comision", 12)) / 100

    productos = cargar_productos()
    ok = 0
    for p in productos:
        if p.get("sku") and p.get("precio_normal", 0) > 0:
            precio_base = p["precio_oferta"] if p.get("precio_oferta", 0) > 0 else p["precio_normal"]
            precio_walmart = precio_base * (1 + comision)
            # Redondear a x90
            precio_walmart = int(precio_walmart / 100) * 100 + 90
            if precio_walmart < precio_base:
                precio_walmart += 100
            actualizar_precio_walmart(p["sku"], precio_walmart)
            ok += 1
    return {"ok": ok}

@app.route("/walmart/sync_ordenes")
def walmart_sync_ordenes():
    registrar_audit(session.get("usuario","Sistema"), request.remote_addr, "sync_walmart", entidad="ordenes", detalle="Sync manual órdenes Walmart")
    if not session.get("logged"):
        return {"error": "no autorizado"}, 401

    productos = cargar_productos()
    nuevas = 0
    errores = []

    # Walmart Chile usa Created y Acknowledged para ordenes pendientes
    for estado in ["Created", "Acknowledged", "Shipped", "Delivered"]:
        ordenes = obtener_ordenes_walmart(estado)
        print(f"[Walmart Ordenes] Estado:{estado} Total:{len(ordenes)}")

        for o in ordenes:
            order_id = o.get("purchaseOrderId")
            print(f"[Walmart] Procesando orden:{order_id}")
            if not order_id:
                print("[Walmart] Sin order_id, saltando")
                continue

            # Usar customerOrderId (número largo) para evitar duplicados
            customer_order_id = str(o.get("customerOrderId", order_id))
            if orden_ya_procesada_texto(customer_order_id):
                print(f"[Walmart] Orden {customer_order_id} ya procesada, saltando")
                continue

            lineas = o.get("orderLines", {}).get("orderLine", [])
            if isinstance(lineas, dict):
                lineas = [lineas]

            for linea in lineas:
                try:
                    sku = linea.get("item", {}).get("sku")
                    if not sku:
                        continue
                    # Walmart Chile: cantidad viene en orderLineQuantity o es 1
                    cantidad = 1
                    qty = linea.get("orderLineQuantity", {})
                    if qty and qty.get("amount"):
                        cantidad = int(float(qty.get("amount", 1)))
                    # También puede venir en statusQuantity
                    if cantidad == 1:
                        status_qty = linea.get("statusQuantity", {})
                        if status_qty and status_qty.get("amount"):
                            cantidad = int(float(status_qty.get("amount", 1)))

                    for p in productos:
                        if p["sku"] == sku:
                            p["stock"] = max(0, p["stock"] - cantidad)
                            guardar_producto(p)
                            registrar_movimiento("salida", p["sku"], p["nombre"],
                                                cantidad, "Venta Walmart",
                                                usuario="Sistema", canal="Walmart",
                                                orden_id=customer_order_id)
                            actualizar_stock_woo(p["sku"], p["stock"])
                            actualizar_stock_walmart(p["sku"], p["stock"])
                            actualizar_stock_paris(p["sku"], p["stock"])
                            print(f"[Walmart] Procesado SKU:{sku} Cant:{cantidad} Stock restante:{p['stock']}")
                except Exception as e:
                    errores.append(str(e))
                    print(f"[Walmart] Error linea: {e}")

            marcar_orden_procesada_texto(customer_order_id)
            nuevas += 1

    return {"ok": True, "nuevas_ordenes": nuevas, "errores": errores[:5]}

@app.route("/walmart/ver_ordenes")
def walmart_ver_ordenes():
    if not session.get("logged"):
        return {"error": "no autorizado"}, 401
    import requests as req
    from walmart import walmart_headers, WALMART_BASE_URL
    resultado = {}

    from datetime import datetime, timedelta
    fecha_inicio = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%dT00:00:00.000Z")

    # Probar sin filtro de estado pero con fecha
    try:
        h = walmart_headers()
        res = req.get(
            f"{WALMART_BASE_URL}/v3/orders",
            headers=h,
            params={"createdStartDate": fecha_inicio, "limit": 5}
        )
        resultado["sin_filtro_status"] = res.status_code
        resultado["sin_filtro_resp"] = res.text[:600]
    except Exception as e:
        resultado["sin_filtro_error"] = str(e)

    # Probar con cada estado
    for estado in ["Created", "Acknowledged", "Shipped", "Delivered"]:
        try:
            h2 = walmart_headers()
            res2 = req.get(
                f"{WALMART_BASE_URL}/v3/orders",
                headers=h2,
                params={"createdStartDate": fecha_inicio, "status": estado, "limit": 5}
            )
            resultado[estado+"_status"] = res2.status_code
            resultado[estado+"_resp"] = res2.text[:300]
        except Exception as e:
            resultado[estado+"_error"] = str(e)

    return resultado

@app.route("/fix_woo_limpiar_duplicados")
def fix_woo_limpiar_duplicados():
    """Limpia duplicados de WooCommerce y deja solo 1 movimiento por orden+SKU con fecha real"""
    if not session.get("logged"):
        return {"error": "no autorizado"}, 401
    from inventario import get_conn
    from datetime import datetime
    import pytz
    conn = get_conn()
    cur = conn.cursor()

    # 1. Borrar TODOS los movimientos de WooCommerce para empezar limpio
    cur.execute("DELETE FROM movimientos WHERE canal = 'WooCommerce'")
    borrados = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()

    # 2. Volver a registrar desde WooCommerce con fecha real de compra
    res = requests.get(
        "https://www.babymine.cl/wp-json/wc/v3/orders",
        params={"consumer_key": WC_KEY, "consumer_secret": WC_SECRET,
                "status": "processing", "per_page": 100}
    )
    if res.status_code != 200:
        return {"error": "Woo error", "borrados": borrados}

    productos = cargar_productos()
    registrados = 0
    chile_tz = pytz.timezone('America/Santiago')  # pytz maneja UTC-3/UTC-4 automáticamente

    for o in res.json():
        try:
            # WooCommerce ya guarda en hora Chile — sin conversión
            fecha_real = datetime.strptime(o.get("date_created",""), "%Y-%m-%dT%H:%M:%S")
        except:
            fecha_real = None

        for item in o.get("line_items", []):
            sku = item.get("sku")
            cantidad = item.get("quantity", 1)
            for p in productos:
                if p["sku"] == sku:
                    registrar_movimiento(
                        "salida", p["sku"], p["nombre"],
                        cantidad, "Venta Web",
                        usuario="Sistema", canal="WooCommerce",
                        orden_id=str(o["id"]),
                        fecha_override=fecha_real
                    )
                    registrados += 1

    return {"ok": True, "borrados": borrados, "registrados": registrados}

@app.route("/fix_woo_fechas")
def fix_woo_fechas():
    """Corrige la fecha de movimientos WooCommerce guardados con hora UTC incorrecta"""
    if not session.get("logged"):
        return {"error": "no autorizado"}, 401
    from inventario import get_conn
    conn = get_conn()
    cur = conn.cursor()
    # Restar 3 horas a movimientos de WooCommerce del 27/04 que son del 26/04 en Chile
    cur.execute("""
        UPDATE movimientos
        SET fecha = fecha - INTERVAL '3 hours'
        WHERE canal = 'WooCommerce'
        AND DATE(fecha) = '2026-04-27'
        AND EXTRACT(HOUR FROM fecha) < 7
    """)
    corregidos = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    return {"ok": True, "corregidos": corregidos}

@app.route("/fix_woo_movimientos")
def fix_woo_movimientos():
    """Registra movimientos faltantes de órdenes WooCommerce ya procesadas"""
    if not session.get("logged"):
        return {"error": "no autorizado"}, 401

    res = requests.get(
        "https://www.babymine.cl/wp-json/wc/v3/orders",
        params={"consumer_key": WC_KEY, "consumer_secret": WC_SECRET,
                "status": "processing", "per_page": 50}
    )
    if res.status_code != 200:
        return {"error": "Woo error"}

    productos = cargar_productos()
    registrados = 0

    for o in res.json():
        # Solo procesar las ya marcadas (que no tienen movimiento)
        if not orden_ya_procesada(o["id"]):
            continue

        # Verificar si ya tiene movimiento registrado
        from inventario import get_conn
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM movimientos WHERE orden_id = %s AND canal = 'WooCommerce'",
            (str(o["id"]),)
        )
        ya_tiene_movimiento = cur.fetchone() is not None
        cur.close()
        conn.close()

        if ya_tiene_movimiento:
            continue

        # Registrar el movimiento con la fecha REAL de la orden de WooCommerce
        from datetime import datetime
        import pytz
        chile_tz = pytz.timezone('America/Santiago')  # pytz maneja UTC-3/UTC-4 automáticamente
        fecha_orden_str = o.get("date_created", "")
        try:
            # WooCommerce devuelve fecha en UTC — convertir a Chile
            fecha_utc = datetime.strptime(fecha_orden_str, "%Y-%m-%dT%H:%M:%S")
            fecha_utc = pytz.utc.localize(fecha_utc)
            fecha_chile = fecha_utc.astimezone(chile_tz)
        except:
            fecha_chile = None

        for item in o.get("line_items", []):
            sku = item.get("sku")
            cantidad = item.get("quantity", 1)
            for p in productos:
                if p["sku"] == sku:
                    registrar_movimiento(
                        "salida", p["sku"], p["nombre"],
                        cantidad, "Venta Web",
                        usuario="Sistema", canal="WooCommerce",
                        orden_id=str(o["id"]),
                        fecha_override=fecha_chile
                    )
                    registrados += 1

    return {"ok": True, "movimientos_registrados": registrados}

@app.route("/debug_woo_ordenes")
def debug_woo_ordenes():
    """Ver órdenes de WooCommerce en estado processing"""
    res = requests.get(
        "https://www.babymine.cl/wp-json/wc/v3/orders",
        params={"consumer_key": WC_KEY, "consumer_secret": WC_SECRET, "status": "processing", "per_page": 10}
    )
    if res.status_code != 200:
        return {"error": res.status_code, "detalle": res.text[:200]}
    ordenes = res.json()
    resultado = []
    for o in ordenes:
        ya = orden_ya_procesada(o["id"])
        resultado.append({
            "id": o["id"],
            "fecha": o.get("date_created"),
            "ya_procesada": ya,
            "items": [{"sku": i.get("sku"), "cantidad": i.get("quantity")} for i in o.get("line_items", [])]
        })
    return {"total": len(ordenes), "ordenes": resultado}

@app.route("/hora_servidor")
def hora_servidor():
    from datetime import datetime
    import pytz
    utc_now = datetime.utcnow()
    chile_tz = pytz.timezone('America/Santiago')  # pytz maneja UTC-3/UTC-4 automáticamente
    chile_now = datetime.now(chile_tz)
    return {
        "utc": utc_now.strftime("%d/%m/%Y %H:%M:%S"),
        "chile_pytz": chile_now.strftime("%d/%m/%Y %H:%M:%S"),
        "chile_offset": str(chile_now.utcoffset()),
        "postgres_now": None
    }

@app.route("/fix_db")
def fix_db():
    """Crea columnas faltantes en la BD"""
    from inventario import get_conn
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("ALTER TABLE movimientos ADD COLUMN IF NOT EXISTS orden_id TEXT DEFAULT NULL")
        cur.execute("ALTER TABLE movimientos ADD COLUMN IF NOT EXISTS usuario TEXT DEFAULT 'Sistema'")
        cur.execute("ALTER TABLE movimientos ADD COLUMN IF NOT EXISTS canal TEXT DEFAULT 'Sistema'")
        cur.execute("ALTER TABLE ordenes_procesadas ADD COLUMN IF NOT EXISTS order_id_texto TEXT")
        conn.commit()
        cur.close()
        conn.close()
        return {"ok": True, "mensaje": "Columnas creadas correctamente"}
    except Exception as e:
        conn.rollback()
        cur.close()
        conn.close()
        return {"error": str(e)}

@app.route("/walmart/reset_y_limpiar")
def walmart_reset_y_limpiar():
    """Borra movimientos de Walmart y limpia órdenes procesadas para resincronizar limpio"""
    if not session.get("logged"):
        return {"error": "no autorizado"}, 401
    from inventario import get_conn
    conn = get_conn()
    cur = conn.cursor()

    # 1. Borrar movimientos de Walmart únicamente
    cur.execute("DELETE FROM movimientos WHERE canal = 'Walmart' AND motivo = 'Venta Walmart'")
    movimientos_borrados = cur.rowcount

    # 2. Limpiar SOLO órdenes de Walmart (las que tienen order_id_texto con formato P...)
    cur.execute("""
        DELETE FROM ordenes_procesadas
        WHERE order_id_texto IS NOT NULL
    """)
    ordenes_borradas = cur.rowcount

    # 3. Crear columna order_id_texto si no existe
    cur.execute("ALTER TABLE ordenes_procesadas ADD COLUMN IF NOT EXISTS order_id_texto TEXT")

    conn.commit()
    cur.close()
    conn.close()
    return {
        "ok": True,
        "movimientos_borrados": movimientos_borrados,
        "ordenes_borradas": ordenes_borradas,
        "mensaje": "Listo. Ahora sincroniza órdenes de Walmart desde el panel."
    }

@app.route("/walmart/ver_fechas")
def walmart_ver_fechas():
    """Ver fechas exactas de movimientos de Walmart"""
    if not session.get("logged"):
        return {"error": "no autorizado"}, 401
    from inventario import get_conn
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT
            TO_CHAR(fecha, 'DD/MM/YYYY HH24:MI') as utc,
            TO_CHAR(fecha AT TIME ZONE 'America/Santiago', 'DD/MM/YYYY HH24:MI') as santiago,
            TO_CHAR(fecha AT TIME ZONE 'America/Santiago', 'DD/MM/YYYY') as fecha_santiago,
            motivo, canal
        FROM movimientos
        WHERE canal = 'Walmart'
        ORDER BY fecha DESC
        LIMIT 5
    """)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return {"movimientos": [
        {"utc":r[0],"santiago":r[1],"fecha_santiago":r[2],"motivo":r[3],"canal":r[4]}
        for r in rows
    ]}

@app.route("/walmart/ver_movimientos_db")
def walmart_ver_movimientos_db():
    """Ver movimientos de hoy en la BD para diagnóstico"""
    if not session.get("logged"):
        return {"error": "no autorizado"}, 401
    from inventario import get_conn
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT tipo, sku, nombre, cantidad, motivo, canal, usuario,
               TO_CHAR(fecha AT TIME ZONE 'America/Santiago', 'HH24:MI') as hora
        FROM movimientos
        WHERE DATE(fecha AT TIME ZONE 'America/Santiago') = CURRENT_DATE
        ORDER BY fecha DESC
        LIMIT 20
    """)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return {"movimientos": [
        {"tipo":r[0],"sku":r[1],"nombre":r[2][:30],"cantidad":r[3],
         "motivo":r[4],"canal":r[5],"usuario":r[6],"hora":r[7]}
        for r in rows
    ]}

@app.route("/walmart/fix_canales")
def walmart_fix_canales():
    """Corrige hora UTC de movimientos de Walmart procesados antes del fix de timezone"""
    if not session.get("logged"):
        return {"error": "no autorizado"}, 401
    from inventario import get_conn
    conn = get_conn()
    cur = conn.cursor()
    # Restar 1 hora adicional a movimientos de Walmart del 27/04 (ya se restaron 4, falta 1 más → total 3h)
    cur.execute("""
        UPDATE movimientos
        SET fecha = fecha - INTERVAL '1 hour'
        WHERE canal = 'Walmart'
        AND motivo = 'Venta Walmart'
        AND DATE(fecha AT TIME ZONE 'UTC') = '2026-04-27'
        AND EXTRACT(HOUR FROM fecha AT TIME ZONE 'UTC') = 0
    """)
    actualizados = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    return {"ok": True, "movimientos_corregidos": actualizados}

@app.route("/walmart/sync_debug")
def walmart_sync_debug():
    """Ejecuta el sync completo y retorna resultado detallado"""
    if not session.get("logged"):
        return {"error": "no autorizado"}, 401

    productos = cargar_productos()
    log = []
    nuevas = 0

    for estado in ["Created", "Acknowledged", "Shipped", "Delivered"]:
        ordenes = obtener_ordenes_walmart(estado)
        log.append(f"Estado {estado}: {len(ordenes)} ordenes")

        for o in ordenes:
            order_id = o.get("purchaseOrderId")
            if not order_id:
                log.append("Sin order_id, saltando")
                continue

            # Bug ③ fix: usar customerOrderId consistente con el resto del sistema
            customer_order_id = str(o.get("customerOrderId", order_id))
            ya = orden_ya_procesada_texto(customer_order_id)
            log.append(f"Orden {order_id} customerOrderId:{customer_order_id} ya_procesada:{ya}")

            if ya:
                continue

            lineas = o.get("orderLines", {}).get("orderLine", [])
            if isinstance(lineas, dict):
                lineas = [lineas]

            log.append(f"  Lineas: {len(lineas)}")

            for linea in lineas:
                sku = linea.get("item", {}).get("sku")
                cantidad = 1
                qty = linea.get("orderLineQuantity", {})
                if qty and qty.get("amount"):
                    cantidad = int(float(qty["amount"]))
                if cantidad == 1:
                    status_qty = linea.get("statusQuantity", {})
                    if status_qty and status_qty.get("amount"):
                        cantidad = int(float(status_qty.get("amount", 1)))

                log.append(f"  SKU:{sku} Cantidad:{cantidad}")

                encontrado = False
                for p in productos:
                    if p["sku"] == sku:
                        encontrado = True
                        stock_antes = p["stock"]
                        p["stock"] = max(0, p["stock"] - cantidad)
                        guardar_producto(p)
                        registrar_movimiento("salida", p["sku"], p["nombre"],
                                            cantidad, "Venta Walmart",
                                            usuario="Sistema", canal="Walmart",
                                            orden_id=customer_order_id)
                        actualizar_stock_woo(p["sku"], p["stock"])
                        actualizar_stock_walmart(p["sku"], p["stock"])
                        actualizar_stock_paris(p["sku"], p["stock"])
                        log.append(f"  OK {p['nombre']} stock:{stock_antes}->{p['stock']}")

                if not encontrado:
                    log.append(f"  SKU {sku} no encontrado en Lusync")

            marcar_orden_procesada_texto(customer_order_id)
            nuevas += 1

    # ── CANCELACIONES en sync manual
    try:
        canceladas = obtener_ordenes_walmart("Cancelled")
        for o in canceladas:
            order_id = o.get("purchaseOrderId")
            if not order_id:
                continue
            customer_order_id = str(o.get("customerOrderId", order_id))
            cancel_key = f"CANCEL-{customer_order_id}"
            if not orden_ya_procesada_texto(customer_order_id):
                continue
            if orden_ya_procesada_texto(cancel_key):
                continue
            lineas = o.get("orderLines", {}).get("orderLine", [])
            if isinstance(lineas, dict):
                lineas = [lineas]
            for linea in lineas:
                sku = linea.get("item", {}).get("sku")
                if not sku:
                    continue
                cantidad = 1
                qty = linea.get("orderLineQuantity", {})
                if qty and qty.get("amount"):
                    cantidad = int(float(qty.get("amount", 1)))
                for p in productos:
                    if p["sku"] == sku:
                        p["stock"] += cantidad
                        guardar_producto(p)
                        registrar_movimiento("entrada", p["sku"], p["nombre"],
                                            cantidad, "Cancelación Walmart",
                                            usuario="Sistema", canal="Walmart",
                                            orden_id=customer_order_id)
                        actualizar_stock_woo(p["sku"], p["stock"])
                        actualizar_stock_walmart(p["sku"], p["stock"])
                        actualizar_stock_paris(p["sku"], p["stock"])
                        log.append(f"CANCELACION SKU:{sku} +{cantidad} Stock:{p['stock']}")
            marcar_orden_procesada_texto(cancel_key)
    except Exception as e:
        log.append(f"Error cancelaciones: {e}")

    return {"nuevas_ordenes": nuevas, "log": log}

@app.route("/walmart/debug_ordenes")
def walmart_debug_ordenes():
    if not session.get("logged"):
        return {"error": "no autorizado"}, 401
    from datetime import datetime, timedelta
    import requests as req
    from walmart import walmart_headers, WALMART_BASE_URL

    fecha_inicio = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%dT00:00:00.000Z")
    h = walmart_headers()
    res = req.get(
        f"{WALMART_BASE_URL}/v3/orders",
        headers=h,
        params={"createdStartDate": fecha_inicio, "status": "Acknowledged", "limit": 2}
    )
    if res.status_code != 200:
        return {"error": res.text}

    data = res.json()
    ordenes = data.get("list", {}).get("elements", {}).get("order", [])
    if isinstance(ordenes, dict):
        ordenes = [ordenes]

    # Mostrar estructura completa de la primera orden
    if ordenes:
        o = ordenes[0]
        return {
            "purchaseOrderId": o.get("purchaseOrderId"),
            "orderLines_raw": str(o.get("orderLines", {}))[:1000],
            "keys_orden": list(o.keys()),
            "orden_completa": str(o)[:1500]
        }
    return {"mensaje": "sin ordenes"}

@app.route("/eliminar_producto", methods=["POST"])
def eliminar_producto_route():
    data_in = request.json or {}
    registrar_audit(session.get("usuario","Sistema"), request.remote_addr, "eliminar_producto",
                    entidad="productos", entidad_id=data_in.get("sku","?"),
                    detalle=f"Eliminación producto SKU:{data_in.get('sku','?')}")
    if not session.get("logged"):
        return {"error": "no autorizado"}, 401
    data = request.json
    sku = data.get("sku")
    if not sku:
        return {"error": "SKU requerido"}
    eliminar_producto(sku)
    return {"ok": True}

@app.route("/configuracion", methods=["GET","POST"])
def configuracion():
    if not session.get("logged"):
        return {"error": "no autorizado"}, 401
    if request.method == "POST":
        data = request.json
        set_configuracion(data)
        return {"ok": True}
    return {"config": get_configuracion()}

@app.route("/lead_time", methods=["POST"])
def lead_time():
    if not session.get("logged"):
        return {"error": "no autorizado"}, 401
    data = request.json
    set_lead_time(data.get("sku"), data.get("lead_time", 45))
    return {"ok": True}

# ── DEVOLUCIONES ──

@app.route("/devoluciones")
def devoluciones_list():
    if not session.get("logged"):
        return {"error": "no autorizado"}, 401
    estado = request.args.get("estado", "todas")
    return {"devoluciones": listar_devoluciones(estado)}

@app.route("/devoluciones/nueva", methods=["POST"])
def devoluciones_nueva():
    if not session.get("logged"):
        return {"error": "no autorizado"}, 401
    data = request.json
    dev_id = crear_devolucion(data)
    registrar_audit(session.get("usuario","Sistema"), request.remote_addr,
                    "crear_devolucion", entidad="devoluciones", entidad_id=str(dev_id),
                    detalle=f"Nueva DEV: OC={data.get('oc_origen')} SKU={data.get('sku')}")
    return {"ok": True, "id": dev_id}

@app.route("/devoluciones/<int:dev_id>")
def devoluciones_get(dev_id):
    if not session.get("logged"):
        return {"error": "no autorizado"}, 401
    dev = get_devolucion(dev_id=dev_id)
    if not dev:
        return {"error": "no encontrada"}, 404
    return {"devolucion": dev}

@app.route("/devoluciones/buscar")
def devoluciones_buscar_codigo():
    """Lookup por código DEV para pistoleo"""
    if not session.get("logged"):
        return {"error": "no autorizado"}, 401
    codigo = request.args.get("codigo", "").strip()
    dev = get_devolucion(codigo=codigo)
    if not dev:
        return {"error": "no encontrada"}, 404
    return {"devolucion": dev}

@app.route("/devoluciones/lookup_oc")
def devoluciones_lookup_oc():
    """Busca productos asociados a una OC en movimientos"""
    if not session.get("logged"):
        return {"error": "no autorizado"}, 401
    oc = request.args.get("oc", "").strip()
    if not oc:
        return {"error": "OC requerida"}, 400
    conn = __import__('psycopg2').connect(__import__('os').environ.get("DATABASE_URL"))
    cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT m.sku, m.nombre, m.canal,
               ABS(m.cantidad) as cantidad,
               TO_CHAR(
                 CASE WHEN COALESCE(m.canal,'') IN ('Walmart','WooCommerce')
                      THEN m.fecha - INTERVAL '4 hours'
                      ELSE m.fecha
                 END, 'DD/MM/YYYY HH24:MI') as fecha
        FROM movimientos m
        WHERE m.orden_id = %s AND m.tipo = 'salida'
        ORDER BY m.sku
    """, (oc,))
    rows = cur.fetchall()
    cur.close(); conn.close()
    if not rows:
        return {"error": "OC no encontrada en movimientos"}, 404
    items = [{"sku": r[0], "nombre": r[1], "canal": r[2],
              "cantidad": r[3], "fecha": r[4]} for r in rows]
    return {"items": items, "oc": oc}

@app.route("/devoluciones/<int:dev_id>/actualizar", methods=["POST"])
def devoluciones_actualizar(dev_id):
    if not session.get("logged"):
        return {"error": "no autorizado"}, 401
    data = request.json
    registrar_audit(session.get("usuario","Sistema"), request.remote_addr,
                    "actualizar_devolucion", entidad="devoluciones", entidad_id=str(dev_id),
                    detalle=f"Estado: {data.get('estado','?')} · Resolución: {data.get('resolucion','?')}")
    dev = get_devolucion(dev_id=dev_id)
    if not dev:
        return {"error": "no encontrada"}, 404
    actualizar_devolucion(dev_id, data)
    # Si se reingresa al stock, registrar movimiento
    if data.get("estado") == "reingresada" and dev.get("sku") and not dev.get("impacto_stock_reingresado"):
        productos = cargar_productos()
        for p in productos:
            if p["sku"] == dev["sku"]:
                p["stock"] += int(dev.get("cantidad", 1))
                guardar_producto(p)
                registrar_movimiento("entrada", p["sku"], p["nombre"],
                                     int(dev.get("cantidad", 1)), "Devolución reingresada",
                                     usuario=session.get("usuario", "Sistema"),
                                     canal="Manual", orden_id=dev.get("oc_origen"))
                actualizar_stock_woo(p["sku"], p["stock"])
                actualizar_stock_walmart(p["sku"], p["stock"])
                actualizar_stock_paris(p["sku"], p["stock"])
                break
    return {"ok": True}

@app.route("/devoluciones/<int:dev_id>/eliminar", methods=["POST"])
def devoluciones_eliminar(dev_id):
    if not session.get("logged"):
        return {"error": "no autorizado"}, 401
    data = request.json
    clave = data.get("clave", "")
    clave_admin = __import__('os').environ.get("PASSWORD", "")
    if clave != clave_admin:
        registrar_audit(session.get("usuario","?"), request.remote_addr,
                        "intento_eliminar_devolucion", entidad="devoluciones", entidad_id=str(dev_id),
                        resultado="fallido", detalle="Clave admin incorrecta")
        return {"error": "Clave incorrecta"}, 403
    conn = __import__('psycopg2').connect(__import__('os').environ.get("DATABASE_URL"))
    cur = conn.cursor()
    cur.execute("SELECT codigo, oc_origen, nombre FROM devoluciones WHERE id = %s", (dev_id,))
    row = cur.fetchone()
    detalle_dev = str(row) if row else str(dev_id)
    cur.execute("DELETE FROM devoluciones WHERE id = %s", (dev_id,))
    conn.commit()
    cur.close(); conn.close()
    registrar_audit(session.get("usuario","admin"), request.remote_addr,
                    "eliminar_devolucion", entidad="devoluciones", entidad_id=str(dev_id),
                    detalle=f"Devolución eliminada: {detalle_dev}", dato_antes=detalle_dev)
    return {"ok": True}

@app.route("/devoluciones/<int:dev_id>/generar_codigo", methods=["POST"])
def devoluciones_generar_codigo(dev_id):
    if not session.get("logged"):
        return {"error": "no autorizado"}, 401
    dev = get_devolucion(dev_id=dev_id)
    if not dev:
        return {"error": "no encontrada"}, 404
    if dev.get("codigo"):
        return {"ok": True, "codigo": dev["codigo"]}
    codigo = generar_codigo_dev()
    asignar_codigo_dev(dev_id, codigo)
    registrar_audit(session.get("usuario","Sistema"), request.remote_addr,
                    "generar_codigo_dev", entidad="devoluciones", entidad_id=str(dev_id),
                    detalle=f"Código generado: {codigo}")
    return {"ok": True, "codigo": codigo}

# ── PARIS ──

@app.route("/paris/test")
def paris_test():
    if not session.get("logged"):
        return {"error": "no autorizado"}, 401
    result = verificar_conexion_paris()
    registrar_audit(session.get("usuario","Sistema"), request.remote_addr,
                    "paris_test", detalle=f"Test conexión Paris: {result}")
    return result

@app.route("/paris/ordenes")
def paris_ordenes():
    if not session.get("logged"):
        return {"error": "no autorizado"}, 401
    dias = int(request.args.get("dias", 30))
    estado = request.args.get("estado") or None
    ordenes = obtener_ordenes_paris_todas(dias=dias, estado=estado)
    return {"ordenes": ordenes, "total": len(ordenes)}

@app.route("/paris/stock")
def paris_stock():
    if not session.get("logged"):
        return {"error": "no autorizado"}, 401
    data = obtener_stock_paris()
    return data or {"error": "sin datos"}

@app.route("/paris/sync_ordenes")
def paris_sync_ordenes():
    if not session.get("logged"):
        return {"error": "no autorizado"}, 401
    registrar_audit(session.get("usuario","Sistema"), request.remote_addr,
                    "sync_paris", detalle="Sync manual órdenes Paris")
    productos = cargar_productos()
    nuevas = 0
    errores = []
    for estado in ["awaiting_fullfillment", "ready_to_ship"]:
        ordenes = obtener_ordenes_paris_todas(dias=7, estado=estado)
        for so in ordenes:
            sub_order_num = str(so.get("subOrderNumber", ""))
            paris_key = f"PARIS-{sub_order_num}"
            if orden_ya_procesada_texto(paris_key):
                continue
            shipments = so.get("shipments", [])
            for ship in shipments:
                items = ship.get("items", [])
                for item in items:
                    sku_seller = item.get("seller_sku") or item.get("sellerSku") or ""
                    cantidad = 1
                    if not sku_seller:
                        continue
                    for p in productos:
                        if p["sku"] == sku_seller:
                            p["stock"] = max(0, p["stock"] - cantidad)
                            guardar_producto(p)
                            registrar_movimiento("salida", p["sku"], p["nombre"],
                                                cantidad, "Venta Paris",
                                                usuario="Sistema", canal="Paris",
                                                orden_id=sub_order_num)
                            actualizar_stock_woo(p["sku"], p["stock"])
                            actualizar_stock_walmart(p["sku"], p["stock"])
                            actualizar_stock_paris(p["sku"], p["stock"])
            marcar_orden_procesada_texto(paris_key)
            nuevas += 1
    return {"ok": True, "nuevas_ordenes": nuevas}

# ── AUDIT LOG ──

@app.route("/audit")
def audit_view():
    if not session.get("logged"):
        return {"error": "no autorizado"}, 401
    # Asegurar tabla existe (por si el deploy no la creó)
    init_audit()
    # Registrar que el admin consultó el log
    registrar_audit(
        session.get("usuario", "admin"),
        request.remote_addr,
        "consultar_audit",
        detalle="Vista del Audit Log"
    )
    limite = int(request.args.get("limite", 200))
    filtro_accion    = request.args.get("accion") or None
    filtro_usuario   = request.args.get("usuario") or None
    filtro_resultado = request.args.get("resultado") or None
    logs = listar_audit(limite, filtro_accion, filtro_usuario, filtro_resultado)
    return {"logs": logs, "total": len(logs)}

@app.route("/audit/test", methods=["POST"])
def audit_test():
    """Endpoint para verificar que el audit funciona — solo admin"""
    if not session.get("logged"):
        return {"error": "no autorizado"}, 401
    init_audit()
    registrar_audit(
        session.get("usuario", "admin"),
        request.remote_addr,
        "test_audit",
        detalle="Test manual del sistema de audit"
    )
    return {"ok": True, "mensaje": "Registro de prueba creado"}

# ── LOGIN / PANEL ──

@app.route("/")
def home():
    if session.get("logged"):
        return redirect("/panel")
    return render_template("login.html")

@app.route("/login_check", methods=["POST"])
def login_check():
    data = request.json
    if data.get("user") == USUARIO and data.get("password") == PASSWORD:
        session["logged"] = True
        session["usuario"] = data.get("user")
        registrar_audit(data.get("user"), request.remote_addr, "login", detalle="Inicio de sesión exitoso")
        return {"ok": True}
    registrar_audit(data.get("user","?"), request.remote_addr, "login", resultado="fallido", detalle="Clave incorrecta")
    return {"ok": False}

@app.route("/logout")
def logout():
    registrar_audit(session.get("usuario","?"), request.remote_addr, "logout", detalle="Cierre de sesión")
    session.clear()
    return redirect("/")

@app.route("/panel")
def panel():
    if not session.get("logged"):
        return redirect("/")
    return render_template("panel.html")

@app.route("/debug/estado_bd")
def debug_estado_bd():
    if not session.get("logged"):
        return {"error": "no autorizado"}, 401
    from inventario import get_conn
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT
          (SELECT COUNT(*) FROM ordenes_procesadas) as total_op,
          (SELECT COUNT(*) FROM ordenes_procesadas WHERE order_id_texto IS NOT NULL) as con_texto,
          (SELECT COUNT(DISTINCT order_id_texto) FROM ordenes_procesadas
           WHERE order_id_texto IS NOT NULL) as unicos,
          (SELECT COUNT(*) FROM movimientos
           WHERE canal='Walmart') as mov_walmart_total,
          (SELECT COUNT(*) FROM movimientos
           WHERE canal='Walmart' AND orden_id IN (
             SELECT orden_id FROM movimientos
             WHERE canal='Walmart' AND orden_id IS NOT NULL AND orden_id != ''
             GROUP BY orden_id HAVING COUNT(*) > 1
           )) as mov_con_orden_duplicada
    """)
    r = cur.fetchone()

    cur.execute("""
        SELECT orden_id, sku, COUNT(*) as veces,
               MIN(TO_CHAR(fecha, 'DD/MM HH24:MI')) as primera,
               MAX(TO_CHAR(fecha, 'DD/MM HH24:MI')) as ultima
        FROM movimientos
        WHERE canal='Walmart' AND orden_id IS NOT NULL AND orden_id != ''
        GROUP BY orden_id, sku
        HAVING COUNT(*) > 1
        ORDER BY veces DESC
        LIMIT 20
    """)
    dupes = [{"orden_id": x[0], "sku": x[1], "veces": x[2],
              "primera": x[3], "ultima": x[4]} for x in cur.fetchall()]

    cur.execute("""
        SELECT orden_id, order_id_texto,
               TO_CHAR(fecha, 'DD/MM HH24:MI') as fecha
        FROM ordenes_procesadas
        ORDER BY fecha DESC LIMIT 10
    """)
    ultimas_op = [{"orden_id": x[0], "texto": x[1], "fecha": x[2]}
                  for x in cur.fetchall()]

    cur.close(); conn.close()
    return {
        "ordenes_procesadas_total": r[0],
        "con_order_id_texto": r[1],
        "unicos": r[2],
        "movimientos_walmart_total": r[3],
        "movimientos_con_orden_duplicada": r[4],
        "duplicados_detalle": dupes,
        "ultimas_ordenes_procesadas": ultimas_op
    }


@app.route("/debug/paris_skus")
def debug_paris_skus():
    """Trae los SKUs reales de París para tu seller."""
    if not session.get("logged"): return {"error": "no autorizado"}, 401
    import requests as req
    from paris import paris_headers, PARIS_BASE_URL, obtener_stock_paris, obtener_productos_paris

    # Opción 1: stock real
    stock_data = obtener_stock_paris(limite=100, offset=0)

    # Opción 2: productos publicados
    prod_data = obtener_productos_paris(limite=25, offset=0)

    # Opción 3: llamada directa a v2/stock para ver estructura
    try:
        res = req.get(f"{PARIS_BASE_URL}/v2/stock",
                      headers=paris_headers(),
                      params={"limit": 50, "offset": 0},
                      timeout=15)
        stock_raw = {"status": res.status_code, "body": res.json() if res.status_code == 200 else res.text[:500]}
    except Exception as e:
        stock_raw = {"error": str(e)}

    return {
        "stock_v2": stock_raw,
        "productos_search": prod_data,
        "stock_data": stock_data
    }


# ── MAPEO SKUs ──────────────────────────────────────────────────────────────

@app.route("/sku_mapeo")
def ruta_sku_mapeo():
    if not session.get("logged"): return redirect("/")
    return jsonify(listar_sku_mapeo())

@app.route("/sku_mapeo/historial")
def ruta_sku_mapeo_historial():
    if not session.get("logged"): return redirect("/")
    return jsonify(listar_historial_mapeo())

@app.route("/sku_mapeo/guardar", methods=["POST"])
def ruta_sku_mapeo_guardar():
    if not session.get("logged"): return jsonify({"ok": False}), 401
    data = request.json or {}
    ok = guardar_sku_mapeo_fila(
        data.get("sku_lusync", "").strip(),
        {
            "web":         data.get("sku_web", ""),
            "walmart":     data.get("sku_walmart", ""),
            "paris":       data.get("sku_paris", ""),
            "falabella":   data.get("sku_falabella", ""),
            "ripley":      data.get("sku_ripley", ""),
            "mercadolibre":data.get("sku_mercadolibre", ""),
            "hites":       data.get("sku_hites", "")
        }
    )
    return jsonify({"ok": ok})

@app.route("/sku_mapeo/plataforma_web", methods=["GET", "POST"])
def ruta_plataforma_web():
    if not session.get("logged"): return jsonify({}), 401
    if request.method == "POST":
        data = request.json or {}
        set_plataforma_web(data.get("plataforma", "woocommerce"))
        return jsonify({"ok": True})
    return jsonify({"plataforma": get_plataforma_web()})

@app.route("/sku_mapeo/exportar_excel")
def ruta_exportar_excel():
    if not session.get("logged"): return redirect("/")
    try:
        import io, openpyxl
        filas = listar_sku_mapeo()
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Mapeo SKUs"
        ws.append(["SKU Lusync","Producto","SKU Web","SKU Walmart","SKU Paris",
                   "SKU Falabella","SKU Ripley","SKU MercadoLibre","SKU Hites"])
        for f in filas:
            ws.append([f.get("sku_lusync",""), f.get("nombre",""),
                       f.get("sku_web",""), f.get("sku_walmart",""),
                       f.get("sku_paris",""), f.get("sku_falabella",""),
                       f.get("sku_ripley",""), f.get("sku_mercadolibre",""),
                       f.get("sku_hites","")])
        buf = io.BytesIO()
        wb.save(buf); buf.seek(0)
        return send_file(buf, download_name="mapeo_skus.xlsx", as_attachment=True,
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/sku_mapeo/importar_excel", methods=["POST"])
def ruta_importar_excel():
    if not session.get("logged"): return jsonify({"ok": False, "error": "no autorizado"}), 401
    try:
        import io, openpyxl
        archivo = request.files.get("archivo")
        if not archivo:
            return jsonify({"ok": False, "error": "No se recibio archivo"})
        wb = openpyxl.load_workbook(io.BytesIO(archivo.read()), data_only=True)
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
        if len(rows) < 2:
            return jsonify({"ok": False, "error": "Archivo vacio o sin datos"})
        importados = 0
        errores = []
        for i, row in enumerate(rows[1:], start=2):
            try:
                sku_lusync = str(row[0]).strip() if row[0] else ""
                if not sku_lusync or sku_lusync == "None":
                    continue
                skus = {
                    "web":          str(row[2]).strip() if len(row)>2 and row[2] else "",
                    "walmart":      str(row[3]).strip() if len(row)>3 and row[3] else "",
                    "paris":        str(row[4]).strip() if len(row)>4 and row[4] else "",
                    "falabella":    str(row[5]).strip() if len(row)>5 and row[5] else "",
                    "ripley":       str(row[6]).strip() if len(row)>6 and row[6] else "",
                    "mercadolibre": str(row[7]).strip() if len(row)>7 and row[7] else "",
                    "hites":        str(row[8]).strip() if len(row)>8 and row[8] else "",
                }
                guardar_sku_mapeo_fila(sku_lusync, skus)
                importados += 1
            except Exception as e:
                errores.append(f"Fila {i}: {str(e)}")
        registrar_importacion_mapeo(session.get("usuario","Sistema"), archivo.filename, importados, [{"fila": i, "error": e} for i, e in enumerate(errores)])
        return jsonify({"ok": True, "importados": importados, "errores": errores})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500



@app.route("/debug/paris_stock")
def debug_paris_stock():
    """Diagnóstico: prueba envío de stock a Paris para todos los productos mapeados."""
    if not session.get("logged"): return redirect("/")
    try:
        from inventario import listar_sku_mapeo, cargar_productos
        from paris import actualizar_stock_paris, verificar_conexion_paris

        conexion = verificar_conexion_paris()
        productos = {p["sku"]: p for p in cargar_productos()}
        mapeo = listar_sku_mapeo()

        resultados = []
        for fila in mapeo:
            sku_lusync = fila.get("sku_lusync", "")
            sku_paris  = fila.get("sku_paris", "")
            if not sku_paris:
                continue
            prod = productos.get(sku_lusync)
            stock_actual = prod.get("stock", 0) if prod else 0

            ok = actualizar_stock_paris(sku_lusync, stock_actual)
            resultados.append({
                "sku_lusync": sku_lusync,
                "sku_paris":  sku_paris,
                "nombre":     fila.get("nombre", ""),
                "stock":      stock_actual,
                "ok":         ok
            })

        return jsonify({
            "conexion": conexion,
            "total_mapeados": len(resultados),
            "exitosos": sum(1 for r in resultados if r["ok"]),
            "fallidos": sum(1 for r in resultados if not r["ok"]),
            "detalle":  resultados
        })
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
