from flask import Flask, request, render_template, session, redirect
import requests
import os
from config import *
from woo import actualizar_stock_woo
from inventario import (cargar_productos, guardar_productos, guardar_producto,
                        registrar_movimiento, cargar_movimientos, cargar_movimientos_hoy,
                        init_db, orden_ya_procesada, marcar_orden_procesada, actualizar_precios,
                        get_configuracion, set_configuracion, set_lead_time)

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
                    registrar_movimiento("salida", p["sku"], p["nombre"], cantidad, "Venta Web", usuario="Sistema", canal="WooCommerce")
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
