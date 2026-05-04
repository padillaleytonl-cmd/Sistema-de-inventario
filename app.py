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
from ripley import ripley_bp
from falabella import falabella_bp
app.register_blueprint(walmart_bp)
app.register_blueprint(paris_bp)
app.register_blueprint(ripley_bp)
app.register_blueprint(falabella_bp)

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
    """Envía precios actuales (precio_normal/oferta) tal cual a Walmart, sin transformaciones.
    Las comisiones, márgenes y redondeos se manejarán en el módulo Motor de Precios."""
    if not session.get("logged"):
        return {"error": "no autorizado"}, 401
    productos = cargar_productos()
    enviados = 0
    fallidos = 0
    log = []
    for p in productos:
        if not p.get("sku") or not p.get("precio_normal", 0) > 0:
            continue
        # Enviar precio tal cual: si hay oferta, usar oferta; si no, precio normal
        precio = p["precio_oferta"] if p.get("precio_oferta", 0) > 0 else p["precio_normal"]
        if actualizar_precio_walmart(p["sku"], precio):
            enviados += 1
            log.append(f"✓ {p['sku']} → ${precio}")
        else:
            fallidos += 1
            log.append(f"× {p['sku']} falló")
    return {"ok": True, "enviados": enviados, "fallidos": fallidos, "log": log[:30]}

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


@app.route("/devoluciones/buscar_orden", methods=["GET", "POST"])
def devoluciones_buscar_orden():
    """Busca una orden de manera inteligente:
    1) Primero en BD local (movimientos) — si la encuentra, ya sabe el canal
    2) Si no está local, intenta detectar marketplace por formato del N°
    3) Devuelve items + canal detectado para que el usuario seleccione qué devolver
    """
    if not session.get("logged"):
        return jsonify({"error": "no autorizado"}), 401

    if request.method == "POST":
        numero = (request.json or {}).get("numero", "").strip()
    else:
        numero = request.args.get("numero", "").strip()

    if not numero:
        return jsonify({"error": "Número de orden requerido"}), 400

    # Limpiar el número (quitar espacios, prefijos opcionales que el usuario haya puesto)
    numero_limpio = numero.upper().strip()
    # Si pegó algo como "ML-2000012757", quitar el prefijo
    for prefix in ("ML-", "FA-", "WM-", "PA-", "RP-", "WC-"):
        if numero_limpio.startswith(prefix):
            numero_limpio = numero_limpio[len(prefix):]
            break

    # ── Paso 1: Buscar en BD local ─────────────────────────────────
    conn = __import__('psycopg2').connect(__import__('os').environ.get("DATABASE_URL"))
    cur = conn.cursor()
    cur.execute("""
        SELECT m.sku, m.nombre, m.canal, ABS(m.cantidad) as cantidad,
               TO_CHAR(m.fecha, 'DD/MM/YYYY HH24:MI') as fecha,
               m.bodega_codigo, m.orden_id
        FROM movimientos m
        WHERE m.orden_id = %s AND m.tipo = 'salida'
        ORDER BY m.sku
    """, (numero_limpio,))
    rows = cur.fetchall()
    cur.close(); conn.close()

    if rows:
        # ¡Encontrada en BD! Devolvemos toda la info al toque
        canal_detectado = rows[0][2] or "Desconocido"
        items = []
        for r in rows:
            items.append({
                "sku": r[0],
                "nombre": r[1],
                "canal": r[2],
                "cantidad": int(r[3] or 1),
                "fecha": r[4],
                "bodega_origen": r[5] or "CENTRAL",
                "orden_id": r[6]
            })
        return jsonify({
            "encontrada": True,
            "fuente": "bd_local",
            "marketplace": canal_detectado,
            "marketplace_codigo": _normalizar_marketplace(canal_detectado),
            "numero_orden": numero_limpio,
            "items": items,
            "total_items": len(items)
        })

    # ── Paso 2: No encontrada local — detectar por formato e intentar API ──
    marketplace_sugerido = _detectar_marketplace_por_formato(numero_limpio)

    return jsonify({
        "encontrada": False,
        "fuente": "no_encontrada",
        "marketplace_sugerido": marketplace_sugerido,
        "numero_orden": numero_limpio,
        "mensaje": (
            f"Orden no encontrada en BD local. "
            f"Posible marketplace: {marketplace_sugerido or 'desconocido'}. "
            f"Puedes registrar la devolución manualmente seleccionando el SKU."
        )
    })


def _detectar_marketplace_por_formato(numero):
    """Detecta el marketplace por el formato del número de orden."""
    n = numero.strip().upper()
    # Reglas en orden de especificidad
    if n.endswith("-A") or n.endswith("-B"):
        return "Ripley"
    if n.startswith("CLP") or n.startswith("PAR"):
        return "Paris"
    if n.startswith("PO") or n.startswith("WMT"):
        return "Walmart"
    # Por longitud numérica
    if n.isdigit():
        if len(n) >= 14:
            return "MercadoLibre"  # 16 dígitos típicos
        if 7 <= len(n) <= 9:
            return "Falabella"
        if len(n) <= 6:
            return "WooCommerce"
    return None


def _normalizar_marketplace(canal_raw):
    """Convierte el nombre del canal en código estándar (lowercase sin espacios)"""
    if not canal_raw: return ""
    c = canal_raw.lower().strip()
    if "mercadolibre" in c or "meli" in c: return "mercadolibre"
    if "paris" in c or "parís" in c: return "paris"
    if "walmart" in c or "wfs" in c: return "walmart"
    if "woo" in c: return "woocommerce"
    if "ripley" in c: return "ripley"
    if "falabella" in c: return "falabella"
    if "hites" in c: return "hites"
    return c


# ════════════════════════════════════════════════════════════════════════════
# DEVOLUCIONES — SISTEMA AVANZADO con tipificación, deadline 72h hábiles y etiqueta
# ════════════════════════════════════════════════════════════════════════════

@app.route("/devoluciones/registrar_avanzado", methods=["POST"])
def devoluciones_registrar_avanzado():
    """Registra una devolución con el flujo completo:
    - OC asociada (manual o detectada)
    - Producto seleccionado de la OC
    - Tipificación (buen_estado / reparable / dado_de_baja / reembolsado / reenviado)
    - Motivo/notas
    - Calcula deadline = ahora + 72h hábiles
    - Genera código DEV-YYYY-NNNN

    Body JSON:
    {
      "oc_origen": "2000012654022175",
      "marketplace": "MercadoLibre",
      "sku": "SDCMM001",
      "nombre": "Silla de comer Menta",
      "cantidad": 1,
      "tipificacion": "dado_de_baja",
      "motivo_texto": "Carcasa fracturada lateral derecho...",
      "responsable": "Luis"
    }
    """
    if not session.get("logged"):
        return jsonify({"error": "no autorizado"}), 401

    try:
        from inventario import (crear_devolucion, generar_codigo_dev,
                                ajustar_stock_dev, registrar_audit, get_conn)
        from feriados import calcular_deadline_habil
        from datetime import datetime
        import json

        data = request.json or {}
        oc = (data.get("oc_origen") or "").strip()
        sku = (data.get("sku") or "").strip()
        tipificacion = (data.get("tipificacion") or "").strip()
        motivo_texto = data.get("motivo_texto", "").strip()
        cantidad = int(data.get("cantidad", 1))
        nombre = data.get("nombre", "")
        marketplace = data.get("marketplace", "")
        responsable = session.get("usuario", "Sistema")

        # Validaciones
        if not oc:
            return jsonify({"ok": False, "error": "OC origen requerida"}), 400
        if not sku:
            return jsonify({"ok": False, "error": "SKU requerido"}), 400
        tipificaciones_validas = {
            "buen_estado", "reenviado", "reparable",
            "dado_de_baja", "reembolsado"
        }
        if tipificacion not in tipificaciones_validas:
            return jsonify({"ok": False, "error": f"Tipificación inválida. Válidas: {tipificaciones_validas}"}), 400
        # Para casos críticos exigir motivo
        if tipificacion in ("reparable", "dado_de_baja") and not motivo_texto:
            return jsonify({"ok": False, "error": "Motivo obligatorio para reparable/dado_de_baja"}), 400

        ahora = datetime.now()
        deadline = calcular_deadline_habil(ahora, dias_habiles=3)
        codigo = generar_codigo_dev()

        # Snapshot de la orden (por si después se necesita)
        orden_snapshot = json.dumps({
            "oc_origen": oc,
            "marketplace": marketplace,
            "fecha_registro": ahora.isoformat()
        })

        # Crear devolución (usa función existente)
        dev_data = {
            "oc_origen": oc,
            "canal": marketplace,
            "sku": sku,
            "nombre": nombre,
            "cantidad": cantidad,
            "motivo_cliente": motivo_texto[:500],
            "estado_producto": tipificacion,
            "responsable": responsable,
            "estado": _estado_segun_tipificacion(tipificacion)
        }
        dev_id = crear_devolucion(dev_data)
        if not dev_id:
            return jsonify({"ok": False, "error": "No se pudo crear devolución en BD"}), 500

        # Actualizar campos avanzados
        conn = get_conn(); cur = conn.cursor()
        cur.execute("""
            UPDATE devoluciones SET
                codigo = COALESCE(codigo, %s),
                tipificacion = %s,
                motivo_texto = %s,
                usuario_revisor = %s,
                fecha_deadline = %s,
                fecha_recepcion = %s,
                origen_datos = 'manual',
                orden_data_json = %s
            WHERE id = %s
        """, (codigo, tipificacion, motivo_texto, responsable,
              deadline, ahora, orden_snapshot, dev_id))
        conn.commit()
        cur.close(); conn.close()

        # Aplicar impacto en stock según tipificación
        impacto = _aplicar_impacto_devolucion(tipificacion, sku, cantidad, dev_id)

        registrar_audit(responsable, request.remote_addr,
                        "devolucion_avanzada",
                        entidad="devoluciones", entidad_id=str(dev_id),
                        detalle=f"OC {oc} · {sku} · {tipificacion} · {impacto}")

        return jsonify({
            "ok": True,
            "id": dev_id,
            "codigo": codigo,
            "tipificacion": tipificacion,
            "deadline_iso": deadline.isoformat(),
            "deadline_legible": deadline.strftime("%d/%m/%Y %H:%M"),
            "impacto_stock": impacto,
            "estado": _estado_segun_tipificacion(tipificacion)
        })
    except Exception as e:
        import traceback
        return jsonify({"ok": False, "error": str(e), "trace": traceback.format_exc()}), 500


def _estado_segun_tipificacion(tipif):
    """Mapea tipificación a estado interno"""
    return {
        "buen_estado": "aceptada_reintegrada",
        "reenviado": "aceptada_reenviada",
        "reparable": "en_reparacion",
        "dado_de_baja": "dada_de_baja",
        "reembolsado": "reembolsada"
    }.get(tipif, "pendiente")


def _aplicar_impacto_devolucion(tipificacion, sku, cantidad, dev_id):
    """Aplica el impacto en stock según la tipificación."""
    try:
        from inventario import get_conn, ajustar_stock_dev
        if tipificacion == "buen_estado":
            # Reintegra al stock CENTRAL
            ajustar_stock_dev(sku, cantidad, dev_id, "reintegro_buen_estado")
            return f"Reintegrado +{cantidad} a CENTRAL"
        elif tipificacion in ("reenviado", "reembolsado", "dado_de_baja", "reparable"):
            # No reintegra
            return "Sin impacto en stock (no reintegrable)"
        return "Sin impacto"
    except Exception as e:
        return f"Error aplicando impacto: {e}"


@app.route("/devoluciones/<int:dev_id>/etiqueta_pdf")
def devoluciones_etiqueta_pdf(dev_id):
    """Genera un PDF con código de barras para identificar el producto físico.

    Solo aplica para tipificaciones: reparable, dado_de_baja, reembolsado
    """
    if not session.get("logged"):
        return "No autorizado", 401
    try:
        from inventario import get_devolucion
        from io import BytesIO
        from flask import send_file
        from reportlab.lib.pagesizes import A6, landscape
        from reportlab.pdfgen import canvas
        from reportlab.lib.units import mm
        from reportlab.lib.colors import HexColor, black, white
        import barcode
        from barcode.writer import ImageWriter

        dev = get_devolucion(dev_id=dev_id)
        if not dev:
            return "Devolución no encontrada", 404

        tipif = dev.get("tipificacion", "")
        codigo = dev.get("codigo", f"DEV-{dev_id:06d}")
        sku = dev.get("sku", "")
        nombre = dev.get("nombre", "") or sku
        oc = dev.get("oc_origen", "")
        canal = dev.get("canal", "")
        motivo = dev.get("motivo_texto", "") or dev.get("motivo_cliente", "")
        fecha_recepcion = dev.get("fecha_recepcion") or dev.get("fecha_solicitud")

        # Configurar título y color según tipificación
        titulo_color, titulo_text = {
            "dado_de_baja": (HexColor("#7f1d1d"), "DAR DE BAJA"),
            "reparable": (HexColor("#92400e"), "EN REPARACION"),
            "reembolsado": (HexColor("#1e3a8a"), "REEMBOLSADO"),
            "buen_estado": (HexColor("#065f46"), "REINTEGRADO"),
            "reenviado": (HexColor("#854F0B"), "REENVIADO")
        }.get(tipif, (black, "DEVOLUCION"))

        # Generar código de barras como imagen en memoria
        EAN = barcode.get_barcode_class('code128')
        barcode_io = BytesIO()
        EAN(codigo, writer=ImageWriter()).write(barcode_io, options={
            "module_width": 0.4,
            "module_height": 12.0,
            "font_size": 10,
            "text_distance": 4.0,
            "quiet_zone": 2.0
        })
        barcode_io.seek(0)
        # Guardar a temporal para reportlab
        import tempfile
        with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
            tmp.write(barcode_io.read())
            barcode_path = tmp.name

        # Crear PDF
        pdf_buf = BytesIO()
        # Etiqueta tamaño A6 horizontal (10x15cm aprox)
        c = canvas.Canvas(pdf_buf, pagesize=landscape(A6))
        ancho, alto = landscape(A6)

        # Header
        c.setFillColor(black)
        c.setFont("Helvetica-Bold", 7)
        c.drawString(8*mm, alto - 8*mm, "DEVOLUCIÓN")
        c.setFillColor(titulo_color)
        c.setFont("Helvetica-Bold", 14)
        c.drawString(8*mm, alto - 14*mm, titulo_text)

        # Línea divisoria
        c.setStrokeColor(black)
        c.setLineWidth(1)
        c.line(8*mm, alto - 17*mm, ancho - 8*mm, alto - 17*mm)

        # Datos lado izquierdo
        c.setFillColor(black)
        c.setFont("Helvetica-Bold", 8)
        y = alto - 22*mm
        info_lines = [
            ("SKU:", sku),
            ("Producto:", nombre[:35]),
            ("Orden:", f"{oc} ({canal})"),
            ("Recepción:", fecha_recepcion.strftime("%d/%m/%Y %H:%M") if fecha_recepcion and hasattr(fecha_recepcion, 'strftime') else "—")
        ]
        for label, value in info_lines:
            c.setFont("Helvetica-Bold", 7)
            c.drawString(8*mm, y, label)
            c.setFont("Helvetica", 7)
            c.drawString(28*mm, y, str(value))
            y -= 4*mm

        # Motivo (en caja con fondo)
        if motivo:
            y_motivo_top = y - 2*mm
            c.setFillColor(HexColor("#fef2f2") if tipif == "dado_de_baja" else HexColor("#fef3c7"))
            c.rect(8*mm, y_motivo_top - 18*mm, ancho - 16*mm, 18*mm, fill=1, stroke=0)
            c.setFillColor(titulo_color)
            c.setFont("Helvetica-Bold", 7)
            c.drawString(10*mm, y_motivo_top - 4*mm, "MOTIVO:")
            c.setFillColor(black)
            c.setFont("Helvetica", 7)
            # Wrap del texto
            words = motivo.split()
            linea = ""
            y_text = y_motivo_top - 8*mm
            for w in words:
                test = (linea + " " + w).strip()
                if len(test) > 60:
                    c.drawString(10*mm, y_text, linea)
                    y_text -= 3.5*mm
                    linea = w
                    if y_text < y_motivo_top - 16*mm: break
                else:
                    linea = test
            if linea:
                c.drawString(10*mm, y_text, linea)

        # Código de barras en la parte inferior
        from reportlab.lib.utils import ImageReader
        try:
            barcode_img = ImageReader(barcode_path)
            barcode_w = (ancho - 16*mm)
            c.drawImage(barcode_img, 8*mm, 5*mm, width=barcode_w, height=18*mm,
                        preserveAspectRatio=True, anchor='c')
        except Exception as e:
            c.setFont("Helvetica", 6)
            c.drawString(8*mm, 8*mm, f"Error generando barcode: {e}")

        # Footer
        c.setFont("Helvetica", 5)
        c.setFillColor(HexColor("#666666"))
        c.drawString(8*mm, 2*mm, "Lusync ERP · Babymine")

        c.showPage()
        c.save()
        pdf_buf.seek(0)

        # Limpiar tempfile
        try:
            import os
            os.unlink(barcode_path)
        except: pass

        # Marcar etiqueta como generada
        try:
            from inventario import get_conn
            conn = get_conn(); cur = conn.cursor()
            cur.execute("UPDATE devoluciones SET etiqueta_generada=TRUE WHERE id=%s", (dev_id,))
            conn.commit()
            cur.close(); conn.close()
        except: pass

        return send_file(pdf_buf, as_attachment=True,
                         download_name=f"etiqueta_{codigo}.pdf",
                         mimetype="application/pdf")
    except Exception as e:
        import traceback
        return f"Error generando PDF: {e}\n\n{traceback.format_exc()}", 500


@app.route("/devoluciones/pendientes_revision")
def devoluciones_pendientes_revision():
    """Lista devoluciones que están pendientes de revisión, con info de deadline."""
    if not session.get("logged"):
        return jsonify({"error": "no autorizado"}), 401
    try:
        from inventario import get_conn
        from feriados import descripcion_tiempo_restante, color_urgencia
        conn = get_conn(); cur = conn.cursor()
        cur.execute("""
            SELECT id, codigo, oc_origen, canal, sku, nombre, cantidad,
                   tipificacion, motivo_texto, fecha_recepcion, fecha_deadline,
                   etiqueta_generada, estado
            FROM devoluciones
            WHERE estado IN ('pendiente', 'en_reparacion')
              AND fecha_deadline IS NOT NULL
            ORDER BY fecha_deadline ASC
            LIMIT 100
        """)
        rows = cur.fetchall()
        cur.close(); conn.close()

        items = []
        for r in rows:
            deadline = r[10]
            items.append({
                "id": r[0], "codigo": r[1], "oc_origen": r[2], "canal": r[3],
                "sku": r[4], "nombre": r[5], "cantidad": r[6],
                "tipificacion": r[7], "motivo_texto": r[8],
                "fecha_recepcion": r[9].isoformat() if r[9] else None,
                "fecha_deadline": deadline.isoformat() if deadline else None,
                "etiqueta_generada": bool(r[11]),
                "estado": r[12],
                "tiempo_restante": descripcion_tiempo_restante(deadline) if deadline else "Sin deadline",
                "urgencia": color_urgencia(deadline) if deadline else "normal"
            })

        return jsonify({
            "total": len(items),
            "items": items
        })
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


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


# ════════════════════════════════════════════════════════════════════════════
# SYNC_ESTADO PARA MELI Y WALMART (NUEVO - patrón unificado)
# ════════════════════════════════════════════════════════════════════════════

@app.route("/mercadolibre/sync_estado")
def ruta_meli_sync_estado():
    """Compara stock en Lusync (CENTRAL) vs stock real en MercadoLibre.
    Usa el mismo formato que /paris/sync_estado para reutilizar UI."""
    if not session.get("logged"): return jsonify({"error": "no autorizado"}), 401
    try:
        from mercadolibre import obtener_publicaciones_meli, verificar_conexion_meli
        from inventario import listar_sku_mapeo, get_stock_bodega

        conexion = verificar_conexion_meli()

        # Obtener publicaciones del seller (las que están publicadas en MELI)
        publicaciones = obtener_publicaciones_meli(limite=50, offset=0)
        if publicaciones is None:
            return jsonify({"error": "No se pudo conectar con MercadoLibre", "conexion": conexion}), 500

        # Indexar por SKU MELI (item_id) y por seller_custom_field
        meli_dict_por_item = {}
        meli_dict_por_seller_sku = {}
        for it in publicaciones.get("items", []):
            item_id = it.get("item_id", "")
            seller_sku = (it.get("sku_seller", "") or "").strip()
            data_meli = {
                "stock": it.get("stock", 0),
                "title": it.get("title", ""),
                "price": it.get("price", 0),
                "status": it.get("status", "")
            }
            if item_id: meli_dict_por_item[item_id] = data_meli
            if seller_sku: meli_dict_por_seller_sku[seller_sku] = data_meli

        # Comparar stock CENTRAL vs MELI
        mapeo = listar_sku_mapeo()
        resultados = []
        for fila in mapeo:
            sku_meli = (fila.get("sku_mercadolibre", "") or "").strip()
            if not sku_meli:
                continue
            sku_lusync = fila.get("sku_lusync", "")
            stock_central = get_stock_bodega(sku_lusync, "CENTRAL")

            # Buscar primero por item_id, después por seller_custom_field
            meli_data = meli_dict_por_item.get(sku_meli) or meli_dict_por_seller_sku.get(sku_meli, {})
            stock_meli = meli_data.get("stock", None) if meli_data else None

            if stock_meli is None:
                estado = "no_encontrado"
            elif stock_meli == stock_central:
                estado = "sincronizado"
            else:
                estado = "desincronizado"

            resultados.append({
                "sku_lusync": sku_lusync,
                "sku_paris": sku_meli,  # nombre genérico para template
                "sku_meli": sku_meli,
                "nombre": fila.get("nombre", "") or meli_data.get("title", ""),
                "stock_lusync": stock_central,
                "stock_paris": stock_meli,  # nombre genérico
                "stock_meli": stock_meli,
                "diferencia": (stock_meli - stock_central) if stock_meli is not None else None,
                "ultima_actualizacion_paris": "",
                "status_meli": meli_data.get("status", ""),
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


@app.route("/mercadolibre/forzar_sync_sku", methods=["POST"])
def ruta_meli_forzar_sync_sku():
    """Re-envía el stock de un SKU específico a MercadoLibre."""
    if not session.get("logged"): return jsonify({"ok": False}), 401
    try:
        from mercadolibre import actualizar_stock_meli
        from inventario import listar_sku_mapeo, get_stock_bodega
        data = request.json or {}
        sku_lusync = data.get("sku_lusync", "").strip()
        if not sku_lusync:
            return jsonify({"ok": False, "error": "sku_lusync requerido"}), 400

        sku_meli = None
        for fila in listar_sku_mapeo():
            if fila.get("sku_lusync") == sku_lusync:
                sku_meli = (fila.get("sku_mercadolibre", "") or "").strip()
                break
        if not sku_meli:
            return jsonify({"ok": False, "error": f"SKU {sku_lusync} no tiene mapeo MELI"}), 400

        stock = get_stock_bodega(sku_lusync, "CENTRAL")
        ok = actualizar_stock_meli(sku_meli, stock)
        return jsonify({"ok": ok, "sku": sku_lusync, "sku_meli": sku_meli, "stock_enviado": stock})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/walmart/sync_estado")
def ruta_walmart_sync_estado():
    """Compara stock en Lusync (CENTRAL) vs stock real en Walmart.
    Devuelve el mismo formato que /paris/sync_estado para reutilizar UI."""
    if not session.get("logged"): return jsonify({"error": "no autorizado"}), 401
    try:
        from walmart import walmart_headers, WALMART_BASE_URL
        from inventario import listar_sku_mapeo, get_stock_bodega
        import requests as req

        # Obtener stock actual de Walmart
        # Walmart paginar items - usar endpoint /v3/items
        walmart_dict = {}
        try:
            # Buscar items del seller
            res = req.get(
                f"{WALMART_BASE_URL}/v3/items",
                headers=walmart_headers(),
                params={"limit": 200, "offset": 0},
                timeout=20
            )
            if res.status_code == 200:
                data = res.json()
                items = data.get("ItemResponse", []) or data.get("itemResponse", []) or []
                for it in items:
                    sku = it.get("sku") or it.get("itemSku", "")
                    if sku:
                        walmart_dict[sku] = {
                            "stock": int(it.get("availableQuantity") or it.get("totalAvailableQty") or 0),
                            "title": it.get("productName", "") or it.get("title", ""),
                            "price": float(it.get("price", {}).get("amount", 0)) if isinstance(it.get("price"), dict) else 0,
                            "status": it.get("status", ""),
                            "wfs": it.get("wfsEnabled", False)
                        }
        except Exception as e:
            return jsonify({"error_walmart": str(e)}), 500

        # Comparar
        mapeo = listar_sku_mapeo()
        resultados = []
        for fila in mapeo:
            sku_walmart = (fila.get("sku_walmart", "") or "").strip()
            if not sku_walmart:
                continue
            sku_lusync = fila.get("sku_lusync", "")
            stock_central = get_stock_bodega(sku_lusync, "CENTRAL")
            wm_data = walmart_dict.get(sku_walmart, {})
            stock_walmart = wm_data.get("stock", None) if wm_data else None

            if stock_walmart is None:
                estado = "no_encontrado"
            elif stock_walmart == stock_central:
                estado = "sincronizado"
            else:
                estado = "desincronizado"

            resultados.append({
                "sku_lusync": sku_lusync,
                "sku_paris": sku_walmart,  # nombre genérico
                "sku_walmart": sku_walmart,
                "nombre": fila.get("nombre", "") or wm_data.get("title", ""),
                "stock_lusync": stock_central,
                "stock_paris": stock_walmart,  # nombre genérico
                "stock_walmart": stock_walmart,
                "diferencia": (stock_walmart - stock_central) if stock_walmart is not None else None,
                "ultima_actualizacion_paris": "",
                "wfs": wm_data.get("wfs", False),
                "estado": estado
            })

        return jsonify({
            "total": len(resultados),
            "sincronizados": sum(1 for r in resultados if r["estado"] == "sincronizado"),
            "desincronizados": sum(1 for r in resultados if r["estado"] == "desincronizado"),
            "no_encontrados": sum(1 for r in resultados if r["estado"] == "no_encontrado"),
            "resultados": resultados
        })
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@app.route("/walmart/forzar_sync_sku", methods=["POST"])
def ruta_walmart_forzar_sync_sku():
    """Re-envía el stock de un SKU específico a Walmart."""
    if not session.get("logged"): return jsonify({"ok": False}), 401
    try:
        from walmart import actualizar_stock_walmart
        from inventario import listar_sku_mapeo, get_stock_bodega
        data = request.json or {}
        sku_lusync = data.get("sku_lusync", "").strip()
        if not sku_lusync:
            return jsonify({"ok": False, "error": "sku_lusync requerido"}), 400

        sku_walmart = None
        for fila in listar_sku_mapeo():
            if fila.get("sku_lusync") == sku_lusync:
                sku_walmart = (fila.get("sku_walmart", "") or "").strip()
                break
        if not sku_walmart:
            return jsonify({"ok": False, "error": f"SKU {sku_lusync} no tiene mapeo Walmart"}), 400

        stock = get_stock_bodega(sku_lusync, "CENTRAL")
        ok = actualizar_stock_walmart(sku_walmart, stock)
        return jsonify({"ok": ok, "sku": sku_lusync, "sku_walmart": sku_walmart, "stock_enviado": stock})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/walmart/forzar_sync_todos", methods=["POST"])
def ruta_walmart_forzar_sync_todos():
    """Re-envía el stock de todos los SKUs mapeados a Walmart."""
    if not session.get("logged"): return jsonify({"ok": False}), 401
    try:
        from walmart import actualizar_stock_walmart
        from inventario import listar_sku_mapeo, get_stock_bodega
        enviados = 0
        fallidos = 0
        for fila in listar_sku_mapeo():
            sku_walmart = (fila.get("sku_walmart", "") or "").strip()
            sku_lusync = fila.get("sku_lusync", "")
            if not sku_walmart or not sku_lusync:
                continue
            stock = get_stock_bodega(sku_lusync, "CENTRAL")
            if actualizar_stock_walmart(sku_walmart, stock):
                enviados += 1
            else:
                fallidos += 1
        return jsonify({"ok": True, "enviados": enviados, "fallidos": fallidos})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500






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


@app.route("/stats/propia_vs_fulfillment")
def ruta_stats_propia_vs_fulfillment():
    """Desglose de ventas por bodega: propia (CENTRAL) vs fulfillment (otras).
    Devuelve montos, unidades y % por cada bodega individual."""
    if not session.get("logged"): return jsonify({}), 401
    desde, hasta = _parse_rango_fechas()
    try:
        from inventario import get_conn, listar_bodegas
        conn = get_conn(); cur = conn.cursor()

        # Query: agrupar movimientos de salida por bodega y calcular monto + cantidad
        # Para el monto: cantidad × precio del producto
        cur.execute("""
            SELECT
              COALESCE(m.bodega_codigo, 'CENTRAL') AS bodega,
              COUNT(DISTINCT m.orden_id) AS ventas,
              COALESCE(SUM(m.cantidad), 0) AS unidades,
              COALESCE(SUM(m.cantidad * COALESCE(p.precio_oferta, p.precio_normal, 0)), 0) AS monto
            FROM movimientos m
            LEFT JOIN productos p ON p.sku = m.sku
            WHERE m.tipo = 'salida'
              AND m.canal NOT IN ('Manual', 'Sistema')
              AND m.fecha::date BETWEEN %s AND %s
            GROUP BY COALESCE(m.bodega_codigo, 'CENTRAL')
        """, (desde, hasta))
        rows = cur.fetchall()
        cur.close(); conn.close()

        # Mapear por bodega y enriquecer con metadata
        bodegas_meta = {b["codigo"]: b for b in listar_bodegas()}
        por_bodega = {}
        for bod_codigo, ventas, unidades, monto in rows:
            por_bodega[bod_codigo] = {
                "codigo": bod_codigo,
                "nombre": bodegas_meta.get(bod_codigo, {}).get("nombre", bod_codigo),
                "tipo": bodegas_meta.get(bod_codigo, {}).get("tipo", "propia"),
                "ventas": int(ventas or 0),
                "unidades": int(unidades or 0),
                "monto": float(monto or 0)
            }

        # Asegurar que todas las bodegas activas aparezcan (aunque sea con 0)
        for b in listar_bodegas():
            if b["codigo"] not in por_bodega:
                por_bodega[b["codigo"]] = {
                    "codigo": b["codigo"],
                    "nombre": b["nombre"],
                    "tipo": b["tipo"],
                    "ventas": 0, "unidades": 0, "monto": 0
                }

        # Totales agregados: propia vs fulfillment
        total_propia_monto = sum(b["monto"] for b in por_bodega.values() if b["tipo"] == "propia")
        total_propia_ventas = sum(b["ventas"] for b in por_bodega.values() if b["tipo"] == "propia")
        total_propia_unidades = sum(b["unidades"] for b in por_bodega.values() if b["tipo"] == "propia")
        total_full_monto = sum(b["monto"] for b in por_bodega.values() if b["tipo"] != "propia")
        total_full_ventas = sum(b["ventas"] for b in por_bodega.values() if b["tipo"] != "propia")
        total_full_unidades = sum(b["unidades"] for b in por_bodega.values() if b["tipo"] != "propia")
        total_general = total_propia_monto + total_full_monto

        return jsonify({
            "desde": str(desde),
            "hasta": str(hasta),
            "propia": {
                "monto": total_propia_monto,
                "ventas": total_propia_ventas,
                "unidades": total_propia_unidades,
                "porcentaje": round((total_propia_monto / total_general * 100) if total_general > 0 else 0, 1)
            },
            "fulfillment": {
                "monto": total_full_monto,
                "ventas": total_full_ventas,
                "unidades": total_full_unidades,
                "porcentaje": round((total_full_monto / total_general * 100) if total_general > 0 else 0, 1)
            },
            "total": total_general,
            "por_bodega": sorted(por_bodega.values(), key=lambda x: -x["monto"])
        })
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


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


@app.route("/mercadolibre/sync_precios", methods=["POST"])
def ruta_meli_sync_precios():
    """Envía precios actuales tal cual a MercadoLibre, sin transformaciones.
    Las comisiones, márgenes y redondeos se manejarán en el módulo Motor de Precios."""
    if not session.get("logged"): return jsonify({"ok": False}), 401
    try:
        from mercadolibre import actualizar_precio_meli
        from inventario import listar_sku_mapeo

        productos = cargar_productos()
        productos_dict = {p["sku"]: p for p in productos}
        mapeos = listar_sku_mapeo()

        registrar_audit(session.get("usuario","Sistema"), request.remote_addr,
                        "sync_meli_precios", detalle="Sync masivo precios MercadoLibre")

        enviados = 0
        fallidos = 0
        log = []
        for fila in mapeos:
            sku_lusync = fila.get("sku_lusync", "")
            sku_meli = (fila.get("sku_mercadolibre", "") or "").strip()
            if not sku_meli or not sku_lusync:
                continue
            p = productos_dict.get(sku_lusync, {})
            precio_normal = p.get("precio_normal") or 0
            precio_oferta = p.get("precio_oferta") or 0
            # Enviar precio tal cual: oferta si existe, si no normal
            precio = precio_oferta if precio_oferta > 0 else precio_normal
            if precio <= 0:
                log.append(f"⚠ {sku_lusync} sin precio")
                continue
            if actualizar_precio_meli(sku_meli, precio):
                enviados += 1
                log.append(f"✓ {sku_meli} → ${precio}")
            else:
                fallidos += 1
                log.append(f"× {sku_meli} falló")
        return jsonify({"ok": True, "enviados": enviados, "fallidos": fallidos, "log": log[:30]})
    except Exception as e:
        import traceback
        return jsonify({"ok": False, "error": str(e), "trace": traceback.format_exc()}), 500


@app.route("/paris/sync_precios", methods=["POST"])
def ruta_paris_sync_precios():
    """Envía precios actuales tal cual a París, sin transformaciones.
    Las comisiones, márgenes y redondeos se manejarán en el módulo Motor de Precios."""
    if not session.get("logged"): return jsonify({"ok": False}), 401
    try:
        from paris import actualizar_precio_paris
        from inventario import listar_sku_mapeo

        productos = cargar_productos()
        productos_dict = {p["sku"]: p for p in productos}
        mapeos = listar_sku_mapeo()

        registrar_audit(session.get("usuario","Sistema"), request.remote_addr,
                        "sync_paris_precios", detalle="Sync masivo precios Paris")

        enviados = 0
        fallidos = 0
        log = []
        for fila in mapeos:
            sku_lusync = fila.get("sku_lusync", "")
            sku_paris = (fila.get("sku_paris", "") or "").strip()
            if not sku_paris or not sku_lusync:
                continue
            p = productos_dict.get(sku_lusync, {})
            precio_normal = p.get("precio_normal") or 0
            precio_oferta = p.get("precio_oferta") or 0
            if precio_normal <= 0:
                log.append(f"⚠ {sku_lusync} sin precio_normal")
                continue
            # Enviar precios tal cual: precio_normal y precio_oferta si existe
            precio_oferta_final = precio_oferta if (precio_oferta > 0 and precio_oferta < precio_normal) else None
            ok = actualizar_precio_paris(sku_paris, precio_normal, precio_oferta_final)
            if ok:
                enviados += 1
                txt = f"✓ {sku_paris} → ${precio_normal}"
                if precio_oferta_final: txt += f" (oferta ${precio_oferta_final})"
                log.append(txt)
            else:
                fallidos += 1
                log.append(f"× {sku_paris} falló")
        return jsonify({"ok": True, "enviados": enviados, "fallidos": fallidos, "log": log[:30]})
    except Exception as e:
        import traceback
        return jsonify({"ok": False, "error": str(e), "trace": traceback.format_exc()}), 500


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

                    # ── Extraer fecha real de compra del marketplace ────────
                    # MELI devuelve date_created en ISO con timezone (ej: 2026-05-03T18:32:15.000-04:00)
                    # Lo parseamos y dejamos que descontar_venta_inteligente lo convierta a Chile
                    fecha_compra_meli = None
                    try:
                        import pytz as _pytz
                        date_str = o.get("date_created", "") or ""
                        if date_str:
                            # Manejar ambos formatos: con .000 milisegundos o sin
                            if "." in date_str:
                                # Formato: 2026-05-03T18:32:15.000-04:00
                                date_str_clean = date_str.replace("Z", "+00:00")
                                fecha_compra_meli = datetime.fromisoformat(date_str_clean)
                            else:
                                # Formato: 2026-05-03T18:32:15-04:00 o con Z
                                date_str_clean = date_str.replace("Z", "+00:00")
                                fecha_compra_meli = datetime.fromisoformat(date_str_clean)
                            # DEBUG temporal: loggear lo que se parseó
                            log.append(f"  Orden {order_id}: date_created='{date_str}' → parsed={fecha_compra_meli}")
                        else:
                            log.append(f"  Orden {order_id}: date_created VACÍO en payload")
                    except Exception as e:
                        log.append(f"  Orden {order_id}: NO se pudo parsear date_created '{o.get('date_created','')}': {e}")
                        fecha_compra_meli = None

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
                            usuario="Sistema",
                            fecha_compra_marketplace=fecha_compra_meli,
                            origen_registro="sync_manual"
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


# ════════════════════════════════════════════════════════════════════════════
# DIAGNÓSTICO DE TRAZABILIDAD DE FECHAS (temporal/debug)
# ════════════════════════════════════════════════════════════════════════════

@app.route("/debug/movimientos_trazabilidad")
def debug_movimientos_trazabilidad():
    """Muestra los últimos 30 movimientos con TODOS los campos de trazabilidad.
    Sirve para verificar si fecha_compra_marketplace se está guardando correctamente.
    """
    if not session.get("logged"):
        return jsonify({"error": "no autorizado"}), 401
    try:
        from inventario import get_conn
        conn = get_conn(); cur = conn.cursor()

        # Asegurar columnas
        cur.execute("ALTER TABLE movimientos ADD COLUMN IF NOT EXISTS fecha_compra_marketplace TIMESTAMP")
        cur.execute("ALTER TABLE movimientos ADD COLUMN IF NOT EXISTS origen_registro TEXT DEFAULT 'sistema'")
        cur.execute("ALTER TABLE movimientos ADD COLUMN IF NOT EXISTS stock_antes INTEGER")
        cur.execute("ALTER TABLE movimientos ADD COLUMN IF NOT EXISTS stock_despues INTEGER")
        conn.commit()

        # Resumen agregado
        cur.execute("""
            SELECT COALESCE(canal, '?') as canal,
                   COALESCE(bodega_codigo, 'CENTRAL') as bodega,
                   COALESCE(origen_registro, 'sistema') as origen,
                   COUNT(*) as total,
                   COUNT(fecha_compra_marketplace) as con_fecha_real,
                   COUNT(fecha_importacion) as con_fecha_import,
                   COUNT(stock_antes) as con_stock_antes,
                   TO_CHAR(MIN(fecha), 'DD/MM HH24:MI') as primer_mov,
                   TO_CHAR(MAX(fecha), 'DD/MM HH24:MI') as ultimo_mov
            FROM movimientos
            WHERE fecha > NOW() - INTERVAL '7 days'
            GROUP BY canal, bodega_codigo, origen_registro
            ORDER BY total DESC
        """)
        resumen = []
        for r in cur.fetchall():
            resumen.append({
                "canal": r[0], "bodega": r[1], "origen": r[2],
                "total": r[3], "con_fecha_real": r[4],
                "con_fecha_import": r[5], "con_stock_antes": r[6],
                "primer": r[7], "ultimo": r[8],
                "porcentaje_con_fecha_real": round((r[4]/r[3])*100, 1) if r[3] > 0 else 0
            })

        # Detalle de los últimos 30 movimientos
        cur.execute("""
            SELECT id, tipo, sku, canal, bodega_codigo, orden_id,
                   TO_CHAR(fecha, 'DD/MM/YYYY HH24:MI') as fecha_mov,
                   TO_CHAR(fecha_importacion, 'DD/MM/YYYY HH24:MI') as fecha_imp,
                   TO_CHAR(fecha_compra_marketplace, 'DD/MM/YYYY HH24:MI') as fecha_compra,
                   origen_registro, stock_antes, stock_despues, cantidad
            FROM movimientos
            ORDER BY fecha DESC
            LIMIT 30
        """)
        detalles = []
        for r in cur.fetchall():
            detalles.append({
                "id": r[0], "tipo": r[1], "sku": r[2], "canal": r[3],
                "bodega": r[4], "orden_id": r[5],
                "fecha_mov": r[6], "fecha_import": r[7],
                "fecha_compra_marketplace": r[8],
                "origen": r[9],
                "stock_antes": r[10], "stock_despues": r[11],
                "cantidad": r[12]
            })

        cur.close(); conn.close()

        # Diagnóstico automático
        diagnostico = []
        total_recientes = sum(r["total"] for r in resumen)
        total_con_fecha = sum(r["con_fecha_real"] for r in resumen)

        if total_recientes == 0:
            diagnostico.append("⚠ No hay movimientos en los últimos 7 días")
        elif total_con_fecha == 0:
            diagnostico.append("✗ NINGÚN movimiento tiene fecha_compra_marketplace. El parsing/guardado está fallando.")
        elif total_con_fecha < total_recientes:
            faltantes = total_recientes - total_con_fecha
            diagnostico.append(f"⚠ {faltantes}/{total_recientes} movimientos sin fecha_compra_marketplace")
        else:
            diagnostico.append(f"✓ Todos los {total_recientes} movimientos recientes tienen fecha de compra real")

        # Detectar si todavía hay movimientos sin bodega_codigo
        sin_bodega = [r for r in resumen if not r["bodega"] or r["bodega"] == "CENTRAL" and "FULL" not in str(r)]

        return jsonify({
            "diagnostico": diagnostico,
            "resumen_por_canal": resumen,
            "ultimos_30_movimientos": detalles
        })
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@app.route("/debug/payload_orden_meli/<order_id>")
def debug_payload_orden_meli(order_id):
    """Muestra el payload crudo de una orden MELI específica para verificar
    qué campos vienen y poder diagnosticar el parsing de date_created."""
    if not session.get("logged"):
        return jsonify({"error": "no autorizado"}), 401
    try:
        from mercadolibre import obtener_orden_meli
        from datetime import datetime as _dt
        orden = obtener_orden_meli(order_id)
        if not orden:
            return jsonify({"error": f"Orden {order_id} no encontrada"}), 404

        # Intentar parsear date_created como lo hace el sync
        date_str = orden.get("date_created", "") or ""
        parseo_result = {
            "date_created_raw": date_str,
            "tipo": str(type(date_str).__name__),
            "es_string_vacio": date_str == "",
            "longitud": len(str(date_str))
        }
        try:
            if date_str:
                date_str_clean = date_str.replace("Z", "+00:00")
                fecha = _dt.fromisoformat(date_str_clean)
                parseo_result["parseado_ok"] = True
                parseo_result["datetime_parseado"] = str(fecha)
                parseo_result["tiene_tzinfo"] = fecha.tzinfo is not None
        except Exception as e:
            parseo_result["parseado_ok"] = False
            parseo_result["error"] = str(e)

        return jsonify({
            "order_id": order_id,
            "campos_fecha_disponibles": {
                "date_created": orden.get("date_created"),
                "date_closed": orden.get("date_closed"),
                "last_updated": orden.get("last_updated")
            },
            "diagnostico_parseo": parseo_result,
            "shipping_id": (orden.get("shipping") or {}).get("id"),
            "status": orden.get("status"),
            "total_items": len(orden.get("order_items", []))
        })
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@app.route("/debug/test_parseo_fechas")
def debug_test_parseo_fechas():
    """Test directo del código de parseo con strings de ejemplo de cada marketplace."""
    if not session.get("logged"):
        return jsonify({"error": "no autorizado"}), 401
    from datetime import datetime as _dt
    import pytz as _pytz
    TZ = _pytz.timezone('America/Santiago')

    casos = [
        ("MELI con tz Chile", "2026-05-03T18:32:15.000-04:00"),
        ("MELI con tz UTC", "2026-05-03T22:32:15.000Z"),
        ("Paris UTC con Z", "2026-05-03T05:02:00.000Z"),
        ("Ripley UTC con Z", "2026-05-03T18:32:15Z"),
        ("Falabella sin tz", "2026-05-03 14:32:15"),
        ("Falabella +0000", "2026-05-03T14:32:15+0000")
    ]
    resultados = []
    for nombre, date_str in casos:
        item = {"nombre": nombre, "input": date_str}
        try:
            try:
                fecha = _dt.fromisoformat(date_str.replace("Z", "+00:00"))
                item["parseado"] = str(fecha)
                if fecha.tzinfo:
                    fecha_chile = fecha.astimezone(TZ).replace(tzinfo=None)
                    item["en_chile"] = str(fecha_chile)
                else:
                    item["en_chile"] = "sin tz, no se convierte"
            except ValueError:
                # Fallback Falabella sin T
                fecha_naive = _dt.strptime(date_str.strip(), "%Y-%m-%d %H:%M:%S")
                fecha = _pytz.utc.localize(fecha_naive)
                fecha_chile = fecha.astimezone(TZ).replace(tzinfo=None)
                item["parseado"] = str(fecha) + " (UTC asumido)"
                item["en_chile"] = str(fecha_chile)
            item["ok"] = True
        except Exception as e:
            item["ok"] = False
            item["error"] = str(e)
        resultados.append(item)
    return jsonify({"resultados": resultados})


# ════════════════════════════════════════════════════════════════════════════
# RECONSTRUCCIÓN DE FECHAS DE COMPRA HISTÓRICAS
# ════════════════════════════════════════════════════════════════════════════
# Endpoint admin para rellenar la columna fecha_compra_marketplace de los
# movimientos antiguos consultando los APIs de cada marketplace.
#
# Estrategia: traer todas las órdenes de los últimos N días una sola vez por
# canal (mucho más eficiente que consultar de a una), construir un diccionario
# {orden_id: fecha_compra} en memoria, y luego hacer UN UPDATE por movimiento.
# ════════════════════════════════════════════════════════════════════════════

@app.route("/admin/reconstruir_fechas_compra", methods=["GET", "POST"])
def admin_reconstruir_fechas_compra():
    """Reconstruye fecha_compra_marketplace para movimientos antiguos.

    Query string:
      ?canales=mercadolibre,paris,walmart,falabella   (default: los 4)
      ?dias=30                                         (default: 30)
      ?dry_run=1                                       (default: 0; si 1 no escribe)

    Usa los APIs de cada marketplace para obtener la fecha real de compra,
    luego hace UPDATE en bloque por orden_id.
    """
    if not session.get("logged"):
        return jsonify({"error": "no autorizado"}), 401

    from datetime import datetime as _dt
    import pytz as _pytz
    TZ_CHILE = _pytz.timezone('America/Santiago')

    canales_str = request.args.get("canales", "mercadolibre,paris,walmart,falabella")
    canales_pedidos = set(c.strip().lower() for c in canales_str.split(",") if c.strip())
    dias = int(request.args.get("dias", 30))
    dry_run = request.args.get("dry_run", "0") == "1"

    registrar_audit(session.get("usuario","Sistema"), request.remote_addr,
                    "reconstruir_fechas_compra",
                    detalle=f"canales={canales_pedidos} dias={dias} dry_run={dry_run}")

    log = []
    log.append(f"Configuración: canales={list(canales_pedidos)}, dias={dias}, dry_run={dry_run}")

    # ── 1) Cargar movimientos pendientes de la BD ──
    from inventario import get_conn
    conn = get_conn(); cur = conn.cursor()
    cur.execute("""
        SELECT id, canal, orden_id
        FROM movimientos
        WHERE fecha_compra_marketplace IS NULL
          AND orden_id IS NOT NULL AND orden_id != ''
          AND fecha > NOW() - (%s || ' days')::INTERVAL
        ORDER BY fecha DESC
    """, (dias,))
    pendientes = cur.fetchall()
    cur.close(); conn.close()

    log.append(f"Movimientos pendientes en BD: {len(pendientes)}")

    # Agrupar pendientes por canal normalizado
    pendientes_por_canal = {}
    for mov_id, canal, orden_id in pendientes:
        canal_norm = (canal or "").lower()
        # Normalizar nombres de canales
        if "mercadolibre" in canal_norm or "meli" in canal_norm:
            canal_norm = "mercadolibre"
        elif "paris" in canal_norm or "parís" in canal_norm:
            canal_norm = "paris"
        elif "walmart" in canal_norm or "wfs" in canal_norm:
            canal_norm = "walmart"
        elif "falabella" in canal_norm:
            canal_norm = "falabella"
        else:
            continue  # Manual, WooCommerce, Sistema, etc → no se reconstruye desde API
        if canal_norm not in canales_pedidos:
            continue
        pendientes_por_canal.setdefault(canal_norm, []).append((mov_id, str(orden_id)))

    for c, items in pendientes_por_canal.items():
        log.append(f"  {c}: {len(items)} movimientos pendientes")

    # ── 2) Para cada canal, traer las órdenes y armar mapa orden_id → fecha ──
    fecha_por_orden = {}  # {(canal_norm, orden_id): datetime con tz}

    # MELI: consultar orden por orden (no hay endpoint que filtre por order_id en bloque útil)
    if "mercadolibre" in pendientes_por_canal:
        ids_meli = pendientes_por_canal["mercadolibre"]
        log.append(f"[MELI] Consultando {len(set(o for _, o in ids_meli))} órdenes únicas...")
        try:
            from mercadolibre import obtener_orden_meli
            ya_consultados = set()
            for mov_id, orden_id in ids_meli:
                if orden_id in ya_consultados:
                    continue
                ya_consultados.add(orden_id)
                try:
                    orden = obtener_orden_meli(orden_id)
                    if orden:
                        date_str = orden.get("date_created", "") or ""
                        if date_str:
                            try:
                                fecha = _dt.fromisoformat(date_str.replace("Z", "+00:00"))
                                fecha_por_orden[("mercadolibre", orden_id)] = fecha
                            except Exception as e:
                                log.append(f"  MELI {orden_id}: error parseando '{date_str}': {e}")
                except Exception as e:
                    log.append(f"  MELI {orden_id}: error consultando API: {e}")
            log.append(f"[MELI] Fechas obtenidas: {len([k for k in fecha_por_orden if k[0]=='mercadolibre'])}")
        except Exception as e:
            log.append(f"[MELI] ERROR general: {e}")

    # PARÍS: traer todas las órdenes recientes y filtrar
    if "paris" in pendientes_por_canal:
        log.append(f"[Paris] Trayendo órdenes últimos {dias} días...")
        try:
            from paris import obtener_ordenes_paris_todas
            ordenes = obtener_ordenes_paris_todas(dias=dias, estado=None)
            log.append(f"[Paris] {len(ordenes)} órdenes obtenidas")
            for so in ordenes:
                sub_order_num = str(so.get("subOrderNumber", "") or "")
                if not sub_order_num:
                    continue
                date_str = (so.get("createdAt") or so.get("created_at") or "")
                if date_str:
                    try:
                        fecha = _dt.fromisoformat(date_str.replace("Z", "+00:00"))
                        fecha_por_orden[("paris", sub_order_num)] = fecha
                    except Exception as e:
                        pass
            log.append(f"[Paris] Fechas obtenidas: {len([k for k in fecha_por_orden if k[0]=='paris'])}")
        except Exception as e:
            log.append(f"[Paris] ERROR: {e}")
            import gc; gc.collect()

    # WALMART: traer órdenes recientes (todos los estados que conocemos)
    if "walmart" in pendientes_por_canal:
        log.append(f"[Walmart] Trayendo órdenes últimos {dias} días...")
        try:
            from walmart import obtener_ordenes_walmart
            ya_consultados = set()
            # Walmart requiere consultar por estado. Iteramos los estados activos.
            for estado in ["Created", "Acknowledged", "Shipped", "Delivered"]:
                try:
                    ords_walmart = obtener_ordenes_walmart(estado=estado, max_paginas=2, limit=50, dias=dias)
                    log.append(f"[Walmart] {estado}: {len(ords_walmart)} órdenes")
                    for o in ords_walmart:
                        po = str(o.get("purchaseOrderId", "") or "")
                        co = str(o.get("customerOrderId", "") or po)
                        # En BD usamos customerOrderId como orden_id
                        if co in ya_consultados:
                            continue
                        ya_consultados.add(co)
                        # Walmart trae orderDate como timestamp en ms (epoch)
                        order_date_raw = o.get("orderDate")
                        fecha = None
                        if order_date_raw:
                            try:
                                # Si es int (epoch ms), convertir
                                if isinstance(order_date_raw, (int, float)):
                                    fecha = _dt.fromtimestamp(order_date_raw / 1000, tz=_pytz.utc)
                                elif isinstance(order_date_raw, str):
                                    if order_date_raw.isdigit():
                                        fecha = _dt.fromtimestamp(int(order_date_raw) / 1000, tz=_pytz.utc)
                                    else:
                                        fecha = _dt.fromisoformat(order_date_raw.replace("Z", "+00:00"))
                            except Exception as e:
                                log.append(f"  Walmart {co}: error parseando orderDate '{order_date_raw}': {e}")
                        if fecha:
                            fecha_por_orden[("walmart", co)] = fecha
                            # Walmart en BD a veces usa purchaseOrderId, también guardamos por po
                            if po and po != co:
                                fecha_por_orden[("walmart", po)] = fecha
                except Exception as e:
                    log.append(f"[Walmart] estado {estado}: error {e}")
            log.append(f"[Walmart] Fechas obtenidas: {len([k for k in fecha_por_orden if k[0]=='walmart'])}")
            import gc; gc.collect()
        except Exception as e:
            log.append(f"[Walmart] ERROR general: {e}")

    # FALABELLA: traer órdenes recientes (varios estados)
    if "falabella" in pendientes_por_canal:
        log.append(f"[Falabella] Trayendo órdenes últimos {dias} días...")
        try:
            from falabella import obtener_ordenes_falabella
            ya_consultados = set()
            for estado in [None, "pending", "ready_to_ship", "shipped", "delivered"]:
                try:
                    ords_fal = obtener_ordenes_falabella(estado=estado, dias=dias, limit=100)
                    log.append(f"[Falabella] {estado or 'todos'}: {len(ords_fal)} órdenes")
                    for o in ords_fal:
                        oid = str(o.get("OrderId") or o.get("OrderNumber") or "")
                        if not oid or oid in ya_consultados:
                            continue
                        ya_consultados.add(oid)
                        date_str = (o.get("CreatedAt") or o.get("created_at") or "")
                        if date_str:
                            try:
                                fecha = _dt.fromisoformat(date_str.replace("Z", "+00:00"))
                            except ValueError:
                                try:
                                    fecha_naive = _dt.strptime(date_str.strip(), "%Y-%m-%d %H:%M:%S")
                                    fecha = _pytz.utc.localize(fecha_naive)
                                except Exception as e:
                                    log.append(f"  Falabella {oid}: error parseando '{date_str}': {e}")
                                    continue
                            fecha_por_orden[("falabella", oid)] = fecha
                except Exception as e:
                    log.append(f"[Falabella] estado {estado}: error {e}")
            log.append(f"[Falabella] Fechas obtenidas: {len([k for k in fecha_por_orden if k[0]=='falabella'])}")
            import gc; gc.collect()
        except Exception as e:
            log.append(f"[Falabella] ERROR general: {e}")

    # ── 3) Hacer UPDATE en BD por cada movimiento que tenga match ──
    actualizados = 0
    sin_match = 0
    errores_db = 0
    detalle_no_encontrados = []

    if dry_run:
        log.append("DRY RUN: no se escribirá nada en BD")

    conn = get_conn(); cur = conn.cursor()
    for canal_norm, lista in pendientes_por_canal.items():
        for mov_id, orden_id in lista:
            fecha = fecha_por_orden.get((canal_norm, orden_id))
            if fecha is None:
                sin_match += 1
                if len(detalle_no_encontrados) < 30:
                    detalle_no_encontrados.append(f"{canal_norm}:{orden_id} (mov_id={mov_id})")
                continue
            # Convertir a Chile sin tz para guardar
            try:
                if hasattr(fecha, 'tzinfo') and fecha.tzinfo:
                    fecha_chile = fecha.astimezone(TZ_CHILE).replace(tzinfo=None)
                else:
                    fecha_chile = fecha
                if not dry_run:
                    cur.execute("""UPDATE movimientos
                                   SET fecha_compra_marketplace = %s
                                   WHERE id = %s""", (fecha_chile, mov_id))
                actualizados += 1
            except Exception as e:
                errores_db += 1
                log.append(f"  ERR DB mov_id={mov_id}: {e}")

    if not dry_run:
        try:
            conn.commit()
        except Exception as e:
            conn.rollback()
            log.append(f"COMMIT ERROR: {e}")
            errores_db += 1
    cur.close(); conn.close()

    # ── 4) Verificar cuántos quedan pendientes después ──
    conn = get_conn(); cur = conn.cursor()
    cur.execute("""
        SELECT COUNT(*) FROM movimientos
        WHERE fecha_compra_marketplace IS NULL
          AND orden_id IS NOT NULL AND orden_id != ''
          AND fecha > NOW() - (%s || ' days')::INTERVAL
    """, (dias,))
    quedan_pendientes = cur.fetchone()[0]
    cur.close(); conn.close()

    return jsonify({
        "ok": True,
        "dry_run": dry_run,
        "movimientos_pendientes_iniciales": len(pendientes),
        "actualizados": actualizados,
        "sin_match_en_api": sin_match,
        "errores_db": errores_db,
        "movimientos_pendientes_restantes": quedan_pendientes,
        "ejemplos_no_encontrados": detalle_no_encontrados,
        "log": log
    })


@app.route("/admin/estado_reconstruccion")
def admin_estado_reconstruccion():
    """Muestra cuántos movimientos tienen/no tienen fecha_compra_marketplace,
    agrupado por canal. Útil para ver el progreso antes/después de ejecutar
    /admin/reconstruir_fechas_compra."""
    if not session.get("logged"):
        return jsonify({"error": "no autorizado"}), 401
    try:
        from inventario import get_conn
        conn = get_conn(); cur = conn.cursor()
        cur.execute("""
            SELECT COALESCE(canal, '?') as canal,
                   COUNT(*) as total,
                   COUNT(fecha_compra_marketplace) as con_fecha,
                   COUNT(*) - COUNT(fecha_compra_marketplace) as sin_fecha,
                   TO_CHAR(MIN(fecha), 'DD/MM/YYYY') as desde,
                   TO_CHAR(MAX(fecha), 'DD/MM/YYYY') as hasta
            FROM movimientos
            WHERE orden_id IS NOT NULL AND orden_id != ''
            GROUP BY canal
            ORDER BY total DESC
        """)
        resumen = []
        for r in cur.fetchall():
            pct = round((r[2]/r[1])*100, 1) if r[1] > 0 else 0
            resumen.append({
                "canal": r[0],
                "total": r[1],
                "con_fecha_real": r[2],
                "sin_fecha_real": r[3],
                "porcentaje_completo": pct,
                "desde": r[4],
                "hasta": r[5]
            })
        cur.close(); conn.close()

        total_general = sum(r["total"] for r in resumen)
        total_con = sum(r["con_fecha_real"] for r in resumen)
        return jsonify({
            "total_movimientos_con_orden": total_general,
            "con_fecha_real": total_con,
            "sin_fecha_real": total_general - total_con,
            "porcentaje_completo": round((total_con/total_general)*100, 1) if total_general > 0 else 0,
            "por_canal": resumen
        })
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


# ════════════════════════════════════════════════════════════════════════════
# PLANTILLAS DE STOCK Y RESET CONTROLADO DE MOVIMIENTOS
# ════════════════════════════════════════════════════════════════════════════
# Endpoints para el flujo de "empezar limpio":
#   1) /admin/plantilla_stock_central   → descarga Excel solo con CENTRAL
#   2) /admin/plantilla_stock_fulfillment → descarga Excel con todas las
#      bodegas fulfillment (MELI_FULL, PARIS_CD, etc.)
#   3) /admin/reset_movimientos         → borra movimientos + ordenes_procesadas
#      con confirmación obligatoria y backup automático
# ════════════════════════════════════════════════════════════════════════════

@app.route("/admin/plantilla_stock_central")
def admin_plantilla_stock_central():
    """Descarga plantilla Excel con todos los SKUs de productos
    + columna CENTRAL para que el usuario complete el stock.
    Si el SKU ya tenía stock en CENTRAL, lo prellena para que sirva
    también como exportación del estado actual."""
    if not session.get("logged"): return redirect("/")
    try:
        import io, openpyxl
        from inventario import cargar_productos, get_stock_bodega

        productos = cargar_productos()
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Stock Central"

        # Encabezados
        headers = ["sku_lusync", "nombre", "CENTRAL"]
        ws.append(headers)

        # Estilo encabezado
        from openpyxl.styles import Font, PatternFill
        for col_idx in range(1, len(headers) + 1):
            cell = ws.cell(row=1, column=col_idx)
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill(start_color="3C3489", end_color="3C3489", fill_type="solid")

        # Datos: sku, nombre, stock actual en CENTRAL
        for p in productos:
            sku = p.get("sku", "")
            stock_actual = 0
            try:
                stock_actual = get_stock_bodega(sku, "CENTRAL") or 0
            except:
                stock_actual = 0
            ws.append([sku, p.get("nombre", ""), stock_actual])

        # Hoja de instrucciones
        ws2 = wb.create_sheet("Instrucciones")
        instrucciones = [
            ["INSTRUCCIONES — Plantilla de stock CENTRAL"],
            [""],
            ["1. Esta plantilla contiene TODOS los productos de tu sistema."],
            ["2. La columna CENTRAL contiene el stock ACTUAL en bodega central."],
            ["3. Edita los valores que necesites cambiar y guarda el archivo."],
            ["4. Sube este archivo desde la sección Bodegas → Importar Excel."],
            [""],
            ["IMPORTANTE:"],
            ["- NO borres ni cambies los nombres de las columnas (sku_lusync, nombre, CENTRAL)."],
            ["- NO cambies los valores de la columna sku_lusync."],
            ["- Si dejas una celda CENTRAL vacía, el sistema NO modifica ese SKU."],
            ["- Si pones 0, el sistema dejará ese SKU con stock cero."],
            [""],
            ["Nombres de bodega válidos para esta plantilla: CENTRAL"]
        ]
        for fila in instrucciones:
            ws2.append(fila)
        ws2.column_dimensions['A'].width = 80

        # Ajustar ancho columnas hoja principal
        ws.column_dimensions['A'].width = 20
        ws.column_dimensions['B'].width = 50
        ws.column_dimensions['C'].width = 12

        buf = io.BytesIO()
        wb.save(buf); buf.seek(0)
        return send_file(buf, download_name="plantilla_stock_central.xlsx",
                         as_attachment=True,
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@app.route("/admin/plantilla_stock_fulfillment")
def admin_plantilla_stock_fulfillment():
    """Descarga plantilla Excel con todos los SKUs + columnas para cada
    bodega de fulfillment (MELI_FULL, PARIS_CD, WALMART_FBM, FALABELLA_FBM,
    RIPLEY_FBM, HITES_FBM, WOO_DROP). Prellena con el stock actual."""
    if not session.get("logged"): return redirect("/")
    try:
        import io, openpyxl
        from inventario import cargar_productos, get_stock_bodega, listar_bodegas

        productos = cargar_productos()

        # Solo bodegas tipo fulfillment o dropship (excluye CENTRAL)
        bodegas = [b for b in listar_bodegas() if b.get("tipo") in ("fulfillment", "dropship")]
        bodegas_codigos = [b["codigo"] for b in bodegas]

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Stock Fulfillment"

        # Encabezados: sku, nombre, + cada bodega fulfillment
        headers = ["sku_lusync", "nombre"] + bodegas_codigos
        ws.append(headers)

        # Estilo encabezado
        from openpyxl.styles import Font, PatternFill
        for col_idx in range(1, len(headers) + 1):
            cell = ws.cell(row=1, column=col_idx)
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill(start_color="7C4A03", end_color="7C4A03", fill_type="solid")

        # Datos: sku, nombre, stock actual por cada fulfillment
        for p in productos:
            sku = p.get("sku", "")
            row = [sku, p.get("nombre", "")]
            for cod in bodegas_codigos:
                try:
                    row.append(get_stock_bodega(sku, cod) or 0)
                except:
                    row.append(0)
            ws.append(row)

        # Hoja de referencia con nombres legibles de cada bodega
        ws_ref = wb.create_sheet("Referencia bodegas")
        ws_ref.append(["Código", "Nombre legible", "Tipo", "Canal asociado"])
        from openpyxl.styles import Font as _F
        ws_ref.cell(row=1, column=1).font = _F(bold=True)
        ws_ref.cell(row=1, column=2).font = _F(bold=True)
        ws_ref.cell(row=1, column=3).font = _F(bold=True)
        ws_ref.cell(row=1, column=4).font = _F(bold=True)
        for b in bodegas:
            ws_ref.append([b["codigo"], b.get("nombre",""),
                           b.get("tipo",""), b.get("canal","") or "-"])
        ws_ref.column_dimensions['A'].width = 18
        ws_ref.column_dimensions['B'].width = 30
        ws_ref.column_dimensions['C'].width = 15
        ws_ref.column_dimensions['D'].width = 18

        # Hoja de instrucciones
        ws2 = wb.create_sheet("Instrucciones")
        ws2.append(["INSTRUCCIONES — Plantilla de stock FULFILLMENT"])
        ws2.append([""])
        ws2.append(["Esta plantilla contiene una columna por cada bodega de fulfillment."])
        ws2.append(["Las columnas pre-completadas tienen el stock ACTUAL en cada bodega."])
        ws2.append([""])
        ws2.append(["IMPORTANTE:"])
        ws2.append(["- Solo edita las columnas de bodegas que necesites actualizar."])
        ws2.append(["- Si una celda está vacía, el sistema NO toca ese SKU/bodega."])
        ws2.append(["- Si pones 0, el sistema dejará ese SKU/bodega con stock cero."])
        ws2.append(["- NO cambies los nombres de las columnas (sku_lusync, MELI_FULL, etc)."])
        ws2.append([""])
        ws2.append(["Para descargar plantilla solo de Bodega Central usa:"])
        ws2.append(["/admin/plantilla_stock_central"])
        ws2.column_dimensions['A'].width = 80

        # Ajustar columnas principal
        ws.column_dimensions['A'].width = 20
        ws.column_dimensions['B'].width = 50
        for i, _ in enumerate(bodegas_codigos):
            col_letter = openpyxl.utils.get_column_letter(3 + i)
            ws.column_dimensions[col_letter].width = 16

        buf = io.BytesIO()
        wb.save(buf); buf.seek(0)
        return send_file(buf, download_name="plantilla_stock_fulfillment.xlsx",
                         as_attachment=True,
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@app.route("/admin/reset_movimientos", methods=["POST", "GET"])
def admin_reset_movimientos():
    """Borra TODOS los movimientos + las marcas de órdenes procesadas, para
    permitir un sync limpio desde cero. Hace backup automático de movimientos
    a una tabla temporal por si algo sale mal.

    REQUIERE confirmación EXPLÍCITA: ?confirmar=SI_BORRAR_TODO

    NO TOCA: productos, sku_mapeo, bodegas, stock_bodega, usuarios, audit_log,
             devoluciones, alertas_config.

    Después de ejecutar, los syncs de los marketplaces re-procesarán todas
    las órdenes que todavía estén en sus listados (últimos 30 días típicamente)."""
    if not session.get("logged"): return jsonify({"error": "no autorizado"}), 401

    confirmar = request.args.get("confirmar", "")
    if confirmar != "SI_BORRAR_TODO":
        # Mostrar info de qué se va a borrar antes de ejecutar
        try:
            from inventario import get_conn
            conn = get_conn(); cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM movimientos")
            total_mov = cur.fetchone()[0]
            try:
                cur.execute("SELECT COUNT(*) FROM ordenes_procesadas")
                total_ord = cur.fetchone()[0]
            except:
                total_ord = 0
            cur.close(); conn.close()
            return jsonify({
                "ok": False,
                "modo": "preview",
                "mensaje": "Para confirmar la operación, agrega ?confirmar=SI_BORRAR_TODO a la URL",
                "se_borrarian": {
                    "movimientos": total_mov,
                    "ordenes_procesadas": total_ord
                },
                "se_mantienen": [
                    "productos", "sku_mapeo", "bodegas", "stock_bodega",
                    "usuarios", "audit_log", "devoluciones", "alertas",
                    "configuracion", "import_logs"
                ],
                "url_para_confirmar": "/admin/reset_movimientos?confirmar=SI_BORRAR_TODO"
            })
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # Ejecutar reset con backup
    try:
        from inventario import get_conn
        from datetime import datetime as _dt
        conn = get_conn(); cur = conn.cursor()

        timestamp = _dt.now().strftime("%Y%m%d_%H%M%S")
        backup_movimientos = f"movimientos_backup_{timestamp}"
        backup_ordenes = f"ordenes_procesadas_backup_{timestamp}"

        log = []

        # 1. Backup de movimientos a tabla nueva
        try:
            cur.execute(f'CREATE TABLE "{backup_movimientos}" AS SELECT * FROM movimientos')
            cur.execute(f'SELECT COUNT(*) FROM "{backup_movimientos}"')
            count_mov = cur.fetchone()[0]
            log.append(f"✓ Backup de movimientos creado: {backup_movimientos} ({count_mov} filas)")
        except Exception as e:
            log.append(f"⚠ Error creando backup movimientos: {e}")
            conn.rollback()
            return jsonify({"ok": False, "error": f"No se pudo crear backup: {e}", "log": log}), 500

        # 2. Backup de ordenes_procesadas (si existe)
        try:
            cur.execute(f'CREATE TABLE "{backup_ordenes}" AS SELECT * FROM ordenes_procesadas')
            cur.execute(f'SELECT COUNT(*) FROM "{backup_ordenes}"')
            count_ord = cur.fetchone()[0]
            log.append(f"✓ Backup de ordenes_procesadas creado: {backup_ordenes} ({count_ord} filas)")
        except Exception as e:
            log.append(f"  (ordenes_procesadas no existía o error: {e})")
            conn.rollback()

        # 3. Borrar movimientos
        try:
            cur.execute("DELETE FROM movimientos")
            log.append(f"✓ Movimientos borrados")
        except Exception as e:
            conn.rollback()
            return jsonify({"ok": False, "error": f"Error borrando movimientos: {e}", "log": log}), 500

        # 4. Borrar ordenes_procesadas (si existe)
        try:
            cur.execute("DELETE FROM ordenes_procesadas")
            log.append(f"✓ Ordenes_procesadas borradas")
        except Exception as e:
            log.append(f"  (no se pudo borrar ordenes_procesadas: {e})")
            conn.rollback()
            # Reintentar de nuevo el delete de movimientos
            cur.execute("DELETE FROM movimientos")

        conn.commit()

        # 5. Audit log
        try:
            registrar_audit(session.get("usuario","Sistema"), request.remote_addr,
                            "RESET_MOVIMIENTOS", entidad="movimientos",
                            detalle=f"backup={backup_movimientos}, ord_backup={backup_ordenes}")
            log.append("✓ Audit log registrado")
        except Exception as e:
            log.append(f"  No se pudo registrar audit: {e}")

        cur.close(); conn.close()

        return jsonify({
            "ok": True,
            "mensaje": "Reset completado correctamente",
            "tablas_backup": [backup_movimientos, backup_ordenes],
            "instrucciones_siguientes": [
                "1. Verifica que el panel de Movimientos esté vacío",
                "2. Carga el stock CENTRAL via plantilla Excel (Bodegas → Importar Excel)",
                "3. Carga el stock FULFILLMENT via segunda plantilla Excel",
                "4. Ejecuta los syncs de cada marketplace para traer las órdenes recientes",
                "5. Si algo sale mal, los datos están en las tablas backup mencionadas arriba"
            ],
            "para_recuperar_backup": (
                f"INSERT INTO movimientos SELECT * FROM \"{backup_movimientos}\"; "
                f"INSERT INTO ordenes_procesadas SELECT * FROM \"{backup_ordenes}\";"
            ),
            "log": log
        })
    except Exception as e:
        import traceback
        return jsonify({"ok": False, "error": str(e), "trace": traceback.format_exc()}), 500


@app.route("/admin/listar_backups")
def admin_listar_backups():
    """Lista todas las tablas de backup creadas por reset_movimientos.
    Útil si necesitas recuperar datos o limpiar backups antiguos."""
    if not session.get("logged"): return jsonify({"error": "no autorizado"}), 401
    try:
        from inventario import get_conn
        conn = get_conn(); cur = conn.cursor()
        cur.execute("""
            SELECT tablename FROM pg_tables
            WHERE schemaname = 'public'
              AND (tablename LIKE 'movimientos_backup_%'
                OR tablename LIKE 'ordenes_procesadas_backup_%')
            ORDER BY tablename DESC
        """)
        tablas = []
        for (nombre,) in cur.fetchall():
            try:
                cur.execute(f'SELECT COUNT(*) FROM "{nombre}"')
                cnt = cur.fetchone()[0]
            except:
                cnt = -1
            tablas.append({"tabla": nombre, "filas": cnt})
        cur.close(); conn.close()
        return jsonify({"backups": tablas, "total": len(tablas)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ════════════════════════════════════════════════════════════════════════════
# AUTO-MAPEO DE SKUs ENTRE LUSYNC Y MARKETPLACES
# ════════════════════════════════════════════════════════════════════════════
# Endpoints para traer productos de cada marketplace y matchearlos
# automáticamente contra los productos Lusync por SKU, nombre y precio.
# ════════════════════════════════════════════════════════════════════════════

def _normalizar_texto(s):
    """Normaliza un texto para comparación: lowercase, sin acentos, sin espacios extra."""
    if not s: return ""
    import unicodedata, re
    s = str(s).lower().strip()
    # Quitar acentos
    s = ''.join(c for c in unicodedata.normalize('NFD', s)
                if unicodedata.category(c) != 'Mn')
    # Quitar caracteres no alfanuméricos
    s = re.sub(r'[^a-z0-9\s]', ' ', s)
    # Colapsar espacios
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def _ratio_similitud(s1, s2):
    """Calcula similitud entre dos strings (0.0 a 1.0) usando SequenceMatcher."""
    from difflib import SequenceMatcher
    if not s1 or not s2: return 0.0
    return SequenceMatcher(None, _normalizar_texto(s1), _normalizar_texto(s2)).ratio()


def _calcular_score_match(producto_lusync, item_marketplace, sku_field, title_field, price_field):
    """Calcula score 0-100 de qué tan probable es que sean el mismo producto.

    Prioridad:
      1. SKU exacto del marketplace = SKU Lusync → 100 (match seguro)
      2. SKU Lusync contenido en título o sku del marketplace → 85
      3. Nombre normalizado idéntico → 80
      4. Nombre similitud > 0.85 + precio cercano (±10%) → 70-90
      5. Solo nombre similitud > 0.85 → 50-65
      6. Nombre similitud > 0.70 → 30-50
    """
    sku_lusync = (producto_lusync.get("sku") or "").strip().upper()
    nombre_lusync = producto_lusync.get("nombre") or ""
    precio_lusync = producto_lusync.get("precio") or 0

    sku_mp = str(item_marketplace.get(sku_field, "") or "").strip().upper()
    title_mp = item_marketplace.get(title_field, "") or ""
    price_mp = item_marketplace.get(price_field, 0) or 0
    try: price_mp = float(price_mp)
    except: price_mp = 0

    motivos = []
    score = 0

    # 1. SKU exacto
    if sku_lusync and sku_mp and sku_lusync == sku_mp:
        return 100, ["sku_exacto"]

    # 2. SKU Lusync contenido en sku del marketplace o en título
    if sku_lusync and len(sku_lusync) >= 4:
        if sku_mp and sku_lusync in sku_mp:
            score = 85
            motivos.append("sku_contenido_en_sku_mp")
        elif sku_lusync in (title_mp or "").upper():
            score = 80
            motivos.append("sku_en_titulo")
        if score >= 80:
            return score, motivos

    # 3. Nombre exacto normalizado
    nl = _normalizar_texto(nombre_lusync)
    nm = _normalizar_texto(title_mp)
    if nl and nm and nl == nm:
        score = 80
        motivos.append("nombre_exacto")

    # 4. Similitud de nombre
    if not score:
        sim = _ratio_similitud(nombre_lusync, title_mp)
        if sim >= 0.85:
            score = int(50 + sim * 30)  # 75-80
            motivos.append(f"nombre_sim_{int(sim*100)}%")
        elif sim >= 0.70:
            score = int(30 + sim * 20)  # 44-44
            motivos.append(f"nombre_sim_{int(sim*100)}%")

    # 5. Bonus por precio cercano (si tenemos un score base)
    if score > 0 and precio_lusync and price_mp:
        try:
            diff = abs(float(precio_lusync) - price_mp) / float(precio_lusync)
            if diff < 0.05:  # ±5%
                score += 15
                motivos.append("precio_muy_cercano")
            elif diff < 0.10:  # ±10%
                score += 10
                motivos.append("precio_cercano")
            elif diff < 0.20:  # ±20%
                score += 5
                motivos.append("precio_aproximado")
        except: pass

    return min(score, 99), motivos  # 99 max sin SKU exacto


def _buscar_mejor_match(producto_lusync, items_mp, sku_field, title_field, price_field):
    """Para un producto Lusync, encuentra el item del marketplace con mejor score.
    Devuelve (mejor_match_dict, score, motivos) o (None, 0, [])"""
    if not items_mp:
        return None, 0, []
    mejor = None
    mejor_score = 0
    mejor_motivos = []
    for item in items_mp:
        score, motivos = _calcular_score_match(
            producto_lusync, item, sku_field, title_field, price_field
        )
        if score > mejor_score:
            mejor_score = score
            mejor = item
            mejor_motivos = motivos
            if score >= 100:  # match perfecto, no seguir
                break
    return mejor, mejor_score, mejor_motivos


@app.route("/admin/auto_mapeo_skus", methods=["GET", "POST"])
def admin_auto_mapeo_skus():
    """Trae productos publicados de los marketplaces conectados y propone
    matches contra los productos Lusync.

    Query params:
      ?canales=mercadolibre,paris,walmart,falabella,ripley   (default: todos)
      ?guardar=1   (si 1, guarda automáticamente los matches con score >= 90)
      ?formato=json|excel   (default: json)

    Returns:
      JSON con la propuesta de mapeo, o un Excel descargable.
    """
    if not session.get("logged"): return jsonify({"error": "no autorizado"}), 401

    canales_str = request.args.get("canales", "mercadolibre,paris,walmart,falabella,ripley")
    canales_pedidos = set(c.strip().lower() for c in canales_str.split(",") if c.strip())
    guardar_auto = request.args.get("guardar", "0") == "1"
    formato = request.args.get("formato", "json").lower()
    umbral_auto = int(request.args.get("umbral_auto", 90))  # score mínimo para auto-guardar

    registrar_audit(session.get("usuario","Sistema"), request.remote_addr,
                    "auto_mapeo_skus",
                    detalle=f"canales={canales_pedidos} guardar={guardar_auto}")

    log = []

    # ── 1. Cargar productos Lusync ──
    from inventario import cargar_productos, listar_sku_mapeo
    productos_lusync = cargar_productos()
    log.append(f"Productos Lusync: {len(productos_lusync)}")

    mapeo_actual = {}
    try:
        for fila in listar_sku_mapeo():
            mapeo_actual[fila["sku_lusync"]] = fila
    except Exception as e:
        log.append(f"Error cargando mapeo actual: {e}")

    # ── 2. Traer items de cada marketplace ──
    items_por_canal = {}

    if "mercadolibre" in canales_pedidos:
        try:
            from mercadolibre import obtener_publicaciones_meli
            todos = []
            offset = 0
            for _ in range(20):  # max 20 páginas (1000 items)
                data = obtener_publicaciones_meli(limite=50, offset=offset)
                if not data or not data.get("items"):
                    break
                todos.extend(data["items"])
                if len(data["items"]) < 50:
                    break
                offset += 50
            items_por_canal["mercadolibre"] = todos
            log.append(f"[MELI] {len(todos)} publicaciones obtenidas")
        except Exception as e:
            log.append(f"[MELI] ERROR: {e}")
            items_por_canal["mercadolibre"] = []

    if "paris" in canales_pedidos:
        try:
            from paris import obtener_productos_paris
            todos = []
            offset = 0
            for _ in range(20):
                data = obtener_productos_paris(limite=25, offset=offset)
                if not data:
                    break
                productos = data.get("products", []) if isinstance(data, dict) else (data or [])
                if isinstance(productos, dict):
                    productos = [productos]
                if not productos:
                    break
                # París devuelve formato distinto, normalizamos
                for p in productos:
                    items_paris = {
                        "sellerSku":    p.get("sellerSku") or p.get("sku") or "",
                        "name":         p.get("name") or p.get("productName") or "",
                        "price":        (p.get("price") or {}).get("normal") if isinstance(p.get("price"), dict) else p.get("price"),
                        "stock":        p.get("stock", 0),
                        "status":       p.get("status", "")
                    }
                    todos.append(items_paris)
                if len(productos) < 25:
                    break
                offset += 25
            items_por_canal["paris"] = todos
            log.append(f"[Paris] {len(todos)} productos obtenidos")
        except Exception as e:
            log.append(f"[Paris] ERROR: {e}")
            items_por_canal["paris"] = []

    if "walmart" in canales_pedidos:
        try:
            from walmart import obtener_productos_walmart
            todos = obtener_productos_walmart(limit=200, max_paginas=5)
            items_por_canal["walmart"] = todos
            log.append(f"[Walmart] {len(todos)} productos obtenidos")
        except Exception as e:
            log.append(f"[Walmart] ERROR: {e}")
            items_por_canal["walmart"] = []

    if "falabella" in canales_pedidos:
        try:
            from falabella import obtener_productos_falabella
            todos = []
            offset = 0
            for _ in range(20):
                productos = obtener_productos_falabella(limit=100, offset=offset, filter_status="all")
                if not productos:
                    break
                # Normalizar formato Falabella
                for p in productos:
                    item = {
                        "sellerSku":  p.get("SellerSku") or p.get("sellerSku") or "",
                        "name":       p.get("Name") or p.get("name") or "",
                        "price":      p.get("Price") or p.get("price"),
                        "quantity":   p.get("Quantity") or p.get("quantity") or 0,
                        "status":     p.get("Status") or p.get("status", "")
                    }
                    try: item["price"] = float(item["price"]) if item["price"] else 0
                    except: item["price"] = 0
                    todos.append(item)
                if len(productos) < 100:
                    break
                offset += 100
            items_por_canal["falabella"] = todos
            log.append(f"[Falabella] {len(todos)} productos obtenidos")
        except Exception as e:
            log.append(f"[Falabella] ERROR: {e}")
            items_por_canal["falabella"] = []

    if "ripley" in canales_pedidos:
        try:
            from ripley import obtener_productos_ripley
            todos = obtener_productos_ripley(max_paginas=15, page_size=100)
            items_por_canal["ripley"] = todos
            log.append(f"[Ripley] {len(todos)} productos obtenidos")
        except Exception as e:
            log.append(f"[Ripley] ERROR: {e}")
            items_por_canal["ripley"] = []

    # ── 3. Para cada producto Lusync, buscar mejor match en cada marketplace ──
    # Configuración de campos por canal
    canal_config = {
        "mercadolibre": {"sku_field": "sku_seller", "title": "title", "price": "price"},
        "paris":        {"sku_field": "sellerSku", "title": "name", "price": "price"},
        "walmart":      {"sku_field": "sku", "title": "productName", "price": "price"},
        "falabella":    {"sku_field": "sellerSku", "title": "name", "price": "price"},
        "ripley":       {"sku_field": "shop_sku", "title": "product_title", "price": "price"}
    }

    propuesta_mapeo = []
    auto_guardados = 0
    necesitan_revision = 0
    sin_match = 0

    for p in productos_lusync:
        fila_propuesta = {
            "sku_lusync": p.get("sku", ""),
            "nombre":     p.get("nombre", ""),
            "precio":     p.get("precio", 0),
            "matches":    {}
        }

        # Mapeo actual (para no sobreescribir si ya existe match manual)
        m_actual = mapeo_actual.get(p.get("sku", ""), {})

        for canal, items in items_por_canal.items():
            cfg = canal_config[canal]
            mejor, score, motivos = _buscar_mejor_match(
                p, items, cfg["sku_field"], cfg["title"], cfg["price"]
            )

            # Determinar el SKU del marketplace que se guardaría
            sku_propuesto = ""
            titulo_mp = ""
            precio_mp = 0
            if mejor:
                sku_propuesto = str(mejor.get(cfg["sku_field"], "") or "").strip()
                titulo_mp = mejor.get(cfg["title"], "") or ""
                precio_mp = mejor.get(cfg["price"], 0) or 0

            # Para MELI usamos item_id si no hay sku_seller
            if canal == "mercadolibre" and mejor and not sku_propuesto:
                sku_propuesto = str(mejor.get("item_id", "") or "")

            # SKU actualmente en BD para este canal
            campo_bd = {
                "mercadolibre": "sku_mercadolibre",
                "paris":        "sku_paris",
                "walmart":      "sku_walmart",
                "falabella":    "sku_falabella",
                "ripley":       "sku_ripley"
            }.get(canal, "")
            sku_actual = m_actual.get(campo_bd, "") if campo_bd else ""

            fila_propuesta["matches"][canal] = {
                "sku_propuesto":  sku_propuesto,
                "sku_actual_bd":  sku_actual,
                "score":          score,
                "motivos":        motivos,
                "titulo_mp":      titulo_mp,
                "precio_mp":      precio_mp,
                "estado": (
                    "auto" if score >= umbral_auto else
                    "revisar" if score >= 50 else
                    "sin_match"
                )
            }

        # Contar resúmenes
        algun_auto = any(m["estado"] == "auto" for m in fila_propuesta["matches"].values())
        algun_revisar = any(m["estado"] == "revisar" for m in fila_propuesta["matches"].values())
        if algun_auto:
            auto_guardados += 1
        elif algun_revisar:
            necesitan_revision += 1
        else:
            sin_match += 1

        propuesta_mapeo.append(fila_propuesta)

    # ── 4. Guardar automáticamente los matches con score >= umbral_auto ──
    if guardar_auto:
        from inventario import guardar_sku_mapeo_fila
        guardados_efectivos = 0
        for fila in propuesta_mapeo:
            sku_lus = fila["sku_lusync"]
            m_actual = mapeo_actual.get(sku_lus, {})
            skus_a_guardar = {
                "web":          m_actual.get("sku_web", ""),
                "walmart":      m_actual.get("sku_walmart", ""),
                "paris":        m_actual.get("sku_paris", ""),
                "falabella":    m_actual.get("sku_falabella", ""),
                "ripley":       m_actual.get("sku_ripley", ""),
                "mercadolibre": m_actual.get("sku_mercadolibre", ""),
                "hites":        m_actual.get("sku_hites", "")
            }
            algun_cambio = False
            for canal_name, match_data in fila["matches"].items():
                if match_data["estado"] == "auto" and match_data["sku_propuesto"]:
                    if not skus_a_guardar.get(canal_name):  # solo si no había nada antes
                        skus_a_guardar[canal_name] = match_data["sku_propuesto"]
                        algun_cambio = True
            if algun_cambio:
                try:
                    guardar_sku_mapeo_fila(sku_lus, skus_a_guardar)
                    guardados_efectivos += 1
                except Exception as e:
                    log.append(f"  ERROR guardando {sku_lus}: {e}")
        log.append(f"Auto-guardados: {guardados_efectivos} mapeos actualizados en BD")

    # ── 5. Devolver según formato ──
    if formato == "excel":
        return _generar_excel_propuesta(propuesta_mapeo, items_por_canal, log)

    return jsonify({
        "ok": True,
        "resumen": {
            "total_productos_lusync": len(productos_lusync),
            "con_matches_seguros": auto_guardados,
            "necesitan_revision": necesitan_revision,
            "sin_match": sin_match,
            "umbral_auto": umbral_auto
        },
        "items_por_canal": {c: len(v) for c, v in items_por_canal.items()},
        "log": log,
        "propuesta": propuesta_mapeo
    })


def _generar_excel_propuesta(propuesta_mapeo, items_por_canal, log):
    """Genera un Excel descargable con la propuesta de mapeo."""
    import io, openpyxl
    from datetime import datetime
    from openpyxl.styles import Font, PatternFill, Alignment

    wb = openpyxl.Workbook()

    # Hoja 1: Propuesta principal
    ws = wb.active
    ws.title = "Propuesta de Mapeo"
    headers = [
        "SKU Lusync", "Nombre Lusync", "Precio Lusync",
        "MELI: SKU", "MELI: score", "MELI: título encontrado",
        "PARIS: SKU", "PARIS: score", "PARIS: título encontrado",
        "WALMART: SKU", "WALMART: score", "WALMART: título encontrado",
        "FALABELLA: SKU", "FALABELLA: score", "FALABELLA: título encontrado",
        "RIPLEY: SKU", "RIPLEY: score", "RIPLEY: título encontrado",
    ]
    ws.append(headers)
    # Estilo header
    for c in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=c)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill(start_color="3C3489", end_color="3C3489", fill_type="solid")
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    # Colores por estado
    fill_auto    = PatternFill(start_color="D4EDDA", end_color="D4EDDA", fill_type="solid")  # verde
    fill_revisar = PatternFill(start_color="FFF3CD", end_color="FFF3CD", fill_type="solid")  # amarillo
    fill_nada    = PatternFill(start_color="F8D7DA", end_color="F8D7DA", fill_type="solid")  # rojo

    for fila in propuesta_mapeo:
        row = [fila["sku_lusync"], fila["nombre"], fila["precio"]]
        for canal in ["mercadolibre", "paris", "walmart", "falabella", "ripley"]:
            m = fila["matches"].get(canal, {})
            row.extend([
                m.get("sku_propuesto", ""),
                m.get("score", 0),
                (m.get("titulo_mp", "") or "")[:80]
            ])
        ws.append(row)

        # Aplicar color a las celdas de cada canal según estado
        row_idx = ws.max_row
        for i, canal in enumerate(["mercadolibre", "paris", "walmart", "falabella", "ripley"]):
            m = fila["matches"].get(canal, {})
            estado = m.get("estado", "sin_match")
            fill = fill_auto if estado == "auto" else (fill_revisar if estado == "revisar" else fill_nada)
            for offset in range(3):  # las 3 columnas de cada canal
                col = 4 + (i * 3) + offset
                ws.cell(row=row_idx, column=col).fill = fill

    # Ajustar anchos
    ws.column_dimensions['A'].width = 18
    ws.column_dimensions['B'].width = 40
    ws.column_dimensions['C'].width = 12
    for letter in ['D', 'G', 'J', 'M', 'P']:  # SKU columns
        ws.column_dimensions[letter].width = 18
    for letter in ['E', 'H', 'K', 'N', 'Q']:  # score
        ws.column_dimensions[letter].width = 8
    for letter in ['F', 'I', 'L', 'O', 'R']:  # título
        ws.column_dimensions[letter].width = 35
    ws.row_dimensions[1].height = 30
    ws.freeze_panes = "D2"

    # Hoja 2: instrucciones
    ws2 = wb.create_sheet("Instrucciones")
    instr = [
        ["INSTRUCCIONES — Auto-mapeo de SKUs"],
        [""],
        ["LEYENDA DE COLORES:"],
        ["🟢 Verde:    Score ≥ 90 (match seguro, recomendado guardar)"],
        ["🟡 Amarillo: Score 50-89 (revisar manualmente, puede ser correcto)"],
        ["🔴 Rojo:     Score < 50 (probablemente no hay match, revisa el catálogo)"],
        [""],
        ["CÓMO USAR:"],
        ["1. Revisa esta hoja, especialmente las celdas amarillas y rojas."],
        ["2. Si un match es correcto, no hagas nada (ya está propuesto)."],
        ["3. Si un match es incorrecto, edita la columna 'XXX: SKU' con el SKU correcto."],
        ["4. Si no hay match para un canal, deja la celda vacía."],
        ["5. Cuando termines, guarda este Excel."],
        ["6. Súbelo desde el panel: sección 'Mapeo SKUs' → 'Importar Excel'."],
        [""],
        ["IMPORTANTE:"],
        ["- NO cambies el SKU Lusync (columna A) ni los nombres de columnas."],
        ["- El sistema solo considera la columna 'SKU' de cada canal al importar."],
        ["- Las columnas 'score' y 'título encontrado' son informativas."],
        [""],
        ["LOG DE EJECUCIÓN:"],
    ]
    for i in instr:
        ws2.append(i)
    for line in log:
        ws2.append([line])
    ws2.column_dimensions['A'].width = 90

    # Hoja 3: items crudos por canal (debug)
    for canal, items in items_por_canal.items():
        ws_c = wb.create_sheet(f"Items {canal[:8]}")
        if items:
            keys = list(items[0].keys())
            ws_c.append(keys)
            for it in items:
                ws_c.append([str(it.get(k, ""))[:200] for k in keys])
            for c in range(1, len(keys) + 1):
                ws_c.cell(row=1, column=c).font = Font(bold=True)
            ws_c.freeze_panes = "A2"

    buf = io.BytesIO()
    wb.save(buf); buf.seek(0)
    return send_file(
        buf,
        download_name=f"propuesta_mapeo_skus_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx",
        as_attachment=True,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
