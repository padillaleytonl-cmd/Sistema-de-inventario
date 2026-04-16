from flask import Flask, request, render_template_string, session, redirect
import json
import os
import requests

app = Flask(__name__)
app.secret_key = "clave_super_segura"

USUARIO = "admin"
PASSWORD = "1234"

ARCHIVO_PRODUCTOS = "productos.json"

# ---------------- ARCHIVOS ----------------

def cargar_productos():
    if os.path.exists(ARCHIVO_PRODUCTOS):
        with open(ARCHIVO_PRODUCTOS, "r") as f:
            return json.load(f)
    return []

def guardar_productos(productos):
    with open(ARCHIVO_PRODUCTOS, "w") as f:
        json.dump(productos, f)

productos = cargar_productos()

# ---------------- SYNC A WOOCOMMERCE ----------------

def actualizar_stock_woo(sku, stock):
    url = "https://www.babymine.cl/wp-json/custom/v1/actualizar"

    try:
        requests.post(url, json={
            "sku": sku,
            "stock": stock
        })
    except:
        pass

# ---------------- IMPORTAR ----------------

@app.route("/importar_woo")
def importar():
    url = "https://www.babymine.cl/wp-json/custom/v1/productos"
    nuevos = 0

    response = requests.get(url)
    data = response.json()

    for p in data:
        sku = p.get("sku")
        if not sku:
            continue

        existe = any(prod["sku"] == sku for prod in productos)

        if not existe:
            productos.append({
                "sku": sku,
                "nombre": p.get("nombre"),
                "stock": p.get("stock") or 0
            })
            nuevos += 1

    guardar_productos(productos)
    return {"mensaje": f"{nuevos} productos importados"}

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

# ---------------- API ----------------

@app.route("/productos")
def ver_productos():
    return {"productos": productos}

@app.route("/agregar", methods=["POST"])
def agregar():
    data = request.json

    productos.append({
        "sku": data["sku"],
        "nombre": data["nombre"],
        "stock": int(data["stock"])
    })

    guardar_productos(productos)
    return {"ok": True}

@app.route("/entrada", methods=["POST"])
def entrada():
    data = request.json

    for p in productos:
        if p["sku"] == data["sku"]:
            p["stock"] += int(data["cantidad"])

            actualizar_stock_woo(p["sku"], p["stock"])

            guardar_productos(productos)
            return {"ok": True}

    return {"error": "no encontrado"}

@app.route("/salida", methods=["POST"])
def salida():
    data = request.json

    for p in productos:
        if p["sku"] == data["sku"]:
            p["stock"] -= int(data["cantidad"])

            actualizar_stock_woo(p["sku"], p["stock"])

            guardar_productos(productos)
            return {"ok": True}

    return {"error": "no encontrado"}

# ---------------- PANEL ----------------

@app.route("/panel")
def panel():
    if not session.get("logged"):
        return redirect("/login")

    return render_template_string("""
    <style>
    body { font-family: Arial; background:#f5f6ff; padding:20px; }
    h1 { color:#222163; }
    button { background:#222163; color:white; padding:10px; border:none; border-radius:5px; }
    button:hover { background:#8576FF; }
    input { padding:8px; margin:5px; }
    table { width:100%; margin-top:20px; border-collapse: collapse; }
    th { background:#8576FF; color:white; padding:10px; }
    td { padding:8px; border:1px solid #ddd; }
    </style>

    <h1>📦 Inventario BabyMine</h1>

    <input id="buscador" placeholder="Buscar producto..." onkeyup="buscar()">

    <br><br>

    <button onclick="importar()">Importar Woo</button>

    <br><br>

    <input id="sku" placeholder="SKU">
    <input id="nombre" placeholder="Nombre">
    <input id="stock" type="number">
    <button onclick="crear()">Crear</button>

    <br><br>

    <input id="skuE" placeholder="SKU">
    <input id="cantE" type="number">
    <button onclick="entrada()">Entrada</button>

    <br><br>

    <input id="skuS" placeholder="SKU">
    <input id="cantS" type="number">
    <button onclick="salida()">Salida</button>

    <table>
        <thead>
            <tr><th>SKU</th><th>Nombre</th><th>Stock</th></tr>
        </thead>
        <tbody id="tabla"></tbody>
    </table>

    <script>
    let productosGlobal = [];

    function importar(){
        fetch("/importar_woo").then(r=>r.json()).then(d=>{
            alert(JSON.stringify(d));
            cargar();
        });
    }

    function crear(){
        fetch("/agregar",{method:"POST",headers:{'Content-Type':'application/json'},
        body:JSON.stringify({sku:sku.value,nombre:nombre.value,stock:stock.value})})
        .then(()=>cargar())
    }

    function entrada(){
        fetch("/entrada",{method:"POST",headers:{'Content-Type':'application/json'},
        body:JSON.stringify({sku:skuE.value,cantidad:cantE.value})})
        .then(()=>cargar())
    }

    function salida(){
        fetch("/salida",{method:"POST",headers:{'Content-Type':'application/json'},
        body:JSON.stringify({sku:skuS.value,cantidad:cantS.value})})
        .then(()=>cargar())
    }

    function cargar(){
        fetch("/productos").then(r=>r.json()).then(d=>{
            productosGlobal = d.productos;
            render(productosGlobal);
        });
    }

    function render(lista){
        let html="";
        lista.forEach(p=>{
            html+=`<tr><td>${p.sku}</td><td>${p.nombre}</td><td>${p.stock}</td></tr>`
        });
        tabla.innerHTML=html;
    }

    function buscar(){
        let texto = buscador.value.toLowerCase();
        let filtrados = productosGlobal.filter(p =>
            p.nombre.toLowerCase().includes(texto) ||
            p.sku.toLowerCase().includes(texto)
        );
        render(filtrados);
    }

    cargar();
    </script>
    """)

# ---------------- RUN ----------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))