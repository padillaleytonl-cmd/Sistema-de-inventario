# -*- coding: utf-8 -*-
"""
Switch de ambiente (certificación ↔ producción) por tenant, para el panel
admin master de Lusync.

A diferencia de /facturacion/cambiar-ambiente (que cambia el ambiente del tenant
LOGUEADO), este blueprint permite al super-admin de Lusync cambiar el ambiente
de CUALQUIER tenant desde el panel master, pasando el tenant_id por la URL.

Rutas:
  GET  /admin/lusync/tenant/<tid>/ambiente          → panel con el switch
  POST /admin/lusync/tenant/<tid>/ambiente/cambiar  → aplica el cambio

Protección: session['is_lusync_admin'] (igual que el resto del panel master).

IMPORTANTE (salvaguarda): pasar a 'produccion' exige que el tenant tenga al
menos un CAF de producción cargado para el tipo de documento que emite; de lo
contrario el SII rechazará las emisiones. El switch advierte si falta.
"""
from flask import Blueprint, request, session, redirect, jsonify

admin_ambiente_bp = Blueprint("admin_ambiente_bp", __name__)


def _guard():
    """Solo el super-admin de Lusync puede operar el switch."""
    return bool(session.get("is_lusync_admin"))


def _estado_tenant(tenant_id):
    """Lee ambiente actual + conteo de CAF por ambiente del tenant."""
    from inventario import get_conn, release_conn
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT rut_emisor, razon_social, ambiente
                           FROM facturacion_config_tenant WHERE tenant_id = %s""",
                        (tenant_id,))
            row = cur.fetchone()
            if not row:
                return None
            rut, razon, ambiente = row
            # CAF disponibles por ambiente (no agotados)
            cur.execute("""SELECT ambiente, tipo_dte, COUNT(*)
                           FROM facturacion_cafs
                           WHERE tenant_id = %s AND agotado = FALSE
                           GROUP BY ambiente, tipo_dte""", (tenant_id,))
            cafs = {}
            for amb, tipo, n in cur.fetchall():
                cafs.setdefault(amb, {})[int(tipo)] = int(n)
            return {"rut": rut, "razon": razon, "ambiente": ambiente or "certificacion", "cafs": cafs}
    finally:
        release_conn(conn)


@admin_ambiente_bp.route("/admin/lusync/tenant/<int:tenant_id>/ambiente", methods=["GET"])
def admin_tenant_ambiente(tenant_id):
    if not _guard():
        return redirect("/admin/lusync/login")
    est = _estado_tenant(tenant_id)
    if est is None:
        return f"<p>Tenant {tenant_id} no tiene configuración de facturación.</p>", 404

    amb = est["ambiente"]
    es_prod = amb == "produccion"
    cafs_prod = est["cafs"].get("produccion", {})
    cafs_cert = est["cafs"].get("certificacion", {})

    # Resumen de CAF por ambiente
    def _resumen(d):
        if not d:
            return "<span style='color:#b91c1c'>ninguno</span>"
        return " · ".join(f"tipo {t}: {n}" for t, n in sorted(d.items()))

    # Advertencia si se quiere ir a producción sin CAF de producción
    aviso_prod = ""
    if not cafs_prod:
        aviso_prod = ("<div style='background:#fef3c7;border:1px solid #f59e0b;"
                      "padding:10px;border-radius:8px;margin:10px 0;color:#92400e'>"
                      "⚠️ Este tenant <b>no tiene CAF de producción</b> cargados. "
                      "Si pasas a producción, las emisiones serán rechazadas por el "
                      "SII hasta que cargues folios reales (descargados de "
                      "palena.sii.cl).</div>")

    color = "#16a34a" if es_prod else "#d97706"
    estado_txt = "PRODUCCIÓN" if es_prod else "CERTIFICACIÓN"
    otro = "certificacion" if es_prod else "produccion"
    otro_txt = "Certificación" if es_prod else "Producción"

    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>Ambiente · Tenant {tenant_id}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#f8fafc;margin:0;padding:24px;color:#0f172a}}
  .card{{max-width:560px;margin:0 auto;background:#fff;border:1px solid #e2e8f0;border-radius:14px;padding:24px;box-shadow:0 1px 3px rgba(0,0,0,.06)}}
  h1{{font-size:18px;margin:0 0 4px}} .sub{{color:#64748b;font-size:13px;margin-bottom:18px}}
  .badge{{display:inline-block;padding:6px 14px;border-radius:999px;font-weight:700;font-size:13px;color:#fff;background:{color}}}
  .row{{display:flex;justify-content:space-between;padding:10px 0;border-bottom:1px solid #f1f5f9;font-size:14px}}
  .row b{{color:#334155}}
  button{{width:100%;padding:13px;border:0;border-radius:10px;font-weight:700;font-size:15px;cursor:pointer;margin-top:18px}}
  .toprod{{background:#16a34a;color:#fff}} .tocert{{background:#d97706;color:#fff}}
  a.back{{display:inline-block;margin-bottom:14px;color:#2563eb;text-decoration:none;font-size:13px}}
</style></head><body>
<div class="card">
  <a class="back" href="/admin/lusync/tenant/{tenant_id}/inspeccionar">← Volver al tenant</a>
  <h1>{est['razon']}</h1>
  <div class="sub">RUT {est['rut']} · Tenant #{tenant_id}</div>
  <div>Ambiente actual: <span class="badge">{estado_txt}</span></div>
  <div style="margin-top:18px">
    <div class="row"><b>CAF certificación</b><span>{_resumen(cafs_cert)}</span></div>
    <div class="row"><b>CAF producción</b><span>{_resumen(cafs_prod)}</span></div>
  </div>
  {aviso_prod}
  <button class="{'tocert' if es_prod else 'toprod'}"
          onclick="cambiar('{otro}')">
    Cambiar a {otro_txt}
  </button>
</div>
<script>
async function cambiar(nuevo){{
  if(nuevo==='produccion' && !confirm('¿Pasar este tenant a PRODUCCIÓN? Las boletas y facturas que emita serán documentos tributarios REALES ante el SII.')) return;
  if(nuevo==='certificacion' && !confirm('¿Volver a CERTIFICACIÓN? Las emisiones dejarán de ser válidas ante el SII (modo prueba).')) return;
  const r = await fetch('/admin/lusync/tenant/{tenant_id}/ambiente/cambiar',{{
    method:'POST',headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{ambiente:nuevo}})
  }});
  const j = await r.json();
  if(j.ok){{ location.reload(); }}
  else {{ alert('Error: '+(j.error||'desconocido')); }}
}}
</script>
</body></html>"""


@admin_ambiente_bp.route("/admin/lusync/tenant/<int:tenant_id>/ambiente/cambiar", methods=["POST"])
def admin_tenant_ambiente_cambiar(tenant_id):
    if not _guard():
        return jsonify({"ok": False, "error": "no autorizado"}), 401
    data = request.get_json(silent=True) or {}
    nuevo = (data.get("ambiente") or "").strip().lower()
    if nuevo not in ("certificacion", "produccion"):
        return jsonify({"ok": False, "error": "Ambiente inválido"}), 400

    # Salvaguarda: no dejar pasar a producción sin CAF de producción.
    if nuevo == "produccion":
        est = _estado_tenant(tenant_id)
        if est is None:
            return jsonify({"ok": False, "error": "Tenant sin configuración"}), 404
        if not est["cafs"].get("produccion"):
            return jsonify({"ok": False,
                            "error": "El tenant no tiene CAF de producción cargados. "
                                     "Carga folios reales antes de pasar a producción."}), 409

    from inventario import get_conn, release_conn
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""UPDATE facturacion_config_tenant
                           SET ambiente = %s, fecha_actualizacion = NOW()
                           WHERE tenant_id = %s""", (nuevo, tenant_id))
            if cur.rowcount == 0:
                conn.rollback()
                return jsonify({"ok": False, "error": "Tenant no encontrado"}), 404
        conn.commit()
        return jsonify({"ok": True, "ambiente": nuevo})
    except Exception as e:
        conn.rollback()
        return jsonify({"ok": False, "error": str(e)[:200]}), 500
    finally:
        release_conn(conn)
