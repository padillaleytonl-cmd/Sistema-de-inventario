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
        "estado_envio": "https://apicert.sii.cl/recursos/v1/boleta.electronica.envio/{trackid}",
        "host_envio": "pangal.sii.cl",
    },
    "produccion": {
        "semilla": "https://api.sii.cl/recursos/v1/boleta.electronica.semilla",
        "token":   "https://api.sii.cl/recursos/v1/boleta.electronica.token",
        "envio":   "https://rahue.sii.cl/recursos/v1/boleta.electronica.envio",
        "estado_envio": "https://api.sii.cl/recursos/v1/boleta.electronica.envio/{trackid}",
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

    Returns:
        bytes del XML <getToken> firmado, listo para enviar
    """
    if isinstance(password, str):
        password = password.encode("utf-8")
    private_key, certificate, _ = pkcs12.load_key_and_certificates(pfx_bytes, password)

    from cryptography.hazmat.primitives import serialization

    def _wrap64(s: str) -> str:
        """Parte un base64 en líneas de 64 caracteres, como el manual del SII.
        El parser legacy del SII (Java) espera el certificado en este formato
        PEM-like; con una sola línea larga reporta 'Certificate no existe'."""
        return "\n".join(s[i:i+64] for i in range(0, len(s), 64))

    cert_der = certificate.public_bytes(serialization.Encoding.DER)
    cert_b64 = _wrap64(base64.b64encode(cert_der).decode("ascii"))
    pub = certificate.public_key().public_numbers()
    mod_b64 = _wrap64(base64.b64encode(pub.n.to_bytes((pub.n.bit_length()+7)//8, "big")).decode("ascii"))
    exp_b64 = base64.b64encode(pub.e.to_bytes((pub.e.bit_length()+7)//8, "big")).decode("ascii")

    # XML base (la firma es enveloped sobre todo el documento, Reference URI="")
    doc_xml = f'<getToken><item><Semilla>{semilla}</Semilla></item></getToken>'
    root = etree.fromstring(doc_xml.encode("utf-8"))

    # Digest del documento completo (con la firma removida = el doc tal cual, URI="")
    doc_c14n = _c14n(root)
    digest_value = base64.b64encode(hashlib.sha1(doc_c14n).digest()).decode("ascii")

    # Construir el Signature COMPLETO (con SignatureValue vacío) e insertarlo
    # PRIMERO en el árbol, para canonicalizar SignedInfo en el mismo contexto
    # de namespaces tanto al firmar como al verificar (evita firma inválida).
    # NOTA: cert_b64 y mod_b64 van partidos en líneas de 64 chars (formato SII).
    # Esto NO afecta la firma porque KeyInfo no está dentro de SignedInfo.
    signed_info = (
        f'<SignedInfo>'
        f'<CanonicalizationMethod Algorithm="{C14N_METHOD}"/>'
        f'<SignatureMethod Algorithm="http://www.w3.org/2000/09/xmldsig#rsa-sha1"/>'
        f'<Reference URI="">'
        f'<Transforms><Transform Algorithm="http://www.w3.org/2000/09/xmldsig#enveloped-signature"/></Transforms>'
        f'<DigestMethod Algorithm="http://www.w3.org/2000/09/xmldsig#sha1"/>'
        f'<DigestValue>{digest_value}</DigestValue>'
        f'</Reference>'
        f'</SignedInfo>'
    )
    signature = (
        f'<Signature xmlns="{NS_DSIG}">'
        f'{signed_info}'
        f'<SignatureValue></SignatureValue>'
        f'<KeyInfo>'
        f'<KeyValue><RSAKeyValue><Modulus>{mod_b64}</Modulus><Exponent>{exp_b64}</Exponent></RSAKeyValue></KeyValue>'
        f'<X509Data><X509Certificate>{cert_b64}</X509Certificate></X509Data>'
        f'</KeyInfo>'
        f'</Signature>'
    )
    sig_el = etree.fromstring(signature.encode("utf-8"))
    root.append(sig_el)

    # Canonicalizar el SignedInfo YA DENTRO del árbol y firmar
    si_en_arbol = None
    for e in sig_el.iter():
        if e.tag.endswith("}SignedInfo"):
            si_en_arbol = e
            break
    si_c14n = _c14n(si_en_arbol)
    firma = private_key.sign(si_c14n, padding.PKCS1v15(), hashes.SHA1())
    sig_value = _wrap64(base64.b64encode(firma).decode("ascii"))

    # Poner el SignatureValue en el árbol
    for e in sig_el.iter():
        if e.tag.endswith("}SignatureValue"):
            e.text = sig_value
            break

    # IMPORTANTE: el SII usa un parser antiguo (Java/Axis) muy estricto.
    # Requiere la declaración XML EXACTA con comillas dobles y sin encoding,
    # tal como aparece en el manual oficial: <?xml version="1.0"?>
    cuerpo = etree.tostring(root, xml_declaration=False, encoding="UTF-8").decode("utf-8")
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
        "Accept": "application/json",
        "Cookie": f"TOKEN={token}",
        "Host": host,
    }
    # multipart/form-data con el archivo
    files = {
        "archivo": ("envio.xml", envio_xml, "application/xml"),
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

    # Buscar track id (puede venir en JSON o XML)
    track_id = None
    # JSON
    try:
        j = resp.json()
        track_id = j.get("trackid") or j.get("trackId") or j.get("track_id")
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

    url = ENDPOINTS[ambiente]["estado_envio"].format(trackid=track_id)
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Cookie": f"TOKEN={token}",
    }
    params = {"rutConsultante": rut_num, "dvConsultante": rut_dv}
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=TIMEOUT)
    except Exception as e:
        raise SIIError(f"No se pudo conectar a {url}: {e}")

    texto = resp.text
    if "NO ESTA AUTENTICADO" in texto.upper():
        return {"ok": False, "error": "Token vencido", "respuesta_cruda": texto[:500]}

    estado = None
    try:
        j = resp.json()
        estado = j.get("estado") or j.get("status")
    except Exception:
        pass

    return {
        "ok": resp.status_code == 200,
        "estado": estado,
        "respuesta_cruda": texto[:800],
        "status": resp.status_code,
    }
