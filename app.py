from flask import Flask, request, render_template, session, redirect
import requests
import os
from config import *
from woo import actualizar_stock_woo
from inventario import (cargar_productos, guardar_productos, guardar_producto,
                        registrar_movimiento, cargar_movimientos, init_db)

app = Flask(__name__)
app.secret_key = "clave_super_segura"

init_db()

ordenes_procesadas = set()

@app.route("/agregar", methods=["POST"])
def agregar():
    data = request.json
    p = {"sku": data["sku"], "nombre": data["nombre"], "stock": int(data["stock"])}
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
                guardar_producto({"sku": sku, "nombre": p["name"], "stock": p.get("stock_quantity") or 0})
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
                    guardar_producto({"sku": sku, "nombre": f"{p['name']} - {sku}", "stock": v.get("stock_quantity") or 0})
                    nuevos += 1

    return {"mensaje": f"{nuevos} productos importados"}

@app.route("/entrada", methods=["POST"])
def entrada():
    data = request.json
    productos = cargar_productos()
    for p in productos:
        if p["sku"] == data["sku"]:
            p["stock"] += int(data["cantidad"])
            guardar_producto(p)
            registrar_movimiento("entrada", p["sku"], p["nombre"], int(data["cantidad"]), data.get("motivo"))
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
            registrar_movimiento("salida", p["sku"], p["nombre"], int(data["cantidad"]), data.get("motivo"))
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
    for o in res.json():
        if o["id"] in ordenes_procesadas:
            continue
        for item in o["line_items"]:
            sku = item.get("sku")
            cantidad = item.get("quantity")
            for p in productos:
                if p["sku"] == sku:
                    p["stock"] -= cantidad
                    guardar_producto(p)
                    registrar_movimiento("salida", p["sku"], p["nombre"], cantidad, "Venta Web")
                    actualizar_stock_woo(p["sku"], p["stock"])
        ordenes_procesadas.add(o["id"])

    return {"ok": True}

@app.route("/productos")
def ver_productos():
    return {"productos": cargar_productos()}

@app.route("/movimientos")
def ver_movimientos():
    return {"movimientos": cargar_movimientos()}

# ── LOGIN / PANEL ──

@app.route("/")
def home():
    if session.get("logged"):
        return redirect("/panel")
    return render_template("panel.html", logged=False)

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
    return {"ok": True}

@app.route("/panel")
def panel():
    if not session.get("logged"):
        return redirect("/")
    return render_template("panel.html", logged=True)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
