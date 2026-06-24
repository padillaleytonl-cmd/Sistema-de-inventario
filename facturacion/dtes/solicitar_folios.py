# -*- coding: utf-8 -*-
"""
Solicitud automática de folios (CAF) al SII, directamente desde Lusync.

Replica lo que hace un humano en
  https://palena.sii.cl/cvc_cgi/dte/of_solicita_folios   (producción)
  https://maullin.sii.cl/cvc_cgi/dte/of_solicita_folios  (certificación)
pero de forma programática, reutilizando la autenticación que ya tiene el
sistema (obtener_token_dte → cookie TOKEN).

⚠️ IMPORTANTE — leer antes de usar en producción:
El endpoint of_solicita_folios es un CGI antiguo (no una API REST). Espera los
campos de un formulario web y responde HTML. Los nombres de campo aquí están
basados en el comportamiento conocido del CGI, pero el SII NO los documenta
públicamente; el primer test real contra palena/maullin puede requerir ajustar
los nombres de los campos del POST (ver TODO abajo). Por eso este módulo:
  • Separa cada paso (token, consulta de máximo, solicitud, descarga CAF).
  • Devuelve siempre la respuesta cruda para diagnóstico.
  • NO asume éxito: valida que la respuesta contenga un CAF (<AUTORIZACION>).

Flujo:
  1. obtener_token_dte(pfx, pass, ambiente)   → token (ya existe en sii_client)
  2. consultar_folios_disponibles(...)         → máximo autorizado por el SII
  3. solicitar_folios(...)                      → POST al CGI, devuelve el CAF XML
"""
import re
import requests

from .sii_client import obtener_token_dte, USER_AGENT

TIMEOUT = 60

# Hosts del CGI de timbraje por ambiente
CGI_HOST = {
    "certificacion": "maullin.sii.cl",
    "produccion": "palena.sii.cl",
}


class FoliosError(Exception):
    pass


def _split_rut(rut: str):
    rut = (rut or "").replace(".", "").replace("-", "").strip().upper()
    return rut[:-1], rut[-1]


def _headers(token: str, host: str):
    return {
        "User-Agent": USER_AGENT,
        "Cookie": f"TOKEN={token}",
        "Host": host,
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "text/html,application/xhtml+xml",
    }


def _crear_cert_temporal(pfx_bytes: bytes, password: str):
    """Extrae cert+llave del .pfx a un archivo PEM temporal para mTLS.

    El CGI of_solicita_folios puede exigir el certificado cliente en la
    conexión TLS (no solo el token). Esta función crea un PEM temporal que
    `requests` puede usar con el parámetro cert=. Devuelve la ruta del archivo
    (hay que borrarlo después con os.unlink).

    Devuelve None si no se pudo (en ese caso se intenta solo con token).
    """
    try:
        import tempfile
        from cryptography.hazmat.primitives.serialization import (
            Encoding, PrivateFormat, NoEncryption, pkcs12)
        pwd = password.encode("utf-8") if isinstance(password, str) else password
        key, cert, _ = pkcs12.load_key_and_certificates(pfx_bytes, pwd)
        key_pem = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
        cert_pem = cert.public_bytes(Encoding.PEM)
        f = tempfile.NamedTemporaryFile(mode="wb", suffix=".pem", delete=False)
        f.write(cert_pem + b"\n" + key_pem)
        f.close()
        return f.name
    except Exception:
        return None


def _post_cgi(url, data, token, host, pfx_bytes=None, password=None, usar_mtls=False):
    """POST al CGI. Si usar_mtls=True, adjunta el certificado cliente al TLS.

    Devuelve (texto_respuesta, error_str). error_str es None si fue bien.
    """
    import os
    cert_file = None
    try:
        kwargs = {"headers": _headers(token, host), "data": data, "timeout": TIMEOUT}
        if usar_mtls and pfx_bytes:
            cert_file = _crear_cert_temporal(pfx_bytes, password)
            if cert_file:
                kwargs["cert"] = cert_file
        resp = requests.post(url, **kwargs)
        return resp.text or "", None
    except Exception as e:
        return "", str(e)
    finally:
        if cert_file:
            try:
                os.unlink(cert_file)
            except Exception:
                pass


def solicitar_folios(
    pfx_bytes: bytes,
    password: str,
    rut_emisor: str,
    tipo_dte: int,
    cantidad: int,
    ambiente: str = "certificacion",
    rut_solicitante: str = None,
    token: str = None,
    usar_mtls: bool = False,
) -> dict:
    """Solicita `cantidad` folios del `tipo_dte` al SII y devuelve el CAF XML.

    Args:
        pfx_bytes, password: certificado digital del TENANT (no de Lusync).
        rut_emisor: RUT de la empresa (ej '76922862-4').
        tipo_dte: 33, 34, 39, 52, 56, 61, etc.
        cantidad: cuántos folios pedir.
        ambiente: 'certificacion' o 'produccion'.
        rut_solicitante: RUT del representante legal (default = rut_emisor).
        token: si ya tienes uno, evita re-autenticar.
        usar_mtls: si el CGI exige certificado cliente en el TLS (no solo token),
                   activar esto para adjuntar el certificado a la conexión.

    Returns:
        dict {ok, caf_xml, folio_desde, folio_hasta, respuesta_cruda, error}
    """
    if ambiente not in CGI_HOST:
        raise FoliosError(f"Ambiente inválido: {ambiente}")
    host = CGI_HOST[ambiente]
    url = f"https://{host}/cvc_cgi/dte/of_solicita_folios"

    # 1. Token (reutiliza la autenticación ya probada del sistema)
    if not token:
        try:
            token = obtener_token_dte(pfx_bytes, password, ambiente)
        except Exception as e:
            return {"ok": False, "error": f"No se pudo autenticar al SII: {e}",
                    "caf_xml": None, "respuesta_cruda": ""}

    rut_e_num, rut_e_dv = _split_rut(rut_emisor)
    rut_sol = rut_solicitante or rut_emisor
    rut_s_num, rut_s_dv = _split_rut(rut_sol)

    # 2. POST de solicitud.
    # TODO(primer test real): confirmar los nombres EXACTOS de estos campos
    # inspeccionando el formulario real del CGI. Los habituales son:
    #   RUT_EMP / DV_EMP, COD_DOCTO (tipo dte), FOLIO_INI implícito, NUM_DOC (cantidad)
    # Si el SII responde con el formulario en vez del CAF, ajustar aquí.
    data = {
        "RUT_EMP": rut_e_num,
        "DV_EMP": rut_e_dv,
        "RUT_REQ": rut_s_num,
        "DV_REQ": rut_s_dv,
        "COD_DOCTO": str(tipo_dte),
        "FOLIO_INICIAL": "",          # el SII asigna el siguiente disponible
        "CONT": str(cantidad),         # cantidad de folios
        "ACEPTAR": "Solicitar numeración",
    }

    texto, err_conn = _post_cgi(url, data, token, host,
                                pfx_bytes=pfx_bytes, password=password,
                                usar_mtls=usar_mtls)
    if err_conn:
        return {"ok": False, "error": f"No se pudo conectar a {url}: {err_conn}",
                "caf_xml": None, "respuesta_cruda": ""}

    # 3. Extraer el CAF de la respuesta.
    # El CGI devuelve HTML; el CAF puede venir embebido como <AUTORIZACION>...
    # o requerir una segunda llamada de descarga. Intentamos extraerlo directo.
    caf = _extraer_caf(texto)
    if caf:
        desde, hasta = _rango_caf(caf)
        return {"ok": True, "caf_xml": caf, "folio_desde": desde,
                "folio_hasta": hasta, "respuesta_cruda": texto[:1000], "error": None}

    # Si no vino el CAF, puede que el CGI haya devuelto un link/ID de descarga.
    # Intentar localizar un enlace de descarga del XML.
    link = _buscar_link_descarga(texto, host)
    if link:
        try:
            r2 = requests.get(link, headers=_headers(token, host), timeout=TIMEOUT)
            caf2 = _extraer_caf(r2.text)
            if caf2:
                desde, hasta = _rango_caf(caf2)
                return {"ok": True, "caf_xml": caf2, "folio_desde": desde,
                        "folio_hasta": hasta, "respuesta_cruda": r2.text[:1000], "error": None}
        except Exception as e:
            return {"ok": False, "error": f"CAF no descargable: {e}",
                    "caf_xml": None, "respuesta_cruda": texto[:1500]}

    # No se pudo extraer: devolver crudo para diagnóstico (errores típicos:
    # situaciones pendientes, tipo no autorizado, sin folios disponibles).
    return {"ok": False,
            "error": _detectar_error(texto) or "No se encontró CAF en la respuesta del SII",
            "caf_xml": None, "respuesta_cruda": texto[:2000]}


def _extraer_caf(texto: str):
    """Busca un bloque <AUTORIZACION>...</AUTORIZACION> (el CAF) en el texto."""
    if not texto:
        return None
    m = re.search(r"<AUTORIZACION>.*?</AUTORIZACION>", texto, re.DOTALL)
    return m.group(0) if m else None


def _rango_caf(caf_xml: str):
    """Extrae folio desde (D) y hasta (H) del CAF."""
    d = re.search(r"<D>(\d+)</D>", caf_xml)
    h = re.search(r"<H>(\d+)</H>", caf_xml)
    return (int(d.group(1)) if d else None, int(h.group(1)) if h else None)


def _buscar_link_descarga(texto: str, host: str):
    """Busca un enlace de descarga del XML del CAF en el HTML de respuesta."""
    if not texto:
        return None
    m = re.search(r'href=["\']([^"\']*(?:caf|xml|genera_folios)[^"\']*)["\']', texto, re.IGNORECASE)
    if not m:
        return None
    href = m.group(1)
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        return f"https://{host}{href}"
    return f"https://{host}/cvc_cgi/dte/{href}"


def _detectar_error(texto: str):
    """Detecta mensajes de error típicos del SII en el HTML."""
    if not texto:
        return None
    t = texto.lower()
    if "situaciones pendientes" in t or "situacion pendiente" in t:
        return "El contribuyente tiene situaciones pendientes con el SII (timbraje bloqueado)."
    if "no autorizado" in t or "no se encuentra autorizado" in t:
        return "El tipo de documento no está autorizado/certificado para este RUT."
    if "verificaci" in t and "actividad" in t:
        return "Falta verificación de actividades ante el SII."
    if "token" in t and ("venc" in t or "inv" in t):
        return "Token vencido o inválido; reintentar autenticación."
    return None


def consultar_folios_disponibles(
    pfx_bytes: bytes, password: str, rut_emisor: str,
    tipo_dte: int, ambiente: str = "certificacion", token: str = None,
) -> dict:
    """Consulta el máximo de folios que el SII autoriza para ese tipo de documento.
    Útil para mostrar al usuario cuántos puede pedir antes de solicitar.

    Returns: dict {ok, maximo, respuesta_cruda, error}
    """
    if ambiente not in CGI_HOST:
        raise FoliosError(f"Ambiente inválido: {ambiente}")
    host = CGI_HOST[ambiente]
    url = f"https://{host}/cvc_cgi/dte/of_solicita_folios"

    if not token:
        try:
            token = obtener_token_dte(pfx_bytes, password, ambiente)
        except Exception as e:
            return {"ok": False, "error": f"No se pudo autenticar: {e}", "maximo": None}

    rut_num, rut_dv = _split_rut(rut_emisor)
    data = {"RUT_EMP": rut_num, "DV_EMP": rut_dv, "COD_DOCTO": str(tipo_dte)}
    try:
        resp = requests.post(url, headers=_headers(token, host), data=data, timeout=TIMEOUT)
    except Exception as e:
        return {"ok": False, "error": str(e), "maximo": None}

    # Buscar el "máximo autorizado" en la respuesta (formato variable).
    m = re.search(r"(?:m[áa]ximo|autoriza)[^\d]*(\d+)", resp.text, re.IGNORECASE)
    maximo = int(m.group(1)) if m else None
    return {"ok": maximo is not None, "maximo": maximo,
            "respuesta_cruda": resp.text[:1500],
            "error": None if maximo else "No se pudo determinar el máximo autorizado"}
