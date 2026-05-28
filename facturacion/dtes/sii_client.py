"""
facturacion/dtes/sii_client.py
─────────────────────────────────────────────────────────────
Cliente de comunicación con el SII para BOLETAS ELECTRÓNICAS.

⚠ IMPORTANTE: las boletas usan servidores REST DISTINTOS de las facturas:
  CERTIFICACIÓN:
    • Token/semilla/consultas → apicert.sii.cl
    • Envío del documento      → pangal.sii.cl
  PRODUCCIÓN:
    • Token/semilla/consultas → api.sii.cl
    • Envío del documento      → rahue.sii.cl

(Las facturas usan maullin.sii.cl / palena.sii.cl vía SOAP — eso NO es esto.)

Flujo de autenticación:
  1. GET semilla
  2. Construir <getToken><item><Semilla>N</Semilla></item></getToken>
  3. Firmarlo con XMLDSig (.pfx)
  4. POST → obtener TOKEN
  5. Usar TOKEN en cookie para enviar/consultar

El track id de boletas tiene 15 dígitos (factura tiene 10).
Si el token vence, el SII responde el string "NO ESTA AUTENTICADO".
"""
from __future__ import annotations
import re
import base64
import hashlib
from typing import Optional

import requests
from lxml import etree
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

NS_DSIG = "http://www.w3.org/2000/09/xmldsig#"
C14N_METHOD = "http://www.w3.org/TR/2001/REC-xml-c14n-20010315"

# Endpoints según ambiente
ENDPOINTS = {
    "certificacion": {
        "semilla": "https://apicert.sii.cl/recursos/v1/boleta.electronica.semilla",
        "token":   "https://apicert.sii.cl/recursos/v1/boleta.electronica.token",
        "envio":   "https://pangal.sii.cl/recursos/v1/boleta.electronica.envio",
        # Estado del envío: {rut}-{dv}-{trackid}/estado  (track id boletas = 15 díg)
        "estado_envio": "https://apicert.sii.cl/recursos/v1/boleta.electronica.envio/{rut}-{dv}-{trackid}/estado",
        "host_envio": "pangal.sii.cl",
    },
    "produccion": {
        "semilla": "https://api.sii.cl/recursos/v1/boleta.electronica.semilla",
        "token":   "https://api.sii.cl/recursos/v1/boleta.electronica.token",
        "envio":   "https://rahue.sii.cl/recursos/v1/boleta.electronica.envio",
        "estado_envio": "https://api.sii.cl/recursos/v1/boleta.electronica.envio/{rut}-{dv}-{trackid}/estado",
        "host_envio": "rahue.sii.cl",
    },
}

USER_AGENT = "Mozilla/4.0 (compatible; PROG 1.0; Windows NT 5.0)"
TIMEOUT = 30


class SIIError(Exception):
    """Error de comunicación con el SII."""
    pass


def _c14n(elemento) -> bytes:
    return etree.tostring(elemento, method="c14n", exclusive=False, with_comments=False)


def obtener_semilla(ambiente: str = "certificacion") -> str:
    """Paso 1: solicita una semilla al SII.

    Returns:
        str: la semilla (ej '000000000078')
    Raises:
        SIIError si falla
    """
    url = ENDPOINTS[ambiente]["semilla"]
    headers = {"User-Agent": USER_AGENT, "Accept": "application/xml"}
    try:
        resp = requests.get(url, headers=headers, timeout=TIMEOUT)
    except Exception as e:
        raise SIIError(f"No se pudo conectar a {url}: {e}")

    if resp.status_code != 200:
        raise SIIError(f"SII respondió {resp.status_code} al pedir semilla: {resp.text[:200]}")

    # La respuesta es XML: buscar <SEMILLA>...</SEMILLA>
    m = re.search(r"<SEMILLA>\s*([^<]+?)\s*</SEMILLA>", resp.text)
    if not m:
        raise SIIError(f"No se encontró <SEMILLA> en la respuesta: {resp.text[:300]}")
    return m.group(1).strip()


def firmar_semilla(semilla: str, pfx_bytes: bytes, password: str) -> bytes:
    """Paso 2-3: construye <getToken> con la semilla y lo firma con XMLDSig.

    Usa signxml (implementación estándar W3C de XML-DSig) para garantizar que la
    canonicalización y el digest sean EXACTAMENTE los que el SII valida. El SII
    requiere SHA1 (inseguro pero obligatorio), por eso se desactiva el bloqueo de
    SHA1 de signxml mediante una subclase. La estructura resultante coincide con
    el formato del manual oficial del SII: Reference URI="", transform enveloped,
    KeyInfo con X509Data/X509Certificate (cert partido en líneas de 64 chars).

    Returns:
        bytes del XML <getToken> firmado, listo para enviar
    """
    from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, NoEncryption
    from signxml import XMLSigner, methods

    if isinstance(password, str):
        password = password.encode("utf-8")
    private_key, certificate, _ = pkcs12.load_key_and_certificates(pfx_bytes, password)

    # Exportar clave y cert a PEM para signxml
    key_pem = private_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    cert_pem = certificate.public_bytes(Encoding.PEM)

    # Subclase que permite SHA1 (el SII lo exige)
    class _SIIXMLSigner(XMLSigner):
        def check_deprecated_methods(self):
            pass

    # Documento a firmar
    doc = etree.fromstring(
        f'<getToken><item><Semilla>{semilla}</Semilla></item></getToken>'.encode("utf-8")
    )

    signer = _SIIXMLSigner(
        method=methods.enveloped,
        signature_algorithm="rsa-sha1",
        digest_algorithm="sha1",
        c14n_algorithm=C14N_METHOD,
    )
    # Namespace por defecto (sin prefijo ds:), como en el ejemplo del SII
    signer.namespaces = {None: NS_DSIG}

    signed = signer.sign(
        doc,
        key=key_pem,
        cert=cert_pem,
        exclude_c14n_transform_element=True,  # deja solo el transform enveloped
    )

    # Serializar con la declaración exacta que el SII parser acepta
    cuerpo = etree.tostring(signed, xml_declaration=False, encoding="UTF-8").decode("utf-8")
    xml_final = '<?xml version="1.0"?>' + cuerpo
    return xml_final.encode("utf-8")


def obtener_token(semilla_firmada: bytes, ambiente: str = "certificacion") -> str:
    """Paso 4: envía la semilla firmada y obtiene el token.

    El endpoint REST de boletas (apicert/boleta.electronica.token) espera el XML
    de la semilla firmada. Probamos las formas conocidas de envío que aceptan
    las distintas versiones del SII, en orden, hasta obtener el token.

    Returns:
        str: el token de autenticación
    """
    url = ENDPOINTS[ambiente]["token"]
    xml_str = semilla_firmada.decode("utf-8") if isinstance(semilla_firmada, bytes) else semilla_firmada

    intentos = []

    # Forma A: body XML con charset explícito
    intentos.append({
        "nombre": "body-xml-charset",
        "kwargs": {
            "data": xml_str.encode("utf-8"),
            "headers": {"User-Agent": USER_AGENT,
                        "Content-Type": "application/xml; charset=utf-8"},
        },
    })
    # Forma B: body text/xml
    intentos.append({
        "nombre": "body-text-xml",
        "kwargs": {
            "data": xml_str.encode("utf-8"),
            "headers": {"User-Agent": USER_AGENT, "Content-Type": "text/xml"},
        },
    })
    # Forma C: form-urlencoded con campo 'getToken'
    intentos.append({
        "nombre": "form-getToken",
        "kwargs": {
            "data": {"getToken": xml_str},
            "headers": {"User-Agent": USER_AGENT,
                        "Content-Type": "application/x-www-form-urlencoded"},
        },
    })
    # Forma D: body XML simple (sin charset)
    intentos.append({
        "nombre": "body-xml",
        "kwargs": {
            "data": xml_str.encode("utf-8"),
            "headers": {"User-Agent": USER_AGENT, "Content-Type": "application/xml"},
        },
    })

    log = []
    for intento in intentos:
        try:
            resp = requests.post(url, timeout=TIMEOUT, **intento["kwargs"])
        except Exception as e:
            log.append(f"[{intento['nombre']}] conexión: {str(e)[:120]}")
            continue

        texto = resp.text
        # Buscar token
        m = re.search(r"<TOKEN>\s*([^<]+?)\s*</TOKEN>", texto)
        if m:
            return m.group(1).strip()
        try:
            j = resp.json()
            tok = j.get("token") or j.get("TOKEN")
            if tok:
                return tok
        except Exception:
            pass
        # Extraer estado/glosa si viene
        est = re.search(r"<ESTADO>([^<]*)</ESTADO>", texto)
        glo = re.search(r"<GLOSA>([^<]*)</GLOSA>", texto)
        resumen = ""
        if est:
            resumen = f"estado={est.group(1)}"
            if glo:
                resumen += f" glosa={glo.group(1)}"
        else:
            resumen = texto[:150]
        log.append(f"[{intento['nombre']}] HTTP {resp.status_code}: {resumen}")

    raise SIIError("No se obtuvo TOKEN. Respuestas: || " + " || ".join(log))


def autenticar(pfx_bytes: bytes, password: str, ambiente: str = "certificacion") -> str:
    """Flujo completo de autenticación: semilla → firmar → token.

    Returns:
        str: token listo para usar
    """
    semilla = obtener_semilla(ambiente)
    semilla_firmada = firmar_semilla(semilla, pfx_bytes, password)
    token = obtener_token(semilla_firmada, ambiente)
    return token


def enviar_boletas(
    envio_xml: bytes,
    token: str,
    rut_emisor: str,
    rut_envia: str,
    ambiente: str = "certificacion",
) -> dict:
    """Paso 5: envía el EnvioBOLETA al SII (POST multipart).

    Args:
        envio_xml: el sobre EnvioBOLETA firmado
        token: token de autenticación
        rut_emisor: RUT empresa (sin DV separado, ej '76922862')
        rut_envia: RUT del que envía (representante)
        ambiente: certificacion o produccion

    Returns:
        dict {ok, track_id, respuesta_cruda, status}
    """
    url = ENDPOINTS[ambiente]["envio"]
    host = ENDPOINTS[ambiente]["host_envio"]

    # Separar RUT y DV
    def _split_rut(rut):
        rut = rut.replace(".", "").replace("-", "")
        return rut[:-1], rut[-1]
    rut_e_num, rut_e_dv = _split_rut(rut_emisor)
    rut_env_num, rut_env_dv = _split_rut(rut_envia)

    headers = {
        "User-Agent": USER_AGENT,
        "accept": "application/json",
        "Cookie": f"TOKEN={token}",
        "Expect": "100-continue",
    }
    # multipart/form-data con el archivo.
    # El SII espera el campo 'archivo' con tipo text/xml y charset ISO-8859-1.
    files = {
        "archivo": ("envioBoleta.xml", envio_xml, "text/xml; charset=ISO-8859-1"),
    }
    data = {
        "rutSender": rut_env_num, "dvSender": rut_env_dv,
        "rutCompany": rut_e_num, "dvCompany": rut_e_dv,
    }
    try:
        resp = requests.post(url, headers=headers, files=files, data=data, timeout=TIMEOUT)
    except Exception as e:
        raise SIIError(f"No se pudo conectar a {url}: {e}")

    texto = resp.text
    # Si el token venció
    if "NO ESTA AUTENTICADO" in texto.upper():
        return {"ok": False, "error": "Token vencido o inválido (NO ESTA AUTENTICADO)",
                "respuesta_cruda": texto[:500], "status": resp.status_code}

    # Buscar track id. La respuesta es JSON: {"trackid":1014,"estado":"REC",...}
    track_id = None
    estado = None
    try:
        j = resp.json()
        track_id = j.get("trackid") or j.get("trackId") or j.get("track_id")
        estado = j.get("estado")
    except Exception:
        pass
    # XML fallback
    if not track_id:
        m = re.search(r"<TRACKID>\s*([0-9]+)\s*</TRACKID>", texto, re.IGNORECASE)
        if m:
            track_id = m.group(1)

    ok = track_id is not None and resp.status_code in (200, 201)
    return {
        "ok": ok,
        "track_id": track_id,
        "estado": estado,
        "respuesta_cruda": texto[:500],
        "status": resp.status_code,
    }


def consultar_estado_envio(
    track_id: str,
    token: str,
    rut_emisor: str,
    ambiente: str = "certificacion",
) -> dict:
    """Consulta el estado de un envío por su track id.

    Returns:
        dict {ok, estado, respuesta_cruda}
    """
    def _split_rut(rut):
        rut = rut.replace(".", "").replace("-", "")
        return rut[:-1], rut[-1]
    rut_num, rut_dv = _split_rut(rut_emisor)

    url = ENDPOINTS[ambiente]["estado_envio"].format(
        rut=rut_num, dv=rut_dv, trackid=track_id)
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Cookie": f"TOKEN={token}",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=TIMEOUT)
    except Exception as e:
        raise SIIError(f"No se pudo conectar a {url}: {e}")

    texto = resp.text
    if "NO ESTA AUTENTICADO" in texto.upper():
        return {"ok": False, "error": "Token vencido", "respuesta_cruda": texto[:500]}

    # Interpretar la respuesta JSON del SII
    estado_envio = None       # EPR, RCH, etc. (estado del sobre)
    aceptados = rechazados = reparos = informados = None
    aceptado_final = None     # True si las boletas quedaron aceptadas sin reparos
    try:
        j = resp.json()
        estado_envio = j.get("estado") or j.get("statusCode") or j.get("status")
        # El SII devuelve estadísticas del envío en distintas formas según versión
        est = j.get("estadistica") or j.get("estadisticas") or []
        if isinstance(est, list) and est:
            e0 = est[0]
            informados = e0.get("informados")
            aceptados = e0.get("aceptados")
            rechazados = e0.get("rechazados")
            reparos = e0.get("reparos") or e0.get("aceptadosConReparo")
        # Algunos formatos traen los totales directo
        aceptados = aceptados if aceptados is not None else j.get("aceptados")
        rechazados = rechazados if rechazados is not None else j.get("rechazados")
        reparos = reparos if reparos is not None else j.get("reparos")

        if rechazados is not None and reparos is not None:
            aceptado_final = (int(rechazados) == 0 and int(reparos) == 0
                              and int(aceptados or 0) > 0)
    except Exception:
        pass

    return {
        "ok": resp.status_code == 200,
        "status": resp.status_code,
        "estado_envio": estado_envio,
        "informados": informados,
        "aceptados": aceptados,
        "rechazados": rechazados,
        "reparos": reparos,
        "aceptado_sin_reparos": aceptado_final,
        "respuesta_cruda": texto[:1500],
    }


# ═══════════════════════════════════════════════════════════════════════════
# CONSULTA DE ESTADO DE UN DTE POR SUS DATOS (getEstDte) — SOAP, vía PÚBLICA
# ═══════════════════════════════════════════════════════════════════════════
# Esta es la consulta DOCUMENTADA oficialmente por el SII (manual QueryEstDte,
# OI2004_CEDTE_MDE). A diferencia de la consulta por track id (API REST, cuyo
# path está en el Swagger autenticado), getEstDte usa una URL FIJA y conocida:
#   Certificación: https://maullin.sii.cl/DTEWS/QueryEstDte.jws
#   Producción:    https://palena.sii.cl/DTEWS/QueryEstDte.jws
#
# Resuelve dos necesidades:
#   1. Que el emisor sepa si el SII tiene la boleta registrada y con datos OK.
#   2. Que CUALQUIERA (incl. el cliente) verifique la boleta con los datos
#      impresos: tipo, folio, fecha, monto, RUT emisor y receptor.
#
# Requiere un token del WS clásico (GetTokenFromSeed en .../DTEWS), distinto al
# token de la API REST de boletas. Las funciones de abajo lo obtienen solas.

DTEWS = {
    "certificacion": {
        "seed":  "https://maullin.sii.cl/DTEWS/CrSeed.jws?WSDL",
        "token": "https://maullin.sii.cl/DTEWS/GetTokenFromSeed.jws?WSDL",
        "query": "https://maullin.sii.cl/DTEWS/QueryEstDte.jws",
        "upload": "https://maullin.sii.cl/cgi_dte/UPL/DTEUpload",
        "upload_host": "maullin.sii.cl",
    },
    "produccion": {
        "seed":  "https://palena.sii.cl/DTEWS/CrSeed.jws?WSDL",
        "token": "https://palena.sii.cl/DTEWS/GetTokenFromSeed.jws?WSDL",
        "query": "https://palena.sii.cl/DTEWS/QueryEstDte.jws",
        "upload": "https://palena.sii.cl/cgi_dte/UPL/DTEUpload",
        "upload_host": "palena.sii.cl",
    },
}


def _dtews_obtener_semilla(ambiente: str = "certificacion") -> str:
    """Pide una semilla al WS clásico CrSeed (SOAP/xfire).

    El servicio es xfire (Java). Espera un envelope SOAP con el método getSeed
    en el namespace por defecto. Responde un XML escapado con la semilla en
    /SII:RESPUESTA/SII:RESP_BODY/SEMILLA y ESTADO=00.
    """
    url = DTEWS[ambiente]["seed"].replace("?WSDL", "").replace("?wsdl", "")
    soap = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
        'xmlns:def="http://DefaultNamespace">'
        '<soapenv:Header/>'
        '<soapenv:Body><def:getSeed/></soapenv:Body>'
        '</soapenv:Envelope>'
    )
    headers = {
        "Content-Type": "text/xml; charset=utf-8",
        "SOAPAction": "",
        "User-Agent": USER_AGENT,
    }
    r = requests.post(url, data=soap.encode("utf-8"), headers=headers, timeout=TIMEOUT)
    txt = r.text
    # La semilla viene en XML escapado dentro del <getSeedReturn> (o sin escapar)
    m = re.search(r"<SEMILLA>(\d+)</SEMILLA>", txt)
    if not m:
        m = re.search(r"&lt;SEMILLA&gt;(\d+)&lt;/SEMILLA&gt;", txt)
    if not m:
        raise SIIError(f"No se obtuvo semilla del WS clásico (status {r.status_code}): {txt[:400]}")
        raise SIIError(f"No se obtuvo semilla del WS clásico: {r.text[:300]}")
    return m.group(1)


def _dtews_obtener_token(pfx_bytes: bytes, password: str,
                         ambiente: str = "certificacion") -> str:
    """Obtiene el token del WS clásico (GetTokenFromSeed): semilla → firmar → token."""
    from signxml import XMLSigner, methods
    from cryptography.hazmat.primitives.serialization import pkcs12
    from lxml import etree

    semilla = _dtews_obtener_semilla(ambiente)
    # Armar y firmar el getToken
    key, cert, _ = pkcs12.load_key_and_certificates(pfx_bytes, password.encode())
    root = etree.fromstring(
        f'<getToken><item><Semilla>{semilla}</Semilla></item></getToken>'.encode())

    class _S(XMLSigner):
        def check_deprecated_methods(self):  # permitir SHA1 (lo exige el SII)
            pass
    from cryptography.hazmat.primitives import serialization
    key_pem = key.private_bytes(serialization.Encoding.PEM,
                                serialization.PrivateFormat.TraditionalOpenSSL,
                                serialization.NoEncryption())
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    signer = _S(method=methods.enveloped, signature_algorithm="rsa-sha1",
                digest_algorithm="sha1", c14n_algorithm="http://www.w3.org/TR/2001/REC-xml-c14n-20010315")
    signed = signer.sign(root, key=key_pem, cert=cert_pem)
    signed_xml = etree.tostring(signed, encoding="ISO-8859-1").decode("ISO-8859-1")

    url = DTEWS[ambiente]["token"].replace("?WSDL", "")
    soap = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/">'
        '<SOAP-ENV:Body><getToken><pszXml><![CDATA[' + signed_xml +
        ']]></pszXml></getToken></SOAP-ENV:Body></SOAP-ENV:Envelope>'
    )
    r = requests.post(url, data=soap.encode("ISO-8859-1"),
                      headers={"Content-Type": "text/xml; charset=utf-8",
                               "User-Agent": USER_AGENT}, timeout=TIMEOUT)
    m = re.search(r"<TOKEN>([^<]+)</TOKEN>", r.text) or \
        re.search(r"&lt;TOKEN&gt;([^&]+)&lt;/TOKEN&gt;", r.text)
    if not m:
        raise SIIError(f"No se obtuvo token del WS clásico: {r.text[:300]}")
    return m.group(1)


def obtener_token_dte(pfx_bytes: bytes, password: str,
                      ambiente: str = "certificacion") -> str:
    """Token para enviar DTE tradicionales (NC, facturas) vía DTEUpload.
    Es el token del WS clásico DTEWS (mismo que usa getEstDte)."""
    return _dtews_obtener_token(pfx_bytes, password, ambiente)


def enviar_dte(
    envio_xml: bytes,
    token: str,
    rut_emisor: str,
    rut_envia: str,
    ambiente: str = "certificacion",
) -> dict:
    """Envía un sobre EnvioDTE (NC tipo 61, facturas, etc.) al SII por DTEUpload.

    A diferencia de enviar_boletas (que va a pangal/REST), esto va a
    maullin/palena /cgi_dte/UPL/DTEUpload (POST multipart, igual que el browser).
    La respuesta es XML <RECEPCIONDTE> con <STATUS> y <TRACKID>.

    Args:
        envio_xml: el sobre EnvioDTE ya firmado
        token: token del WS clásico (obtener_token_dte)
        rut_emisor: RUT empresa (ej '76922862-4')
        rut_envia: RUT del representante que firma (ej '18849272-K')
        ambiente: certificacion o produccion

    Returns:
        dict {ok, track_id, status_sii, respuesta_cruda, status_http}
    """
    url = DTEWS[ambiente]["upload"]
    host = DTEWS[ambiente]["upload_host"]

    def _split_rut(rut):
        rut = rut.replace(".", "").replace("-", "")
        return rut[:-1], rut[-1]
    rut_e_num, rut_e_dv = _split_rut(rut_emisor)
    rut_env_num, rut_env_dv = _split_rut(rut_envia)

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "image/gif, image/x-xbitmap, image/jpeg, image/pjpeg, */*",
        "Cookie": f"TOKEN={token}",
        "Cache-Control": "no-cache",
    }
    # multipart/form-data: el SII espera 'archivo' con tipo text/xml ISO-8859-1.
    files = {
        "archivo": ("EnvioDTE.xml", envio_xml, "text/xml"),
    }
    data = {
        "rutSender": rut_env_num, "dvSender": rut_env_dv,
        "rutCompany": rut_e_num, "dvCompany": rut_e_dv,
    }
    try:
        resp = requests.post(url, headers=headers, files=files, data=data, timeout=TIMEOUT)
    except Exception as e:
        raise SIIError(f"No se pudo conectar a {url}: {e}")

    texto = resp.text
    if "NO ESTA AUTENTICADO" in texto.upper():
        return {"ok": False, "error": "Token vencido o inválido (NO ESTA AUTENTICADO)",
                "respuesta_cruda": texto[:600], "status_http": resp.status_code}

    # Respuesta XML <RECEPCIONDTE> con <STATUS> y <TRACKID>
    m_status = re.search(r"<STATUS>\s*([0-9]+)\s*</STATUS>", texto, re.IGNORECASE)
    m_track = re.search(r"<TRACKID>\s*([0-9]+)\s*</TRACKID>", texto, re.IGNORECASE)
    status_sii = m_status.group(1) if m_status else None
    track_id = m_track.group(1) if m_track else None

    # STATUS 0 = recepción OK
    ok = (status_sii == "0") and (track_id is not None) and resp.status_code in (200, 201)

    # Extraer detalle de error si hay (STATUS 7 = schema, 8 = firma)
    errores = re.findall(r"<ERROR>([^<]+)</ERROR>", texto, re.IGNORECASE)

    return {
        "ok": ok,
        "track_id": track_id,
        "status_sii": status_sii,
        "errores": errores,
        "respuesta_cruda": texto[:600],
        "status_http": resp.status_code,
    }


def consultar_estado_dte(
    pfx_bytes: bytes, password: str,
    rut_consultante: str, rut_emisor: str, rut_receptor: str,
    tipo_dte: int, folio: int, fecha_emision: str, monto_total: int,
    ambiente: str = "certificacion",
    token: str = None,
) -> dict:
    """Consulta el estado de un DTE por sus DATOS (getEstDte, SOAP, vía pública).

    Esta es la consulta robusta y documentada por el SII. Sirve tanto para que
    el emisor confirme la aceptación como para que el cliente verifique su boleta.

    Args:
        pfx_bytes, password: certificado para autenticar (si no se pasa token).
        rut_consultante: RUT de quien consulta (ej. el emisor) "12345678-9".
        rut_emisor: RUT de la empresa emisora "76922862-4".
        rut_receptor: RUT del receptor (boletas: "66666666-6" consumidor final).
        tipo_dte: 39 (boleta) / 41 (exenta) / etc.
        folio, fecha_emision (YYYY-MM-DD), monto_total.
        ambiente: "certificacion" | "produccion".
        token: opcional; si no se entrega, se obtiene automáticamente.

    Returns:
        dict {ok, estado, glosa, aceptado, respuesta_cruda} donde:
          estado="DOK" → recibido y datos OK (✅ aceptado/registrado)
          estado="DNK" → recibido pero datos NO coinciden
          estado="FAU" → no recibido por el SII
          estado="FAN" → no autorizado / "FNA"
          estado="ANC"/"FAN" → anulado, etc.
        aceptado=True solo si estado=="DOK".
    """
    def _split(rut):
        rut = rut.replace(".", "").replace("-", "")
        return rut[:-1], rut[-1]

    if token is None:
        token = _dtews_obtener_token(pfx_bytes, password, ambiente)

    rc, dvc = _split(rut_consultante)
    re_, dvr_e = _split(rut_emisor)
    rr, dvr_r = _split(rut_receptor)
    # El monto va sin separadores; la fecha en formato DD-MM-YYYY para getEstDte
    y, m, d = fecha_emision.split("-")
    fecha_sii = f"{d}-{m}-{y}"

    url = DTEWS[ambiente]["query"]
    soap = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xmlns:xsd="http://www.w3.org/2001/XMLSchema">'
        '<SOAP-ENV:Body>'
        '<m:getEstDte xmlns:m="http://maullin.sii.cl/DTEWS/QueryEstDte.jws">'
        f'<RutConsultante xsi:type="xsd:string">{rc}</RutConsultante>'
        f'<DvConsultante xsi:type="xsd:string">{dvc}</DvConsultante>'
        f'<RutCompania xsi:type="xsd:string">{re_}</RutCompania>'
        f'<DvCompania xsi:type="xsd:string">{dvr_e}</DvCompania>'
        f'<RutReceptor xsi:type="xsd:string">{rr}</RutReceptor>'
        f'<DvReceptor xsi:type="xsd:string">{dvr_r}</DvReceptor>'
        f'<TipoDte xsi:type="xsd:string">{tipo_dte}</TipoDte>'
        f'<FolioDte xsi:type="xsd:string">{folio}</FolioDte>'
        f'<FechaEmisionDte xsi:type="xsd:string">{fecha_sii}</FechaEmisionDte>'
        f'<MontoDte xsi:type="xsd:string">{int(monto_total)}</MontoDte>'
        f'<Token xsi:type="xsd:string">{token}</Token>'
        '</m:getEstDte>'
        '</SOAP-ENV:Body></SOAP-ENV:Envelope>'
    )
    try:
        r = requests.post(url, data=soap.encode("ISO-8859-1"),
                          headers={"Content-Type": "text/xml; charset=utf-8",
                                   "User-Agent": USER_AGENT, "SOAPAction": ""},
                          timeout=TIMEOUT)
    except Exception as e:
        raise SIIError(f"No se pudo conectar a QueryEstDte: {e}")

    texto = r.text
    # El estado viene como <ESTADO>XXX</ESTADO> (a veces escapado)
    m_estado = (re.search(r"<ESTADO>([^<]+)</ESTADO>", texto) or
                re.search(r"&lt;ESTADO&gt;([^&]+)&lt;/ESTADO&gt;", texto))
    m_glosa = (re.search(r"<GLOSA(?:_ESTADO)?>([^<]+)</GLOSA(?:_ESTADO)?>", texto) or
               re.search(r"&lt;GLOSA(?:_ESTADO)?&gt;([^&]+)&lt;/GLOSA", texto))
    estado = m_estado.group(1).strip() if m_estado else None
    glosa = m_glosa.group(1).strip() if m_glosa else None

    return {
        "ok": r.status_code == 200 and estado is not None,
        "estado": estado,
        "glosa": glosa,
        "aceptado": estado == "DOK",
        "status": r.status_code,
        "respuesta_cruda": texto[:1500],
    }
