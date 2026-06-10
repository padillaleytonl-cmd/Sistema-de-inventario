"""
facturacion/endpoint_liquidacion_bp.py
─────────────────────────────────────────────────────────────
Blueprint con el endpoint del Set Liquidación-Factura SII (DTE 43, set 4829128).

INTEGRACIÓN en app.py — agregar estas 2 líneas (junto a los otros blueprints):

    from endpoint_liquidacion_bp import liquidacion_set_bp
    app.register_blueprint(liquidacion_set_bp)

Este archivo va en la RAÍZ del repo (al lado de app.py, walmart.py, paris.py).
Ruta que expone:  GET /admin/lusync/sii/test-set-liquidacion
"""
from flask import Blueprint, session, redirect, request, jsonify, Response
from datetime import datetime

liquidacion_set_bp = Blueprint("liquidacion_set_bp", __name__)


@liquidacion_set_bp.route("/admin/lusync/sii/test-set-liquidacion", methods=["GET"])
def admin_lusync_sii_test_set_liquidacion():
    """Emite los 4 casos del Set Liquidación-Factura SII (4829128) en UN sobre EnvioDTE.
    DTE tipo 43. Estructura especial: contenedor <Liquidacion>, detalle con montos
    que pueden ser negativos, y nodo <Comisiones> para comisiones del liquidador.
    """
    if not session.get("logged"):
        return redirect("/login")
    if session.get("rol") != "admin" and not session.get("is_lusync_admin"):
        return jsonify({"ok": False, "error": "solo admin del tenant"}), 403
    from inventario import get_conn, release_conn
    from facturacion.certificados import obtener_certificado
    from facturacion.db import obtener_config_facturacion

    tenant_id = request.args.get("tenant_id", default=3, type=int)
    ambiente = request.args.get("ambiente", default="certificacion")
    confirmar = request.args.get("confirmar", default="")
    descargar = request.args.get("descargar", default="")
    debug = request.args.get("debug", default="")

    pasos = []
    def paso(nombre, ok, detalle=""):
        pasos.append({"nombre": nombre, "ok": ok, "detalle": detalle})

    RECEPTOR_SET = {
        "rut": "55555555-5",
        "razon_social": "LIQUIDADOR DE PRUEBAS LUSYNC",
        "giro": "Comercio al por menor",
        "direccion": "Av. Providencia 1234",
        "comuna": "Providencia",
    }

    if confirmar != "si" and descargar != "si" and debug == "":
        f43_q = request.args.get("f43", "")
        extra_q = f"&f43={f43_q}" if f43_q else ""
        return """<!DOCTYPE html><html><head><meta charset="utf-8">
        <title>Confirmar Set Liquidación SII</title>
        <style>body{font-family:-apple-system,sans-serif;background:#f6f5f1;padding:40px;}
        .card{max-width:680px;margin:0 auto;background:white;border-radius:14px;padding:28px;
        box-shadow:0 4px 20px rgba(0,0,0,0.06);}
        .warn{background:#fef3c7;border:1px solid #f59e0b;border-radius:8px;padding:14px;color:#92400e;font-size:13px;margin-bottom:10px;}
        table{width:100%;border-collapse:collapse;margin:14px 0;font-size:12px;}
        td{padding:6px 8px;border-bottom:1px solid #f0f0ee;}
        a.btn{display:inline-block;margin-top:18px;background:#534AB7;color:white;padding:12px 20px;
        border-radius:8px;text-decoration:none;font-weight:600;font-size:14px;margin-right:10px;}
        a.btn-warn{background:#dc2626;}</style></head><body>
        <div class="card">
        <h2 style="margin-top:0;">📋 Emitir SET LIQUIDACIÓN-FACTURA SII (4829128)</h2>
        <div class="warn">
        Genera los 4 casos del set oficial: 4 Liquidaciones-Factura (43).<br>
        Estructura especial: montos pueden ser negativos (NC), nodo Comisiones.<br><br>
        • <b>Descargar</b>: genera el sobre EnvioDTE.xml para subir manual al portal SII (recomendado)<br>
        • Consume: 4 folios CAF 43
        </div>
        <table>
        <tr><td><b>CASO 1</b></td><td>Liquidación (43)</td><td>4 líneas neto/exento</td><td style="text-align:right;">Total $1.199.314</td></tr>
        <tr><td><b>CASO 2</b></td><td>Liquidación (43)</td><td>FE, NC negativas, boletas</td><td style="text-align:right;">Total $7.340.486</td></tr>
        <tr><td><b>CASO 3</b></td><td>Liquidación (43)</td><td>FE + comisiones</td><td style="text-align:right;">Total $735.814</td></tr>
        <tr><td><b>CASO 4</b></td><td>Liquidación (43)</td><td>anticipo, NC, comisiones negativas</td><td style="text-align:right;">Total $2.521.137</td></tr>
        </table>
        <a class="btn" href="?tenant_id=""" + str(tenant_id) + """&descargar=si""" + extra_q + """">
        📥 Generar y descargar sobre EnvioDTE</a>
        <a class="btn btn-warn" href="?tenant_id=""" + str(tenant_id) + """&confirmar=si""" + extra_q + """">
        ⚡ Generar y enviar al SII vía SOAP</a>
        </div></body></html>"""

    error_fatal = False
    track_id = None
    import html as _html
    detalles_casos = []
    sobre = None
    try:
        # ─── 1. Certificado ───
        cert = obtener_certificado(get_conn, release_conn, tenant_id)
        if not cert.get("ok"):
            paso("Leer certificado", False, cert.get("error", "?"))
            error_fatal = True
        else:
            paso("Leer certificado", True, cert["metadata"].get("titular", "?"))

        # ─── 2. Config emisor + CAF 43 ───
        caf43 = None
        if not error_fatal:
            config = obtener_config_facturacion(get_conn, release_conn, tenant_id)
            emisor = {
                "rut": config["rut_emisor"], "razon_social": config["razon_social"],
                "giro": config.get("giro", "Venta al por menor"),
                "dir_origen": config.get("direccion", "Sin dirección"),
                "cmna_origen": config.get("comuna", "Santiago"),
            }
            if config.get("acteco"):
                emisor["acteco"] = config["acteco"]
            paso("Datos del emisor", True, f"{emisor['razon_social']} · {emisor['rut']}")

            from facturacion.dtes.caf_parser import parsear_caf_xml
            conn = get_conn()
            try:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT xml_caf FROM facturacion_cafs
                        WHERE tenant_id = %s AND tipo_dte = 43
                        ORDER BY id DESC LIMIT 1
                    """, (tenant_id,))
                    row = cur.fetchone()
                    if not row:
                        paso("CAF tipo 43", False, "No hay CAF tipo 43 cargado")
                        error_fatal = True
                    else:
                        caf43 = parsear_caf_xml(row[0])
                        paso("CAF tipo 43", True, f"Rango {caf43.rango_desde}-{caf43.rango_hasta}")
            finally:
                release_conn(conn)

        # ─── 3. Generar los 4 documentos ───
        documentos_sin_firma = []
        documento_ids = []
        if not error_fatal:
            from facturacion.dtes.liquidacion import generar_liquidacion_xml
            try:
                from zoneinfo import ZoneInfo
                fecha = datetime.now(ZoneInfo("America/Santiago")).strftime("%Y-%m-%d")
            except Exception:
                from datetime import timezone as _tz, timedelta as _td
                fecha = (datetime.now(_tz.utc) - _td(hours=4)).strftime("%Y-%m-%d")

            SET = "4829128"
            f43_param = (request.args.get("f43") or "").strip()
            folio_base = int(f43_param) if f43_param.isdigit() else caf43.rango_desde
            paso("Folios a usar", True, f"43: {folio_base} a {folio_base+3}")

            casos = [
                # CASO 1 (glosas EXACTAS del set, con acentos donde corresponde)
                dict(items=[
                    {'nombre': 'NETO FACTURAS', 'cantidad': 11, 'monto': 670860, 'exento': False, 'tpo_doc_liq': 30},
                    {'nombre': 'EXENTO FACTURAS', 'cantidad': 8, 'monto': 168607, 'exento': True, 'tpo_doc_liq': 30},
                    {'nombre': 'NETO FACTURAS ELECTRONICAS', 'cantidad': 51, 'monto': 109129, 'exento': False, 'tpo_doc_liq': 33},
                    {'nombre': 'EXENTO FACTURAS ELECTRONICAS', 'cantidad': 37, 'monto': 102520, 'exento': True, 'tpo_doc_liq': 33},
                ], comisiones=None),
                # CASO 2 — boleta AFECTA ÍNTEGRA (MontoItem entero a MntNeto, MntTotal=8523935 confirmado OK por SII 05-jun); tdl=35
                dict(items=[
                    {'nombre': 'NETO FACTURA ELECTRÓNICA 4254', 'cantidad': 1, 'monto': 48705, 'exento': False, 'tpo_doc_liq': 33},
                    {'nombre': 'EXENTO FACTURA ELECTRÓNICA 4254', 'cantidad': 1, 'monto': 23845, 'exento': True, 'tpo_doc_liq': 33},
                    {'nombre': 'NETO FACTURA ELECTRÓNICA 4768', 'cantidad': 1, 'monto': 624461, 'exento': False, 'tpo_doc_liq': 33},
                    {'nombre': 'EXENTO FACTURA ELECTRÓNICA 4768', 'cantidad': 1, 'monto': 365464, 'exento': True, 'tpo_doc_liq': 33},
                    {'nombre': 'NETO NOTA DE CRÉDITO 328', 'cantidad': 1, 'monto': -50894, 'exento': False, 'tpo_doc_liq': 60},
                    {'nombre': 'EXENTO NOTA DE CRÉDITO 328', 'cantidad': 1, 'monto': -18006, 'exento': True, 'tpo_doc_liq': 60},
                    {'nombre': 'BOLETAS', 'cantidad': 8262, 'monto': 6228679, 'exento': False, 'tpo_doc_liq': 39},
                ], comisiones=None),
                # CASO 3 — glosas con acentos exactas
                dict(items=[
                    {'nombre': 'NETO FACTURA ELECTRÓNICA 1515', 'cantidad': 1, 'monto': 373473, 'exento': False, 'tpo_doc_liq': 33},
                    {'nombre': 'NETO FACTURAS ELECTRÓNICAS', 'cantidad': 299, 'monto': 148087, 'exento': False, 'tpo_doc_liq': 33},
                    {'nombre': 'EXENTO FACTURAS ELECTRÓNICAS', 'cantidad': 51, 'monto': 115158, 'exento': True, 'tpo_doc_liq': 33},
                ], comisiones=[
                    {'tipo_movim': 'C', 'glosa': 'NETO COMISIÓN FIJA', 'neto': 3074},
                    {'tipo_movim': 'C', 'glosa': 'NETO COMISIÓN VARIABLE', 'neto': 7404},
                ]),
                # CASO 4 — glosas con acentos exactas; anticipo tdl=99; fix peso en liquidacion.py
                dict(items=[
                    {'nombre': 'NETO ANTICIPO FACTURACIÓN', 'cantidad': 299, 'monto': 550000, 'exento': False, 'tpo_doc_liq': 99},
                    {'nombre': 'NETO FACTURAS', 'cantidad': 51, 'monto': 353979, 'exento': False, 'tpo_doc_liq': 30},
                    {'nombre': 'EXENTO FACTURAS', 'cantidad': 57, 'monto': 208950, 'exento': True, 'tpo_doc_liq': 30},
                    {'nombre': 'NETO FACTURAS ELECTRÓNICAS', 'cantidad': 44, 'monto': 106363, 'exento': False, 'tpo_doc_liq': 33},
                    {'nombre': 'EXENTO FACTURAS ELECTRÓNICAS', 'cantidad': 9, 'monto': 1531568, 'exento': True, 'tpo_doc_liq': 33},
                    {'nombre': 'NETO NOTA DE CRÉDITO 1981', 'cantidad': 1, 'monto': -92616, 'exento': False, 'tpo_doc_liq': 60},
                    {'nombre': 'NETO LIQUIDACIÓN FACTURA ELECTRÓNICA 4554', 'cantidad': 1, 'monto': -141710, 'exento': False, 'tpo_doc_liq': 43},
                    {'nombre': 'EXENTO LIQUIDACIÓN FACTURA ELECTRÓNICA 4554', 'cantidad': 1, 'monto': -142840, 'exento': True, 'tpo_doc_liq': 43},
                ], comisiones=[
                    {'tipo_movim': 'C', 'glosa': 'NETO COMISIÓN CONSIGNACION', 'neto': 2156},
                    {'tipo_movim': 'C', 'glosa': 'NETO COMISIONES LIQUIDACIÓN FACTURA ELECTRÓNICA 4554', 'neto': -7086},
                ]),
            ]

            for idx, caso in enumerate(casos, start=1):
                folio = folio_base + (idx - 1)
                emisor_caso = dict(emisor)
                if caso.get("omitir_ipt"):
                    emisor_caso["omitir_iva_prop_terc"] = True
                r = generar_liquidacion_xml(
                    caf=caf43, folio=folio, fecha_emision=fecha,
                    emisor=emisor_caso, receptor=RECEPTOR_SET,
                    items=caso["items"], comisiones=caso["comisiones"],
                    referencias=[{
                        "tpo_doc_ref": "SET", "folio_ref": SET,
                        "fecha_ref": fecha, "razon_ref": f"CASO {SET}-{idx}",
                    }],
                )
                documentos_sin_firma.append(r["xml"])
                documento_ids.append(r["documento_id"])
                t = r["totales"]
                detalles_casos.append(f"C{idx} f{folio}: Total ${t['mnt_total']:,}".replace(",", "."))

            paso("Generar 4 liquidaciones", True, " · ".join(detalles_casos))

        # ─── 4. Armar sobre EnvioDTE ───
        if not error_fatal:
            from facturacion.dtes.envio_dte import armar_envio_dte
            set_id = "SetDoc"
            sobre = armar_envio_dte(
                dtes_firmados=documentos_sin_firma,
                rut_emisor=emisor["rut"],
                rut_envia=cert["metadata"].get("rut", "18849272-K"),
                fch_resol="2026-05-15", nro_resol=0,
                subtotales={43: 4}, set_dte_id=set_id,
            )
            paso("Armar sobre EnvioDTE (4 docs)", True, f"{len(sobre)} bytes")

        # ─── debug sin-firma ───
        if not error_fatal and debug == "sin-firma":
            return Response(sobre, mimetype="application/xml",
                headers={"Content-Disposition": 'attachment; filename="EnvioDTE_SetLiquidacion_SinFirma_DEBUG.xml"'})

        # ─── 5. Firmar el sobre completo ───
        if not error_fatal:
            from facturacion.dtes.firma import firmar_envio_completo
            try:
                sobre_firmado = firmar_envio_completo(
                    sobre, cert["pfx_bytes"], cert["password"],
                    set_dte_id=set_id, documento_ids=documento_ids)
                paso("Firmar sobre completo", True, f"{len(sobre_firmado)} bytes")
            except Exception as e_firma:
                import traceback
                paso("Firmar sobre completo", False, f"{str(e_firma)[:400]}\n{traceback.format_exc()[:300]}")
                error_fatal = True

        if not error_fatal and descargar == "si":
            return Response(sobre_firmado, mimetype="application/xml",
                headers={"Content-Disposition": 'attachment; filename="EnvioDTE_SetLiquidacion4829128.xml"'})

        # ─── 6. Envío SOAP ───
        if not error_fatal and confirmar == "si":
            from facturacion.dtes.sii_client import obtener_token_dte, enviar_dte
            try:
                tok = obtener_token_dte(cert["pfx_bytes"], cert["password"], ambiente)
                paso("Obtener token SOAP", True, f"Token: {tok[:18]}…")
            except Exception as e:
                paso("Obtener token SOAP", False, str(e)[:300])
                error_fatal = True
            if not error_fatal:
                resultado = enviar_dte(
                    envio_xml=sobre_firmado, token=tok,
                    rut_emisor=emisor["rut"],
                    rut_envia=cert["metadata"].get("rut", "18849272-K"),
                    ambiente=ambiente)
                if resultado.get("ok"):
                    track_id = resultado["track_id"]
                    paso("Enviar al SII (DTEUpload)", True, f"✓ Track ID: {track_id}")
                else:
                    detalle = resultado.get("error") or _html.escape(str(resultado.get("respuesta_cruda", ""))[:400])
                    paso("Enviar al SII (DTEUpload)", False, detalle)
                    error_fatal = True

    except Exception as e:
        import traceback
        paso("Error", False, _html.escape(traceback.format_exc()[:600]))
        error_fatal = True

    todo_ok = all(p["ok"] for p in pasos) and (track_id is not None or confirmar != "si")
    color = "#10b981" if todo_ok else "#dc2626"
    emoji = "🎉" if todo_ok else "❌"
    filas = ""
    for p in pasos:
        ic = "✅" if p["ok"] else "❌"
        col = "#10b981" if p["ok"] else "#dc2626"
        filas += f"""<div style="display:flex;gap:10px;padding:12px 14px;border-bottom:1px solid #f0f0ee;">
          <div>{ic}</div><div style="flex:1;"><div style="font-weight:600;font-size:13px;">{p['nombre']}</div>
          <div style="color:{col};font-size:12px;font-family:monospace;word-break:break-word;white-space:pre-wrap;">{p['detalle']}</div>
          </div></div>"""
    track_msg = ""
    if track_id:
        track_msg = (f'<div style="padding:14px 20px;background:#ecfdf5;color:#065f46;font-size:13px;">'
                     f'Track ID: <b>{track_id}</b> — consulta el estado en DTEauth?3</div>')
    return f"""<!DOCTYPE html><html lang="es"><head><meta charset="utf-8">
    <title>Set Liquidación · Lusync</title><meta name="viewport" content="width=device-width, initial-scale=1">
    <style>body{{font-family:-apple-system,sans-serif;background:#f6f5f1;margin:0;padding:24px;}}
    .card{{max-width:680px;margin:0 auto;background:white;border-radius:14px;overflow:hidden;box-shadow:0 4px 20px rgba(0,0,0,0.06);}}
    .hd{{background:{color};color:white;padding:20px;}}.hd h1{{margin:0;font-size:17px;}}</style></head><body>
    <div class="card"><div class="hd"><h1>{emoji} Set Liquidación-Factura (4829128)</h1></div>
    {track_msg}<div>{filas}</div></div></body></html>"""
