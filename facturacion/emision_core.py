# -*- coding: utf-8 -*-
"""
Lógica central de emisión de boletas/facturas electrónicas, extraída del
endpoint HTTP para poder reutilizarla desde:
  - El endpoint web (emisión manual desde el panel).
  - La emisión automática por ventas de marketplace (scheduler, sin sesión web).

La función `emitir_boleta_core` es PURA respecto a Flask: no lee `session` ni
`request`. Todo entra por parámetros. Así el mismo motor que ya funciona en
producción sirve para ambos flujos, sin duplicar código ni arriesgar
divergencias de comportamiento.
"""

from datetime import datetime


def emitir_boleta_core(tenant_id, items, receptor=None, ambiente=None,
                       referencias=None, actualizar_estado_fn=None):
    """Emite UNA boleta/factura electrónica y la envía al SII.

    Flujo profesional (guardar-antes-de-enviar), idéntico al del endpoint web:
      1. Reservar folio (atómico)
      2. Generar + firmar
      3. REGISTRAR en BD con estado 'generado' (ANTES de enviar)
      4. Enviar al SII
      5. Actualizar estado: 'enviado' (con track_id) o 'error_envio'

    Args:
        tenant_id: id del tenant emisor.
        items: lista de dicts con al menos {nombre, precio_unitario, cantidad, exento?}.
        receptor: dict {rut, razon_social} o None (usa Consumidor Final).
        ambiente: 'produccion'|'certificacion' o None (usa el de la config).
        referencias: lista de referencias del DTE o None.
        actualizar_estado_fn: callback para actualizar estado del DTE en BD
            (se pasa la misma _fact_actualizar_estado_dte del app para no duplicarla).

    Returns:
        dict: {ok, folio, tipo_dte, track_id, total, boleta_id, pdf_url, pasos, error?}
        Nunca lanza excepción hacia afuera: cualquier fallo se devuelve en el dict.
    """
    from inventario import get_conn, release_conn
    from facturacion.certificados import obtener_certificado
    from facturacion.db import obtener_config_facturacion
    from facturacion.cafs import obtener_folio_disponible
    from facturacion.utils import normalizar_ambiente

    pasos = []
    def paso(nombre, ok, detalle=""):
        pasos.append({"nombre": nombre, "ok": ok, "detalle": detalle})

    # 0. Validar items
    items = items or []
    if not items:
        return {"ok": False, "error": "Debes agregar al menos un ítem", "pasos": pasos}
    for it in items:
        if not it.get("nombre") or not it.get("precio_unitario"):
            return {"ok": False, "error": "Cada ítem necesita nombre y precio", "pasos": pasos}

    # 1. Config del tenant
    config = obtener_config_facturacion(get_conn, release_conn, tenant_id)
    if not config:
        return {"ok": False, "error": "No hay configuración de facturación para este tenant",
                "pasos": pasos}

    ambiente = normalizar_ambiente(ambiente or config.get("ambiente") or "certificacion")
    todos_exentos = all(it.get("exento") for it in items)
    tipo_dte = 41 if todos_exentos else 39

    emisor = {
        "rut": config["rut_emisor"], "razon_social": config["razon_social"],
        "giro": config.get("giro", "Venta al por menor"),
        "dir_origen": config.get("direccion", "Sin dirección"),
        "cmna_origen": config.get("comuna", "Santiago"),
        "telefono": config.get("telefono"),
        "correo": config.get("email"),
    }

    nro_resol = config.get("resolucion_sii_numero")
    fch_resol = config.get("resolucion_sii_fecha")
    if fch_resol and not isinstance(fch_resol, str):
        try:
            fch_resol = fch_resol.isoformat()
        except Exception:
            fch_resol = str(fch_resol)
    if nro_resol is None:
        nro_resol = 0
    if not fch_resol:
        fch_resol = "2014-08-22"

    # 2. Certificado .pfx
    cert = obtener_certificado(get_conn, release_conn, tenant_id)
    if not cert.get("ok"):
        return {"ok": False, "error": "Certificado: " + str(cert.get("error", "no disponible")),
                "pasos": pasos}
    paso("Leer certificado .pfx", True, cert["metadata"].get("titular", "?"))
    rut_envia = cert["metadata"].get("rut", emisor["rut"])

    # Receptor: genérico si no viene
    receptor = receptor or {"rut": "66666666-6", "razon_social": "Consumidor Final"}
    if not receptor.get("rut"):
        receptor["rut"] = "66666666-6"
    if not receptor.get("razon_social"):
        receptor["razon_social"] = "Consumidor Final"

    # 3. Reservar folio (ATÓMICO)
    folio_res = obtener_folio_disponible(get_conn, release_conn, tenant_id, tipo_dte, ambiente)
    if not folio_res.get("ok"):
        return {"ok": False, "error": folio_res.get("error"), "pasos": pasos}
    folio = folio_res["folio"]
    paso("Reservar folio", True, "Folio " + str(folio) + " (tipo " + str(tipo_dte) + ", " + ambiente + ")")

    track_id = None
    boleta_id = None
    total = 0

    try:
        from facturacion.dtes.caf_parser import parsear_caf_xml
        from facturacion.dtes.boleta import generar_boleta_xml
        from facturacion.dtes.envio_boleta import armar_envio_boleta
        from facturacion.dtes.firma import firmar_envio_completo
        from facturacion.dtes.sii_client import autenticar, enviar_boletas

        caf = parsear_caf_xml(folio_res["xml_caf"])
        # Fecha de emisión en hora de Chile (no UTC del servidor)
        try:
            from zoneinfo import ZoneInfo
            fecha = datetime.now(ZoneInfo("America/Santiago")).strftime("%Y-%m-%d")
        except Exception:
            from datetime import timezone as _tz, timedelta as _td
            fecha = (datetime.now(_tz.utc) - _td(hours=4)).strftime("%Y-%m-%d")

        # 4. Generar la boleta
        referencias = referencias or []
        if not isinstance(referencias, list):
            referencias = []
        res_bol = generar_boleta_xml(
            caf=caf, folio=folio, fecha_emision=fecha,
            emisor=emisor, items=items, receptor=receptor,
            referencias=referencias if referencias else None,
        )
        boleta_xml = res_bol["xml"]
        total = res_bol["totales"]["mnt_total"]
        documento_id = res_bol["documento_id"]
        paso("Generar boleta", True, ("$" + format(total, ",")).replace(",", "."))

        # 5. Armar sobre + firmar
        set_id = "SetDoc"
        sobre = armar_envio_boleta(
            dtes_firmados=[boleta_xml], rut_emisor=emisor["rut"],
            rut_envia=rut_envia, fch_resol=fch_resol, nro_resol=nro_resol,
            tipo_dte=tipo_dte, set_dte_id=set_id,
        )
        sobre_firmado = firmar_envio_completo(
            sobre, cert["pfx_bytes"], cert["password"],
            set_dte_id=set_id, documento_ids=[documento_id])
        paso("Firmar sobre", True, str(len(sobre_firmado)) + " bytes")

        # 6. REGISTRAR EN BD ANTES DE ENVIAR (estado 'generado')
        conn = get_conn(tenant_id=tenant_id) if _acepta_tenant(get_conn) else get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO facturacion_dtes
                      (tenant_id, tipo_dte, folio, rut_receptor, razon_social_receptor,
                       monto_neto, monto_iva, monto_total, xml_firmado, estado,
                       fecha_emision)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    RETURNING id
                """, (tenant_id, tipo_dte, folio, receptor["rut"], receptor["razon_social"],
                      res_bol["totales"]["mnt_neto"], res_bol["totales"]["mnt_iva"],
                      total, boleta_xml.decode("iso-8859-1", errors="replace"), "generado",
                      fecha))
                boleta_id = cur.fetchone()[0]
            conn.commit()
            paso("Registrar boleta (pre-envío)", True, "ID " + str(boleta_id) + " · estado: generado")
        except Exception as e:
            conn.rollback()
            release_conn(conn)
            paso("Registrar boleta (pre-envío)", False, str(e)[:200])
            return {"ok": False, "folio": folio,
                    "error": "No se pudo registrar la boleta antes de enviar: " + str(e)[:200],
                    "pasos": pasos}
        finally:
            try:
                release_conn(conn)
            except Exception:
                pass

        # 7. Autenticar + enviar al SII
        try:
            tok = autenticar(cert["pfx_bytes"], cert["password"], ambiente)
            resultado = enviar_boletas(
                envio_xml=sobre_firmado, token=tok,
                rut_emisor=emisor["rut"], rut_envia=rut_envia, ambiente=ambiente)
        except Exception as e:
            if actualizar_estado_fn:
                actualizar_estado_fn(boleta_id, "error_envio", glosa=str(e)[:300])
            paso("Enviar al SII", False, str(e)[:200])
            return {"ok": False, "boleta_id": boleta_id, "folio": folio,
                    "error": "No se pudo conectar con el SII. La boleta quedó guardada para reintentar.",
                    "reintentable": True, "pasos": pasos}

        if not resultado.get("ok"):
            detalle = resultado.get("error") or str(resultado.get("respuesta_cruda", ""))[:300]
            if actualizar_estado_fn:
                actualizar_estado_fn(boleta_id, "error_envio", glosa=detalle)
            paso("Enviar al SII", False, detalle)
            return {"ok": False, "boleta_id": boleta_id, "folio": folio,
                    "error": "El SII rechazó el envío: " + str(detalle),
                    "reintentable": True, "pasos": pasos}

        track_id = resultado["track_id"]
        # 8. Actualizar a 'enviado' con track_id
        if actualizar_estado_fn:
            actualizar_estado_fn(boleta_id, "enviado", track_id=track_id,
                                 estado_sii=resultado.get("estado"), set_fecha_envio=True)
        paso("Enviar al SII", True, "Track ID: " + str(track_id))

    except Exception as e:
        import traceback
        paso("Error", False, traceback.format_exc()[:400])
        if boleta_id and actualizar_estado_fn:
            try:
                actualizar_estado_fn(boleta_id, "error_envio", glosa=str(e)[:300])
            except Exception:
                pass
        return {"ok": False, "error": str(e)[:300], "folio": folio,
                "boleta_id": boleta_id, "pasos": pasos}

    return {
        "ok": True, "folio": folio, "tipo_dte": tipo_dte, "track_id": track_id,
        "total": total, "boleta_id": boleta_id,
        "pdf_url": ("/facturacion/boleta/" + str(boleta_id) + "/pdf") if boleta_id else None,
        "pasos": pasos,
    }


def _acepta_tenant(get_conn_func):
    """Detecta si get_conn acepta el parámetro tenant_id (para contexto RLS)."""
    try:
        import inspect
        return "tenant_id" in inspect.signature(get_conn_func).parameters
    except Exception:
        return False
