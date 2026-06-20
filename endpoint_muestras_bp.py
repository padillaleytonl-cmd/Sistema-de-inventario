# -*- coding: utf-8 -*-
"""
endpoint_muestras_bp.py
─────────────────────────────────────────────────────────────
Etapa "Documentos Impresos" de la certificación SII.

Genera los PDF (representación impresa con timbre PDF417) a partir de un sobre
EnvioDTE.xml ya firmado. Sirve para:
  • El sobre de Simulación (23 DTE de todos los tipos) — ya aprobado por el SII
  • Los sobres de cada Set de Prueba

Recibe el XML del sobre (subido como archivo o pegado), separa cada
<DTE>...</DTE>, genera su PDF con generar_pdf_dte() y devuelve un ZIP. Los
documentos cedibles incluyen su copia CEDIBLE (2 páginas).

Rutas:
  GET  /admin/lusync/sii/muestras-impresas  → formulario subir/pegar sobre
  POST /admin/lusync/sii/muestras-impresas  → descarga ZIP con los PDF
       params: ?formato=carta|rollo  ?uno_por_tipo=si  ?preview=tipo:folio

Los endpoints de certificación/simulación NO guardan los XML en la BD (solo los
generan y envían), por eso esta vía basada en el sobre es la más fiable: usa el
MISMO XML firmado que el SII recibió y aprobó.
"""
from __future__ import annotations

import io
import re
import zipfile

from flask import Blueprint, request, jsonify, session, Response

muestras_bp = Blueprint("muestras_bp", __name__)

URL_CONSULTA = "lusync.cl/consultadte"

NOMBRE_TIPO = {
    33: "factura", 34: "factura_exenta", 39: "boleta", 41: "boleta_exenta",
    43: "liquidacion", 46: "factura_compra", 52: "guia_despacho",
    56: "nota_debito", 61: "nota_credito",
    110: "fact_exportacion", 111: "nd_exportacion", 112: "nc_exportacion",
}


def _slug_tipo(tipo):
    try:
        return NOMBRE_TIPO.get(int(tipo), f"dte{tipo}")
    except (ValueError, TypeError):
        return f"dte{tipo}"


def _separar_dtes(sobre_xml: str):
    """Extrae cada bloque <DTE>...</DTE> del sobre EnvioDTE."""
    docs = []
    for m in re.finditer(r'<DTE\b.*?</DTE>', sobre_xml, re.DOTALL):
        bloque = m.group(0)
        mt = re.search(r'<TipoDTE>(\d+)</TipoDTE>', bloque)
        mf = re.search(r'<Folio>(\d+)</Folio>', bloque)
        docs.append({
            "xml": bloque,
            "tipo": mt.group(1) if mt else "",
            "folio": mf.group(1) if mf else "",
        })
    return docs


def _leer_sobre_del_request():
    """Obtiene el XML del sobre desde un archivo subido ('sobre') o campo 'xml'."""
    f = request.files.get("sobre")
    if f and f.filename:
        data = f.read()
        try:
            return data.decode("iso-8859-1", errors="replace")
        except Exception:
            return data.decode("utf-8", errors="replace")
    xml = request.form.get("xml") or request.values.get("xml")
    return xml or None


@muestras_bp.route("/admin/lusync/sii/muestras-impresas", methods=["GET", "POST"])
def muestras_impresas():
    if not session.get("logged"):
        return jsonify({"ok": False, "error": "no autenticado"}), 401
    if session.get("rol") != "admin" and not session.get("is_lusync_admin"):
        return jsonify({"ok": False, "error": "solo admin del tenant"}), 403

    from facturacion.dtes.pdf_dte import generar_pdf_dte

    tenant_id = request.args.get("tenant_id", default=3, type=int)
    formato = request.args.get("formato", default="carta")
    uno_por_tipo = request.args.get("uno_por_tipo", default="") == "si"
    preview = request.args.get("preview", default="")

    # ─── GET: formulario ───
    if request.method == "GET":
        return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Muestras Impresas SII</title>
<style>body{{font-family:-apple-system,sans-serif;background:#f6f5f1;padding:40px;}}
.card{{max-width:760px;margin:0 auto;background:white;border-radius:14px;padding:28px;
box-shadow:0 4px 20px rgba(0,0,0,0.06);}}
.info{{background:#dbeafe;border:1px solid #3b82f6;border-radius:8px;padding:14px;color:#1e40af;font-size:13px;margin-bottom:16px;line-height:1.5;}}
textarea{{width:100%;height:160px;font-family:monospace;font-size:11px;border:1px solid #ddd;border-radius:8px;padding:10px;box-sizing:border-box;}}
.row{{margin:14px 0;}}
label{{font-size:13px;color:#444;}}
button{{background:#534AB7;color:white;padding:12px 22px;border:none;border-radius:8px;
font-weight:600;font-size:14px;cursor:pointer;margin-top:10px;}}
h2{{margin-top:0;}}</style></head><body>
<div class="card">
<h2>🖨️ Muestras Impresas — Documentos Impresos SII</h2>
<div class="info">
Genera los PDF con timbre PDF417 para enviar a <b>SII_dte_impresos@sii.cl</b>.<br>
Pega o sube el <b>sobre EnvioDTE.xml</b> (el mismo que subiste a maullin):
el de Simulación trae los 23 documentos de todos los tipos; los de cada Set
de Prueba traen sus casos. Los documentos cedibles incluyen copia CEDIBLE.
</div>
<form method="POST" enctype="multipart/form-data"
      action="?tenant_id={tenant_id}&formato={formato}{'&uno_por_tipo=si' if uno_por_tipo else ''}">
  <div class="row"><b>Opción A — subir archivo del sobre:</b><br>
    <input type="file" name="sobre" accept=".xml,text/xml"></div>
  <div class="row"><b>Opción B — pegar el XML del sobre:</b><br>
    <textarea name="xml" placeholder="<EnvioDTE>...</EnvioDTE>"></textarea></div>
  <div class="row"><label><input type="checkbox" name="upt"
    onchange="this.form.action=this.checked?'?tenant_id={tenant_id}&formato={formato}&uno_por_tipo=si':'?tenant_id={tenant_id}&formato={formato}'">
    Solo una muestra por tipo (modo Simulación)</label></div>
  <button type="submit">📦 Generar y descargar PDFs (ZIP)</button>
</form>
<p style="font-size:12px;color:#666;margin-top:18px;">
Para la Simulación: usa el sobre de los 23 DTE con "una por tipo".
Para el Set de Pruebas: sube cada sobre de set completo.</p>
</div></body></html>"""

    # ─── POST: procesar el sobre ───
    sobre = _leer_sobre_del_request()
    if not sobre:
        return jsonify({"ok": False, "error": "No se recibió el sobre (sube un archivo o pega el XML)."}), 400

    docs = _separar_dtes(sobre)
    if not docs:
        return jsonify({"ok": False, "error": "No se encontraron documentos <DTE> en el sobre."}), 400

    # Preview de un documento puntual
    if preview and ":" in preview:
        t_q, f_q = preview.split(":", 1)
        doc = next((d for d in docs if d["tipo"] == t_q and d["folio"] == f_q), None)
        if not doc:
            return jsonify({"ok": False, "error": f"No existe DTE tipo {t_q} folio {f_q}"}), 404
        pdf = generar_pdf_dte(doc["xml"].encode("iso-8859-1", errors="replace"),
                              formato=formato, url_consulta=URL_CONSULTA)
        return Response(pdf, mimetype="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="{_slug_tipo(doc["tipo"])}_folio{doc["folio"]}.pdf"'})

    # Una muestra por tipo
    if uno_por_tipo:
        vistos = {}
        for d in docs:
            if d["tipo"] not in vistos:
                vistos[d["tipo"]] = d
        docs_sel = list(vistos.values())
    else:
        docs_sel = docs

    # Armar ZIP
    zip_buf = io.BytesIO()
    errores = []
    generados = 0
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for d in docs_sel:
            try:
                pdf = generar_pdf_dte(d["xml"].encode("iso-8859-1", errors="replace"),
                                      formato=formato, url_consulta=URL_CONSULTA)
                zf.writestr(f"{_slug_tipo(d['tipo'])}_folio{d['folio']}.pdf", pdf)
                generados += 1
            except Exception as e:
                errores.append(f"tipo {d['tipo']} folio {d['folio']}: {e}")
        manifiesto = [f"Muestras impresas — {generados} PDF generados", ""]
        for d in docs_sel:
            manifiesto.append(f"  {_slug_tipo(d['tipo']):18} tipo {d['tipo']:>3} folio {d['folio']}")
        if errores:
            manifiesto += ["", "ERRORES:"] + ["  " + e for e in errores]
        zf.writestr("_manifiesto.txt", "\n".join(manifiesto))

    zip_buf.seek(0)
    nombre_zip = "muestras_simulacion.zip" if uno_por_tipo else "muestras_impresas.zip"
    return Response(zip_buf.read(), mimetype="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{nombre_zip}"'})
