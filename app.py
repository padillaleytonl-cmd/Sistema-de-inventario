from flask import Flask, request, render_template, session, redirect
import requests
import os
from config import *
from walmart import (actualizar_stock_walmart, actualizar_precio_walmart,
                     obtener_ordenes_walmart, confirmar_orden_walmart,
                     verificar_conexion_walmart)
from woo import actualizar_stock_woo
from inventario import (cargar_productos, guardar_productos, guardar_producto,
                        registrar_movimiento, cargar_movimientos, cargar_movimientos_hoy,
                        init_db, orden_ya_procesada, marcar_orden_procesada, actualizar_precios,
                        get_configuracion, set_configuracion, set_lead_time, eliminar_producto,
                        orden_ya_procesada_texto, marcar_orden_procesada_texto)

app = Flask(__name__)
app.secret_key = "clave_super_segura"

init_db()

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
    data = request.json
    productos = cargar_productos()
    for p in productos:
        if p["sku"] == data["sku"]:
            p["stock"] += int(data["cantidad"])
            guardar_producto(p)
            registrar_movimiento("entrada", p["sku"], p["nombre"], int(data["cantidad"]), data.get("motivo"), usuario="Luis Padilla", canal="Manual")
            actualizar_stock_woo(p["sku"], p["stock"])
            actualizar_stock_walmart(p["sku"], p["stock"])
            return {"ok": True}
    return {"error": "no encontrado"}

@app.route("/salida", methods=["POST"])
def salida():
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
            return {"ok": True}
    return {"error": "no encontrado"}

@app.route("/sync_ordenes")
def sync_ordenes():
    res = requests.get(
        "https://www.babymine.cl/wp-json/wc/v3/orders",
        params={"consumer_key": WC_KEY, "consumer_secret": WC_SECRET, "status": "processing"}
    )
    if res.status_code != 200:
        return {"error": "Woo error"}

    productos = cargar_productos()
    nuevas = 0

    for o in res.json():
        if orden_ya_procesada(o["id"]):
            continue
        for item in o["line_items"]:
            sku = item.get("sku")
            cantidad = item.get("quantity")
            for p in productos:
                if p["sku"] == sku:
                    p["stock"] -= cantidad
                    guardar_producto(p)
                    registrar_movimiento("salida", p["sku"], p["nombre"], cantidad, "Venta Web", usuario="Sistema", canal="WooCommerce", orden_id=str(o["id"]))
                    actualizar_stock_woo(p["sku"], p["stock"])
        marcar_orden_procesada(o["id"])
        nuevas += 1

    return {"ok": True, "nuevas_ordenes": nuevas}

@app.route("/movimientos_hoy")
def movimientos_hoy():
    return {"ventas": cargar_movimientos_hoy()}

@app.route("/productos")
def ver_productos():
    return {"productos": cargar_productos()}

@app.route("/movimientos")
def ver_movimientos():
    limite = int(request.args.get("limite", 20))
    return {"movimientos": cargar_movimientos(limite)}

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
    if not session.get("logged"):
        return {"error": "no autorizado"}, 401

    productos = cargar_productos()
    nuevas = 0
    errores = []

    # Walmart Chile usa Created y Acknowledged para ordenes pendientes
    for estado in ["Created", "Acknowledged"]:
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
    for estado in ["Created", "Acknowledged", "Shipped"]:
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
    chile_tz = pytz.timezone('America/Santiago')
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

    for estado in ["Created", "Acknowledged"]:
        ordenes = obtener_ordenes_walmart(estado)
        log.append(f"Estado {estado}: {len(ordenes)} ordenes")

        for o in ordenes:
            order_id = o.get("purchaseOrderId")
            if not order_id:
                log.append("Sin order_id, saltando")
                continue

            order_hash = abs(hash(str(order_id))) % (10**15)
            ya = orden_ya_procesada(order_hash)
            log.append(f"Orden {order_id} hash:{order_hash} ya_procesada:{ya}")

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
                                            usuario="Sistema", canal="Walmart")
                        actualizar_stock_woo(p["sku"], p["stock"])
                        actualizar_stock_walmart(p["sku"], p["stock"])
                        log.append(f"  ✅ {p['nombre']} stock:{stock_antes}→{p['stock']}")

                if not encontrado:
                    log.append(f"  ❌ SKU {sku} no encontrado en Lusync")

            marcar_orden_procesada_texto(customer_order_id)
            nuevas += 1

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
        return {"ok": True}
    return {"ok": False}

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")

@app.route("/panel")
def panel():
    if not session.get("logged"):
        return redirect("/")
    return render_template("panel.html")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
