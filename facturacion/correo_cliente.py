# -*- coding: utf-8 -*-
"""
Correo al cliente final con su documento tributario (boleta o factura).

Decision del usuario (21/10/2026): los correos salen desde el dominio de
Lusync, para dar a conocer Lusync. El cliente ve el NOMBRE y el LOGO de la
tienda como remitente ("Babymine <documentos@lusync.cl>"), las respuestas
le llegan a la tienda (Reply-To) y el pie dice "Enviado con Lusync".

Cada tenant edita su plantilla en Configuracion > Correo al cliente: logo,
nombre de la tienda, asunto, mensaje con variables, firma, correo para
respuestas y en que canales se envia (hoy Walmart y Web: los demas
marketplaces reciben el documento por API).

Envio:
  - Con RESEND_API_KEY en el entorno: por Resend, desde CORREO_REMITENTE
    (default documentos@lusync.cl). El dominio lusync.cl tiene que estar
    verificado en Resend (registros DNS SPF/DKIM).
  - Sin eso: por el SMTP que el tenant configuro en Alertas (para probar).
"""

import base64
import html
import json
import logging
import os
import re
import secrets

import requests

logger = logging.getLogger("lusync")

VARIABLES = ("cliente", "documento", "folio", "orden", "canal", "total", "tienda")
CANALES_CORREO = ("walmart", "web")
LOGO_MAX_BYTES = 300 * 1024

POR_DEFECTO = {
    "asunto": "Tu {documento} N°{folio} de tu compra {orden}",
    "mensaje": ("Hola {cliente},\n\n¡Gracias por tu compra en {canal}! Te adjuntamos la "
                "{documento} electrónica N°{folio} por {total}.\n\n"
                "Si tienes cualquier duda, responde este correo y te ayudamos."),
    "firma": "",
    "canales": {"walmart": True, "web": True},
}


def _remitente_lusync():
    return os.environ.get("CORREO_REMITENTE") or "documentos@lusync.cl"


def _url_publica():
    return (os.environ.get("LUSYNC_URL_PUBLICA") or "https://lusync.cl").rstrip("/")


def asegurar_tabla(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS correo_cliente_config (
            tenant_id     INTEGER PRIMARY KEY,
            nombre_tienda TEXT,
            asunto        TEXT,
            mensaje       TEXT,
            firma         TEXT,
            reply_to      TEXT,
            canales       JSONB,
            logo          BYTEA,
            logo_mime     TEXT,
            logo_token    TEXT UNIQUE,
            actualizado   TIMESTAMP
        )""")


def _nombre_tenant(cur, tenant_id):
    try:
        cur.execute("SELECT nombre FROM tenants WHERE id = %s", (tenant_id,))
        r = cur.fetchone()
        return (r[0] or "").strip() if r else ""
    except Exception:  # noqa: BLE001
        return ""


def leer_config(tenant_id):
    from inventario import get_conn, release_conn
    conn = get_conn(tenant_id=tenant_id, is_admin=True)
    try:
        with conn.cursor() as cur:
            asegurar_tabla(cur)
            cur.execute("""SELECT nombre_tienda, asunto, mensaje, firma, reply_to, canales,
                                  logo IS NOT NULL, logo_token, actualizado
                             FROM correo_cliente_config WHERE tenant_id = %s""", (tenant_id,))
            r = cur.fetchone()
            nombre_t = _nombre_tenant(cur, tenant_id)
        conn.commit()
    finally:
        release_conn(conn)
    cfg = {"nombre_tienda": nombre_t, "reply_to": "", "tiene_logo": False, "logo_url": None,
           "remitente": _remitente_lusync(), "via": "resend" if os.environ.get("RESEND_API_KEY") else "smtp",
           **POR_DEFECTO}
    if r:
        cfg.update({k: v for k, v in {
            "nombre_tienda": r[0], "asunto": r[1], "mensaje": r[2], "firma": r[3],
            "reply_to": r[4], "canales": r[5]}.items() if v not in (None, "")})
        cfg["tiene_logo"] = bool(r[6])
        if r[6] and r[7]:
            cfg["logo_url"] = "%s/correo/logo/%s" % (_url_publica(), r[7])
        cfg["actualizado"] = r[8].strftime("%d/%m/%Y %H:%M") if r[8] else None
    return cfg


def guardar_config(tenant_id, datos):
    from inventario import get_conn, release_conn, now_chile
    canales = datos.get("canales") or {}
    canales = {c: bool(canales.get(c)) for c in CANALES_CORREO}
    valores = {
        "nombre_tienda": str(datos.get("nombre_tienda") or "").strip()[:80],
        "asunto": str(datos.get("asunto") or "").strip()[:150] or POR_DEFECTO["asunto"],
        "mensaje": str(datos.get("mensaje") or "").strip()[:3000] or POR_DEFECTO["mensaje"],
        "firma": str(datos.get("firma") or "").strip()[:200],
        "reply_to": str(datos.get("reply_to") or "").strip()[:120],
    }
    if valores["reply_to"] and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", valores["reply_to"]):
        return {"ok": False, "error": "El correo para respuestas no es válido"}
    conn = get_conn(tenant_id=tenant_id, is_admin=True)
    try:
        with conn.cursor() as cur:
            asegurar_tabla(cur)
            cur.execute("""
                INSERT INTO correo_cliente_config (tenant_id, nombre_tienda, asunto, mensaje, firma,
                                                   reply_to, canales, actualizado)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (tenant_id) DO UPDATE SET nombre_tienda=EXCLUDED.nombre_tienda,
                    asunto=EXCLUDED.asunto, mensaje=EXCLUDED.mensaje, firma=EXCLUDED.firma,
                    reply_to=EXCLUDED.reply_to, canales=EXCLUDED.canales, actualizado=EXCLUDED.actualizado
            """, (tenant_id, valores["nombre_tienda"], valores["asunto"], valores["mensaje"],
                  valores["firma"], valores["reply_to"], json.dumps(canales),
                  now_chile().replace(tzinfo=None)))
        conn.commit()
    finally:
        release_conn(conn)
    return {"ok": True}


def guardar_logo(tenant_id, contenido, mime):
    from inventario import get_conn, release_conn, now_chile
    if mime not in ("image/png", "image/jpeg", "image/jpg"):
        return {"ok": False, "error": "El logo tiene que ser PNG o JPG"}
    if len(contenido) > LOGO_MAX_BYTES:
        return {"ok": False, "error": "El logo pesa %s KB; el máximo es 300 KB" % (len(contenido) // 1024)}
    conn = get_conn(tenant_id=tenant_id, is_admin=True)
    try:
        with conn.cursor() as cur:
            asegurar_tabla(cur)
            # Token nuevo en cada cambio: los clientes de correo guardan la imagen en cache
            token = secrets.token_urlsafe(18)
            cur.execute("""
                INSERT INTO correo_cliente_config (tenant_id, logo, logo_mime, logo_token, actualizado)
                VALUES (%s,%s,%s,%s,%s)
                ON CONFLICT (tenant_id) DO UPDATE SET logo=EXCLUDED.logo, logo_mime=EXCLUDED.logo_mime,
                    logo_token=EXCLUDED.logo_token, actualizado=EXCLUDED.actualizado
            """, (tenant_id, contenido, "image/jpeg" if mime == "image/jpg" else mime, token,
                  now_chile().replace(tzinfo=None)))
        conn.commit()
    finally:
        release_conn(conn)
    return {"ok": True, "logo_url": "%s/correo/logo/%s" % (_url_publica(), token)}


def logo_por_token(token):
    """Para la ruta publica del logo (se ve en el correo del cliente)."""
    from inventario import get_conn, release_conn
    conn = get_conn(is_admin=True)
    try:
        with conn.cursor() as cur:
            asegurar_tabla(cur)
            cur.execute("SELECT logo, logo_mime FROM correo_cliente_config WHERE logo_token = %s",
                        (token,))
            r = cur.fetchone()
        conn.commit()
    finally:
        release_conn(conn)
    return (bytes(r[0]), r[1]) if r and r[0] else (None, None)


def _reemplazar(texto, valores):
    def _sub(m):
        return str(valores.get(m.group(1), m.group(0)))
    return re.sub(r"\{(%s)\}" % "|".join(VARIABLES), _sub, texto or "")


def armar_correo(cfg, valores):
    """(asunto, html, texto). valores: las variables ya formateadas."""
    asunto = _reemplazar(cfg.get("asunto"), valores)
    cuerpo = _reemplazar(cfg.get("mensaje"), valores)
    tienda = html.escape(cfg.get("nombre_tienda") or valores.get("tienda") or "")
    parrafos = "".join('<p style="margin:0 0 14px">%s</p>' % html.escape(p).replace("\n", "<br>")
                       for p in re.split(r"\n\s*\n", cuerpo) if p.strip())
    logo = ('<img src="%s" alt="%s" style="max-height:56px;max-width:180px;display:block">'
            % (html.escape(cfg["logo_url"]), tienda)) if cfg.get("logo_url") else \
           '<div style="font-size:20px;font-weight:600;color:#1a1a2e">%s</div>' % tienda
    firma = ('<p style="margin:18px 0 0;color:#666;font-size:13px">%s</p>'
             % html.escape(cfg["firma"])) if cfg.get("firma") else ""
    cuerpo_html = """<!doctype html><html><body style="margin:0;background:#f4f4f7;font-family:Arial,Helvetica,sans-serif;color:#222">
<table role="presentation" width="100%%" cellpadding="0" cellspacing="0" style="background:#f4f4f7;padding:24px 12px">
<tr><td align="center">
<table role="presentation" width="100%%" cellpadding="0" cellspacing="0" style="max-width:560px;background:#ffffff;border-radius:12px;overflow:hidden">
<tr><td style="padding:24px 28px 8px">%s</td></tr>
<tr><td style="padding:12px 28px 4px;font-size:15px;line-height:1.55">%s%s</td></tr>
<tr><td style="padding:14px 28px 24px;font-size:12px;color:#888">Adjuntamos tu documento en PDF.</td></tr>
</table>
<table role="presentation" width="100%%" cellpadding="0" cellspacing="0" style="max-width:560px">
<tr><td align="center" style="padding:16px 12px;font-size:12px;color:#888">
Enviado con <a href="https://lusync.cl" style="color:#534AB7;text-decoration:none;font-weight:600">Lusync</a>
· inventario, ventas y facturación para tiendas online</td></tr>
</table>
</td></tr></table></body></html>""" % (logo, parrafos, firma)
    texto = cuerpo + ("\n\n" + cfg["firma"] if cfg.get("firma") else "") + \
        "\n\n--\nEnviado con Lusync · https://lusync.cl"
    return asunto, cuerpo_html, texto


def _pesos(n):
    try:
        return "$" + format(int(round(float(n))), ",").replace(",", ".")
    except (TypeError, ValueError):
        return ""


def valores_de(cfg, cliente, tipo_dte, folio, orden, canal, total):
    nombres = {"walmart": "Walmart", "web": "nuestra tienda", "falabella": "Falabella",
               "mercadolibre": "Mercado Libre", "paris": "Paris", "ripley": "Ripley"}
    return {"cliente": (cliente or "").split(" ")[0] or "cliente",
            "documento": "factura" if int(tipo_dte or 39) in (33, 34) else "boleta",
            "folio": folio, "orden": orden, "canal": nombres.get(canal, canal or ""),
            "total": _pesos(total), "tienda": cfg.get("nombre_tienda") or ""}


def enviar(tenant_id, destino, asunto, cuerpo_html, texto, adjunto=None, nombre_adjunto=None):
    """Envia un correo con el remitente de Lusync y el nombre de la tienda."""
    cfg = leer_config(tenant_id)
    nombre = (cfg.get("nombre_tienda") or "Lusync").replace('"', "")
    reply_to = cfg.get("reply_to") or None
    clave = os.environ.get("RESEND_API_KEY")
    if clave:
        cuerpo = {"from": '"%s" <%s>' % (nombre, _remitente_lusync()), "to": [destino],
                  "subject": asunto, "html": cuerpo_html, "text": texto}
        if reply_to:
            cuerpo["reply_to"] = reply_to
        if adjunto:
            cuerpo["attachments"] = [{"filename": nombre_adjunto or "documento.pdf",
                                      "content": base64.b64encode(adjunto).decode("ascii")}]
        r = requests.post("https://api.resend.com/emails", json=cuerpo, timeout=30,
                          headers={"Authorization": "Bearer " + clave})
        if r.status_code in (200, 201):
            return True, "Enviado a %s" % destino
        return False, "El servicio de correo respondió %s: %s" % (r.status_code, r.text[:300])

    # Sin Resend: SMTP de Alertas (para probar)
    import smtplib
    from email.mime.application import MIMEApplication
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.utils import formataddr
    from inventario import get_alertas_config
    smtp = get_alertas_config()
    host, user = smtp.get("smtp_host", "").strip(), smtp.get("smtp_user", "").strip()
    password = smtp.get("smtp_password", "").strip()
    if not host or not user or not password:
        return False, ("Falta conectar el envío de correos: configura Resend para lusync.cl "
                       "o el correo de salida en Configuración > Alertas")
    try:
        port = int(smtp.get("smtp_port", "587"))
    except ValueError:
        port = 587
    msg = MIMEMultipart("mixed")
    msg["Subject"] = asunto
    msg["From"] = formataddr((nombre, smtp.get("smtp_from", user).strip() or user))
    msg["To"] = destino
    if reply_to:
        msg["Reply-To"] = reply_to
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(texto, "plain", "utf-8"))
    alt.attach(MIMEText(cuerpo_html, "html", "utf-8"))
    msg.attach(alt)
    if adjunto:
        a = MIMEApplication(adjunto, _subtype="pdf")
        a.add_header("Content-Disposition", "attachment", filename=nombre_adjunto or "documento.pdf")
        msg.attach(a)
    srv = smtplib.SMTP_SSL(host, port, timeout=30) if port == 465 else smtplib.SMTP(host, port, timeout=30)
    try:
        if port != 465:
            srv.starttls()
        srv.login(user, password)
        srv.sendmail(msg["From"], [destino], msg.as_string())
    finally:
        srv.quit()
    return True, "Enviado a %s" % destino


def enviar_documento(tenant_id, destino, pdf, tipo_dte, folio, orden, canal, cliente, total):
    """El documento tributario al cliente, con la plantilla del tenant.
    Devuelve (ok, detalle, aplica). aplica=False si ese canal no envia por correo."""
    cfg = leer_config(tenant_id)
    if not (cfg.get("canales") or {}).get(canal):
        return False, "Este canal no envía el documento por correo (Configuración > Correo al cliente)", False
    if not destino or "@" not in str(destino):
        return False, "La orden no trae el correo del cliente", True
    asunto, cuerpo_html, texto = armar_correo(cfg, valores_de(cfg, cliente, tipo_dte, folio, orden,
                                                             canal, total))
    nombre_pdf = "%s_%s.pdf" % ("Factura" if int(tipo_dte) in (33, 34) else "Boleta", folio)
    ok, det = enviar(tenant_id, destino, asunto, cuerpo_html, texto, pdf, nombre_pdf)
    return ok, det, True
