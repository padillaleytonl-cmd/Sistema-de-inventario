from flask import Flask, request, render_template, session, redirect
import requests
import os

from config import *
from woo import actualizar_stock_woo
from inventario import cargar_productos, guardar_productos

app = Flask(__name__)
app.secret_key = "clave_super_segura"

# ---------------- DATOS ----------------

productos = cargar_productos()
movimientos = []
ordenes_procesadas = set()

# ---------------- AGREGAR ----------------

@app.route("/agregar", methods=["POST"])
def agregar():
    data = request.json

    producto = {
        "sku": data["sku"],
        "nombre": data["nombre"],
        "stock": int(data["stock"])
    }

    productos.append(producto)
    guardar_productos(productos)

    return {"ok": True}

# ---------------- IMPORTAR WOO ----------------

@app.route("/importar_woo")
def importar():
    nuevos = 0

    res = requests.get(
        "https://www.babymine.cl/wp-json/wc/v3/products",
        params={
            "consumer_key": WC_KEY,
            "consumer_secret": WC_SECRET,
            "per_page": 100
        }
    )

    if res.status_code != 200:
        return {"error": "Woo error"}

    data = res.json()

    for p in data:

        if p["type"] == "simple":
            sku = p.get("sku") or str(p.get("id"))

            if not any(prod["sku"] == sku for prod in productos):
                productos.append({
                    "sku": sku,
                    "nombre": p["name"],
                    "stock": p.get("stock_quantity") or 0
                })
                nuevos += 1

        if p["type"] == "variable":
            res_var = requests.get(
                f"https://www.babymine.cl/wp-json/wc/v3/products/{p['id']}/variations",
                params={
                    "consumer_key": WC_KEY,
                    "consumer_secret": WC_SECRET,
                    "per_page": 100
                }
            )

            if res_var.status_code != 200:
                continue

            for v in res_var.json():
                sku = v.get("sku") or str(v.get("id"))

                if not any(prod["sku"] == sku for prod in productos):
                    productos.append({
                        "sku": sku,
                        "nombre": f"{p['name']} - {sku}",
                        "stock": v.get("stock_quantity") or 0
                    })
                    nuevos += 1

    guardar_productos(productos)
    return {"mensaje": f"{nuevos} productos importados"}

# ---------------- ENTRADA ----------------

@app.route("/entrada", methods=["POST"])
def entrada():
    data = request.json

    for p in productos:
        if p["sku"] == data["sku"]:
            p["stock"] += int(data["cantidad"])

            movimientos.append(f"➕ {data.get('motivo')} | {p['nombre']} (+{data['cantidad']})")

            actualizar_stock_woo(p["sku"], p["stock"])
            guardar_productos(productos)

            return {"ok": True}

    return {"error": "no encontrado"}

# ---------------- SALIDA ----------------

@app.route("/salida", methods=["POST"])
def salida():
    data = request.json

    for p in productos:
        if p["sku"] == data["sku"]:
            p["stock"] -= int(data["cantidad"])

            movimientos.append(f"➖ {data.get('motivo')} | {p['nombre']} (-{data['cantidad']})")

            actualizar_stock_woo(p["sku"], p["stock"])
            guardar_productos(productos)

            return {"ok": True}

    return {"error": "no encontrado"}

# ---------------- SYNC ORDENES ----------------

@app.route("/sync_ordenes")
def sync_ordenes():
    res = requests.get(
        "https://www.babymine.cl/wp-json/wc/v3/orders",
        params={
            "consumer_key": WC_KEY,
            "consumer_secret": WC_SECRET,
            "status": "processing"
        }
    )

    if res.status_code != 200:
        return {"error": "Woo error"}

    for o in res.json():

        if o["id"] in ordenes_procesadas:
            continue

        for item in o["line_items"]:
            sku = item.get("sku")
            cantidad = item.get("quantity")

            for p in productos:
                if p["sku"] == sku:
                    p["stock"] -= cantidad

                    movimientos.append(f"🛒 Venta Web | {p['nombre']} (-{cantidad})")

                    actualizar_stock_woo(p["sku"], p["stock"])

        ordenes_procesadas.add(o["id"])

    guardar_productos(productos)

    return {"ok": True}

# ---------------- API ----------------

@app.route("/productos")
def ver_productos():
    return {"productos": productos}

@app.route("/movimientos")
def ver_movimientos():
    return {"movimientos": movimientos[-20:]}

# ---------------- LOGIN ----------------

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.form["user"] == USUARIO and request.form["password"] == PASSWORD:
            session["logged"] = True
            return redirect("/panel")
        return "Error login"

    return """
    <form method="POST">
    <input name="user">
    <input name="password" type="password">
    <button>Entrar</button>
    </form>
    """

# ---------------- PANEL ----------------

@app.route("/panel")
def panel():
    if not session.get("logged"):
        return redirect("/login")

    return render_template("panel.html")

# ---------------- RUN ----------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))