# -*- coding: utf-8 -*-
"""
Endpoint de PRUEBA para la solicitud automática de folios (CAF) al SII.

Permite disparar solicitar_folios() desde el navegador y ver la RESPUESTA CRUDA
del SII, para ajustar los nombres de los campos del CGI of_solicita_folios.

⚠️ USAR PRIMERO EN CERTIFICACIÓN (maullin). El de producción (palena) pide
folios REALES; no lo uses hasta confirmar que el flujo funciona en certificación.

Ruta:
  GET /admin/lusync/test-solicitar-folios?tenant_id=3&tipo=39&cantidad=5&ambiente=certificacion
      &accion=solicitar|consultar

Protección: session['is_lusync_admin'] (panel master).

Devuelve un HTML con: estado, CAF extraído (si lo hubo), rango de folios, y la
RESPUESTA CRUDA del SII (clave para ajustar los campos del formulario).
"""
from flask import Blueprint, request, session, redirect
from html import escape

test_folios_bp = Blueprint("test_folios_bp", __name__)


@test_folios_bp.route("/admin/lusync/test-solicitar-folios", methods=["GET"])
def admin_test_solicitar_folios():
    if not session.get("is_lusync_admin"):
        return redirect("/admin/lusync/login")

    tenant_id = request.args.get("tenant_id", default=3, type=int)
    tipo = request.args.get("tipo", default=39, type=int)
    cantidad = request.args.get("cantidad", default=5, type=int)
    ambiente = request.args.get("ambiente", default="certificacion")
    accion = request.args.get("accion", default="solicitar")
    usar_mtls = request.args.get("mtls", default="0") == "1"

    if ambiente not in ("certificacion", "produccion"):
        ambiente = "certificacion"

    # Recuperar el certificado del tenant
    try:
        from inventario import get_conn, release_conn
        from facturacion.certificados import obtener_certificado
        cert = obtener_certificado(get_conn, release_conn, tenant_id)
    except Exception as e:
        return _pagina(tenant_id, tipo, cantidad, ambiente, accion,
                       error=f"No se pudo cargar el certificado: {e}")

    if not cert.get("ok"):
        return _pagina(tenant_id, tipo, cantidad, ambiente, accion,
                       error=f"Certificado no disponible: {cert.get('error', '?')}")

    rut_emisor = cert.get("metadata", {}).get("rut") or ""
    titular = cert.get("metadata", {}).get("titular", "?")

    # Disparar la acción
    try:
        from facturacion.dtes.solicitar_folios import (
            solicitar_folios, consultar_folios_disponibles)
    except Exception as e:
        return _pagina(tenant_id, tipo, cantidad, ambiente, accion,
                       error=f"No se pudo importar solicitar_folios: {e}")

    resultado = None
    try:
        if accion == "consultar":
            resultado = consultar_folios_disponibles(
                pfx_bytes=cert["pfx_bytes"], password=cert["password"],
                rut_emisor=rut_emisor, tipo_dte=tipo, ambiente=ambiente)
        else:
            resultado = solicitar_folios(
                pfx_bytes=cert["pfx_bytes"], password=cert["password"],
                rut_emisor=rut_emisor, tipo_dte=tipo, cantidad=cantidad,
                ambiente=ambiente, usar_mtls=usar_mtls)
    except Exception as e:
        return _pagina(tenant_id, tipo, cantidad, ambiente, accion,
                       error=f"Excepción al llamar al SII: {e}", titular=titular,
                       rut=rut_emisor)

    return _pagina(tenant_id, tipo, cantidad, ambiente, accion,
                   resultado=resultado, titular=titular, rut=rut_emisor)


def _pagina(tenant_id, tipo, cantidad, ambiente, accion,
            resultado=None, error=None, titular="?", rut="?"):
    es_prod = ambiente == "produccion"
    banner_prod = ""
    if es_prod:
        banner_prod = ("<div style='background:#fee2e2;border:1px solid #ef4444;"
                       "padding:10px;border-radius:8px;margin:10px 0;color:#991b1b'>"
                       "⚠️ AMBIENTE PRODUCCIÓN — esta solicitud pide folios REALES "
                       "al SII. Úsalo solo si ya confirmaste el flujo en certificación."
                       "</div>")

    bloque_resultado = ""
    if error:
        bloque_resultado = (f"<div class='err'><b>Error:</b> {escape(str(error))}</div>")
    elif resultado is not None:
        ok = resultado.get("ok")
        estado = ("<span class='ok'>✓ OK</span>" if ok else "<span class='no'>✗ Falló</span>")
        err = resultado.get("error")
        caf = resultado.get("caf_xml")
        desde = resultado.get("folio_desde")
        hasta = resultado.get("folio_hasta")
        maximo = resultado.get("maximo")
        cruda = resultado.get("respuesta_cruda", "")

        detalles = f"<div class='estado'>Estado: {estado}</div>"
        if err:
            detalles += f"<div class='err'>{escape(str(err))}</div>"
        if maximo is not None:
            detalles += f"<div class='campo'><b>Máximo autorizado:</b> {maximo} folios</div>"
        if caf:
            detalles += (f"<div class='campo'><b>CAF obtenido ✓</b> — folios "
                         f"{desde} a {hasta}</div>"
                         f"<details><summary>Ver CAF XML</summary>"
                         f"<pre>{escape(caf[:3000])}</pre></details>")
        # La respuesta cruda es lo más importante para ajustar los campos
        detalles += ("<div class='campo' style='margin-top:14px'>"
                     "<b>Respuesta cruda del SII</b> (úsala para ajustar los campos "
                     "del formulario en solicitar_folios.py):</div>"
                     f"<pre class='cruda'>{escape(str(cruda)[:4000]) or '(vacía)'}</pre>")
        bloque_resultado = detalles

    def _amb_link(a):
        return (f"?tenant_id={tenant_id}&tipo={tipo}&cantidad={cantidad}"
                f"&ambiente={a}&accion={accion}")

    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>Test solicitar folios</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#f8fafc;margin:0;padding:24px;color:#0f172a}}
  .card{{max-width:760px;margin:0 auto;background:#fff;border:1px solid #e2e8f0;border-radius:14px;padding:24px}}
  h1{{font-size:18px;margin:0 0 4px}} .sub{{color:#64748b;font-size:13px;margin-bottom:14px}}
  form{{display:flex;flex-wrap:wrap;gap:10px;align-items:end;margin:14px 0;padding:14px;background:#f8fafc;border-radius:10px}}
  label{{display:flex;flex-direction:column;font-size:12px;color:#475569;gap:3px}}
  input,select{{padding:7px 9px;border:1px solid #cbd5e1;border-radius:7px;font-size:14px}}
  button{{padding:9px 16px;border:0;border-radius:8px;font-weight:700;font-size:14px;cursor:pointer;background:#2563eb;color:#fff}}
  .estado{{font-size:15px;margin:10px 0}}
  .ok{{color:#16a34a;font-weight:700}} .no{{color:#dc2626;font-weight:700}}
  .err{{background:#fef2f2;border:1px solid #fecaca;color:#991b1b;padding:10px;border-radius:8px;margin:8px 0}}
  .campo{{font-size:14px;margin:6px 0}}
  pre{{background:#0f172a;color:#e2e8f0;padding:12px;border-radius:8px;overflow:auto;font-size:12px;max-height:340px}}
  pre.cruda{{background:#1e293b}}
  .tabs a{{display:inline-block;padding:6px 12px;border-radius:7px;text-decoration:none;font-size:13px;margin-right:6px;border:1px solid #cbd5e1;color:#334155}}
  .tabs a.activo{{background:#0f172a;color:#fff;border-color:#0f172a}}
</style></head><body>
<div class="card">
  <h1>Test · Solicitud automática de folios al SII</h1>
  <div class="sub">Tenant #{tenant_id} · {escape(str(titular))} · RUT {escape(str(rut))}</div>

  <div class="tabs">
    <a class="{'activo' if not es_prod else ''}" href="{_amb_link('certificacion')}">Certificación (maullin)</a>
    <a class="{'activo' if es_prod else ''}" href="{_amb_link('produccion')}">Producción (palena)</a>
  </div>
  {banner_prod}

  <form method="get" action="/admin/lusync/test-solicitar-folios">
    <input type="hidden" name="tenant_id" value="{tenant_id}">
    <input type="hidden" name="ambiente" value="{ambiente}">
    <label>Tipo DTE
      <select name="tipo">
        {''.join(f'<option value="{t}" {"selected" if t==tipo else ""}>{t} - {n}</option>'
                 for t,n in [(39,"Boleta"),(33,"Factura"),(34,"Fact exenta"),
                             (52,"Guía"),(56,"Nota débito"),(61,"Nota crédito"),(43,"Liquidación")])}
      </select>
    </label>
    <label>Cantidad
      <input type="number" name="cantidad" value="{cantidad}" min="1" max="1000" style="width:90px">
    </label>
    <label>Acción
      <select name="accion">
        <option value="consultar" {"selected" if accion=="consultar" else ""}>Consultar máximo</option>
        <option value="solicitar" {"selected" if accion=="solicitar" else ""}>Solicitar folios</option>
      </select>
    </label>
    <label>Cert. en TLS (mTLS)
      <select name="mtls">
        <option value="0" {"selected" if not usar_mtls else ""}>No (solo token)</option>
        <option value="1" {"selected" if usar_mtls else ""}>Sí (token + certificado)</option>
      </select>
    </label>
    <button type="submit">Ejecutar</button>
  </form>

  {bloque_resultado}
</div>
</body></html>"""
