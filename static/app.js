<div style="display:flex; gap:20px">

<div style="flex:3">

<h1>📦 Inventario BabyMine</h1>

<input id="buscador" placeholder="Buscar..." onkeyup="buscar()"><br><br>

<button onclick="importar()">Importar Woo</button><br><br>

<input id="sku" placeholder="SKU">
<input id="nombre" placeholder="Nombre">
<input id="stock" type="number">
<button onclick="crear()">Crear</button>

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

<table border="1">
<thead>
<tr><th>SKU</th><th>Nombre</th><th>Stock</th></tr>
</thead>
<tbody id="tabla"></tbody>
</table>

</div>

<div style="flex:1; max-height:500px; overflow:auto;">
<h3>📊 Historial</h3>
<div id="historial"></div>
</div>

</div>

<script>

let productosGlobal=[];

function importar(){
fetch("/importar_woo").then(r=>r.json()).then(d=>{
alert(JSON.stringify(d));
cargar();
});
}

function crear(){
fetch("/agregar",{method:"POST",headers:{'Content-Type':'application/json'},
body:JSON.stringify({sku:sku.value,nombre:nombre.value,stock:stock.value})})
.then(()=>cargar());
}

function entrada(){
fetch("/entrada",{method:"POST",headers:{'Content-Type':'application/json'},
body:JSON.stringify({sku:skuE.value,cantidad:cantE.value,motivo:motivoEntrada.value})})
.then(()=>cargar());
}

function salida(){
fetch("/salida",{method:"POST",headers:{'Content-Type':'application/json'},
body:JSON.stringify({sku:skuS.value,cantidad:cantS.value,motivo:motivoSalida.value})})
.then(()=>cargar());
}

function cargar(){

fetch("/productos").then(r=>r.json()).then(d=>{
productosGlobal=d.productos||[];
render(productosGlobal);
});

fetch("/movimientos").then(r=>r.json()).then(d=>{
historial.innerHTML=d.movimientos.reverse().map(m=>"<p>"+m+"</p>").join("");
});
}

function render(lista){
lista.sort((a,b)=>b.stock-a.stock);
tabla.innerHTML=lista.map(p=>`
<tr>
<td>${p.sku}</td>
<td>${p.nombre}</td>
<td>${p.stock}</td>
</tr>`).join("");
}

function buscar(){
let t=buscador.value.toLowerCase();
render(productosGlobal.filter(p=>p.nombre.toLowerCase().includes(t)||p.sku.toLowerCase().includes(t)));
}

setInterval(()=>fetch("/sync_ordenes"),10000);

cargar();

</script>