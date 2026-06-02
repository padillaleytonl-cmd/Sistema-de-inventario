# ════════════════════════════════════════════════════════════════════════════
# SET FACTURA EXENTA (4829127) — pegar en app.py
# ════════════════════════════════════════════════════════════════════════════
# Emite los 8 casos del Set Factura Exenta del SII en UN solo sobre EnvioDTE:
#   3 Facturas Exentas (34) + 3 Notas de Crédito (61) + 2 Notas de Débito (56)
#
# Todas las NC/ND van en MODO EXENTO (es_exenta=True): sin IVA, solo MntExe+MntTotal.
#
# Mapa de casos (set 4829127):
#   C1 FE34  : HORAS PROGRAMADOR 4 x 3.288 (unidad Hora)          → MntExe 13.152
#   C2 NC61  : modifica monto C1, HORAS PROGRAMADOR valor 411     → MntExe 411   (CodRef 3)
#   C3 FE34  : 2 servicios consultoría (209.458 + 206.961)        → MntExe 416.419
#   C4 NC61  : corrige giro C3 (simbólica, sin items)             → MntTotal 0   (CodRef 2)
#   C5 ND56  : anula NC del C4 (simbólica)                        → MntTotal 0   (CodRef 1)
#   C6 FE34  : 2 capacitaciones (285.689 + 182.818)               → MntExe 468.507
#   C7 NC61  : modifica monto C6, CIGÜEÑALES valor 142.844        → MntExe 142.844 (CodRef 3)
#   C8 ND56  : modifica monto C6, PLC's CNC valor 36.564          → MntExe 36.564  (CodRef 3)
#
# Patrón de uso (igual que test-set-basico / test-set-guias):
#   Debug sin firma (no gasta nada, solo inspecciona el XML):
#     https://lusync.cl/admin/lusync/sii/test-set-fact-exenta?tenant_id=3&debug=sin-firma
#   Descargar sobre firmado para subir manual al portal SII (recomendado):
#     https://lusync.cl/admin/lusync/sii/test-set-fact-exenta?tenant_id=3&descargar=si
#   Forzar folios (reintento con folios nuevos tras rechazo):
#     ...&f34=54&f61=119&f56=108
#   Enviar por SOAP (requiere token funcionando):
#     ...&confirmar=si
# ════════════════════════════════════════════════════════════════════════════

@app.route("/admin/lusync/sii/test-set-fact-exenta", methods=["GET"])
def admin_lusync_sii_test_set_fact_exenta():
    """Emite los 8 casos del Set Factura Exenta SII (4829127) en UN sobre EnvioDTE:
       3 Facturas Exentas (34) + 3 Notas de Crédito (61) + 2 Notas de Débito (56).

    Auth: sesión normal de admin de tenant (igual que test-set-basico).
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

    pasos = []
    def paso(nombre, ok, detalle=""):
        pasos.append({"nombre": nombre, "ok": ok, "detalle": detalle})

    # Receptor de pruebas (el SII acepta cualquier RUT válido en certificación)
    RECEPTOR_SET = {
        "rut": "55555555-5",
        "razon_social": "CLIENTE DE PRUEBAS LUSYNC",
        "giro": "Comercio al por menor",
        "direccion": "Av. Providencia 1234",
        "comuna": "Providencia",
    }

    # ─── Pantalla de confirmación ───
    if confirmar != "si" and descargar != "si":
        f34_q = request.args.get("f34", "")
        f61_q = request.args.get("f61", "")
        f56_q = request.args.get("f56", "")
        debug_q = request.args.get("debug", "")
        extra_q = ""
        if f34_q: extra_q += f"&f34={f34_q}"
        if f61_q: extra_q += f"&f61={f61_q}"
        if f56_q: extra_q += f"&f56={f56_q}"
        if debug_q: extra_q += f"&debug={debug_q}"
        folios_msg = ""
        if f34_q or f61_q or f56_q:
            folios_msg = (f'<div class="warn" style="background:#dbeafe;border-color:#3b82f6;color:#1e40af;">'
                          f'Folios forzados por URL: f34={f34_q or "(default)"} · '
                          f'f61={f61_q or "(default)"} · f56={f56_q or "(default)"}</div>')
        return """<!DOCTYPE html><html><head><meta charset="utf-8">
        <title>Confirmar Set Factura Exenta SII</title>
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
        <h2 style="margin-top:0;">📋 Emitir SET FACTURA EXENTA SII (4829127)</h2>
        """ + folios_msg + """
        <div class="warn">
        Genera los 8 casos del set oficial: 3 Facturas Exentas + 3 NC + 2 ND.<br>
        Todas las NC/ND van en modo EXENTO (sin IVA, solo Monto Exento).<br><br>
        • <b>Descargar</b>: genera el sobre EnvioDTE.xml para subir manual al portal SII (recomendado)<br>
        • <b>Enviar SOAP</b>: requiere token SOAP funcionando<br>
        • Consume: 3 folios CAF 34 + 3 folios CAF 61 + 2 folios CAF 56
        </div>
        <table>
        <tr><td><b>CASO 1</b></td><td>Factura Exenta (34)</td><td>HORAS PROGRAMADOR 4 × 3.288 (Hora)</td><td style="text-align:right;">$13.152</td></tr>
        <tr><td><b>CASO 2</b></td><td>NC (61)</td><td>Modifica monto C1 (valor 411)</td><td style="text-align:right;">$411</td></tr>
        <tr><td><b>CASO 3</b></td><td>Factura Exenta (34)</td><td>2 servicios consultoría</td><td style="text-align:right;">$416.419</td></tr>
        <tr><td><b>CASO 4</b></td><td>NC (61)</td><td>Corrige giro C3 (simbólica)</td><td style="text-align:right;">$0</td></tr>
        <tr><td><b>CASO 5</b></td><td>ND (56)</td><td>Anula NC del C4 (simbólica)</td><td style="text-align:right;">$0</td></tr>
        <tr><td><b>CASO 6</b></td><td>Factura Exenta (34)</td><td>2 capacitaciones</td><td style="text-align:right;">$468.507</td></tr>
        <tr><td><b>CASO 7</b></td><td>NC (61)</td><td>Modifica monto C6 (Cigüeñales 142.844)</td><td style="text-align:right;">$142.844</td></tr>
        <tr><td><b>CASO 8</b></td><td>ND (56)</td><td>Modifica monto C6 (PLC's 36.564)</td><td style="text-align:right;">$36.564</td></tr>
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
    try:
        # ─── 1. Certificado ───
        cert = obtener_certificado(get_conn, release_conn, tenant_id)
        if not cert.get("ok"):
            paso("Leer certificado", False, cert.get("error", "?"))
            error_fatal = True
        else:
            paso("Leer certificado", True, cert["metadata"].get("titular", "?"))

        # ─── 2. Config emisor + 3 CAFs (34, 61, 56) ───
        cafs_dict = {}
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
                    for tipo in (34, 61, 56):
                        cur.execute("""
                            SELECT xml_caf FROM facturacion_cafs
                            WHERE tenant_id = %s AND tipo_dte = %s
                            ORDER BY id DESC LIMIT 1
                        """, (tenant_id, tipo))
                        row = cur.fetchone()
                        if not row:
                            paso(f"CAF tipo {tipo}", False, f"No hay CAF tipo {tipo} cargado")
                            error_fatal = True
                        else:
                            caf = parsear_caf_xml(row[0])
                            cafs_dict[tipo] = caf
                            paso(f"CAF tipo {tipo}", True,
                                 f"Rango {caf.rango_desde}-{caf.rango_hasta}")
            finally:
                release_conn(conn)

        # ─── 3. Generar los 8 documentos ───
        documentos_sin_firma = []
        documento_ids = []
        if not error_fatal:
            from facturacion.dtes.factura import generar_factura_xml
            from facturacion.dtes.nota_credito import generar_nota_credito_xml
            from facturacion.dtes.nota_debito import generar_nota_debito_xml
            try:
                from zoneinfo import ZoneInfo
                fecha = datetime.now(ZoneInfo("America/Santiago")).strftime("%Y-%m-%d")
            except Exception:
                from datetime import timezone as _tz, timedelta as _td
                fecha = (datetime.now(_tz.utc) - _td(hours=4)).strftime("%Y-%m-%d")

            SET = "4829127"

            # Folios iniciales (override opcional vía ?f34=&f61=&f56=)
            f34_param = (request.args.get("f34") or "").strip()
            f61_param = (request.args.get("f61") or "").strip()
            f56_param = (request.args.get("f56") or "").strip()
            folio_34 = int(f34_param) if f34_param.isdigit() else cafs_dict[34].rango_desde
            folio_61 = int(f61_param) if f61_param.isdigit() else cafs_dict[61].rango_desde
            folio_56 = int(f56_param) if f56_param.isdigit() else cafs_dict[56].rango_desde
            paso("Folios a usar", True,
                 f"34: {folio_34}-{folio_34+2} (param f34={f34_param!r}) · "
                 f"61: {folio_61}-{folio_61+2} (param f61={f61_param!r}) · "
                 f"56: {folio_56}-{folio_56+1} (param f56={f56_param!r})")

            # CASO 1: Factura Exenta (34) — HORAS PROGRAMADOR 4 x 3288, unidad Hora
            r = generar_factura_xml(
                caf=cafs_dict[34], folio=folio_34, fecha_emision=fecha,
                emisor=emisor, receptor=RECEPTOR_SET,
                items=[
                    {'nombre': 'HORAS PROGRAMADOR', 'cantidad': 4, 'precio_unitario': 3288, 'unidad': 'Hora'},
                ],
                referencias=[{
                    "tpo_doc_ref": "SET", "folio_ref": SET,
                    "fecha_ref": fecha, "razon_ref": "CASO 4829127-1",
                }],
                es_exenta=True,
            )
            c1_folio = r["folio"]; c1_total = r["totales"]["mnt_total"]
            documentos_sin_firma.append(r["xml"]); documento_ids.append(r["documento_id"])
            detalles_casos.append(f"CASO-1 (FE34 f{c1_folio}): ${c1_total:,}".replace(",", "."))
            folio_34 += 1

            # CASO 2: NC (61) modifica monto de la FE C1 — HORAS PROGRAMADOR valor 411
            r = generar_nota_credito_xml(
                caf=cafs_dict[61], folio=folio_61, fecha_emision=fecha,
                emisor=emisor, receptor=RECEPTOR_SET,
                referencia={"folio_ref": c1_folio, "tipo_doc_ref": 34, "fecha_ref": fecha,
                            "cod_ref": 3, "razon_ref": "MODIFICA MONTO"},
                items=[
                    {'nombre': 'HORAS PROGRAMADOR', 'cantidad': 1, 'precio_unitario': 411, 'unidad': 'Hora'},
                ],
                set_referencia={"folio_ref": SET, "fecha_ref": fecha,
                                "razon_ref": "CASO 4829127-2"},
                es_exenta=True,
            )
            c2_folio = r["folio"]; c2_total = r["totales"]["mnt_total"]
            documentos_sin_firma.append(r["xml"]); documento_ids.append(r["documento_id"])
            detalles_casos.append(f"CASO-2 (NC61 f{c2_folio}): ${c2_total:,}".replace(",", "."))
            folio_61 += 1

            # CASO 3: Factura Exenta (34) — 2 servicios consultoría
            r = generar_factura_xml(
                caf=cafs_dict[34], folio=folio_34, fecha_emision=fecha,
                emisor=emisor, receptor=RECEPTOR_SET,
                items=[
                    {'nombre': 'SERV CONSULTORIA FACT ELECTRONICA', 'cantidad': 1, 'precio_unitario': 209458},
                    {'nombre': 'SERV CONSULTORIA GUIA DESPACHO ELECT', 'cantidad': 1, 'precio_unitario': 206961},
                ],
                referencias=[{
                    "tpo_doc_ref": "SET", "folio_ref": SET,
                    "fecha_ref": fecha, "razon_ref": "CASO 4829127-3",
                }],
                es_exenta=True,
            )
            c3_folio = r["folio"]; c3_total = r["totales"]["mnt_total"]
            documentos_sin_firma.append(r["xml"]); documento_ids.append(r["documento_id"])
            detalles_casos.append(f"CASO-3 (FE34 f{c3_folio}): ${c3_total:,}".replace(",", "."))
            folio_34 += 1

            # CASO 4: NC (61) corrige giro de la FE C3 — simbólica (CodRef 2, sin items)
            r = generar_nota_credito_xml(
                caf=cafs_dict[61], folio=folio_61, fecha_emision=fecha,
                emisor=emisor, receptor=RECEPTOR_SET,
                referencia={"folio_ref": c3_folio, "tipo_doc_ref": 34, "fecha_ref": fecha,
                            "cod_ref": 2, "razon_ref": "CORRIGE GIRO"},
                items=None,
                set_referencia={"folio_ref": SET, "fecha_ref": fecha,
                                "razon_ref": "CASO 4829127-4"},
                es_exenta=True,
            )
            c4_folio = r["folio"]; c4_total = r["totales"]["mnt_total"]
            documentos_sin_firma.append(r["xml"]); documento_ids.append(r["documento_id"])
            detalles_casos.append(f"CASO-4 (NC61 f{c4_folio}): ${c4_total:,}".replace(",", "."))
            folio_61 += 1

            # CASO 5: ND (56) anula la NC del C4 — simbólica (CodRef 1, mnt_anulacion=0)
            r = generar_nota_debito_xml(
                caf=cafs_dict[56], folio=folio_56, fecha_emision=fecha,
                emisor=emisor, receptor=RECEPTOR_SET,
                referencia={"folio_ref": c4_folio, "tipo_doc_ref": 61, "fecha_ref": fecha,
                            "cod_ref": 1, "razon_ref": "ANULA NOTA DE CREDITO ELECTRONICA",
                            "mnt_anulacion": 0},
                items=[],
                set_referencia={"folio_ref": SET, "fecha_ref": fecha,
                                "razon_ref": "CASO 4829127-5"},
                es_exenta=True,
            )
            c5_folio = r["folio"]; c5_total = r["totales"]["mnt_total"]
            documentos_sin_firma.append(r["xml"]); documento_ids.append(r["documento_id"])
            detalles_casos.append(f"CASO-5 (ND56 f{c5_folio}): ${c5_total:,}".replace(",", "."))
            folio_56 += 1

            # CASO 6: Factura Exenta (34) — 2 capacitaciones
            r = generar_factura_xml(
                caf=cafs_dict[34], folio=folio_34, fecha_emision=fecha,
                emisor=emisor, receptor=RECEPTOR_SET,
                items=[
                    {'nombre': 'CAPACITACION USO CIGUEÑALES', 'cantidad': 1, 'precio_unitario': 285689},
                    {'nombre': "CAPACITACION USO PLC's CNC", 'cantidad': 1, 'precio_unitario': 182818},
                ],
                referencias=[{
                    "tpo_doc_ref": "SET", "folio_ref": SET,
                    "fecha_ref": fecha, "razon_ref": "CASO 4829127-6",
                }],
                es_exenta=True,
            )
            c6_folio = r["folio"]; c6_total = r["totales"]["mnt_total"]
            documentos_sin_firma.append(r["xml"]); documento_ids.append(r["documento_id"])
            detalles_casos.append(f"CASO-6 (FE34 f{c6_folio}): ${c6_total:,}".replace(",", "."))

            # CASO 7: NC (61) modifica monto de la FE C6 — CIGÜEÑALES valor 142.844
            r = generar_nota_credito_xml(
                caf=cafs_dict[61], folio=folio_61, fecha_emision=fecha,
                emisor=emisor, receptor=RECEPTOR_SET,
                referencia={"folio_ref": c6_folio, "tipo_doc_ref": 34, "fecha_ref": fecha,
                            "cod_ref": 3, "razon_ref": "MODIFICA MONTO"},
                items=[
                    {'nombre': 'CAPACITACION USO CIGUEÑALES', 'cantidad': 1, 'precio_unitario': 142844},
                ],
                set_referencia={"folio_ref": SET, "fecha_ref": fecha,
                                "razon_ref": "CASO 4829127-7"},
                es_exenta=True,
            )
            c7_folio = r["folio"]; c7_total = r["totales"]["mnt_total"]
            documentos_sin_firma.append(r["xml"]); documento_ids.append(r["documento_id"])
            detalles_casos.append(f"CASO-7 (NC61 f{c7_folio}): ${c7_total:,}".replace(",", "."))
            folio_61 += 1

            # CASO 8: ND (56) modifica monto de la FE C6 — PLC's CNC valor 36.564
            # OJO: el set referencia la FACTURA C6 (no la NC), con CodRef 3 (modifica monto).
            r = generar_nota_debito_xml(
                caf=cafs_dict[56], folio=folio_56, fecha_emision=fecha,
                emisor=emisor, receptor=RECEPTOR_SET,
                referencia={"folio_ref": c6_folio, "tipo_doc_ref": 34, "fecha_ref": fecha,
                            "cod_ref": 3, "razon_ref": "MODIFICA MONTO"},
                items=[
                    {'nombre': "CAPACITACION USO PLC's CNC", 'cantidad': 1, 'precio_unitario': 36564},
                ],
                set_referencia={"folio_ref": SET, "fecha_ref": fecha,
                                "razon_ref": "CASO 4829127-8"},
                es_exenta=True,
            )
            c8_folio = r["folio"]; c8_total = r["totales"]["mnt_total"]
            documentos_sin_firma.append(r["xml"]); documento_ids.append(r["documento_id"])
            detalles_casos.append(f"CASO-8 (ND56 f{c8_folio}): ${c8_total:,}".replace(",", "."))

            paso("Generar 8 documentos", True, " · ".join(detalles_casos))

        # ─── 4. Armar UN sobre EnvioDTE con todos ───
        if not error_fatal:
            from facturacion.dtes.envio_dte import armar_envio_dte
            set_id = "SetDoc"
            subtot = {34: 3, 61: 3, 56: 2}  # 3 FE + 3 NC + 2 ND
            sobre = armar_envio_dte(
                dtes_firmados=documentos_sin_firma,
                rut_emisor=emisor["rut"],
                rut_envia=cert["metadata"].get("rut", "18849272-K"),
                fch_resol="2026-05-15", nro_resol=0,
                subtotales=subtot, set_dte_id=set_id,
            )
            paso("Armar sobre EnvioDTE (8 docs)", True, f"{len(sobre)} bytes")

            if request.args.get("debug") == "sin-firma":
                from flask import Response
                return Response(
                    sobre, mimetype="application/xml",
                    headers={"Content-Disposition": 'attachment; filename="EnvioDTE_SetExenta_SinFirma_DEBUG.xml"'},
                )

        # ─── 5. Firmar todo el sobre en contexto ───
        if not error_fatal:
            from facturacion.dtes.firma import firmar_envio_completo
            try:
                sobre_firmado = firmar_envio_completo(
                    sobre, cert["pfx_bytes"], cert["password"],
                    set_dte_id=set_id, documento_ids=documento_ids)
                paso("Firmar sobre completo", True, f"{len(sobre_firmado)} bytes")
            except Exception as e_firma:
                import traceback
                err_msg = str(e_firma)
                import re as _re_local
                m_line = _re_local.search(r'line (\d+),?\s*column (\d+)', err_msg)
                contexto_xml = ""
                if m_line:
                    linea = int(m_line.group(1)); col = int(m_line.group(2))
                    txt = sobre.decode("iso-8859-1", errors="replace")
                    lineas = txt.split('\n')
                    if linea <= len(lineas):
                        offset = sum(len(l)+1 for l in lineas[:linea-1]) + col
                        ini = max(0, offset - 150); fin = min(len(txt), offset + 150)
                        contexto_xml = f"... {txt[ini:offset]}[<<AQUÍ>>]{txt[offset:fin]} ..."
                paso("Firmar sobre completo", False,
                     f"{err_msg[:400]}\n\nContexto del XML:\n{contexto_xml[:600]}\n\nTrace: {traceback.format_exc()[:400]}")
                error_fatal = True

        if not error_fatal:
            # Modo descarga: devuelve el XML para subir manual al portal SII
            if descargar == "si":
                from flask import Response
                return Response(
                    sobre_firmado,
                    mimetype="application/xml",
                    headers={"Content-Disposition": 'attachment; filename="EnvioDTE_SetFacturaExenta4829127.xml"'},
                )

        # ─── 6. Envío SOAP (si confirmar=si) ───
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
                    ambiente=ambiente,
                )
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
    titulo = ("¡Set Factura Exenta enviado al SII!" if (todo_ok and track_id) else
              ("Sobre EnvioDTE generado" if (todo_ok and not track_id) else
               "Problema al procesar el Set"))

    filas = ""
    for p in pasos:
        ic = "✅" if p["ok"] else "❌"
        col = "#10b981" if p["ok"] else "#dc2626"
        filas += f"""
        <div style="display:flex;gap:10px;padding:12px 14px;border-bottom:1px solid #f0f0ee;align-items:flex-start;">
          <div style="font-size:16px;">{ic}</div>
          <div style="flex:1;">
            <div style="font-weight:600;color:#1f1e1b;font-size:13px;">{p['nombre']}</div>
            <div style="color:{col};font-size:12px;margin-top:2px;font-family:monospace;word-break:break-word;">{p['detalle']}</div>
          </div>
        </div>"""

    if track_id:
        nota = (f"🎉 Track ID {track_id}. Los 8 documentos del Set Factura Exenta están en el SII. "
                f"Espera el correo de validación.")
    elif todo_ok:
        nota = ("Sobre generado y firmado. Sube el archivo descargado al portal SII manualmente: "
                "<a href='https://maullin.sii.cl/cgi_dte/UPL/DTEauth?1' target='_blank'>maullin.sii.cl/cgi_dte/UPL/DTEauth?1</a>")
    else:
        nota = "Revisa el paso en rojo y reintenta."

    html = f"""<!DOCTYPE html><html lang="es"><head><meta charset="utf-8">
    <title>Set Factura Exenta SII · Lusync</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
      body {{ font-family:-apple-system,'Segoe UI',sans-serif; background:#f6f5f1; margin:0; padding:24px; color:#1f1e1b; }}
      .card {{ max-width:720px; margin:0 auto; background:white; border-radius:14px; overflow:hidden; box-shadow:0 4px 20px rgba(0,0,0,0.06); }}
      .header {{ background:{color}; color:white; padding:24px; }}
      .header h1 {{ margin:0; font-size:18px; }}
      .header p {{ margin:6px 0 0; opacity:0.9; font-size:13px; }}
      .footer {{ padding:16px 24px; background:#fafaf9; font-size:12px; color:#6b7280; }}
      .footer a {{ color:#534AB7; }}
    </style></head><body>
    <div class="card">
      <div class="header">
        <h1>{emoji} {titulo}</h1>
        <p>Set Factura Exenta SII 4829127 · ambiente {ambiente} · tenant {tenant_id}</p>
      </div>
      <div>{filas}</div>
      <div class="footer">{nota}</div>
    </div>
    </body></html>"""
    return html
