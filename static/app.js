let productosGlobal=[];

function mensaje(txt){
    alert(txt);
}

function importar(){
fetch("/importar_woo").then(r=>r.json()).then(d=>{
mensaje("Importación completada");
cargar();
});
}

function crear(){
fetch("/agregar",{method:"POST",headers:{'Content-Type':'application/json'},
body:JSON.stringify({sku:sku.value,nombre:nombre.value,stock:stock.value})})
.then(r=>r.json())
.then(d=>{
    if(d.error){mensaje(d.error);}
    else{
        mensaje("Producto creado");
        cargar();
    }
});
}

function entrada(){
fetch("/entrada",{method:"POST",headers:{'Content-Type':'application/json'},
body:JSON.stringify({sku:skuE.value,cantidad:cantE.value,motivo:motivoEntrada.value})})
.then(r=>r.json())
.then(d=>{
    if(d.error){mensaje(d.error);}
    else{
        mensaje("Entrada registrada");
        cargar();
        skuE.value="";
        cantE.value="";
        skuE.focus();
    }
});
}

function salida(){
fetch("/salida",{method:"POST",headers:{'Content-Type':'application/json'},
body:JSON.stringify({sku:skuS.value,cantidad:cantS.value,motivo:motivoSalida.value})})
.then(r=>r.json())
.then(d=>{
    if(d.error){mensaje(d.error);}
    else{
        mensaje("Salida registrada");
        cargar();
        skuS.value="";
        cantS.value="";
        skuS.focus();
    }
});
}

function cargar(){
fetch("/productos").then(r=>r.json()).then(d=>{
productosGlobal=d.productos||[];
render(productosGlobal);
});

fetch("/movimientos").then(r=>r.json()).then(d=>{
historial.innerHTML=d.movimientos.reverse().map(m=>
    `<div style="border-bottom:1px solid #ddd; padding:5px">${m}</div>`
).join("");
});
}

function render(lista){
lista.sort((a,b)=>b.stock-a.stock);
tabla.innerHTML=lista.map(p=>`
<tr>
<td>${p.sku}</td>
<td>${p.nombre}</td>
<td style="color:${p.stock < 5 ? 'red':'black'}">${p.stock}</td>
</tr>`).join("");
}

function buscar(){
let t=buscador.value.toLowerCase();
render(productosGlobal.filter(p=>
p.nombre.toLowerCase().includes(t)||p.sku.toLowerCase().includes(t)
));
}

setInterval(()=>fetch("/sync_ordenes"),10000);
cargar();