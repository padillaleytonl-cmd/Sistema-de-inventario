from flask import Flask, request, render_template_string, session, redirect
import json
import os
from datetime import datetime
import requests

app = Flask(__name__)
app.secret_key = "clave_super_segura"

# 🔐 LOGIN
USUARIO = "padillaleytonl@gmail.com"
PASSWORD = "Pii.120715"

# 🔗 WOOCOMMERCE
WC_URL = "https://www.babymine.cl/wp-json/wc/v3/products"
WC_KEY = "ck_0775bcdb4ee90873a05fd391da35d49b9f5f7706"
WC_SECRET = "cs_df78d864adfeac5dc2c968a726bddb361df2e635"

ARCHIVO_PRODUCTOS = "productos.json"
ARCHIVO_MOVIMIENTOS = "movimientos.json"

# ---------------- PRODUCTOS ----------------

def cargar_productos():
    if os.path.exists(ARCHIVO_PRODUCTOS):
        with open(ARCHIVO_PRODUCTOS, "r") as f:
            return json.load(f)
    return []

def guardar_productos(productos):
    with open(ARCHIVO_PRODUCTOS, "w") as f:
        json.dump(productos, f)

# ---------------- MOVIMIENTOS ----------------

def cargar_movimientos():
    if os.path.exists(ARCHIVO_MOVIMIENTOS):
        with open(ARCHIVO_MOVIMIENTOS, "r") as f:
            return json.load(f)
    return []

def guardar_movimientos(movs):
    with open(ARCHIVO_MOVIMIENTOS, "w") as f:
        json.dump(movs, f)

productos = cargar_productos()
movimientos = cargar_movimientos()

# ---------------- WOOCOMMERCE ----------------

def actualizar_stock_woocommerce(sku, stock):
    response = requests.get(WC_URL, auth=(WC_KEY, WC_SECRET), params={"sku": sku})

    if response.status_code != 200:
        return

    data = response.json()

    if data:
        product_id = data[0]["id"]

        requests.put(
            f"{WC_URL}/{product_id}",
            auth=(WC_KEY, WC_SECRET),
            json={"stock_quantity": stock}
        )

def importar_productos_woocommerce():
    page = 1
    nuevos = 0

    while True:
        response = requests.get(
            WC_URL,
            auth=(WC_KEY, WC_SECRET),
            params={"per_page": 100, "page": page}
        )

        if response.status_code != 200:
            break

        try:
            data = response.json()
        except:
            break

        if not data:
            break

        for p in data:
            sku = p.get("sku") or str(p.get("id"))
            nombre = p.get("name")
            stock = p.get("stock_quantity") or 0

            existe = any(prod["sku"] == sku for prod in productos)

            if not existe:
                productos.append({
                    "sku": sku,
                    "nombre": nombre,
                    "stock": stock
                })
                nuevos += 1

        page += 1

    guardar_productos(productos)
    return nuevos

# ---------------- ESTADISTICAS ----------------

def calcular_estadisticas():
    return {
        "total": len(productos),
        "stock_total": sum(p["stock"] for p in productos),
        "bajo_stock": len([p for p in productos if p["stock"] < 5])
    }

# ---------------- LOGIN ----------------

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.form["user"] == USUARIO and request.form["password"] == PASSWORD:
            session["logged"] = True
            return redirect("/panel")
        return "Credenciales incorrectas"

    return """
    <h2>Login</h2>
    <form method="POST">
        <input name="user" placeholder="Usuario"><br><br>
        <input name="password" type="password" placeholder="Clave"><br><br>
        <button>Ingresar</button>
    </form>
    """

# ---------------- PANEL ----------------

@app.route("/panel")
def panel():
    if not session.get("logged"):
        return redirect("/login")

    return render_template_string("""
    <h1>📦 Inventario BabyMine</h1>

    <h3>📊 Estadísticas</h3>
    <div id="stats"></div>

    <h3>Productos</h3>
    <table border="1" id="tabla">
        <thead>
            <tr>
                <th>SKU</th>
                <th>Nombre</th>
                <th>Stock</th>
            </tr>
        </thead>
        <tbody></tbody>
    </table>

    <script>
    function cargar(){
        fetch("/productos")
        .then(r => r.json())
        .then(data => {
            let html = "";
            data.productos.forEach(p=>{
                html += `<tr>
                    <td>${p.sku}</td>
                    <td>${p.nombre}</td>
                    <td>${p.stock}</td>
                </tr>`;
            });
            document.querySelector("#tabla tbody").innerHTML = html;
        });

        fetch("/stats")
        .then(r => r.json())
        .then(d => {
            document.getElementById("stats").innerHTML =
                "Total: " + d.total +
                " | Stock: " + d.stock_total +
                " | Bajo stock: " + d.bajo_stock;
        });
    }

    cargar();
    </script>
    """)

# ---------------- RUTAS ----------------

@app.route("/")
def inicio():
    return "Sistema funcionando 🚀"

@app.route("/productos")
def ver_productos():
    return {"productos": productos}

@app.route("/importar_woo")
def importar():
    return {"mensaje": importar_productos_woocommerce()}

@app.route("/stats")
def stats():
    return calcular_estadisticas()

# ---------------- RUN ----------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))