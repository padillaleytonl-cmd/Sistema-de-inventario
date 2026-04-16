from flask import Flask, request, render_template_string, session, redirect
import json
import os
import requests

app = Flask(__name__)
app.secret_key = "clave_super_segura"

USUARIO = "padillaleytonl@gmail.com"
PASSWORD = "Pii.120715"

WC_KEY = "ck_e664fc41146f00841760e9f5f3da573926409950"
WC_SECRET = "cs_00e75c96b658883032a63ca9cb287c480b0f2a4b"

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
movimientos = []
ordenes_procesadas = set()

# ---------------- ACTUALIZAR WOO ----------------

def actualizar_stock_woo(sku, stock):

    res = requests.get(
        "https://www.babymine.cl/wp-json/wc/v3/products",
        params={
            "consumer_key": WC_KEY,
            "consumer_secret": WC_SECRET,
            "sku": sku
        }
    )

    if res.status_code != 200:
        return

    data = res.json()
    if not data:
        return

    producto = data[0]

    if producto["type"] == "simple":
        requests.put(
            f"https://www.babymine.cl/wp-json/wc/v3/products/{producto['id']}",
            params={
                "consumer_key": WC_KEY,
                "consumer_secret": WC_SECRET
            },
            json={"stock_quantity": stock}
        )

    if producto["type"] == "variation":
        parent_id = producto["parent_id"]

        requests.put(
            f"https://www.babymine.cl/wp-json/wc/v3/products/{parent_id}/variations/{producto['id']}",
            params={
                "consumer_key": WC_KEY,
                "consumer_secret": WC_SECRET
            },
            json={"stock_quantity": stock}
        )

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

            variaciones = res_var.json()

            for v in variaciones:
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
    motivo = data.get("motivo", "Ingreso")

    for p in productos:
        if p["sku"] == data["sku"]:
            p["stock"] += int(data["cantidad"])

            movimientos.append(f"➕ {motivo} | {p['nombre']} (+{data['cantidad']})")

            actualizar_stock_woo(p["sku"], p["stock"])

            guardar_productos(productos)
            return {"ok": True}

    return {"error": "no encontrado"}

# ---------------- SALIDA ----------------

@app.route("/salida", methods=["POST"])
def salida():
    data = request.json
    motivo = data.get("motivo", "Salida")

    for p in productos:
        if p["sku"] == data["sku"]:
            p["stock"] -= int(data["cantidad"])

            movimientos.append(f"➖ {motivo} | {p['nombre']} (-{data['cantidad']})")

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

    ordenes = res.json()

    nuevos = 0

    for o in ordenes:

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
        nuevos += 1

    guardar_productos(productos)

    return {"mensaje": f"{nuevos} ordenes sincronizadas"}

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

    return render_template_string("""
    <style>
    body { font-family: Arial; background:#f5f6ff; padding:20px; }
    h1 { color:#222163; }
    button { background:#222163; color:white; padding:10px; border:none; border-radius:5px; }
    button:hover { background:#8576FF; }
    input, select { padding:8px; margin:5px; }
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

    <h3>Entrada</h3>
    <input id="skuE" placeholder="SKU">
    <input id="cantE" type="number">
    <select id="motivoEntrada">
        <option>Ingreso mercadería</option>
        <option>Devolución</option>
        <option>Otro</option>
    </select>
    <button onclick="entrada()">Entrada</button>

    <h3>Salida</h3>
    <input id="skuS" placeholder="SKU">
    <input id="cantS" type="number">
    <select id="motivoSalida">
        <option>Venta tienda</option>
        <option>Merma</option>
        <option>Otro</option>
    </select>
    <button onclick="salida()">Salida</button>

    <h3>Historial</h3>
    <div id="historial"></div>

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
        body:JSON.stringify({sku:skuE.value,cantidad:cantE.value,motivo:motivoEntrada.value})})
        .then(()=>cargar())
    }

    function salida(){
        fetch("/salida",{method:"POST",headers:{'Content-Type':'application/json'},
        body:JSON.stringify({sku:skuS.value,cantidad:cantS.value,motivo:motivoSalida.value})})
        .then(()=>cargar())
    }

    function cargar(){
        fetch("/productos").then(r=>r.json()).then(d=>{
            productosGlobal = d.productos;
            render(productosGlobal);
        });

        fetch("/movimientos").then(r=>r.json()).then(d=>{
            historial.innerHTML = d.movimientos.map(m=>"<p>"+m+"</p>").join("");
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

    setInterval(()=>{
        fetch("/sync_ordenes")
    },10000)

    cargar();
    </script>
    """)

# ---------------- RUN ----------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))