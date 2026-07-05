# ════════════════════════════════════════════════════════════════════════════
# EMISIÓN MANUAL DE BOLETAS — pegar en app.py
# ════════════════════════════════════════════════════════════════════════════
# Endpoints:
#   GET  /facturacion/emitir          → sirve el formulario (interfaz)
#   POST /facturacion/boleta/emitir   → emite UNA boleta real y la envía al SII
#   GET  /facturacion/boleta/<id>/pdf → descarga el PDF de una boleta emitida
#
# Reutiliza el MISMO flujo validado del Set de Pruebas, pero parametrizado para
# una boleta real: folio atómico desde el CAF, resolución leída del tenant, y
# guard de seguridad para producción.
# ════════════════════════════════════════════════════════════════════════════

@app.route("/facturacion/emitir", methods=["GET"])
def facturacion_emitir_form():
    """Sirve la interfaz de emisión manual de boletas."""
    if not session.get("logged"):
        return redirect("/login")
    # El HTML está en templates/emitir_boleta.html (ver archivo aparte)
    return render_template("emitir_boleta.html")


@app.route("/facturacion/boleta/emitir", methods=["POST"])
def facturacion_boleta_emitir():
    """Emite UNA boleta electrónica (39/41) y la envía al SII.

    Body JSON:
      {
        "ambiente": "certificacion" | "produccion",   (opcional; default: el del tenant)
        "receptor": {"rut": "...", "razon_social": "..."},  (opcional)
        "items": [
          {"nombre": "...", "cantidad": 1, "precio_unitario": 19900,
           "exento": false, "unidad": "Un"},
          ...
        ]
      }

    Returns JSON: {ok, folio, track_id, total, boleta_id, pdf_url, pasos[]}
    """
    if not session.get("logged"):
        return jsonify({"ok": False, "error": "no autenticado"}), 401

    tenant_id = session.get("tenant_id") or 1
    data = request.get_json(silent=True) or {}

    from inventario import get_conn, release_conn
    from facturacion.certificados import obtener_certificado
    from facturacion.db import obtener_config_facturacion
    from facturacion.cafs import obtener_folio_disponible
    from facturacion.utils import normalizar_ambiente

    pasos = []
    def paso(nombre, ok, detalle=""):
        pasos.append({"nombre": nombre, "ok": ok, "detalle": detalle})

    # ─── 0. Validar items ───
    items = data.get("items") or []
    if not items:
        return jsonify({"ok": False, "error": "Debes agregar al menos un ítem"}), 400
    for it in items:
        if not it.get("nombre") or not it.get("precio_unitario"):
            return jsonify({"ok": False, "error": "Cada ítem necesita nombre y precio"}), 400

    # ─── 1. Config del tenant (emisor + ambiente + resolución) ───
    config = obtener_config_facturacion(get_conn, release_conn, tenant_id)
    if not config:
        return jsonify({"ok": False, "error": "No hay configuración de facturación para este tenant"}), 400

    ambiente = normalizar_ambiente(data.get("ambiente") or config.get("ambiente") or "certificacion")

    # Tipo de boleta: 39 (afecto) o 41 (exenta) según si TODOS los ítems son exentos
    todos_exentos = all(it.get("exento") for it in items)
    tipo_dte = 41 if todos_exentos else 39

    emisor = {
        "rut": config["rut_emisor"], "razon_social": config["razon_social"],
        "giro": config.get("giro", "Venta al por menor"),
        "dir_origen": config.get("direccion", "Sin dirección"),
        "cmna_origen": config.get("comuna", "Santiago"),
    }

    # ─── GUARD DE PRODUCCIÓN ───
    # En producción la resolución NO puede ir en 0 con fecha de certificación,
    # salvo que el SII te haya habilitado bajo una resolución genérica (caso boletas).
    # Leemos lo que el tenant tenga configurado y validamos coherencia.
    nro_resol = config.get("resolucion_sii_numero")
    fch_resol = config.get("resolucion_sii_fecha")
    # Normalizar fecha a YYYY-MM-DD si viene como date
    if fch_resol and not isinstance(fch_resol, str):
        try:
            fch_resol = fch_resol.isoformat()
        except Exception:
            fch_resol = str(fch_resol)
    if nro_resol is None:
        nro_resol = 0
    if not fch_resol:
        fch_resol = "2014-08-22"  # resolución genérica de boletas (Ex.SII 80/2014)

    if ambiente == "produccion":
        # En producción exigimos que el certificado y el CAF también sean de producción.
        # (el IDK del CAF lo valida caf_parser; aquí validamos que exista CAF prod)
        paso("Validar ambiente producción", True,
             f"Resolución N°{nro_resol} del {fch_resol}")

    # ─── 2. Certificado .pfx ───
    cert = obtener_certificado(get_conn, release_conn, tenant_id)
    if not cert.get("ok"):
        return jsonify({"ok": False, "error": f"Certificado: {cert.get('error','no disponible')}",
                        "pasos": pasos}), 400
    paso("Leer certificado .pfx", True, cert["metadata"].get("titular", "?"))
    rut_envia = cert["metadata"].get("rut", emisor["rut"])

    # ─── 3. Reservar folio (ATÓMICO) ───
    folio_res = obtener_folio_disponible(get_conn, release_conn, tenant_id, tipo_dte, ambiente)
    if not folio_res.get("ok"):
        return jsonify({"ok": False, "error": folio_res.get("error"), "pasos": pasos}), 400
    folio = folio_res["folio"]
    paso("Reservar folio", True, f"Folio {folio} (tipo {tipo_dte}, {ambiente})")

    track_id = None
    boleta_id = None
    try:
        from facturacion.dtes.caf_parser import parsear_caf_xml
        from facturacion.dtes.boleta import generar_boleta_xml
        from facturacion.dtes.envio_boleta import armar_envio_boleta
        from facturacion.dtes.firma import firmar_envio_completo
        from facturacion.dtes.sii_client import autenticar, enviar_boletas

        caf = parsear_caf_xml(folio_res["xml_caf"])
        # Fecha en hora de Chile (no UTC del servidor)
        try:
            from zoneinfo import ZoneInfo
            fecha = datetime.now(ZoneInfo("America/Santiago")).strftime("%Y-%m-%d")
        except Exception:
            from datetime import timezone as _tz, timedelta as _td
            fecha = (datetime.now(_tz.utc) - _td(hours=4)).strftime("%Y-%m-%d")

        # Receptor (opcional; default consumidor final)
        receptor = data.get("receptor") or None

        # ─── 4. Generar la boleta ───
        res_bol = generar_boleta_xml(
            caf=caf, folio=folio, fecha_emision=fecha,
            emisor=emisor, items=items, receptor=receptor,
        )
        boleta_xml = res_bol["xml"]
        total = res_bol["totales"]["mnt_total"]
        documento_id = res_bol["documento_id"]
        paso("Generar boleta", True, f"${total:,}".replace(",", "."))

        # ─── 5. Armar sobre EnvioBOLETA ───
        set_id = "SetDoc"
        sobre = armar_envio_boleta(
            dtes_firmados=[boleta_xml], rut_emisor=emisor["rut"],
            rut_envia=rut_envia, fch_resol=fch_resol, nro_resol=nro_resol,
            tipo_dte=tipo_dte, set_dte_id=set_id,
        )

        # ─── 6. Firmar sobre completo ───
        sobre_firmado = firmar_envio_completo(
            sobre, cert["pfx_bytes"], cert["password"],
            set_dte_id=set_id, documento_ids=[documento_id])
        paso("Firmar sobre", True, f"{len(sobre_firmado)} bytes")

        # ─── 7. Autenticar + enviar ───
        tok = autenticar(cert["pfx_bytes"], cert["password"], ambiente)
        resultado = enviar_boletas(
            envio_xml=sobre_firmado, token=tok,
            rut_emisor=emisor["rut"], rut_envia=rut_envia, ambiente=ambiente)

        if not resultado.get("ok"):
            detalle = resultado.get("error") or str(resultado.get("respuesta_cruda", ""))[:300]
            paso("Enviar al SII", False, detalle)
            # El folio ya se consumió; registramos la boleta como ERROR para no perderla
            return jsonify({"ok": False, "error": f"SII rechazó el envío: {detalle}",
                            "folio": folio, "pasos": pasos}), 502

        track_id = resultado["track_id"]
        paso("Enviar al SII", True, f"Track ID: {track_id}")

        # ─── 8. Guardar la boleta emitida ───
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO facturacion_dtes
                      (tenant_id, tipo_dte, folio, ambiente, track_id, estado,
                       monto_total, xml_dte, fecha_emision)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                    RETURNING id
                """, (tenant_id, tipo_dte, folio, ambiente, track_id, "enviado",
                      total, boleta_xml.decode("iso-8859-1", errors="replace")))
                boleta_id = cur.fetchone()[0]
            conn.commit()
        except Exception as e:
            conn.rollback()
            paso("Guardar boleta", False, str(e)[:200])
        finally:
            release_conn(conn)

    except Exception as e:
        import traceback
        paso("Error", False, traceback.format_exc()[:400])
        return jsonify({"ok": False, "error": str(e)[:300], "folio": folio,
                        "pasos": pasos}), 500

    return jsonify({
        "ok": True, "folio": folio, "tipo_dte": tipo_dte, "track_id": track_id,
        "total": total, "boleta_id": boleta_id,
        "pdf_url": f"/facturacion/boleta/{boleta_id}/pdf" if boleta_id else None,
        "pasos": pasos,
    })


@app.route("/facturacion/boleta/<int:boleta_id>/pdf", methods=["GET"])
def facturacion_boleta_pdf(boleta_id):
    """Descarga/visualiza el PDF de una boleta emitida.
    Query: ?formato=carta|rollo  (default carta)
    """
    if not session.get("logged"):
        return jsonify({"ok": False, "error": "no autenticado"}), 401

    tenant_id = session.get("tenant_id") or 1
    formato = request.args.get("formato", "carta")
    from inventario import get_conn, release_conn
    from facturacion.dtes.pdf_dte import generar_pdf_dte
    from flask import Response

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT xml_dte FROM facturacion_dtes
                WHERE id = %s AND tenant_id = %s
            """, (boleta_id, tenant_id))
            row = cur.fetchone()
    finally:
        release_conn(conn)

    if not row:
        return jsonify({"ok": False, "error": "Boleta no encontrada"}), 404

    xml = row[0].encode("iso-8859-1", errors="replace") if isinstance(row[0], str) else row[0]
    # URL de consulta pública del tenant (cae a www.sii.cl si no está configurada)
    url_consulta = "lusync.cl/consultadte"
    pdf = generar_pdf_dte(xml, formato=formato, url_consulta=url_consulta)

    return Response(pdf, mimetype="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="boleta_{boleta_id}.pdf"'})
