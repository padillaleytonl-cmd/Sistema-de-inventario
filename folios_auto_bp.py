# -*- coding: utf-8 -*-
"""
Blueprint: descarga automática de folios CAF desde el SII (panel master Lusync).

Expone un endpoint que, dado un tenant, tipo de DTE y cantidad, ejecuta el flujo
completo de timbraje electrónico del SII (AUT2000 + solicitud + descarga) usando
el certificado del tenant, y guarda el CAF resultante en facturacion_cafs.

Registro en app.py:
    from folios_auto_bp import folios_auto_bp
    app.register_blueprint(folios_auto_bp)

Uso (POST JSON):
    POST /admin/lusync/sii/tenant/<tenant_id>/caf/descargar
    body: {"tipo_dte": 39, "cantidad": 5}
    (el ambiente se toma de la config del tenant: certificacion/produccion)

Protección: requiere sesión de admin Lusync (is_lusync_admin).
"""
from flask import Blueprint, request, jsonify, session

folios_auto_bp = Blueprint("folios_auto_bp", __name__)


def _es_lusync_admin():
    return bool(session.get("is_lusync_admin"))


@folios_auto_bp.route(
    "/admin/lusync/sii/tenant/<int:tenant_id>/caf/descargar", methods=["POST"])
def descargar_folios_auto(tenant_id):
    """Descarga folios CAF automáticamente desde el SII para un tenant."""
    if not _es_lusync_admin():
        return jsonify({"ok": False, "error": "No autorizado"}), 403

    data = request.get_json(silent=True) or {}
    try:
        tipo_dte = int(data.get("tipo_dte"))
        cantidad = int(data.get("cantidad"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "tipo_dte y cantidad son obligatorios"}), 400

    if cantidad < 1 or cantidad > 1000:
        return jsonify({"ok": False, "error": "Cantidad fuera de rango (1-1000)"}), 400

    from inventario import get_conn, release_conn
    from facturacion import obtener_config_facturacion
    from facturacion.certificados import obtener_certificado
    from facturacion.dtes.solicitar_folios import descargar_y_guardar

    # 1. Config del tenant (RUT + ambiente)
    config = obtener_config_facturacion(get_conn, release_conn, tenant_id)
    if not config:
        return jsonify({"ok": False, "error": "El tenant no tiene configuración de facturación"}), 400
    rut_emisor = config.get("rut_emisor")
    ambiente = config.get("ambiente") or "certificacion"
    if not rut_emisor:
        return jsonify({"ok": False, "error": "El tenant no tiene RUT emisor configurado"}), 400

    # 2. Certificado del tenant (desencriptado)
    cert = obtener_certificado(get_conn, release_conn, tenant_id)
    if not cert or not cert.get("ok"):
        err = (cert or {}).get("error", "Sin certificado activo")
        return jsonify({"ok": False, "error": "Certificado no disponible: %s" % err}), 400

    # 3. Descargar + guardar
    res = descargar_y_guardar(
        get_conn, release_conn, tenant_id,
        cert["pfx_bytes"], cert["password"],
        rut_emisor, tipo_dte, cantidad, ambiente,
    )

    # 4. Auditoría (best-effort)
    if res.get("ok"):
        try:
            from app import registrar_audit  # type: ignore
            registrar_audit(
                session.get("lusync_admin_email", "Lusync"),
                request.remote_addr, "descargar_caf_auto",
                entidad="facturacion_cafs",
                detalle="Tenant %s, tipo %s, folios %s-%s, ambiente %s" % (
                    tenant_id, tipo_dte, res.get("folio_desde"),
                    res.get("folio_hasta"), ambiente),
            )
        except Exception:
            pass

    status = 200 if res.get("ok") else 400
    # No exponer la traza interna completa al cliente salvo en error
    payload = {
        "ok": res.get("ok"),
        "caf_id": res.get("caf_id"),
        "folio_desde": res.get("folio_desde"),
        "folio_hasta": res.get("folio_hasta"),
        "max_autorizado": res.get("max_autorizado"),
        "ambiente": ambiente,
        "error": res.get("error"),
    }
    return jsonify(payload), status


@folios_auto_bp.route(
    "/admin/lusync/sii/tenant/<int:tenant_id>/caf/listar", methods=["GET"])
def listar_folios_tenant(tenant_id):
    """Lista los CAF descargados/cargados de un tenant (para el panel master).

    Devuelve, por cada CAF: tipo, rango, folios usados/restantes, %, ambiente,
    si está agotado, y fechas. No expone el XML completo.
    Opcional: ?ambiente=certificacion|produccion para filtrar.
    """
    if not _es_lusync_admin():
        return jsonify({"ok": False, "error": "No autorizado"}), 403

    from inventario import get_conn, release_conn
    from facturacion.cafs import listar_cafs_tenant
    from facturacion import obtener_config_facturacion

    cafs = listar_cafs_tenant(get_conn, release_conn, tenant_id)

    filtro_amb = request.args.get("ambiente")
    if filtro_amb in ("certificacion", "produccion"):
        cafs = [c for c in cafs if c.get("ambiente") == filtro_amb]

    config = obtener_config_facturacion(get_conn, release_conn, tenant_id)
    ambiente_activo = (config.get("ambiente") if config else "certificacion") or "certificacion"

    # Resumen rápido por ambiente
    resumen = {"certificacion": 0, "produccion": 0}
    for c in cafs:
        amb = c.get("ambiente") or "certificacion"
        resumen[amb] = resumen.get(amb, 0) + 1

    return jsonify({
        "ok": True,
        "tenant_id": tenant_id,
        "ambiente_activo": ambiente_activo,
        "total": len(cafs),
        "resumen": resumen,
        "cafs": cafs,
    })


# Tipos de DTE para el selector de la UI
_TIPOS_UI = [
    (33, "Factura electrónica"), (34, "Factura exenta"),
    (39, "Boleta electrónica"), (41, "Boleta exenta"),
    (43, "Liquidación factura"), (46, "Factura de compra"),
    (52, "Guía de despacho"), (56, "Nota de débito"),
    (61, "Nota de crédito"), (110, "Factura exportación"),
    (111, "Nota débito exportación"), (112, "Nota crédito exportación"),
]


@folios_auto_bp.route(
    "/admin/lusync/sii/tenant/<int:tenant_id>/folios", methods=["GET"])
def panel_folios_tenant(tenant_id):
    """Página del panel master: ver CAF de un tenant y descargar nuevos."""
    if not _es_lusync_admin():
        return ("No autorizado", 403)

    from inventario import get_conn, release_conn
    from facturacion import obtener_config_facturacion

    config = obtener_config_facturacion(get_conn, release_conn, tenant_id)
    razon = (config.get("razon_social") if config else None) or ("Tenant %s" % tenant_id)
    rut = (config.get("rut_emisor") if config else None) or "—"
    ambiente = (config.get("ambiente") if config else "certificacion") or "certificacion"
    es_prod = ambiente == "produccion"
    badge_color = "#16a34a" if es_prod else "#d97706"
    amb_txt = "Producción" if es_prod else "Certificación"

    opciones = "".join(
        '<option value="%d">%d · %s</option>' % (t, t, n) for t, n in _TIPOS_UI)

    aviso_prod = ""
    if es_prod:
        aviso_prod = (
            '<div style="background:#fef3c7;border:1px solid #f59e0b;color:#92400e;'
            'padding:10px 12px;border-radius:8px;font-size:13px;margin:14px 0">'
            '⚠️ Este tenant está en <b>PRODUCCIÓN</b>. Los folios que descargues '
            'son <b>reales</b> y se consumen del rango autorizado por el SII.</div>')

    return """<!doctype html><html><head><meta charset="utf-8">
<title>Folios CAF · %(razon)s</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#f8fafc;margin:0;padding:24px;color:#0f172a}
  .card{max-width:840px;margin:0 auto;background:#fff;border:1px solid #e2e8f0;border-radius:14px;padding:24px;box-shadow:0 1px 3px rgba(0,0,0,.06)}
  h1{font-size:18px;margin:0 0 4px} .sub{color:#64748b;font-size:13px;margin-bottom:14px}
  .badge{display:inline-block;padding:5px 12px;border-radius:999px;font-weight:700;font-size:12px;color:#fff;background:%(badge_color)s}
  a.back{display:inline-block;margin-bottom:14px;color:#2563eb;text-decoration:none;font-size:13px}
  table{width:100%%;border-collapse:collapse;margin-top:8px;font-size:13px}
  th,td{text-align:left;padding:8px 10px;border-bottom:1px solid #f1f5f9}
  th{color:#64748b;font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.04em}
  .bar{height:6px;background:#e2e8f0;border-radius:99px;overflow:hidden;width:90px;display:inline-block;vertical-align:middle}
  .bar>i{display:block;height:100%%;background:#2563eb}
  .pill{font-size:11px;font-weight:700;padding:3px 8px;border-radius:99px}
  .pill.ok{background:#dcfce7;color:#15803d} .pill.x{background:#fee2e2;color:#b91c1c}
  .form{margin:18px 0;padding:16px;background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end}
  .form label{display:block;font-size:11px;color:#64748b;margin-bottom:4px;font-weight:600}
  select,input[type=number]{padding:9px;border:1px solid #cbd5e1;border-radius:8px;font-size:14px}
  button{padding:10px 18px;border:0;border-radius:9px;font-weight:700;font-size:14px;cursor:pointer;background:#2563eb;color:#fff}
  button:disabled{opacity:.5;cursor:default}
  .empty{color:#94a3b8;text-align:center;padding:24px;font-size:13px}
  #msg{margin-top:12px;font-size:13px}
</style></head><body>
<div class="card">
  <a class="back" href="/admin/lusync/tenant/%(tid)s">← Volver al tenant</a>
  <h1>%(razon)s</h1>
  <div class="sub">RUT %(rut)s · Tenant #%(tid)s · <span class="badge">%(amb_txt)s</span></div>
  %(aviso_prod)s

  <div class="form">
    <div><label>Tipo de documento</label>
      <select id="tipo">%(opciones)s</select></div>
    <div><label>Cantidad</label>
      <input type="number" id="cant" value="10" min="1" max="1000" style="width:90px"></div>
    <button id="btn" onclick="descargar()">Descargar folios del SII</button>
  </div>
  <div id="msg"></div>

  <table id="tabla"><thead><tr>
    <th>Tipo</th><th>Rango</th><th>Uso</th><th>Restantes</th>
    <th>Ambiente</th><th>Autorización</th><th>Estado</th>
  </tr></thead><tbody id="tbody">
    <tr><td colspan="7" class="empty">Cargando…</td></tr>
  </tbody></table>
</div>
<script>
const TID=%(tid)s;
const TIPOS=%(tipos_json)s;
function nombre(t){return TIPOS[t]||('Tipo '+t);}
async function cargar(){
  const r=await fetch('/admin/lusync/sii/tenant/'+TID+'/caf/listar',{credentials:'include'});
  const d=await r.json();
  const tb=document.getElementById('tbody');
  if(!d.ok||!d.cafs.length){tb.innerHTML='<tr><td colspan=7 class=empty>Sin CAF descargados todavía</td></tr>';return;}
  tb.innerHTML=d.cafs.map(c=>{
    const pct=c.pct_usado||0;
    const estado=c.agotado?'<span class="pill x">Agotado</span>':'<span class="pill ok">Activo</span>';
    return '<tr><td><b>'+nombre(c.tipo_dte)+'</b><br><span style="color:#94a3b8;font-size:11px">cod '+c.tipo_dte+'</span></td>'+
      '<td>'+c.folio_desde+' – '+c.folio_hasta+'</td>'+
      '<td><span class=bar><i style="width:'+pct+'%%"></i></span> '+pct+'%%</td>'+
      '<td>'+c.folios_restantes+'</td>'+
      '<td>'+(c.ambiente||'—')+'</td>'+
      '<td>'+(c.fecha_autorizacion||'—')+'</td>'+
      '<td>'+estado+'</td></tr>';
  }).join('');
}
async function descargar(){
  const tipo=+document.getElementById('tipo').value;
  const cant=+document.getElementById('cant').value;
  const btn=document.getElementById('btn'); const msg=document.getElementById('msg');
  btn.disabled=true; msg.style.color='#64748b'; msg.textContent='Descargando del SII… (esto toma unos segundos)';
  try{
    const r=await fetch('/admin/lusync/sii/tenant/'+TID+'/caf/descargar',{
      method:'POST',headers:{'Content-Type':'application/json'},credentials:'include',
      body:JSON.stringify({tipo_dte:tipo,cantidad:cant})});
    const d=await r.json();
    if(d.ok){msg.style.color='#15803d';msg.textContent='✓ Folios '+d.folio_desde+'–'+d.folio_hasta+' descargados y guardados.';cargar();}
    else{msg.style.color='#b91c1c';msg.textContent='✗ '+(d.error||'Error');}
  }catch(e){msg.style.color='#b91c1c';msg.textContent='✗ '+e;}
  btn.disabled=false;
}
cargar();
</script></body></html>""" % {
        "razon": razon, "rut": rut, "tid": tenant_id,
        "badge_color": badge_color, "amb_txt": amb_txt,
        "aviso_prod": aviso_prod, "opciones": opciones,
        "tipos_json": "{" + ",".join('%d:"%s"' % (t, n) for t, n in _TIPOS_UI) + "}",
    }
