from flask import Flask, request, render_template_string
import json
import os
from datetime import datetime
import requests

WC_URL = "https://www.babymine.cl/wp-json/wc/v3/products"
WC_KEY = "ck_e5f878ffebcc4af9d43d14248928e46fdbda4d24"
WC_SECRET = "cs_9dabb67d802051d67d61d49913ceb04a6cdf331b"

app = Flask(__name__)

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

productos = cargar_productos()

# ---------------- MOVIMIENTOS ----------------

def cargar_movimientos():
    if os.path.exists(ARCHIVO_MOVIMIENTOS):
        with open(ARCHIVO_MOVIMIENTOS, "r") as f:
            return json.load(f)
    return []

def guardar_movimientos(movs):
    with open(ARCHIVO_MOVIMIENTOS, "w") as f:
        json.dump(movs, f)

def actualizar_stock_woocommerce(sku, stock):
    response = requests.get(
        WC_URL,
        auth=(WC_KEY, WC_SECRET),
        params={"sku": sku}
    )

    productos = response.json()

    if productos:
        product_id = productos[0]["id"]

        requests.put(
            f"{WC_URL}/{product_id}",
            auth=(WC_KEY, WC_SECRET),
            json={"stock_quantity": stock}
        )


# ---------------- RUTAS ----------------

@app.route("/")
def inicio():
    return "Sistema PRO inventario"

@app.route("/productos")
def ver_productos():
    return {"productos": productos}

@app.route("/movimientos")
def ver_movimientos():
    return {"movimientos": movimientos}

# ---------------- AGREGAR PRODUCTO ----------------

@app.route("/agregar", methods=["POST"])
def agregar_producto():
    data = request.get_json()
    sku = data.get("sku")
    nombre = data.get("nombre")
    stock = int(data.get("stock", 0))

    producto = {
        "sku": sku,
        "nombre": nombre,
        "stock": stock
    }

    productos.append(producto)
    guardar_productos(productos)

    return {"mensaje": "Producto agregado", "producto": producto}

# ---------------- ENTRADA ----------------

@app.route("/entrada", methods=["POST"])
def entrada_stock():
    data = request.get_json()
    sku = data.get("sku")
    cantidad = int(data.get("cantidad", 0))

    for producto in productos:
        if producto["sku"] == sku:
            producto["stock"] += cantidad

            actualizar_stock_woocommerce(sku, producto["stock"])
		
            movimientos.append({
                "tipo": "entrada",
                "producto": producto["nombre"],
                "sku": sku,
                "cantidad": cantidad,
                "fecha": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            })

            guardar_productos(productos)
            guardar_movimientos(movimientos)

            return {"mensaje": "Stock actualizado", "producto": producto}

    return {"error": "Producto no encontrado"}

# ---------------- SALIDA ----------------

@app.route("/salida", methods=["POST"])
def salida_stock():
    data = request.get_json()
    sku = data.get("sku")
    cantidad = int(data.get("cantidad", 0))

    for producto in productos:
        if producto["sku"] == sku:
            if producto["stock"] >= cantidad:
                producto["stock"] -= cantidad

                actualizar_stock_woocommerce(sku, producto["stock"])

                movimientos.append({
                    "tipo": "salida",
                    "producto": producto["nombre"],
                    "sku": sku,
                    "cantidad": cantidad,
                    "fecha": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                })

                guardar_productos(productos)
                guardar_movimientos(movimientos)

                return {"mensaje": "Stock actualizado", "producto": producto}
            else:
                return {"error": "Stock insuficiente"}

    return {"error": "Producto no encontrado"}

# ---------------- PANEL PRO ----------------

@app.route("/panel")
def panel():
    return render_template_string("""
    <html>
    <head>
        <style>
            body {
                font-family: Arial;
                background: #f5f7fb;
                padding: 20px;
            }

            h1 {
                color: #333;
            }

            .card {
                background: white;
                padding: 20px;
                margin-bottom: 20px;
                border-radius: 12px;
                box-shadow: 0px 4px 10px rgba(0,0,0,0.05);
            }

            input {
                padding: 10px;
                width: 100%;
                margin-bottom: 10px;
                border-radius: 8px;
                border: 1px solid #ddd;
            }

            button {
                background: #ff6f91;
                color: white;
                border: none;
                padding: 10px 15px;
                border-radius: 8px;
                cursor: pointer;
            }

            button:hover {
                background: #ff4f78;
            }

            table {
                width: 100%;
                border-collapse: collapse;
                margin-top: 10px;
            }

            th {
                background: #ff6f91;
                color: white;
                padding: 10px;
            }

            td {
                padding: 10px;
                border-bottom: 1px solid #eee;
                text-align: center;
            }

            #mensaje {
                margin-bottom: 15px;
                font-weight: bold;
                color: green;
            }
        </style>
    </head>

    <body>

    <h1>📦 Inventario BabyMine</h1>

    <div id="mensaje"></div>

    <div class="card">
        <h3>🆕 Crear producto</h3>
        <input id="sku" placeholder="SKU">
        <input id="nombre" placeholder="Nombre">
        <input id="stock" type="number" value="0">
        <button onclick="crearProducto()">Crear</button>
    </div>

    <div class="card">
        <h3>➕ Entrada</h3>
        <input id="skuEntrada" placeholder="SKU">
        <input id="cantidadEntrada" type="number" value="1">
        <button onclick="entrada()">Ingresar</button>
    </div>

    <div class="card">
        <h3>➖ Salida</h3>
        <input id="skuSalida" placeholder="SKU">
        <input id="cantidadSalida" type="number" value="1">
        <button onclick="salida()">Salida</button>
    </div>

    <div class="card">
        <h3>📊 Stock en vivo</h3>

        <input type="text" id="buscador" placeholder="Buscar..." onkeyup="filtrar()">

        <table id="tabla">
            <thead>
                <tr>
                    <th>SKU</th>
                    <th>Nombre</th>
                    <th>Stock</th>
                    <th>Acción</th>
                </tr>
            </thead>
            <tbody></tbody>
        </table>
    </div>

    <script>
    function mostrarMensaje(texto){
        document.getElementById("mensaje").innerText = texto;
        setTimeout(() => { document.getElementById("mensaje").innerText = ""; }, 3000);
    }

    function crearProducto(){
        fetch("/agregar", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({
                sku: document.getElementById("sku").value,
                nombre: document.getElementById("nombre").value,
                stock: document.getElementById("stock").value
            })
        })
        .then(res => res.json())
        .then(() => {
            mostrarMensaje("Producto creado");
            cargarTabla();
        });
    }

    function entrada(){
        fetch("/entrada", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({
                sku: document.getElementById("skuEntrada").value,
                cantidad: document.getElementById("cantidadEntrada").value
            })
        })
        .then(() => {
            mostrarMensaje("Stock actualizado");
            cargarTabla();
        });
    }

    function salida(){
        fetch("/salida", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({
                sku: document.getElementById("skuSalida").value,
                cantidad: document.getElementById("cantidadSalida").value
            })
        })
        .then(() => {
            mostrarMensaje("Stock actualizado");
            cargarTabla();
        });
    }

    function cargarTabla(){
        fetch("/productos")
        .then(res => res.json())
        .then(data => {
            let tbody = document.querySelector("#tabla tbody");
            tbody.innerHTML = "";

            data.productos.forEach(p => {
                let fila = `<tr>
                    <td>${p.sku}</td>
                    <td>${p.nombre}</td>
                    <td style="color:${p.stock < 5 ? 'red' : 'black'}">${p.stock}</td>
                    <td><button onclick="ajustarStock('${p.sku}')">Editar</button></td>
                </tr>`;
                tbody.innerHTML += fila;
            });
        });
    }

    function filtrar(){
        let texto = document.getElementById("buscador").value.toLowerCase();
        let filas = document.querySelectorAll("#tabla tbody tr");

        filas.forEach(fila => {
            fila.style.display = fila.innerText.toLowerCase().includes(texto) ? "" : "none";
        });
    }

    function ajustarStock(sku){
        let nuevo = prompt("Cantidad (+ o -):");

        fetch("/entrada", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({
                sku: sku,
                cantidad: parseInt(nuevo)
            })
        })
        .then(() => {
            mostrarMensaje("Stock ajustado");
            cargarTabla();
        });
    }

    cargarTabla();
    </script>

    </body>
    </html>

    <p>Puedes usar pistola escaner o escribir SKU</p>
    """)

# ---------------- RUN ----------------

if __name__ == "__main__":
    import os
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))