# -*- coding: utf-8 -*-
"""
Carga de las boletas emitidas al marketplace de cada orden (o al correo del
cliente donde el canal no recibe documentos).

Endpoints oficiales (revisados el 18/10/2026):
  Falabella     POST /v1/marketplace-sellers/invoice/pdf  (SetInvoicePDF, firma en
                headers, PDF en base64). Exige la orden al menos en ready_to_ship;
                rechaza ordenes Full (FBF), que igual nunca llegan aca.
  MercadoLibre  POST /packs/{pack_id}/fiscal_documents (multipart fiscal_document,
                PDF hasta 1 MB). Sin pack se usa el id de la orden. Rechaza Full.
  Paris         POST /v1/invoice (API Cencosud, multipart: invoice_type,
                order_number = subOrderNumber, invoice_number, file). Paris se la
                envia al cliente.
  Ripley        POST /api/orders/{order_id}/documents (Mirakl OR74, multipart files +
                order_documents con type_code). El type_code se lee de
                GET /api/documents?entities=ORDER (DO01).
  Walmart       Sin endpoint: la boleta va impresa en el paquete.
  Web           Correo al email de la compra (SMTP de Configuracion > Alertas).

No modifica los archivos de integracion: solo usa su autenticacion.
Cada intento queda en boletas_mkt.carga_estado / carga_detalle.
"""

import base64
import json
import logging

import requests

logger = logging.getLogger("lusync")

# Reintentos: el motivo mas comun de un rechazo de Falabella es que la orden
# todavia no esta en ready_to_ship. Se reintenta en el job periodico.
MAX_INTENTOS = 30


def asegurar_columnas(cur):
    for col, tipo in (("carga_estado", "TEXT"), ("carga_detalle", "TEXT"),
                      ("carga_en", "TIMESTAMP"), ("carga_intentos", "INTEGER DEFAULT 0"),
                      ("rut_cliente", "TEXT"), ("factura", "JSONB")):
        cur.execute("ALTER TABLE boletas_mkt ADD COLUMN IF NOT EXISTS %s %s" % (col, tipo))


def pdf_de_dte(tenant_id, dte_id):
    """(pdf_bytes, tipo_dte, folio, fecha_emision 'YYYY-MM-DD')."""
    from inventario import get_conn, release_conn
    from facturacion.db import obtener_config_facturacion
    from facturacion.utils import resolucion_para
    from facturacion.dtes.pdf_dte import generar_pdf_dte
    conn = get_conn(tenant_id=tenant_id, is_admin=True)
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT xml_firmado, tipo_dte, folio, fecha_emision FROM facturacion_dtes
                            WHERE id=%s AND tenant_id=%s""", (dte_id, tenant_id))
            row = cur.fetchone()
        conn.commit()
    finally:
        release_conn(conn)
    if not row or not row[0]:
        raise ValueError("No encontré el documento %s" % dte_id)
    xml = row[0].encode("iso-8859-1", errors="replace") if isinstance(row[0], str) else row[0]
    cfg = obtener_config_facturacion(get_conn, release_conn, tenant_id) or {}
    f_res, n_res = resolucion_para(cfg, row[1])
    anio = None
    try:
        anio = int(str(f_res)[:4]) if f_res else None
    except ValueError:
        pass
    pdf = generar_pdf_dte(xml, formato="carta", url_consulta="lusync.cl/consultadte",
                          datos_tenant={"telefono": cfg.get("telefono"), "correo": cfg.get("email"),
                                        "resolucion_numero": n_res, "resolucion_anio": anio,
                                        "resolucion_fecha": f_res})
    fecha = row[3].strftime("%Y-%m-%d") if row[3] else None
    return pdf, row[1], row[2], fecha


def _tipo_txt(tipo_dte):
    return "FACTURA" if int(tipo_dte) in (33, 34) else "BOLETA"


def _nombre_pdf(tipo_dte, folio):
    return "%s_%s.pdf" % ("Factura" if int(tipo_dte) in (33, 34) else "Boleta", folio)


# ── Falabella ───────────────────────────────────────────────────────────

def cargar_falabella(ref, pdf, tipo_dte, folio, fecha):
    from datetime import datetime, timezone
    from falabella import FALABELLA_BASE_URL, _fal_user_id, generar_firma_falabella
    items = [str(i) for i in (ref.get("order_item_ids") or []) if i]
    if not items:
        return False, "La orden no trae OrderItemIds para cargar el documento"
    params = {"Action": "SetInvoicePDF", "Format": "JSON", "Service": "Invoice",
              "Timestamp": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
              "UserID": _fal_user_id(), "Version": "1.0"}
    headers = {**params, "Signature": generar_firma_falabella(params),
               "Content-Type": "application/json", "Accept": "application/json"}
    body = {"orderItemIds": items, "invoiceNumber": str(folio), "invoiceDate": fecha,
            "invoiceType": _tipo_txt(tipo_dte), "invoiceDocumentFormat": "pdf",
            "invoiceDocument": base64.b64encode(pdf).decode("ascii"), "operatorCode": "FACL"}
    r = requests.post(FALABELLA_BASE_URL.rstrip("/") + "/v1/marketplace-sellers/invoice/pdf",
                      headers=headers, data=json.dumps(body), timeout=60)
    txt = r.text[:600]
    if r.status_code in (200, 201) and "error" not in txt.lower():
        return True, "Cargada en Falabella"
    if "already exists" in txt.lower():
        return True, "Falabella ya la tenía cargada"
    return False, "Falabella respondió %s: %s" % (r.status_code, txt)


# ── MercadoLibre ────────────────────────────────────────────────────────

def cargar_mercadolibre(ref, pdf, tipo_dte, folio, fecha, xml=None):
    from mercadolibre import MELI_API_URL, meli_headers
    pack = ref.get("pack_id") or (ref.get("order_ids") or [None])[0]
    if not pack:
        return False, "Sin pack ni orden para cargar"
    if len(pdf) > 1024 * 1024:
        return False, "El PDF pasa de 1 MB, el máximo de MercadoLibre"
    h = {k: v for k, v in meli_headers().items() if k.lower() != "content-type"}
    r = requests.post("%s/packs/%s/fiscal_documents" % (MELI_API_URL, pack), headers=h,
                      files={"fiscal_document": (_nombre_pdf(tipo_dte, folio), pdf, "application/pdf")},
                      timeout=60)
    if r.status_code in (200, 201):
        return True, "Cargada en MercadoLibre (%s)" % r.text[:120]
    if r.status_code == 409:
        return True, "MercadoLibre ya tenía un documento en ese pack"
    return False, "MercadoLibre respondió %s: %s" % (r.status_code, r.text[:500])


# ── Paris (Cencosud) ────────────────────────────────────────────────────

def cargar_paris(ref, pdf, tipo_dte, folio, fecha):
    from paris import PARIS_BASE_URL, paris_headers
    so = ref.get("sub_order")
    if not so:
        return False, "Sin subOrderNumber"
    h = {k: v for k, v in paris_headers().items() if k.lower() != "content-type"}
    r = requests.post(PARIS_BASE_URL.rstrip("/") + "/v1/invoice", headers=h,
                      data={"invoice_type": "factura" if int(tipo_dte) in (33, 34) else "boleta",
                            "order_number": so, "invoice_number": str(folio)},
                      files={"file": (_nombre_pdf(tipo_dte, folio), pdf, "application/pdf")},
                      timeout=60)
    if r.status_code in (200, 201):
        return True, "Cargada en Paris (Paris la envía al cliente)"
    if "exist" in r.text.lower():
        return True, "Paris ya la tenía cargada"
    return False, "Paris respondió %s: %s" % (r.status_code, r.text[:500])


# ── Ripley (Mirakl) ─────────────────────────────────────────────────────

_TIPOS_RIPLEY = {}


def tipos_documento_ripley():
    """Los tipos de documento de orden que acepta Ripley (DO01). Se cachea en
    memoria: Mirakl pide no llamarlo mas de una vez al dia."""
    from ripley import RIPLEY_BASE_URL, ripley_headers
    if not _TIPOS_RIPLEY:
        r = requests.get(RIPLEY_BASE_URL.rstrip("/") + "/api/documents",
                         headers=ripley_headers(), params={"entities": "ORDER"}, timeout=30)
        r.raise_for_status()
        for d in (r.json() or {}).get("documents") or []:
            _TIPOS_RIPLEY[d.get("code")] = {"label": d.get("label"), "descripcion": d.get("description"),
                                            "mime": d.get("mime_types")}
    return _TIPOS_RIPLEY


def _code_ripley(tipo_dte):
    buscar = ("factura", "invoice") if int(tipo_dte) in (33, 34) else ("boleta",)
    for code, d in tipos_documento_ripley().items():
        txt = " ".join(str(x or "") for x in (code, d.get("label"), d.get("descripcion"))).lower()
        if any(b in txt for b in buscar):
            return code
    return None


def cargar_ripley(ref, pdf, tipo_dte, folio, fecha):
    from ripley import RIPLEY_BASE_URL, ripley_headers
    oid = ref.get("order_id")
    if not oid:
        return False, "Sin order_id"
    code = _code_ripley(tipo_dte)
    if not code:
        return False, ("No encontré en Ripley un tipo de documento para %s (tipos: %s)"
                       % (_tipo_txt(tipo_dte).lower(), ", ".join(tipos_documento_ripley().keys())))
    nombre = _nombre_pdf(tipo_dte, folio)
    h = {k: v for k, v in ripley_headers().items() if k.lower() != "content-type"}
    r = requests.post("%s/api/orders/%s/documents" % (RIPLEY_BASE_URL.rstrip("/"), oid), headers=h,
                      files=[("files", (nombre, pdf, "application/pdf")),
                             ("order_documents", (None, json.dumps(
                                 {"order_documents": [{"file_name": nombre, "type_code": code}]}),
                                 "application/json"))],
                      timeout=60)
    if r.status_code in (200, 201):
        try:
            errores = (r.json() or {}).get("errors_count") or 0
        except ValueError:
            errores = 0
        if not errores:
            return True, "Cargada en Ripley (%s)" % code
    return False, "Ripley respondió %s: %s" % (r.status_code, r.text[:500])


# ── Correo (Web) ────────────────────────────────────────────────────────

def enviar_por_correo(email, pdf, tipo_dte, folio, orden, nombre_tienda=""):
    import smtplib
    from email.mime.application import MIMEApplication
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from inventario import get_alertas_config
    if not email or "@" not in email:
        return False, "La compra no trae correo del cliente"
    cfg = get_alertas_config()
    host, user = cfg.get("smtp_host", "").strip(), cfg.get("smtp_user", "").strip()
    password = cfg.get("smtp_password", "").strip()
    if not host or not user or not password:
        return False, "Falta configurar el correo de salida (Configuración > Alertas)"
    try:
        port = int(cfg.get("smtp_port", "587"))
    except ValueError:
        port = 587
    doc = "factura" if int(tipo_dte) in (33, 34) else "boleta"
    msg = MIMEMultipart()
    msg["Subject"] = "Tu %s electrónica N°%s · pedido %s" % (doc, folio, orden)
    msg["From"] = cfg.get("smtp_from", user).strip() or user
    msg["To"] = email
    msg.attach(MIMEText(
        "Hola,\n\nTe enviamos la %s electrónica N°%s de tu pedido %s.\n\nGracias por tu compra%s.\n"
        % (doc, folio, orden, (" en " + nombre_tienda) if nombre_tienda else ""), "plain", "utf-8"))
    adj = MIMEApplication(pdf, _subtype="pdf")
    adj.add_header("Content-Disposition", "attachment", filename=_nombre_pdf(tipo_dte, folio))
    msg.attach(adj)
    if port == 465:
        srv = smtplib.SMTP_SSL(host, port, timeout=30)
    else:
        srv = smtplib.SMTP(host, port, timeout=30)
        srv.starttls()
    try:
        srv.login(user, password)
        srv.sendmail(msg["From"], [email], msg.as_string())
    finally:
        srv.quit()
    return True, "Enviada a %s" % email


# ── Orquestacion ────────────────────────────────────────────────────────

def cargar_fila(tenant_id, fila_id):
    """Carga (o envia) la boleta de una fila ya emitida. Devuelve (ok, detalle)."""
    from inventario import get_conn, release_conn, now_chile
    conn = get_conn(tenant_id=tenant_id, is_admin=True)
    try:
        with conn.cursor() as cur:
            asegurar_columnas(cur)
            cur.execute("""SELECT canal, orden, ref, dte_id, email, carga_estado, COALESCE(carga_intentos,0)
                             FROM boletas_mkt WHERE tenant_id=%s AND id=%s AND estado='emitida'""",
                        (tenant_id, fila_id))
            r = cur.fetchone()
        conn.commit()
    finally:
        release_conn(conn)
    if not r:
        return False, "La orden no tiene boleta emitida"
    canal, orden, ref, dte_id, email, estado_c, intentos = r
    if estado_c in ("cargada", "no_aplica"):
        return True, "Ya estaba cargada"
    ref = ref or {}
    try:
        if canal == "walmart":
            ok, det, estado = True, "Walmart no recibe documentos por API: va impresa en el paquete", "no_aplica"
        else:
            pdf, tipo, folio, fecha = pdf_de_dte(tenant_id, dte_id)
            fn = {"falabella": cargar_falabella, "mercadolibre": cargar_mercadolibre,
                  "paris": cargar_paris, "ripley": cargar_ripley}.get(canal)
            if fn:
                ok, det = fn(ref, pdf, tipo, folio, fecha)
            else:
                ok, det = enviar_por_correo(email, pdf, tipo, folio, orden)
            estado = "cargada" if ok else "error"
    except Exception as e:  # noqa: BLE001
        ok, det, estado = False, "Error: %s" % str(e)[:300], "error"
    conn = get_conn(tenant_id=tenant_id, is_admin=True)
    try:
        with conn.cursor() as cur:
            cur.execute("""UPDATE boletas_mkt SET carga_estado=%s, carga_detalle=%s, carga_en=%s,
                                  carga_intentos=COALESCE(carga_intentos,0)+1
                            WHERE tenant_id=%s AND id=%s""",
                        (estado, det[:500], now_chile().replace(tzinfo=None), tenant_id, fila_id))
        conn.commit()
    finally:
        release_conn(conn)
    return ok, det


def cargar_pendientes(tenant_id, limite=40):
    """Carga las boletas emitidas que aun no estan en el marketplace."""
    from inventario import get_conn, release_conn
    conn = get_conn(tenant_id=tenant_id, is_admin=True)
    try:
        with conn.cursor() as cur:
            asegurar_columnas(cur)
            cur.execute("""SELECT id FROM boletas_mkt
                            WHERE tenant_id=%s AND estado='emitida' AND dte_id IS NOT NULL
                              AND COALESCE(carga_estado,'pendiente') IN ('pendiente','error')
                              AND COALESCE(carga_intentos,0) < %s
                            ORDER BY emitida_en LIMIT %s""", (tenant_id, MAX_INTENTOS, limite))
            ids = [x[0] for x in cur.fetchall()]
        conn.commit()
    finally:
        release_conn(conn)
    return [(i,) + cargar_fila(tenant_id, i) for i in ids]
