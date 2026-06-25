# -*- coding: utf-8 -*-
"""
Descarga automática de folios (CAF) desde el SII — estilo "un clic", sin navegador.

Replica exactamente el flujo del portal de Timbraje Electrónico del SII usando
requests puro. NO usa Playwright, NO usa servicios de terceros, NO envía el
certificado del contribuyente a nadie: todo ocurre en este servidor.

────────────────────────────────────────────────────────────────────────────
LA CLAVE TÉCNICA (lo que lo hace funcionar):
Los servidores del SII negocian TLS con cifrados antiguos (SECLEVEL bajo).
Python moderno (3.12+) los rechaza por defecto con SSLV3_ALERT_HANDSHAKE_FAILURE,
y entonces el certificado cliente nunca se presenta y el SII redirige a su home.
La solución es un HTTPAdapter con un SSLContext que use 'DEFAULT@SECLEVEL=1' y
cargue el certificado del tenant (cert+llave en PEM) en el contexto. Con eso el
handshake mTLS se completa, el SII autentica al representante legal vía AUT2000
y entrega cookies de sesión.

FLUJO COMPLETO (7 pasos, verificados funcionando en maullin):
  1. POST  herculesr.sii.cl/cgi_AUT2000/CAutInicio.cgi   -> login AUT2000 (cookies+TOKEN)
  2. GET   {host}/cvc_cgi/dte/of_solicita_folios          -> form RUT empresa
  3. POST  {host}/cvc_cgi/dte/of_solicita_folios_dcto     -> (RUT) form selector tipo
  4. POST  {host}/cvc_cgi/dte/of_solicita_folios_dcto     -> (tipo) form cantidad + hidden
  5. POST  {host}/cvc_cgi/dte/of_confirma_folio           -> (cantidad) pantalla confirmación
  6. POST  {host}/cvc_cgi/dte/of_genera_folio             -> genera rango, form descarga
  7. POST  {host}/cvc_cgi/dte/of_genera_archivo           -> devuelve el CAF XML <AUTORIZACION>

host = maullin.sii.cl (certificación) | palena.sii.cl (producción)
El autenticador AUT2000 (herculesr) es el mismo para ambos ambientes.

ADVERTENCIA: En PRODUCCIÓN (palena) los folios son REALES y se consumen del
rango autorizado. En certificación (maullin) son de prueba y cada corrida
avanza el rango.
"""
from __future__ import annotations
import ssl
import tempfile
import os
import re

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context

from cryptography.hazmat.primitives.serialization import (
    Encoding, PrivateFormat, NoEncryption, pkcs12)


HOST = {
    "certificacion": "maullin.sii.cl",
    "produccion": "palena.sii.cl",
}
AUTENTICADOR = "herculesr.sii.cl"   # AUT2000, común a ambos ambientes
TIPOS_VALIDOS = {33, 34, 39, 41, 43, 46, 52, 56, 61, 110, 111, 112}


class SolicitarFoliosError(Exception):
    """Error en el proceso de solicitud de folios."""


class _SIIAdapter(HTTPAdapter):
    """HTTPAdapter que baja el SECLEVEL y monta el cert del tenant en el TLS.

    Es lo que permite el handshake mTLS contra los servidores antiguos del SII.
    """
    def __init__(self, pem_path, *args, **kwargs):
        self._pem_path = pem_path
        super().__init__(*args, **kwargs)

    def init_poolmanager(self, *args, **kwargs):
        ctx = create_urllib3_context()
        ctx.set_ciphers("DEFAULT@SECLEVEL=1")
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.load_cert_chain(self._pem_path)
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)


def _pfx_a_pem_temporal(pfx_bytes, password):
    """Extrae cert+llave del .pfx a un PEM temporal (cert seguido de llave).

    Devuelve la ruta del PEM. El llamador DEBE borrarlo (os.unlink) al terminar.
    """
    pwd = password.encode("utf-8") if isinstance(password, str) else password
    key, cert, _ = pkcs12.load_key_and_certificates(pfx_bytes, pwd)
    pem = tempfile.NamedTemporaryFile(suffix=".pem", delete=False, mode="wb")
    pem.write(cert.public_bytes(Encoding.PEM))
    pem.write(b"\n")
    pem.write(key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    pem.close()
    return pem.name


def _inputs(html):
    """Extrae todos los <INPUT name="..." value="..."> de un HTML como dict."""
    return dict(re.findall(
        r'<INPUT[^>]+NAME="([^"]+)"[^>]*VALUE="([^"]*)"', html, re.IGNORECASE))


def _max_autorizado(html):
    """Lee el valor del campo MAX_AUTOR (rango máximo autorizado) del form."""
    m = re.search(r'NAME="MAX_AUTOR"[^>]*VALUE\s*=\s*"?(\d+)', html, re.IGNORECASE)
    return m.group(1) if m else ""


def _mensaje_sii(html):
    """Intenta extraer un mensaje de error legible de la respuesta del SII."""
    texto = re.sub(r"<[^>]+>", " ", html)
    texto = re.sub(r"\s+", " ", texto).strip()
    for clave in ["situaciones pendientes", "no se encuentra autorizado",
                  "excede", "máximo", "no posee", "error"]:
        i = texto.lower().find(clave)
        if i >= 0:
            return "SII: " + texto[max(0, i - 20):i + 120]
    return ""


def descargar_caf(pfx_bytes, password, rut_emisor, tipo_dte, cantidad,
                  ambiente="certificacion", timeout=60):
    """Descarga `cantidad` folios del `tipo_dte` desde el SII para `rut_emisor`.

    Returns dict {ok, caf_xml, folio_desde, folio_hasta, max_autorizado, error, traza}.
    """
    if ambiente not in HOST:
        return {"ok": False, "error": "Ambiente inválido: %s" % ambiente, "caf_xml": None}
    if int(tipo_dte) not in TIPOS_VALIDOS:
        return {"ok": False, "error": "Tipo DTE no válido: %s" % tipo_dte, "caf_xml": None}
    if int(cantidad) < 1:
        return {"ok": False, "error": "La cantidad debe ser >= 1", "caf_xml": None}

    host = HOST[ambiente]
    base = "https://%s/cvc_cgi/dte" % host
    referencia = "%s/of_solicita_folios" % base

    rut_limpio = rut_emisor.replace(".", "").replace(" ", "").upper()
    if "-" in rut_limpio:
        cuerpo, dv = rut_limpio.split("-", 1)
    else:
        cuerpo, dv = rut_limpio[:-1], rut_limpio[-1]

    pem_path = None
    traza = []
    try:
        pem_path = _pfx_a_pem_temporal(pfx_bytes, password)
        s = requests.Session()
        s.mount("https://", _SIIAdapter(pem_path))

        # 1. Login AUT2000
        s.post("https://%s/cgi_AUT2000/CAutInicio.cgi" % AUTENTICADOR,
               data={"referencia": referencia}, timeout=timeout)
        token = s.cookies.get("TOKEN")
        if not token:
            return {"ok": False, "caf_xml": None,
                    "error": "AUT2000 no autenticó el certificado (sin TOKEN). "
                             "Verifica que el certificado sea del representante "
                             "legal y esté vigente.",
                    "traza": traza}
        traza.append("1. AUT2000 OK, TOKEN=%s" % token)

        # 2. Form RUT empresa
        s.get(referencia, timeout=timeout)
        traza.append("2. form RUT cargado")

        # 3. POST RUT -> selector de tipo
        s.post("%s/of_solicita_folios_dcto" % base,
               data={"RUT_EMP": cuerpo, "DV_EMP": dv, "ACEPTAR": "Continuar"},
               timeout=timeout)
        traza.append("3. RUT empresa enviado")

        # 4. POST tipo -> form cantidad
        r4 = s.post("%s/of_solicita_folios_dcto" % base,
                    data={"RUT_EMP": cuerpo, "DV_EMP": dv,
                          "FOLIO_INICIAL": "0", "COD_DOCTO": str(tipo_dte)},
                    timeout=timeout)
        if "CANT_DOCTOS" not in r4.text:
            return {"ok": False, "caf_xml": None,
                    "error": "El SII no ofreció el formulario de cantidad para el "
                             "tipo %s. Puede que el contribuyente no tenga ese "
                             "documento autorizado o tenga situaciones pendientes."
                             % tipo_dte,
                    "traza": traza}
        max_aut = _max_autorizado(r4.text)
        traza.append("4. form cantidad, MAX_AUTOR=%s" % max_aut)

        # 5. POST cantidad -> confirmación
        d5 = _inputs(r4.text)
        d5.update({
            "RUT_EMP": cuerpo, "DV_EMP": dv, "FOLIO_INICIAL": "0",
            "COD_DOCTO": str(tipo_dte), "CANT_DOCTOS": str(cantidad),
            "ACEPTAR": "Solicitar",
        })
        r5 = s.post("%s/of_confirma_folio" % base, data=d5, timeout=timeout)
        if "Obtener Folios" not in r5.text and "btener" not in r5.text:
            motivo = _mensaje_sii(r5.text)
            return {"ok": False, "caf_xml": None,
                    "error": ("El SII no confirmó la solicitud. %s" % motivo).strip(),
                    "max_autorizado": max_aut, "traza": traza}
        traza.append("5. confirmación OK")

        # 6. POST generar -> reserva rango, form descarga
        d6 = _inputs(r5.text)
        d6["ACEPTAR"] = "Obtener Folios"
        r6 = s.post("%s/of_genera_folio" % base, data=d6, timeout=timeout)
        d7 = _inputs(r6.text)
        if "FOLIO_INI" not in d7:
            return {"ok": False, "caf_xml": None,
                    "error": "El SII no entregó el formulario de descarga del CAF.",
                    "traza": traza}
        traza.append("6. rango reservado %s-%s" % (d7.get("FOLIO_INI"), d7.get("FOLIO_FIN")))

        # 7. POST descargar -> CAF XML
        d7["ACEPTAR"] = "AQUI"
        r7 = s.post("%s/of_genera_archivo" % base, data=d7, timeout=timeout)
        if "<AUTORIZACION>" not in r7.text:
            return {"ok": False, "caf_xml": None,
                    "error": "El SII no devolvió el XML del CAF en el paso final.",
                    "traza": traza}

        m = re.search(r"<AUTORIZACION>.*?</AUTORIZACION>", r7.text, re.DOTALL)
        caf_xml = m.group(0)
        if not caf_xml.lstrip().startswith("<?xml"):
            caf_xml = '<?xml version="1.0"?>\n' + caf_xml
        d = re.search(r"<D>(\d+)</D>", caf_xml)
        h = re.search(r"<H>(\d+)</H>", caf_xml)
        traza.append("7. CAF XML descargado")

        return {
            "ok": True,
            "caf_xml": caf_xml,
            "folio_desde": int(d.group(1)) if d else None,
            "folio_hasta": int(h.group(1)) if h else None,
            "max_autorizado": max_aut,
            "error": None,
            "traza": traza,
        }

    except Exception as e:
        import traceback
        return {"ok": False, "caf_xml": None,
                "error": "%s: %s" % (type(e).__name__, e),
                "traza": traza + [traceback.format_exc()[:500]]}
    finally:
        if pem_path:
            try:
                os.unlink(pem_path)
            except Exception:
                pass


def descargar_y_guardar(get_conn, release_conn, tenant_id, pfx_bytes, password,
                        rut_emisor, tipo_dte, cantidad, ambiente="certificacion"):
    """Descarga el CAF del SII y lo guarda en facturacion_cafs (vía cafs.subir_caf).

    Returns dict {ok, caf_id, folio_desde, folio_hasta, error, traza}.
    """
    res = descargar_caf(pfx_bytes, password, rut_emisor, tipo_dte, cantidad, ambiente)
    if not res["ok"]:
        return res

    from facturacion.cafs import subir_caf
    guardado = subir_caf(get_conn, release_conn, tenant_id, res["caf_xml"],
                         rut_emisor_esperado=rut_emisor, ambiente=ambiente)
    if not guardado.get("ok"):
        return {"ok": False, "caf_xml": res["caf_xml"],
                "error": "CAF descargado pero no se pudo guardar: "
                         + guardado.get("error", "?"),
                "folio_desde": res.get("folio_desde"),
                "folio_hasta": res.get("folio_hasta"),
                "traza": res.get("traza")}

    return {
        "ok": True,
        "caf_id": guardado.get("caf_id"),
        "folio_desde": res.get("folio_desde"),
        "folio_hasta": res.get("folio_hasta"),
        "max_autorizado": res.get("max_autorizado"),
        "ambiente": ambiente,
        "error": None,
    }
