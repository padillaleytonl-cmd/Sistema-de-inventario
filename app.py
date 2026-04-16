from flask import Flask, request, render_template_string, session, redirect
import json
import os
from datetime import datetime
import requests

app = Flask(__name__)
app.secret_key = "clave_super_segura"

USUARIO = "padillaleytonl@gmail.com"
PASSWORD = "Pii.120715"

WC_URL = "https://www.babymine.cl/wp-json/wc/v3/products"
WC_KEY = "ck_0775bcdb4ee90873a05fd391da35d49b9f5f7706"
WC_SECRET = "cs_df78d864adfeac5dc2c968a726bddb361df2e635"

ARCHIVO_PRODUCTOS = "productos.json"
ARCHIVO_MOVIMIENTOS = "movimientos.json"

# ---------------- ARCHIVOS ----------------

def cargar_productos():
    if os.path.exists(ARCHIVO_PRODUCTOS):
        with open(ARCHIVO_PRODUCTOS, "r") as f:
            return json.load(f)
    return []

def guardar_productos(productos):
    with open(ARCHIVO_PRODUCTOS, "w") as f:
        json.dump(productos, f)

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

def importar_productos_woocommerce():
    page = 1
    nuevos = 0

    while True:
 headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
}

response = requests.get(
    WC_URL,
    headers=headers,
    params={
        "consumer_key": WC_KEY,
        "consumer_secret": WC_SECRET,
        "per_page": 100,
        "page": page
    }
)

        if response.status_code != 200:
            return {"error": f"Error Woo: {response.status_code}", "detalle": response.text}

        data = response.json()

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

    return {"mensaje": f"{nuevos} productos importados"}

# ---------------- ESTADISTICAS ----------------

def stats():
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
        <input name="user"><br><br>
        <input name="password" type="password"><br><br>
        <button>Entrar</button>
    </form>
    """

# ---------------- API ----------------

@app.route("/productos")
def ver_productos():
    return {"productos": productos}

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
    return {"mensaje": "ok"}

@app.route("/entrada", methods=["POST"])
def entrada():
    data = request.json
    for p in productos:
        if p["sku"] == data["sku"]:
            p["stock"] += int(data["cantidad"])
            guardar_productos(productos)
            return {"mensaje": "ok"}
    return {"error": "no encontrado"}

@app.route("/salida", methods=["POST"])
def salida():
    data = request.json
    for p in productos:
        if p["sku"] == data["sku"]:
            p["stock"] -= int(data["cantidad"])
            guardar_productos(productos)
            return {"mensaje": "ok"}
    return {"error": "no encontrado"}

@app.route("/importar_woo")
def importar():
    return importar_productos_woocommerce()

@app.route("/stats")
def estadisticas():
    return stats()

# ---------------- PANEL PRO ----------------

@app.route("/panel")
def panel():
    if not session.get("logged"):
        return redirect("/login")

    return render_template_string("""
    <style>
    body { font-family: Arial; background:#f5f6ff; padding:20px; }
    h1 { color:#222163; }
    button {
        background:#222163;
        color:white;
        border:none;
        padding:10px;
        margin-top:5px;
        cursor:pointer;
        border-radius:5px;
    }
    button:hover { background:#8576FF; }
    input { padding:8px; margin:3px; }
    table { width:100%; margin-top:20px; border-collapse: collapse; }
    th { background:#8576FF; color:white; padding:10px; }
    td { padding:8px; border:1px solid #ddd; }
    .card {
        background:white;
        padding:15px;
        border-radius:10px;
        margin-bottom:15px;
    }
    </style>

    <h1>📦 Inventario BabyMine</h1>

    <div class="card">
        <button onclick="importar()">🔄 Importar WooCommerce</button>
        <p id="mensaje"></p>
    </div>

    <div class="card">
        <h3>Crear producto</h3>
        <input id="sku" placeholder="SKU">
        <input id="nombre" placeholder="Nombre">
        <input id="stock" type="number" value="0">
        <button onclick="crear()">Crear</button>
    </div>

    <div class="card">
        <h3>Entrada</h3>
        <input id="skuE" placeholder="SKU">
        <input id="cantE" type="number" value="1">
        <button onclick="entrada()">Ingresar</button>
    </div>

    <div class="card">
        <h3>Salida</h3>
        <input id="skuS" placeholder="SKU">
        <input id="cantS" type="number" value="1">
        <button onclick="salida()">Salida</button>
    </div>

    <div class="card">
        <h3>📊 Estadísticas</h3>
        <div id="stats"></div>
    </div>

    <div class="card">
        <h3>📦 Stock</h3>
        <table>
            <thead>
                <tr><th>SKU</th><th>Nombre</th><th>Stock</th></tr>
            </thead>
            <tbody id="tabla"></tbody>
        </table>
    </div>

    <script>
    function msg(t){ document.getElementById("mensaje").innerText=t }

    function crear(){
        fetch("/agregar",{method:"POST",headers:{'Content-Type':'application/json'},
        body:JSON.stringify({sku:sku.value,nombre:nombre.value,stock:stock.value})})
        .then(()=>{msg("Producto creado"); cargar();})
    }

    function entrada(){
        fetch("/entrada",{method:"POST",headers:{'Content-Type':'application/json'},
        body:JSON.stringify({sku:skuE.value,cantidad:cantE.value})})
        .then(()=>{msg("Entrada OK"); cargar();})
    }

    function salida(){
        fetch("/salida",{method:"POST",headers:{'Content-Type':'application/json'},
        body:JSON.stringify({sku:skuS.value,cantidad:cantS.value})})
        .then(()=>{msg("Salida OK"); cargar();})
    }

    function importar(){
        fetch("/importar_woo")
        .then(r=>r.json())
        .then(d=>{msg(JSON.stringify(d)); cargar();})
    }

    function cargar(){
        fetch("/productos").then(r=>r.json()).then(d=>{
            let html="";
            d.productos.forEach(p=>{
                html+=`<tr><td>${p.sku}</td><td>${p.nombre}</td><td>${p.stock}</td></tr>`
            });
            tabla.innerHTML=html;
        });

        fetch("/stats").then(r=>r.json()).then(d=>{
            stats.innerText = "Total: "+d.total+" | Stock: "+d.stock_total+" | Bajo: "+d.bajo_stock;
        });
    }

    cargar();
    </script>
    """)

# ---------------- RUN ----------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))