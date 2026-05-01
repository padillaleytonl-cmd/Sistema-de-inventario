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
                        registrar_importacion_mapeo, listar_historial_mapeo,
                        init_alertas, crear_alerta, listar_alertas,
                        contar_alertas_no_leidas, marcar_alerta_leida,
                        marcar_todas_leidas, get_alertas_config, set_alertas_config,
                        init_meli_auth, get_meli_auth, set_meli_auth, borrar_meli_auth,
                        stats_ventas_por_canal_dia, stats_top_productos_vendidos,
                        stats_movimientos_dia, stats_distribucion_stock_canal,
                        stats_kpis_dashboard,
                        init_bodegas, listar_bodegas, stock_por_bodega,
                        get_stock_bodega, set_stock_bodega, ajustar_stock_bodega,
                        listar_stock_completo, stock_total_por_bodega,
                        determinar_bodega_para_canal,
                        actualizar_nombres_bodegas)

app = Flask(__name__)
app.secret_key = "clave_super_segura"

init_db()
init_devoluciones()
init_audit()
init_sku_mapeo()
init_alertas()
init_meli_auth()
init_bodegas()

# Registrar Blueprints (módulos de marketplaces)
from walmart import walmart_bp
from paris import paris_bp
app.register_blueprint(walmart_bp)
app.register_blueprint(paris_bp)

# ── SYNC AUTOMÁTICO WALMART CADA 5 MINUTOS ──
def _sync_walmart_automatico():
    """Tarea de background: sincroniza órdenes Walmart sin requerir sesión"""
    try:
        print("[Scheduler] Iniciando sync automático Walmart...")
        productos = cargar_productos()
        nuevas = 0
        errores = []

        # Descuenta apenas Walmart crea la orden (Created) para evitar sobreventa en otros canales.
        # Si el cliente cancela, el bloque de cancelaciones (más abajo) reintegra el stock automáticamente.
        for estado in ["Created", "Acknowledged", "Shipped", "Delivered"]:
            ordenes = obtener_ordenes_walmart(estado)
            for o in ordenes:
                order_id = o.get("purchaseOrderId")
                if not order_id:
                    continue
                customer_order_id = str(o.get("customerOrderId", order_id))
                if orden_ya_procesada_texto(customer_order_id):
                    continue

                # Marcar ANTES de procesar para evitar dobles descuentos si una API externa
                # (Paris/Woo/Walmart) timeout-ea en medio del loop
                marcar_orden_procesada_texto(customer_order_id)

                lineas = o.get("orderLines", {}).get("orderLine", [])
                if isinstance(lineas, dict):
                    lineas = [lineas]

                # Detectar si esta orden es WFS (Walmart Fulfillment Services)
                from inventario import detectar_fulfillment_walmart, descontar_venta_inteligente
                es_wfs = detectar_fulfillment_walmart(o)
                tipo_str = "WFS" if es_wfs else "Seller"

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

                        # Buscar SKU Lusync vía mapeo (sku que viene de Walmart puede no ser igual a sku Lusync)
                        sku_lusync = sku
                        try:
                            from inventario import listar_sku_mapeo
                            for fila in listar_sku_mapeo():
                                if fila.get("sku_walmart") == sku:
                                    sku_lusync = fila.get("sku_lusync")
                                    break
                        except: pass

                        # Buscar producto y descontar de bodega correcta
                        producto_existe = any(p["sku"] == sku_lusync for p in productos)
                        if not producto_existe:
                            print(f"[Scheduler] SKU '{sku_lusync}' no encontrado en inventario")
                            continue

                        resultado = descontar_venta_inteligente(
                            sku=sku_lusync,
                            cantidad=cantidad,
                            canal="Walmart",
                            fulfillment=es_wfs,
                            orden_id=customer_order_id,
                            motivo=f"Venta Walmart {tipo_str}",
                            usuario="Sistema"
                        )
                        print(f"[Scheduler] {customer_order_id} {tipo_str}: {sku_lusync} -{cantidad} desde {resultado['bodega']}")

                        # Sync a otros canales SOLO si fue Seller (afectó Central)
                        if not es_wfs:
                            from inventario import cargar_productos as _cp
                            stock_total = next((pp["stock"] for pp in _cp() if pp["sku"] == sku_lusync), 0)
                            try: actualizar_stock_woo(sku_lusync, stock_total)
                            except: pass
                            try: actualizar_stock_walmart(sku_lusync, stock_total)
                            except: pass
                            try: actualizar_stock_paris(sku_lusync, stock_total)
                            except: pass
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
                items_cancelados = []  # para alerta consolidada
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
                                items_cancelados.append(f"{p['nombre']} (SKU: {sku}) x{cantidad}")
                                print(f"[Scheduler] CANCELACIÓN SKU:{sku} +{cantidad} Stock:{p['stock']}")
                    except Exception as e:
                        print(f"[Scheduler] Error cancelación linea: {e}")

                # Crear alerta consolidada por orden cancelada
                if items_cancelados:
                    try:
                        crear_alerta(
                            tipo="cancelacion",
                            canal="Walmart",
                            titulo=f"Orden cancelada en Walmart: {customer_order_id}",
                            mensaje="El cliente canceló la orden. Stock reintegrado automáticamente:<br><br>" +
                                    "<br>".join(f"• {it}" for it in items_cancelados),
                            orden_id=customer_order_id,
                            sku=items_cancelados[0].split("SKU: ")[1].split(")")[0] if items_cancelados else None
                        )
                    except Exception as e:
                        print(f"[Scheduler] Error creando alerta: {e}")

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

                # Marcar ANTES de procesar para evitar dobles descuentos
                marcar_orden_procesada_texto(customer_order_id)

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
                                try:
                                    from inventario import sincronizar_stock_a_bodega_central
                                    sincronizar_stock_a_bodega_central(p["sku"])
                                except: pass
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
                items_cancel_rec = []
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
                            items_cancel_rec.append(f"{p['nombre']} (SKU: {sku}) x{cantidad}")
                if items_cancel_rec:
                    try:
                        crear_alerta(
                            tipo="cancelacion",
                            canal="Walmart",
                            titulo=f"Orden cancelada en Walmart: {customer_order_id}",
                            mensaje="El cliente canceló la orden. Stock reintegrado automáticamente:<br><br>" +
                                    "<br>".join(f"• {it}" for it in items_cancel_rec),
                            orden_id=customer_order_id
                        )
                    except Exception as e:
                        print(f"[Recuperación] Error creando alerta: {e}")
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

# El endpoint /walmart/sync_ordenes se movió a walmart.py (Blueprint)
# Ver: walmart_bp en walmart.py

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
                        try:
                            from inventario import sincronizar_stock_a_bodega_central
                            sincronizar_stock_a_bodega_central(p["sku"])
                        except: pass
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
            items_cancel_man = []
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
                        items_cancel_man.append(f"{p['nombre']} (SKU: {sku}) x{cantidad}")
                        log.append(f"CANCELACION SKU:{sku} +{cantidad} Stock:{p['stock']}")
            if items_cancel_man:
                try:
                    crear_alerta(
                        tipo="cancelacion",
                        canal="Walmart",
                        titulo=f"Orden cancelada en Walmart: {customer_order_id}",
                        mensaje="El cliente canceló la orden. Stock reintegrado automáticamente:<br><br>" +
                                "<br>".join(f"• {it}" for it in items_cancel_man),
                        orden_id=customer_order_id
                    )
                except Exception as e:
                    log.append(f"Error creando alerta: {e}")
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

# /paris/sync_ordenes movido a paris.py (Blueprint)


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



@app.route("/debug/paris_stock_raw")
def debug_paris_stock_raw():
    """Envía stock a Paris uno por uno y muestra status + body crudo de cada respuesta."""
    if not session.get("logged"): return redirect("/")
    try:
        import requests as req
        from inventario import listar_sku_mapeo, cargar_productos
        from paris import paris_headers, PARIS_BASE_URL

        productos = {p["sku"]: p for p in cargar_productos()}
        mapeo = listar_sku_mapeo()

        resultados = []
        for fila in mapeo:
            sku_lusync = fila.get("sku_lusync", "")
            sku_paris  = (fila.get("sku_paris", "") or "").strip()
            if not sku_paris:
                continue
            prod = productos.get(sku_lusync)
            stock_actual = int(prod.get("stock", 0)) if prod else 0

            payload = {"skus": [{"skuSeller": sku_paris, "quantity": stock_actual}]}
            try:
                res = req.post(
                    f"{PARIS_BASE_URL}/v1/stock/sku-seller",
                    headers=paris_headers(),
                    json=payload,
                    timeout=15
                )
                resultados.append({
                    "sku_lusync": sku_lusync,
                    "sku_paris": sku_paris,
                    "stock": stock_actual,
                    "status": res.status_code,
                    "request_body": payload,
                    "response_body": res.text[:500]
                })
            except Exception as e:
                resultados.append({
                    "sku_lusync": sku_lusync,
                    "sku_paris": sku_paris,
                    "stock": stock_actual,
                    "error": str(e),
                    "request_body": payload
                })

        return jsonify({"resultados": resultados})
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@app.route("/debug/paris_stock_v2")
def debug_paris_stock_v2():
    """Prueba 3 variantes de payload para identificar el formato correcto."""
    if not session.get("logged"): return redirect("/")
    try:
        import requests as req
        from paris import paris_headers, PARIS_BASE_URL

        # Probar con un SKU conocido del catálogo
        sku_test = "CDPPASN001"
        stock = 99

        variantes = [
            {"nombre": "skuSeller (camelCase, actual)",
             "payload": {"skus": [{"skuSeller": sku_test, "quantity": stock}]}},
            {"nombre": "sku_seller (snake_case)",
             "payload": {"skus": [{"sku_seller": sku_test, "quantity": stock}]}},
            {"nombre": "sku (campo simple)",
             "payload": {"skus": [{"sku": sku_test, "quantity": stock}]}},
            {"nombre": "objeto plano sin skus[]",
             "payload": {"skuSeller": sku_test, "quantity": stock}},
            {"nombre": "snake_case plano",
             "payload": {"sku_seller": sku_test, "quantity": stock}},
        ]

        resultados = []
        for v in variantes:
            try:
                res = req.post(
                    f"{PARIS_BASE_URL}/v1/stock/sku-seller",
                    headers=paris_headers(),
                    json=v["payload"],
                    timeout=15
                )
                resultados.append({
                    "variante": v["nombre"],
                    "payload_enviado": v["payload"],
                    "status": res.status_code,
                    "response": res.text[:400]
                })
            except Exception as e:
                resultados.append({
                    "variante": v["nombre"],
                    "payload_enviado": v["payload"],
                    "error": str(e)
                })

        return jsonify({"sku_probado": sku_test, "stock": stock, "resultados": resultados})
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@app.route("/debug/paris_stock_consultar")
def debug_paris_stock_consultar():
    """Consulta el stock actual desde la API de Paris para los SKUs mapeados."""
    if not session.get("logged"): return redirect("/")
    try:
        import requests as req
        from inventario import listar_sku_mapeo
        from paris import paris_headers, PARIS_BASE_URL

        mapeo = listar_sku_mapeo()
        resultados = []

        for fila in mapeo:
            sku_paris = (fila.get("sku_paris", "") or "").strip()
            if not sku_paris:
                continue

            # Endpoint v1: stock por sku_seller
            try:
                res = req.get(
                    f"{PARIS_BASE_URL}/v1/stock/sku-seller/{sku_paris}",
                    headers=paris_headers(),
                    timeout=15
                )
                resultados.append({
                    "sku_paris": sku_paris,
                    "sku_lusync": fila.get("sku_lusync"),
                    "endpoint": "v1/stock/sku-seller/{sku}",
                    "status": res.status_code,
                    "response": res.text[:600]
                })
            except Exception as e:
                resultados.append({
                    "sku_paris": sku_paris,
                    "error": str(e)
                })

        return jsonify({"resultados": resultados})
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@app.route("/debug/paris_stock_listar")
def debug_paris_stock_listar():
    """Lista todo el stock actual del seller usando el endpoint v2/stock."""
    if not session.get("logged"): return redirect("/")
    try:
        import requests as req
        from paris import paris_headers, PARIS_BASE_URL

        # Probar varios endpoints para ver cuál funciona
        endpoints = [
            "/v2/stock",
            "/v1/stock",
            "/v2/stock/sku-seller",
            "/v1/stock/sku-seller",
        ]
        resultados = []
        for ep in endpoints:
            try:
                res = req.get(
                    f"{PARIS_BASE_URL}{ep}",
                    headers=paris_headers(),
                    params={"limit": 200, "offset": 0},
                    timeout=20
                )
                resultados.append({
                    "endpoint": ep,
                    "status": res.status_code,
                    "response": res.text[:1500]
                })
            except Exception as e:
                resultados.append({"endpoint": ep, "error": str(e)})
        return jsonify({"resultados": resultados})
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@app.route("/debug/paris_stock_warehouse")
def debug_paris_stock_warehouse():
    """Probar enviando stock con campo warehouse explícito en distintos formatos."""
    if not session.get("logged"): return redirect("/")
    try:
        import requests as req
        from paris import paris_headers, PARIS_BASE_URL

        sku_test = "RHR2022-1"  # tiene stock real 25, voy a enviar 99 y ver si actualiza
        stock = 99

        variantes = [
            {"nombre": "sin warehouse",
             "payload": {"skus": [{"sku_seller": sku_test, "quantity": stock}]}},
            {"nombre": "warehouse=Dropshipping (nombre real)",
             "payload": {"skus": [{"sku_seller": sku_test, "quantity": stock, "warehouse": "Dropshipping"}]}},
            {"nombre": "warehouse=dropship (lo que retornó la API)",
             "payload": {"skus": [{"sku_seller": sku_test, "quantity": stock, "warehouse": "dropship"}]}},
            {"nombre": "warehouseName=Dropshipping",
             "payload": {"skus": [{"sku_seller": sku_test, "quantity": stock, "warehouseName": "Dropshipping"}]}},
        ]

        resultados = []
        for v in variantes:
            try:
                res = req.post(
                    f"{PARIS_BASE_URL}/v1/stock/sku-seller",
                    headers=paris_headers(),
                    json=v["payload"],
                    timeout=15
                )
                resultados.append({
                    "variante": v["nombre"],
                    "request": v["payload"],
                    "status": res.status_code,
                    "response": res.text[:600]
                })
            except Exception as e:
                resultados.append({"variante": v["nombre"], "error": str(e)})

        # Esperar 2 segundos y consultar el listado para ver si se reflejó
        import time
        time.sleep(2)
        listado = req.get(f"{PARIS_BASE_URL}/v2/stock", headers=paris_headers(),
                          params={"limit": 200}, timeout=20)

        # Buscar el SKU de prueba en el listado
        sku_actual = None
        if listado.status_code == 200:
            data = listado.json()
            for s in data.get("skus", []):
                if s.get("sku_seller") == sku_test:
                    sku_actual = {
                        "sku_seller": s.get("sku_seller"),
                        "quantity": s.get("quantity"),
                        "availableStock": s.get("availableStock"),
                        "warehouseName": s.get("warehouseName"),
                        "updatedAt": s.get("updatedAt")
                    }
                    break

        return jsonify({
            "sku_probado": sku_test,
            "stock_enviado": stock,
            "tests": resultados,
            "estado_actual_en_paris": sku_actual or "no encontrado en listado"
        })
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


# /paris/sync_estado movido a paris.py (Blueprint)


@app.route("/paris/forzar_sync_sku", methods=["POST"])
def ruta_paris_forzar_sync():
    """Re-envía el stock actual de un SKU específico a Paris."""
    if not session.get("logged"): return jsonify({"ok": False}), 401
    try:
        from inventario import cargar_productos
        from paris import actualizar_stock_paris
        data = request.json or {}
        sku = data.get("sku_lusync", "").strip()
        if not sku:
            return jsonify({"ok": False, "error": "SKU no proporcionado"})
        prod = next((p for p in cargar_productos() if p["sku"] == sku), None)
        if not prod:
            return jsonify({"ok": False, "error": f"SKU {sku} no existe en inventario"})
        ok = actualizar_stock_paris(sku, prod["stock"])
        return jsonify({"ok": ok, "sku": sku, "stock_enviado": prod["stock"]})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# /paris/forzar_sync_todos movido a paris.py (Blueprint)






@app.route("/debug/orden_paris_raw/<sub_order_number>")
def debug_orden_paris_raw(sub_order_number):
    """Devuelve el JSON crudo de la orden de París para identificar el campo de estado correcto."""
    if not session.get("logged"): return redirect("/")
    try:
        from paris import obtener_ordenes_paris_todas
        # Buscar en TODOS los estados posibles que conocemos
        for estado in ["awaiting_fullfillment", "ready_to_ship", "shipped", "delivered", "cancelled"]:
            try:
                todas = obtener_ordenes_paris_todas(dias=60, estado=estado)
                for so in todas:
                    if str(so.get("subOrderNumber", "")) == str(sub_order_number):
                        # Devolver TODA la orden para ver qué campos tiene
                        return jsonify({
                            "encontrada_buscando_estado": estado,
                            "campos_top_nivel": list(so.keys()),
                            "orden_completa": so
                        })
            except Exception as e:
                print(f"[debug] Error buscando en {estado}: {e}")
                continue
        return jsonify({"error": f"Orden {sub_order_number} no encontrada en ningún estado"}), 404
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@app.route("/debug/buscar_orden_paris/<sub_order_number>")
def debug_buscar_orden_paris(sub_order_number):
    """Busca una orden Paris SIN filtro de estado para encontrarla."""
    if not session.get("logged"): return redirect("/")
    try:
        from paris import paris_headers, PARIS_BASE_URL, get_seller_id
        import requests as req
        from datetime import datetime, timedelta

        # Probar varias estrategias
        resultados = {"intentos": []}

        # Intento 1: buscar SIN filtro de estado (todas las órdenes 90 días)
        for dias in [30, 60, 90, 180]:
            fecha_desde = (datetime.utcnow() - timedelta(days=dias)).strftime("%Y-%m-%d")
            params = {
                "gteCreatedAt": fecha_desde,
                "limit": 100,
                "offset": 0
            }
            seller_id = get_seller_id()
            if seller_id:
                params["sellerId"] = seller_id

            try:
                res = req.get(f"{PARIS_BASE_URL}/v2/sub-orders",
                              headers=paris_headers(),
                              params=params, timeout=20)

                if res.status_code != 200:
                    resultados["intentos"].append({
                        "dias": dias, "status_code": res.status_code,
                        "error": res.text[:200]
                    })
                    continue

                data = res.json()
                ordenes = data.get("data", [])
                total = data.get("count", 0)

                # Buscar la sub-orden
                encontrada = None
                for o in ordenes:
                    if str(o.get("subOrderNumber", "")) == str(sub_order_number):
                        encontrada = o
                        break

                resultados["intentos"].append({
                    "dias": dias,
                    "total_ordenes_traidas": len(ordenes),
                    "total_ordenes_paris": total,
                    "encontrada": encontrada is not None,
                    "campos_si_encontrada": list(encontrada.keys()) if encontrada else None,
                    "orden_completa": encontrada
                })
                if encontrada:
                    break  # ya la encontró, no seguir
            except Exception as e:
                resultados["intentos"].append({"dias": dias, "error": str(e)})

        # Intento 2: buscar directamente por orderNumber/subOrderNumber en endpoint específico
        try:
            res = req.get(f"{PARIS_BASE_URL}/v2/sub-orders/{sub_order_number}",
                          headers=paris_headers(), timeout=15)
            resultados["endpoint_directo"] = {
                "url": f"/v2/sub-orders/{sub_order_number}",
                "status_code": res.status_code,
                "respuesta": res.json() if res.status_code == 200 else res.text[:300]
            }
        except Exception as e:
            resultados["endpoint_directo"] = {"error": str(e)}

        return jsonify(resultados)
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@app.route("/debug/orden_meli_raw/<order_id>")
def debug_orden_meli_raw(order_id):
    """Devuelve datos crudos de MELI sobre una orden, probando varios endpoints."""
    if not session.get("logged"): return redirect("/")
    try:
        from mercadolibre import meli_headers, MELI_API_URL
        from inventario import get_meli_auth
        import requests as req

        auth = get_meli_auth()
        if not auth or not auth.get("access_token"):
            return jsonify({"error": "MELI no conectado"}), 400

        resultados = {
            "user_id_seller_lusync": auth.get("user_id"),
            "intentos": []
        }

        # Intento 1: /orders/{id} directo
        try:
            res = req.get(f"{MELI_API_URL}/orders/{order_id}",
                          headers=meli_headers(), timeout=15)
            resultados["intentos"].append({
                "endpoint": f"/orders/{order_id}",
                "status_code": res.status_code,
                "respuesta": res.json() if res.status_code == 200 else res.text[:500]
            })
        except Exception as e:
            resultados["intentos"].append({"endpoint": f"/orders/{order_id}", "error": str(e)})

        # Intento 2: buscar la orden en /orders/search?seller=... con q=order_id
        try:
            res = req.get(f"{MELI_API_URL}/orders/search",
                          headers=meli_headers(),
                          params={"seller": auth.get("user_id"), "q": order_id, "limit": 10},
                          timeout=15)
            resultados["intentos"].append({
                "endpoint": "/orders/search?q=...",
                "status_code": res.status_code,
                "total_resultados": res.json().get("paging", {}).get("total", 0) if res.status_code == 200 else None,
                "primeras_3_ordenes": res.json().get("results", [])[:3] if res.status_code == 200 else res.text[:500]
            })
        except Exception as e:
            resultados["intentos"].append({"endpoint": "/orders/search", "error": str(e)})

        # Intento 3: ¿es un PACK? Probar /packs/{id}
        try:
            res = req.get(f"{MELI_API_URL}/packs/{order_id}",
                          headers=meli_headers(), timeout=15)
            resultados["intentos"].append({
                "endpoint": f"/packs/{order_id}",
                "status_code": res.status_code,
                "respuesta": res.json() if res.status_code == 200 else res.text[:300]
            })
        except Exception as e:
            resultados["intentos"].append({"endpoint": f"/packs/{order_id}", "error": str(e)})

        return jsonify(resultados)
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@app.route("/debug/orden_meli/<order_id>")
def debug_orden_meli(order_id):
    """Diagnóstico exhaustivo de una orden MELI específica.
    Te dice por qué NO se descontó el stock."""
    if not session.get("logged"): return redirect("/")
    try:
        from mercadolibre import obtener_orden_meli
        from inventario import (orden_ya_procesada_texto, listar_sku_mapeo,
                                cargar_productos, get_stock_bodega,
                                determinar_bodega_para_canal)
        from bodegas_logic import detectar_fulfillment_meli

        # 1. Traer la orden de MELI
        orden = obtener_orden_meli(order_id)
        if not orden:
            return jsonify({
                "error": f"Orden {order_id} no encontrada en MELI API",
                "posibles_causas": [
                    "El order_id es incorrecto",
                    "El token OAuth expiró",
                    "Esta orden no pertenece a tu cuenta MELI"
                ]
            }), 404

        # 2. Estado de la orden
        estado = orden.get("status", "")
        order_items = orden.get("order_items", [])

        # 3. ¿Ya está marcada como procesada?
        meli_key = f"MELI-{order_id}"
        ya_procesada = orden_ya_procesada_texto(meli_key)

        # 4. Detectar Full vs Seller
        es_full = detectar_fulfillment_meli(orden)
        bodega_correcta = determinar_bodega_para_canal("MercadoLibre", fulfillment=es_full)

        # 5. Para cada item: buscar mapeo y verificar
        productos_dict = {p["sku"]: p for p in cargar_productos()}
        mapeo = listar_sku_mapeo()
        items_diagnostico = []
        for item in order_items:
            sku_seller = (item.get("item", {}).get("seller_custom_field", "") or "").strip()
            item_id = item.get("item", {}).get("id", "")
            qty = int(item.get("quantity", 1) or 1)

            # Buscar SKU Lusync
            sku_lusync_por_mapeo = None
            for fila in mapeo:
                if (fila.get("sku_mercadolibre") == item_id or
                    fila.get("sku_mercadolibre") == sku_seller):
                    sku_lusync_por_mapeo = fila.get("sku_lusync")
                    break

            sku_lusync_final = sku_lusync_por_mapeo or sku_seller
            existe_en_inventario = sku_lusync_final in productos_dict

            stock_actual_central = get_stock_bodega(sku_lusync_final, "CENTRAL") if existe_en_inventario else None
            stock_actual_bodega_correcta = get_stock_bodega(sku_lusync_final, bodega_correcta) if existe_en_inventario else None

            items_diagnostico.append({
                "item_id_meli": item_id,
                "sku_seller_custom_field": sku_seller,
                "cantidad": qty,
                "sku_encontrado_en_mapeo": sku_lusync_por_mapeo,
                "sku_lusync_que_se_usaria": sku_lusync_final,
                "existe_en_inventario_lusync": existe_en_inventario,
                "stock_actual_bodega_central": stock_actual_central,
                "stock_actual_bodega_destino": stock_actual_bodega_correcta,
                "bodega_donde_se_descontaria": bodega_correcta
            })

        # 6. Diagnóstico final: ¿por qué falló?
        razones = []
        if estado not in ("paid", "confirmed", "payment_required"):
            razones.append(f"Estado de la orden es '{estado}', solo se procesan: paid/confirmed/payment_required")
        if ya_procesada:
            razones.append("La orden YA estaba marcada como procesada (se ignora en sync)")
        for it in items_diagnostico:
            if not it["existe_en_inventario_lusync"]:
                razones.append(f"SKU '{it['sku_lusync_que_se_usaria']}' NO existe en inventario Lusync")

        return jsonify({
            "order_id": order_id,
            "estado_orden_meli": estado,
            "es_fulfillment_full": es_full,
            "bodega_donde_descontaria": bodega_correcta,
            "ya_marcada_como_procesada": ya_procesada,
            "items": items_diagnostico,
            "razones_no_descontado": razones if razones else ["✓ Debería descontar correctamente"],
            "fecha_orden": orden.get("date_created", ""),
            "comprador": orden.get("buyer", {}).get("nickname", "")
        })
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@app.route("/debug/orden_paris/<sub_order_number>")
def debug_orden_paris(sub_order_number):
    """Diagnóstico exhaustivo de una orden París específica."""
    if not session.get("logged"): return redirect("/")
    try:
        from paris import obtener_orden_paris, obtener_ordenes_paris_todas
        from inventario import (orden_ya_procesada_texto, listar_sku_mapeo,
                                cargar_productos, get_stock_bodega,
                                determinar_bodega_para_canal)
        from bodegas_logic import detectar_fulfillment_paris

        # 1. Buscar la orden — primero intentar directo, si falla buscar en lista
        orden = None
        try:
            orden = obtener_orden_paris(sub_order_number)
        except: pass

        if not orden:
            # Buscar en todas las órdenes recientes
            for estado in ["awaiting_fullfillment", "ready_to_ship", "shipped", "delivered", "cancelled"]:
                try:
                    todas = obtener_ordenes_paris_todas(dias=60, estado=estado)
                    for so in todas:
                        if str(so.get("subOrderNumber", "")) == str(sub_order_number):
                            orden = so
                            break
                    if orden: break
                except: continue

        if not orden:
            return jsonify({
                "error": f"Orden {sub_order_number} no encontrada en París API (buscado 60 días, todos los estados)",
                "posibles_causas": [
                    "El subOrderNumber es incorrecto",
                    "La orden tiene más de 60 días",
                    "El estado no está en la lista buscada"
                ]
            }), 404

        # 2. Datos de la orden
        estado_paris = orden.get("statusName") or orden.get("status") or ""
        paris_key = f"PARIS-{sub_order_number}"
        ya_procesada = orden_ya_procesada_texto(paris_key)

        # 3. Detectar CD vs Seller
        es_cd = detectar_fulfillment_paris(orden)
        bodega_correcta = determinar_bodega_para_canal("Paris", fulfillment=es_cd)

        # 4. Diagnóstico de cada item
        productos_dict = {p["sku"]: p for p in cargar_productos()}
        mapeo = listar_sku_mapeo()
        items_diagnostico = []
        shipments = orden.get("shipments", [])
        for ship in shipments:
            for item in ship.get("items", []):
                sku_paris = item.get("seller_sku") or item.get("sellerSku") or ""
                qty = int(item.get("quantity", 1) or 1)

                # Buscar mapeo
                sku_lusync_por_mapeo = None
                for fila in mapeo:
                    if fila.get("sku_paris") == sku_paris:
                        sku_lusync_por_mapeo = fila.get("sku_lusync")
                        break

                sku_lusync_final = sku_lusync_por_mapeo or sku_paris
                existe = sku_lusync_final in productos_dict

                stock_central = get_stock_bodega(sku_lusync_final, "CENTRAL") if existe else None
                stock_destino = get_stock_bodega(sku_lusync_final, bodega_correcta) if existe else None

                items_diagnostico.append({
                    "sku_paris_recibido": sku_paris,
                    "cantidad": qty,
                    "sku_encontrado_en_mapeo": sku_lusync_por_mapeo,
                    "sku_lusync_que_se_usaria": sku_lusync_final,
                    "existe_en_inventario_lusync": existe,
                    "stock_actual_bodega_central": stock_central,
                    "stock_actual_bodega_destino": stock_destino,
                    "bodega_donde_se_descontaria": bodega_correcta
                })

        # 5. Razones de no descuento
        razones = []
        estados_validos = ["awaiting_fullfillment", "ready_to_ship", "shipped", "delivered"]
        if estado_paris not in estados_validos:
            razones.append(f"Estado '{estado_paris}' no está en la lista que el sync procesa: {estados_validos}")
        if ya_procesada:
            razones.append("La orden YA estaba marcada como procesada")
        for it in items_diagnostico:
            if not it["existe_en_inventario_lusync"]:
                razones.append(f"SKU '{it['sku_lusync_que_se_usaria']}' NO existe en inventario Lusync")

        return jsonify({
            "sub_order_number": sub_order_number,
            "estado_paris": estado_paris,
            "es_fulfillment_cd": es_cd,
            "bodega_donde_descontaria": bodega_correcta,
            "ya_marcada_como_procesada": ya_procesada,
            "items": items_diagnostico,
            "razones_no_descontado": razones if razones else ["✓ Debería descontar correctamente"],
            "datos_orden_completos": {
                "fecha": orden.get("createdAt") or orden.get("date_created", ""),
                "shipments_count": len(shipments)
            }
        })
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@app.route("/debug/orden_meli_shipment/<order_id>")
def debug_orden_meli_shipment(order_id):
    """Investiga el shipment de una orden MELI para identificar Full vs Seller."""
    if not session.get("logged"): return redirect("/")
    try:
        from mercadolibre import obtener_orden_meli, meli_headers, MELI_API_URL
        import requests as req

        orden = obtener_orden_meli(order_id)
        if not orden:
            return jsonify({"error": "Orden no encontrada"}), 404

        # Obtener shipping_id
        shipping = orden.get("shipping", {}) or {}
        shipping_id = shipping.get("id")

        resultado = {
            "order_id": order_id,
            "shipping_object_en_orden": shipping,
            "shipping_id": shipping_id,
            "tags_orden": orden.get("tags", []),
            "fulfilled_orden": orden.get("fulfilled"),
        }

        # Si hay shipping_id, consultar /shipments/{id}
        if shipping_id:
            try:
                res = req.get(f"{MELI_API_URL}/shipments/{shipping_id}",
                              headers=meli_headers(), timeout=15)
                if res.status_code == 200:
                    ship_detail = res.json()
                    resultado["shipment_detalle_completo"] = ship_detail
                    resultado["CAMPOS_CLAVE_PARA_DETECTAR_FULL"] = {
                        "logistic_type": ship_detail.get("logistic_type"),
                        "logistic.type": (ship_detail.get("logistic") or {}).get("type"),
                        "logistic.mode": (ship_detail.get("logistic") or {}).get("mode"),
                        "shipping_mode": ship_detail.get("shipping_mode"),
                        "shipping_option_name": (ship_detail.get("shipping_option") or {}).get("name"),
                        "service_id": ship_detail.get("service_id"),
                        "tags": ship_detail.get("tags", [])
                    }
                else:
                    resultado["shipment_error"] = f"{res.status_code}: {res.text[:300]}"
            except Exception as e:
                resultado["shipment_error"] = str(e)

        return jsonify(resultado)
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@app.route("/debug/orden_meli_completa/<order_id>")
def debug_orden_meli_completa(order_id):
    """Vuelca la orden + el item asociado para encontrar dónde está el SKU."""
    if not session.get("logged"): return redirect("/")
    try:
        from mercadolibre import obtener_orden_meli, meli_headers, MELI_API_URL
        import requests as req

        # 1. Traer la orden completa
        orden = obtener_orden_meli(order_id)
        if not orden:
            return jsonify({"error": "Orden no encontrada"}), 404

        # 2. Para cada item, traer también el detalle del item
        items_completos = []
        for item in orden.get("order_items", []):
            item_data = item.get("item", {})
            item_id_meli = item_data.get("id", "")

            # Traer el ítem completo de la API
            item_detalle = None
            try:
                res = req.get(f"{MELI_API_URL}/items/{item_id_meli}",
                              headers=meli_headers(), timeout=15)
                if res.status_code == 200:
                    item_detalle = res.json()
            except Exception as e:
                item_detalle = {"error": str(e)}

            items_completos.append({
                "item_en_orden": item_data,
                "campos_top_nivel_item": list(item_data.keys()),
                "item_detalle_completo": item_detalle,
                "campos_top_nivel_detalle": list(item_detalle.keys()) if isinstance(item_detalle, dict) else None,
                # Buscar SKU en posibles ubicaciones del detalle
                "POSIBLES_SKU_DETECTADOS": {
                    "item.seller_custom_field": item_data.get("seller_custom_field"),
                    "item.seller_sku": item_data.get("seller_sku"),
                    "detalle.seller_custom_field": item_detalle.get("seller_custom_field") if isinstance(item_detalle, dict) else None,
                    "detalle.seller_sku": item_detalle.get("seller_sku") if isinstance(item_detalle, dict) else None,
                    "detalle.attributes": [
                        {"id": a.get("id"), "name": a.get("name"), "value": a.get("value_name")}
                        for a in (item_detalle.get("attributes", []) if isinstance(item_detalle, dict) else [])
                        if "SKU" in (a.get("id", "") + a.get("name", "")).upper()
                    ]
                }
            })

        return jsonify({
            "order_id": order_id,
            "campos_top_nivel_orden": list(orden.keys()),
            "items": items_completos
        })
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@app.route("/debug/mapeo_completo")
def debug_mapeo_completo():
    """Lista TODA la tabla sku_mapeo con todos los campos para detectar dónde está E10E11E12."""
    if not session.get("logged"): return redirect("/")
    try:
        from inventario import listar_sku_mapeo
        mapeo = listar_sku_mapeo()

        # Filtrar el SKU específico
        E10 = [m for m in mapeo if m.get("sku_lusync") == "E10E11E12"]

        # Buscar por 'MLC1584290001' en cualquier campo
        contiene_MLC = []
        for m in mapeo:
            for campo, valor in m.items():
                if isinstance(valor, str) and "MLC1584290001" in valor:
                    contiene_MLC.append({"sku_lusync": m.get("sku_lusync"), "campo": campo, "valor": valor})

        return jsonify({
            "total_filas_en_tabla": len(mapeo),
            "todos_los_campos_disponibles": list(mapeo[0].keys()) if mapeo else [],
            "sku_E10E11E12": E10,
            "filas_con_MLC1584290001": contiene_MLC,
            "primeras_5_filas_completas": mapeo[:5]
        })
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@app.route("/debug/mapeo_meli")
def debug_mapeo_meli():
    """Lista todos los mapeos de SKU MercadoLibre para detectar duplicados o errores."""
    if not session.get("logged"): return redirect("/")
    try:
        from inventario import listar_sku_mapeo
        mapeo = listar_sku_mapeo()

        # Filtrar solo los que tienen sku_mercadolibre
        con_meli = [m for m in mapeo if m.get("sku_mercadolibre")]

        # Detectar duplicados (mismo MLC apunta a distintos sku_lusync)
        meli_to_lusync = {}
        for m in con_meli:
            sku_meli = m.get("sku_mercadolibre", "").strip()
            sku_lusync = m.get("sku_lusync", "")
            if sku_meli not in meli_to_lusync:
                meli_to_lusync[sku_meli] = []
            meli_to_lusync[sku_meli].append(sku_lusync)

        duplicados = {k: v for k, v in meli_to_lusync.items() if len(v) > 1}

        return jsonify({
            "total_mapeos_con_meli": len(con_meli),
            "lista_completa": [
                {"sku_lusync": m.get("sku_lusync"),
                 "sku_mercadolibre": m.get("sku_mercadolibre"),
                 "nombre": m.get("nombre", "")}
                for m in con_meli
            ],
            "DUPLICADOS_DETECTADOS": duplicados,
            "buscar_MLC1584290001": [
                m for m in con_meli
                if m.get("sku_mercadolibre", "").strip() == "MLC1584290001"
            ]
        })
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@app.route("/debug/precios_productos")
def debug_precios_productos():
    """Diagnóstico: muestra los precios cargados en BD para los productos vendidos."""
    if not session.get("logged"): return redirect("/")
    try:
        from datetime import datetime, timedelta
        productos = cargar_productos()
        skus_vendidos_top = stats_top_productos_vendidos(
            (datetime.now().date() - timedelta(days=30)),
            datetime.now().date(),
            limite=20
        )
        skus_set = {p["sku"] for p in skus_vendidos_top}
        relevantes = [p for p in productos if p["sku"] in skus_set]
        return jsonify({
            "total_productos_bd": len(productos),
            "productos_con_precio_normal": sum(1 for p in productos if (p.get("precio_normal") or 0) > 0),
            "productos_con_precio_oferta": sum(1 for p in productos if (p.get("precio_oferta") or 0) > 0),
            "productos_sin_precio": sum(1 for p in productos if (p.get("precio_normal") or 0) <= 0 and (p.get("precio_oferta") or 0) <= 0),
            "skus_vendidos_recientemente_y_sus_precios": [
                {
                    "sku": p["sku"],
                    "nombre": p["nombre"],
                    "precio_normal": p.get("precio_normal", 0),
                    "precio_oferta": p.get("precio_oferta", 0),
                    "stock": p.get("stock", 0)
                }
                for p in relevantes
            ],
            "muestra_5_productos": [
                {
                    "sku": p["sku"],
                    "nombre": p["nombre"][:40],
                    "precio_normal": p.get("precio_normal", 0),
                    "precio_oferta": p.get("precio_oferta", 0)
                }
                for p in productos[:5]
            ]
        })
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500



# ── BODEGAS ─────────────────────────────────────────────────────────────────

@app.route("/bodegas")
def ruta_bodegas_listar():
    if not session.get("logged"): return jsonify({}), 401
    return jsonify(listar_bodegas())

@app.route("/bodegas/stock")
def ruta_bodegas_stock():
    """Tabla completa producto × bodega para administración."""
    if not session.get("logged"): return jsonify([]), 401
    return jsonify(listar_stock_completo())

@app.route("/bodegas/totales")
def ruta_bodegas_totales():
    """Totales por bodega para dashboard."""
    if not session.get("logged"): return jsonify({}), 401
    return jsonify(stock_total_por_bodega())

@app.route("/bodegas/stock_sku")
def ruta_bodegas_stock_sku():
    """Stock detallado de un SKU específico en todas las bodegas."""
    if not session.get("logged"): return jsonify({}), 401
    sku = request.args.get("sku", "").strip()
    if not sku: return jsonify({"error": "sku requerido"}), 400
    return jsonify(stock_por_bodega(sku))

@app.route("/bodegas/set_stock", methods=["POST"])
def ruta_bodegas_set_stock():
    """Establece stock de un SKU en una bodega (override)."""
    if not session.get("logged"): return jsonify({"ok": False}), 401
    data = request.json or {}
    sku = (data.get("sku") or "").strip()
    bodega = (data.get("bodega_codigo") or "").strip()
    cantidad = int(data.get("cantidad", 0))
    if not sku or not bodega:
        return jsonify({"ok": False, "error": "sku y bodega_codigo requeridos"})
    try:
        set_stock_bodega(sku, bodega, cantidad)
        registrar_audit(session.get("usuario","Sistema"), request.remote_addr,
                        "set_stock_bodega",
                        detalle=f"SKU {sku} en bodega {bodega} = {cantidad}")
        return jsonify({"ok": True, "nuevo_total": get_stock_bodega(sku, bodega)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/bodegas/actualizar_nombres", methods=["GET", "POST"])
def ruta_bodegas_actualizar_nombres():
    """Sobrescribe nombres de bodegas con los valores actuales del código.
    Útil cuando renombramos una bodega y la BD tiene el nombre viejo."""
    if not session.get("logged"): return jsonify({"ok": False}), 401
    try:
        actualizadas = actualizar_nombres_bodegas()
        registrar_audit(session.get("usuario","Sistema"), request.remote_addr,
                        "actualizar_nombres_bodegas",
                        detalle=f"{len(actualizadas)} bodegas refrescadas")
        return jsonify({"ok": True, "actualizadas": actualizadas})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/bodegas/matriz")
def ruta_bodegas_matriz():
    """Devuelve la matriz completa SKU × Bodega para la UI.
    Returns: {bodegas: [...], skus: [{sku, nombre, stocks: {bodega: cantidad}, total}]}
    """
    if not session.get("logged"): return jsonify({"error": "no autorizado"}), 401
    try:
        from inventario import get_conn, listar_bodegas
        bodegas = listar_bodegas()
        bodega_codigos = [b["codigo"] for b in bodegas]

        conn = get_conn(); cur = conn.cursor()
        # Obtener todos los productos
        cur.execute("SELECT sku, nombre FROM productos ORDER BY nombre")
        productos = cur.fetchall()

        # Obtener todo el stock_bodega en una sola query
        cur.execute("SELECT sku, bodega_codigo, cantidad FROM stock_bodega")
        stock_dict = {}
        for sku, bod, cant in cur.fetchall():
            stock_dict.setdefault(sku, {})[bod] = int(cant or 0)
        cur.close(); conn.close()

        # Construir matriz
        skus = []
        for sku, nombre in productos:
            stocks = {b: stock_dict.get(sku, {}).get(b, 0) for b in bodega_codigos}
            total = sum(stocks.values())
            skus.append({
                "sku": sku,
                "nombre": nombre or sku,
                "stocks": stocks,
                "total": total
            })

        return jsonify({
            "bodegas": bodegas,
            "skus": skus,
            "total_skus": len(skus)
        })
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@app.route("/bodegas/descargar_plantilla")
def ruta_bodegas_descargar_plantilla():
    """Genera Excel con todos los SKUs y columnas por bodega para llenar."""
    if not session.get("logged"): return redirect("/")
    try:
        from inventario import get_conn, listar_bodegas
        from openpyxl import Workbook
        from io import BytesIO
        from flask import send_file
        from datetime import datetime

        bodegas = listar_bodegas()

        conn = get_conn(); cur = conn.cursor()
        cur.execute("SELECT sku, nombre FROM productos ORDER BY nombre")
        productos = cur.fetchall()
        cur.execute("SELECT sku, bodega_codigo, cantidad FROM stock_bodega")
        stock_dict = {}
        for sku, bod, cant in cur.fetchall():
            stock_dict.setdefault(sku, {})[bod] = int(cant or 0)
        cur.close(); conn.close()

        wb = Workbook()
        ws = wb.active
        ws.title = "Stock por Bodega"

        # Encabezados: sku, nombre, + una columna por bodega
        headers = ["sku_lusync", "nombre"] + [b["codigo"] for b in bodegas]
        ws.append(headers)

        # Estilizar encabezado
        from openpyxl.styles import Font, PatternFill, Alignment
        for cell in ws[1]:
            cell.font = Font(bold=True)
            cell.fill = PatternFill("solid", fgColor="EEEDFE")
            cell.alignment = Alignment(horizontal="center")

        # Filas con datos
        for sku, nombre in productos:
            row = [sku, nombre or sku]
            for b in bodegas:
                row.append(stock_dict.get(sku, {}).get(b["codigo"], 0))
            ws.append(row)

        # Hoja con instrucciones
        ws2 = wb.create_sheet("Instrucciones")
        ws2.append(["Plantilla de Stock por Bodega — Lusync"])
        ws2.append([])
        ws2.append(["Cómo usar:"])
        ws2.append(["1. Edita las cantidades en cada celda según tu inventario real"])
        ws2.append(["2. NO modifiques los nombres de columnas (encabezados)"])
        ws2.append(["3. NO modifiques la columna 'sku_lusync' (es el identificador)"])
        ws2.append(["4. Guarda el archivo y súbelo desde el botón 'Importar Excel'"])
        ws2.append([])
        ws2.append(["Bodegas disponibles:"])
        for b in bodegas:
            ws2.append([b["codigo"], "→", b["nombre"]])

        # Generar archivo en memoria
        buf = BytesIO()
        wb.save(buf)
        buf.seek(0)

        nombre_archivo = f"plantilla_stock_bodegas_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
        return send_file(buf, as_attachment=True, download_name=nombre_archivo,
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@app.route("/bodegas/importar_excel", methods=["POST"])
def ruta_bodegas_importar_excel():
    """Recibe un Excel con stock por bodega y lo aplica.
    Procesa en lotes y registra el resultado en bodegas_imports."""
    if not session.get("logged"): return jsonify({"ok": False, "error": "no autorizado"}), 401
    try:
        from openpyxl import load_workbook
        from inventario import (set_stock_bodega, listar_bodegas,
                                crear_import_log, actualizar_import_log)

        if "archivo" not in request.files:
            return jsonify({"ok": False, "error": "Sin archivo"}), 400

        archivo = request.files["archivo"]
        wb = load_workbook(archivo, data_only=True)
        ws = wb.active

        # Leer encabezados (fila 1)
        headers = [str(c.value).strip() if c.value else "" for c in ws[1]]
        if "sku_lusync" not in headers:
            return jsonify({
                "ok": False,
                "error": "Columna 'sku_lusync' no encontrada en el Excel"
            }), 400

        idx_sku = headers.index("sku_lusync")
        idx_nombre = headers.index("nombre") if "nombre" in headers else None

        # Identificar columnas de bodegas (códigos válidos)
        bodegas_validas = {b["codigo"] for b in listar_bodegas()}
        cols_bodega = {}  # {col_idx: codigo_bodega}
        for i, h in enumerate(headers):
            if h in bodegas_validas:
                cols_bodega[i] = h

        if not cols_bodega:
            return jsonify({
                "ok": False,
                "error": f"No se encontraron columnas de bodegas válidas. Columnas válidas: {sorted(bodegas_validas)}"
            }), 400

        # Contar filas (descontando encabezado)
        total_filas = ws.max_row - 1

        # Crear log de import
        usuario = session.get("usuario", "Sistema")
        nombre_archivo = archivo.filename or "stock.xlsx"
        import_id = crear_import_log(nombre_archivo, usuario, total_filas)

        procesados = 0
        advertencias = 0
        errores = 0
        log_lines = []

        # Procesar fila por fila
        for fila_idx, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
            try:
                sku = str(row[idx_sku]).strip() if row[idx_sku] else ""
                if not sku:
                    errores += 1
                    log_lines.append(f"× Fila {fila_idx}: SKU vacío")
                    continue

                # Aplicar cada bodega
                cambios = []
                for col_idx, bod_codigo in cols_bodega.items():
                    val = row[col_idx]
                    if val is None or val == "":
                        continue
                    try:
                        cantidad = int(float(val))
                        if cantidad < 0:
                            advertencias += 1
                            log_lines.append(f"! Fila {fila_idx} {sku}: {bod_codigo} negativo, ajustado a 0")
                            cantidad = 0
                        set_stock_bodega(sku, bod_codigo, cantidad)
                        cambios.append(f"{bod_codigo}={cantidad}")
                    except (ValueError, TypeError):
                        advertencias += 1
                        log_lines.append(f"! Fila {fila_idx} {sku}: {bod_codigo} valor inválido '{val}'")

                if cambios:
                    procesados += 1
                    log_lines.append(f"✓ Fila {fila_idx} {sku}: {', '.join(cambios)}")

                # Actualizar progreso cada 25 filas
                if fila_idx % 25 == 0:
                    actualizar_import_log(import_id, procesados=procesados,
                                          advertencias=advertencias, errores=errores)
            except Exception as e:
                errores += 1
                log_lines.append(f"× Fila {fila_idx}: {str(e)[:100]}")

        # Estado final
        if errores > 0 and procesados == 0:
            estado_final = "error"
        elif advertencias > 0 or errores > 0:
            estado_final = "advertencias"
        else:
            estado_final = "ok"

        actualizar_import_log(import_id,
                              procesados=procesados,
                              advertencias=advertencias,
                              errores=errores,
                              estado=estado_final,
                              log="\n".join(log_lines[-200:]))  # últimas 200 líneas

        registrar_audit(usuario, request.remote_addr, "importar_stock_bodegas",
                        entidad="stock_bodega",
                        detalle=f"{procesados} OK, {advertencias} adv, {errores} err")

        return jsonify({
            "ok": True,
            "import_id": import_id,
            "procesados": procesados,
            "advertencias": advertencias,
            "errores": errores,
            "estado": estado_final,
            "ultimas_lineas": log_lines[-20:]
        })
    except Exception as e:
        import traceback
        return jsonify({"ok": False, "error": str(e), "trace": traceback.format_exc()}), 500


@app.route("/bodegas/imports")
def ruta_bodegas_imports():
    """Lista las últimas importaciones."""
    if not session.get("logged"): return jsonify({"imports": []}), 401
    try:
        from inventario import listar_imports_recientes
        return jsonify({"imports": listar_imports_recientes(limit=20)})
    except Exception as e:
        return jsonify({"imports": [], "error": str(e)}), 500


@app.route("/bodegas/imports/<int:import_id>")
def ruta_bodegas_import_detalle(import_id):
    """Detalle de una importación específica con log completo."""
    if not session.get("logged"): return jsonify({"error": "no autorizado"}), 401
    try:
        from inventario import obtener_import_log
        det = obtener_import_log(import_id)
        if not det:
            return jsonify({"error": "Import no encontrado"}), 404
        return jsonify(det)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/bodegas/guardar_lote", methods=["POST"])
def ruta_bodegas_guardar_lote():
    """Guarda múltiples cambios de stock en una sola llamada (edición inline en UI)."""
    if not session.get("logged"): return jsonify({"ok": False}), 401
    try:
        from inventario import set_stock_bodega
        data = request.json or {}
        cambios = data.get("cambios", [])  # [{sku, bodega_codigo, cantidad}, ...]

        guardados = 0
        errores = []
        for c in cambios:
            try:
                set_stock_bodega(c["sku"], c["bodega_codigo"], int(c["cantidad"]))
                guardados += 1
            except Exception as e:
                errores.append(f"{c.get('sku')}/{c.get('bodega_codigo')}: {e}")

        registrar_audit(session.get("usuario","Sistema"), request.remote_addr,
                        "editar_stock_bodegas", entidad="stock_bodega",
                        detalle=f"{guardados} celdas actualizadas")

        return jsonify({"ok": True, "guardados": guardados, "errores": errores})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/bodegas/transferir", methods=["POST"])
def ruta_bodegas_transferir():
    """Mueve stock de una bodega a otra para un SKU."""
    if not session.get("logged"): return jsonify({"ok": False}), 401
    data = request.json or {}
    sku = (data.get("sku") or "").strip()
    desde = (data.get("desde") or "").strip()
    hasta = (data.get("hasta") or "").strip()
    cantidad = int(data.get("cantidad", 0))
    if not sku or not desde or not hasta or cantidad <= 0:
        return jsonify({"ok": False, "error": "Faltan parámetros"})

    stock_origen = get_stock_bodega(sku, desde)
    if stock_origen < cantidad:
        return jsonify({"ok": False, "error": f"Stock insuficiente en {desde}: {stock_origen}"})

    try:
        ajustar_stock_bodega(sku, desde, -cantidad)
        ajustar_stock_bodega(sku, hasta, cantidad)
        registrar_audit(session.get("usuario","Sistema"), request.remote_addr,
                        "transferir_stock",
                        detalle=f"SKU {sku}: {cantidad}u {desde} → {hasta}")
        return jsonify({"ok": True,
                        "stock_desde": get_stock_bodega(sku, desde),
                        "stock_hasta": get_stock_bodega(sku, hasta)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── DASHBOARD STATS ─────────────────────────────────────────────────────────

def _parse_rango_fechas():
    """Lee desde/hasta de query params, default últimos 7 días."""
    from datetime import datetime, timedelta
    hoy = datetime.now().date()
    desde_str = request.args.get("desde", "").strip()
    hasta_str = request.args.get("hasta", "").strip()
    try:
        hasta = datetime.strptime(hasta_str, "%Y-%m-%d").date() if hasta_str else hoy
    except: hasta = hoy
    try:
        desde = datetime.strptime(desde_str, "%Y-%m-%d").date() if desde_str else (hoy - timedelta(days=6))
    except: desde = hoy - timedelta(days=6)
    return desde, hasta


@app.route("/stats/kpis")
def ruta_stats_kpis():
    if not session.get("logged"): return jsonify({}), 401
    desde, hasta = _parse_rango_fechas()
    return jsonify(stats_kpis_dashboard(desde, hasta))


@app.route("/stats/ventas_por_canal")
def ruta_stats_ventas_canal():
    if not session.get("logged"): return jsonify([]), 401
    desde, hasta = _parse_rango_fechas()
    return jsonify(stats_ventas_por_canal_dia(desde, hasta))


@app.route("/stats/top_productos")
def ruta_stats_top():
    if not session.get("logged"): return jsonify([]), 401
    desde, hasta = _parse_rango_fechas()
    limite = int(request.args.get("limite", 10))
    return jsonify(stats_top_productos_vendidos(desde, hasta, limite))


@app.route("/stats/movimientos_dia")
def ruta_stats_movs():
    if not session.get("logged"): return jsonify([]), 401
    desde, hasta = _parse_rango_fechas()
    return jsonify(stats_movimientos_dia(desde, hasta))


@app.route("/stats/distribucion_stock")
def ruta_stats_distribucion():
    if not session.get("logged"): return jsonify([]), 401
    return jsonify(stats_distribucion_stock_canal())


# ── MERCADOLIBRE ─────────────────────────────────────────────────────────────

@app.route("/mercadolibre/conectar")
def ruta_meli_conectar():
    """Inicia el flujo OAuth2 redirigiendo al usuario al login de MercadoLibre."""
    if not session.get("logged"): return redirect("/")
    try:
        from mercadolibre import construir_url_autorizacion
        url = construir_url_autorizacion(state=session.get("usuario", "lusync"))
        return redirect(url)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/mercadolibre/callback")
def ruta_meli_callback():
    """Recibe el code de MercadoLibre tras autorización del usuario."""
    code = request.args.get("code")
    error = request.args.get("error")
    if error:
        return f"<h2>Error de MercadoLibre</h2><p>{error}: {request.args.get('error_description','')}</p>", 400
    if not code:
        return "<h2>Falta el parámetro code</h2>", 400

    try:
        from mercadolibre import intercambiar_codigo_por_token
        import time
        data = intercambiar_codigo_por_token(code)
        set_meli_auth({
            "access_token":  data["access_token"],
            "refresh_token": data.get("refresh_token", ""),
            "user_id":       data.get("user_id"),
            "expires_at":    int(time.time()) + int(data.get("expires_in", 21600))
        })
        # Redirigir al panel con confirmación
        return redirect("/panel?meli=ok")
    except Exception as e:
        import traceback
        return f"<h2>Error conectando MercadoLibre</h2><pre>{str(e)}\n\n{traceback.format_exc()}</pre>", 500


@app.route("/mercadolibre/desconectar", methods=["POST"])
def ruta_meli_desconectar():
    """Borra el token guardado."""
    if not session.get("logged"): return jsonify({"ok": False}), 401
    borrar_meli_auth()
    return jsonify({"ok": True})


@app.route("/mercadolibre/estado")
def ruta_meli_estado():
    """Devuelve el estado de la conexión (para mostrar en el panel)."""
    if not session.get("logged"): return jsonify({}), 401
    try:
        from mercadolibre import verificar_conexion_meli
        return jsonify(verificar_conexion_meli())
    except Exception as e:
        return jsonify({"conectado": False, "error": str(e)})


@app.route("/mercadolibre/publicaciones")
def ruta_meli_publicaciones():
    """Lista las publicaciones del seller (para mapeo de SKUs)."""
    if not session.get("logged"): return jsonify({}), 401
    try:
        from mercadolibre import obtener_publicaciones_meli
        offset = int(request.args.get("offset", 0))
        return jsonify(obtener_publicaciones_meli(limite=50, offset=offset) or {"items":[],"total":0})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/mercadolibre/sync_ordenes")
def ruta_meli_sync_ordenes():
    """Descarga órdenes históricas de MercadoLibre y descuenta stock de bodega correcta.
    Optimizado para no agotar RAM en Render."""
    if not session.get("logged"): return jsonify({"ok": False}), 401
    try:
        from mercadolibre import obtener_ordenes_meli, obtener_sku_de_item_meli
        from inventario import (descontar_venta_inteligente, detectar_fulfillment_meli,
                                listar_sku_mapeo)
        from datetime import datetime
        registrar_audit(session.get("usuario","Sistema"), request.remote_addr,
                        "sync_meli", detalle="Sync manual órdenes MercadoLibre")

        productos_dict = {p["sku"]: p for p in cargar_productos()}
        nuevas = 0
        errores = []
        log = []

        # Cache de SKUs ya resueltos via /items/{id} para no re-consultar la misma publicación
        cache_sku_por_item = {}

        # ⚠️ PROTECCIÓN RAM: traer máx 2 páginas de 50 = 100 órdenes por request
        # Para cargar más históricos, llamar el endpoint varias veces o usar ?paginas=4
        max_paginas = int(request.args.get("paginas", 2))
        log.append(f"Configuración: máx {max_paginas} páginas × 50 = {max_paginas*50} órdenes")

        for pagina in range(max_paginas):
            offset = pagina * 50
            try:
                ordenes = obtener_ordenes_meli(limit=50, offset=offset)
                log.append(f"Página {pagina+1} (offset {offset}): {len(ordenes)} órdenes")
                if not ordenes:
                    break
                for o in ordenes:
                    order_id = str(o.get("id", ""))
                    estado = o.get("status", "")
                    if estado not in ("paid", "confirmed"):
                        continue
                    meli_key = f"MELI-{order_id}"
                    if orden_ya_procesada_texto(meli_key):
                        continue
                    marcar_orden_procesada_texto(meli_key)

                    # Detectar Full vs Seller
                    es_full = detectar_fulfillment_meli(o)
                    tipo_str = "FULL" if es_full else "Seller"

                    for item in o.get("order_items", []):
                        item_data = item.get("item", {})
                        item_id = item_data.get("id", "")
                        # Leer SKU: seller_sku primero, luego seller_custom_field
                        sku_seller = (
                            (item_data.get("seller_sku") or "").strip()
                            or (item_data.get("seller_custom_field") or "").strip()
                        )
                        # Si vacío, consultar detalle (con cache)
                        if not sku_seller and item_id:
                            if item_id in cache_sku_por_item:
                                sku_seller = cache_sku_por_item[item_id]
                            else:
                                try:
                                    sku_resuelto = obtener_sku_de_item_meli(item_id)
                                    if sku_resuelto:
                                        sku_seller = sku_resuelto
                                        cache_sku_por_item[item_id] = sku_resuelto
                                        log.append(f"  SKU resuelto desde item detail: {sku_seller}")
                                    else:
                                        cache_sku_por_item[item_id] = None
                                except Exception as e:
                                    log.append(f"  Error consultando item: {e}")
                        qty = int(item.get("quantity", 1) or 1)

                        # Buscar SKU en mapeo (item_id o sku_seller)
                        sku_lusync = None
                        try:
                            for fila in listar_sku_mapeo():
                                sku_mapped = (fila.get("sku_mercadolibre") or "").strip()
                                if sku_mapped and (sku_mapped == item_id or sku_mapped == sku_seller):
                                    sku_lusync = fila.get("sku_lusync")
                                    break
                        except: pass
                        if not sku_lusync and sku_seller:
                            sku_lusync = sku_seller

                        if not sku_lusync or sku_lusync not in productos_dict:
                            log.append(f"Orden {order_id}: SKU '{sku_lusync or item_id}' no encontrado")
                            continue

                        resultado = descontar_venta_inteligente(
                            sku=sku_lusync,
                            cantidad=qty,
                            canal="MercadoLibre",
                            fulfillment=es_full,
                            orden_id=order_id,
                            motivo=f"Venta MercadoLibre {tipo_str}",
                            usuario="Sistema"
                        )
                        log.append(f"{order_id} {tipo_str}: {sku_lusync} -{qty} desde {resultado['bodega']}")

                        # Sync a otros canales SOLO si fue Seller (afectó Central)
                        if not es_full:
                            try:
                                from bodegas_logic import sincronizar_stock_a_marketplaces
                                sincronizar_stock_a_marketplaces(sku_lusync, excepto=["mercadolibre"])
                            except Exception as e:
                                log.append(f"  Sync cruzado falló: {e}")
                    nuevas += 1

                # Liberar memoria entre páginas
                del ordenes
                import gc
                gc.collect()
            except Exception as e:
                errores.append(f"página {pagina+1}: {str(e)}")
                log.append(f"Página {pagina+1}: ERROR {str(e)}")
                break

        log.append(f"Cache SKU items consultados: {len(cache_sku_por_item)} ítems")
        return jsonify({
            "ok": True,
            "nuevas_ordenes": nuevas,
            "errores": errores,
            "log": log
        })
    except Exception as e:
        import traceback
        return jsonify({"ok": False, "error": str(e), "trace": traceback.format_exc()}), 500


@app.route("/mercadolibre/reclasificar_bodegas", methods=["GET", "POST"])
def ruta_meli_reclasificar_bodegas():
    """Re-clasifica las ventas históricas de MercadoLibre verificando si fueron Full o Seller.
    Para cada movimiento MELI ya registrado, consulta el shipment y mueve el stock
    de bodega CENTRAL a MELI_FULL si era Full (y viceversa).

    Útil para corregir descuentos hechos con el detector viejo que no consultaba shipments."""
    if not session.get("logged"): return jsonify({"ok": False}), 401
    try:
        from mercadolibre import obtener_orden_meli
        from inventario import (get_conn, ajustar_stock_bodega, get_stock_bodega,
                                determinar_bodega_para_canal)
        from bodegas_logic import detectar_fulfillment_meli
        registrar_audit(session.get("usuario","Sistema"), request.remote_addr,
                        "reclasificar_bodegas_meli",
                        detalle="Re-clasificar ventas MELI por bodega correcta")

        max_ordenes = int(request.args.get("max", 100))
        dry_run = request.args.get("dry_run", "0") == "1"
        log = []
        movidas = 0
        ya_correctas = 0
        sin_orden = 0
        errores = []

        # Buscar movimientos MELI únicos por (orden_id, sku) tomando el más reciente
        conn = get_conn(); cur = conn.cursor()
        cur.execute("""SELECT orden_id, sku, cantidad, bodega_codigo
                       FROM (
                           SELECT orden_id, sku, cantidad, bodega_codigo, id,
                                  ROW_NUMBER() OVER (PARTITION BY orden_id, sku ORDER BY id DESC) AS rn
                           FROM movimientos
                           WHERE canal = 'MercadoLibre'
                             AND tipo = 'salida'
                             AND orden_id IS NOT NULL
                             AND orden_id != ''
                       ) sub
                       WHERE rn = 1
                       ORDER BY id DESC
                       LIMIT %s""", (max_ordenes,))
        rows = cur.fetchall()
        cur.close(); conn.close()
        log.append(f"Revisando {len(rows)} movimientos MELI")

        for orden_id, sku, cantidad, bodega_actual in rows:
            try:
                orden = obtener_orden_meli(orden_id)
                if not orden:
                    sin_orden += 1
                    continue

                es_full = detectar_fulfillment_meli(orden)
                bodega_correcta = determinar_bodega_para_canal("MercadoLibre", fulfillment=es_full)

                if bodega_actual == bodega_correcta:
                    ya_correctas += 1
                    continue

                # Hay que mover: reintegrar a la bodega vieja, descontar de la nueva
                if dry_run:
                    log.append(f"[DRY-RUN] {orden_id} {sku} ({cantidad}u): {bodega_actual} → {bodega_correcta} ({'Full' if es_full else 'Seller'})")
                else:
                    # Reintegrar a bodega actual (deshacer descuento original)
                    ajustar_stock_bodega(sku, bodega_actual, cantidad)
                    # Descontar de bodega correcta
                    ajustar_stock_bodega(sku, bodega_correcta, -cantidad)

                    # Actualizar el bodega_codigo en el movimiento
                    conn2 = get_conn(); cur2 = conn2.cursor()
                    cur2.execute("""UPDATE movimientos SET bodega_codigo=%s
                                   WHERE canal='MercadoLibre' AND tipo='salida'
                                     AND orden_id=%s AND sku=%s""",
                                (bodega_correcta, orden_id, sku))
                    conn2.commit()
                    cur2.close(); conn2.close()
                    log.append(f"✓ {orden_id} {sku} ({cantidad}u): {bodega_actual} → {bodega_correcta}")
                movidas += 1
            except Exception as e:
                errores.append(f"{orden_id}: {str(e)}")

        return jsonify({
            "ok": True,
            "total_revisadas": len(rows),
            "ya_correctas": ya_correctas,
            "movidas": movidas,
            "sin_orden_en_meli": sin_orden,
            "dry_run": dry_run,
            "errores": errores[:10],
            "log": log[:60]
        })
    except Exception as e:
        import traceback
        return jsonify({"ok": False, "error": str(e), "trace": traceback.format_exc()}), 500


@app.route("/mercadolibre/forzar_sync_todos", methods=["POST"])
def ruta_meli_forzar_todos():
    """Re-envía el stock de todos los SKUs mapeados a MercadoLibre."""
    if not session.get("logged"): return jsonify({"ok": False}), 401
    try:
        from mercadolibre import actualizar_stock_meli
        productos = {p["sku"]: p for p in cargar_productos()}
        mapeo = listar_sku_mapeo()
        enviados = 0
        fallidos = 0
        for fila in mapeo:
            sku_lusync = fila.get("sku_lusync", "")
            sku_meli = (fila.get("sku_mercadolibre", "") or "").strip()
            if not sku_meli or sku_lusync not in productos:
                continue
            stock = productos[sku_lusync]["stock"]
            if actualizar_stock_meli(sku_lusync, stock):
                enviados += 1
            else:
                fallidos += 1
        return jsonify({"ok": True, "enviados": enviados, "fallidos": fallidos})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/mercadolibre/webhook", methods=["GET", "POST"])
def ruta_meli_webhook():
    """Endpoint para que MercadoLibre envíe notificaciones automáticas (sin auth)."""
    # GET solo para que MELI valide que la URL responde
    if request.method == "GET":
        return jsonify({"status": "ok"}), 200
    try:
        from mercadolibre import procesar_webhook_meli
        payload = request.json or {}
        ok = procesar_webhook_meli(payload)
        # MELI espera 200 rápido; si tarda mucho reintenta
        return jsonify({"ok": ok}), 200
    except Exception as e:
        print(f"[MELI Webhook] Error: {e}")
        # Importante: devolver 200 igual para que MELI no reintente infinito
        return jsonify({"ok": False, "error": str(e)}), 200


@app.route("/debug/meli_test_conexion")
def debug_meli_test():
    """Diagnóstico de conexión con MercadoLibre."""
    if not session.get("logged"): return redirect("/")
    try:
        from mercadolibre import verificar_conexion_meli
        import os
        return jsonify({
            "app_id_configurado": bool(os.environ.get("MERCADOLIBRE_APP_ID")),
            "secret_configurado": bool(os.environ.get("MERCADOLIBRE_CLIENT_SECRET")),
            "redirect_uri": os.environ.get("MERCADOLIBRE_REDIRECT_URI", "(default)"),
            "estado": verificar_conexion_meli()
        })
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


# ── ALERTAS ─────────────────────────────────────────────────────────────────

@app.route("/alertas")
def ruta_alertas():
    if not session.get("logged"): return redirect("/")
    solo_no = request.args.get("solo_no_leidas", "false").lower() == "true"
    return jsonify(listar_alertas(limite=100, solo_no_leidas=solo_no))

@app.route("/alertas/contador")
def ruta_alertas_contador():
    if not session.get("logged"): return jsonify({"count": 0})
    return jsonify({"count": contar_alertas_no_leidas()})

@app.route("/alertas/leer/<int:alerta_id>", methods=["POST"])
def ruta_alerta_leer(alerta_id):
    if not session.get("logged"): return jsonify({"ok": False}), 401
    marcar_alerta_leida(alerta_id)
    return jsonify({"ok": True})

@app.route("/alertas/leer_todas", methods=["POST"])
def ruta_alertas_leer_todas():
    if not session.get("logged"): return jsonify({"ok": False}), 401
    marcar_todas_leidas()
    return jsonify({"ok": True})

@app.route("/alertas/config", methods=["GET", "POST"])
def ruta_alertas_config():
    if not session.get("logged"): return jsonify({}), 401
    if request.method == "POST":
        data = request.json or {}
        # Filtrar solo claves válidas
        permitidas = {"smtp_host","smtp_port","smtp_user","smtp_password",
                      "smtp_from","destinatarios","notif_cancelaciones","notif_errores_api"}
        filtered = {k: v for k, v in data.items() if k in permitidas}
        set_alertas_config(filtered)
        return jsonify({"ok": True})
    cfg = get_alertas_config()
    # No exponer la contraseña SMTP en GET (solo si está configurada)
    if cfg.get("smtp_password"):
        cfg["smtp_password"] = "********"
    return jsonify(cfg)

@app.route("/alertas/test_email", methods=["POST"])
def ruta_alertas_test():
    """Envía un email de prueba con la configuración actual."""
    if not session.get("logged"): return jsonify({"ok": False}), 401
    try:
        crear_alerta(
            tipo="test",
            canal="Sistema",
            titulo="Email de prueba Lusync",
            mensaje="Si recibes este correo, las notificaciones por email están funcionando correctamente."
        )
        return jsonify({"ok": True, "mensaje": "Alerta creada y email enviado (revisa configuración SMTP en logs si no llega)"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
