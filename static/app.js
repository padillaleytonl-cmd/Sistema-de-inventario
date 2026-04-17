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