"""
facturacion/dtes/firma.py
─────────────────────────────────────────────────────────────
Firma XMLDSig de DTEs y del sobre EnvioBOLETA usando el certificado .pfx.

IMPORTANTE — distinguir las DOS firmas del sistema:
  • TED (FRMT): se firma con la clave PRIVADA del CAF (ver ted.py). NO es esto.
  • XMLDSig: se firma con el certificado .pfx del contribuyente. ESTO es lo de aquí.

El SII exige XMLDSig con restricciones específicas:
  • Algoritmo de firma: RSA-SHA1 (rsa-sha1)  ← obligatorio, no SHA256
  • Digest: SHA1
  • Canonicalización: C14N (http://www.w3.org/TR/2001/REC-xml-c14n-20010315)
  • La firma va como hijo directo del elemento firmado, tipo "enveloped"
  • Debe incluir <KeyInfo> con <X509Certificate> y <RSAKeyValue>

Se usa cryptography (carga del .pfx) + lxml (manipulación XML) + firma manual,
porque signxml a veces no produce exactamente el formato que el SII acepta.
Hacemos la firma manualmente para tener control total sobre el formato.
"""
from __future__ import annotations
import base64
import hashlib
from typing import Tuple

from lxml import etree
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


# Namespaces
NS_SII = "http://www.sii.cl/SiiDte"
NS_DSIG = "http://www.w3.org/2000/09/xmldsig#"
C14N_METHOD = "http://www.w3.org/TR/2001/REC-xml-c14n-20010315"


def cargar_pfx(pfx_bytes: bytes, password: str) -> Tuple:
    """Carga el .pfx y devuelve (private_key, certificate, cert_der_b64, modulus_b64, exponent_b64).

    Args:
        pfx_bytes: contenido binario del .pfx
        password: contraseña del .pfx

    Returns:
        tuple con la clave privada, el certificado, y datos para <KeyInfo>
    """
    if isinstance(password, str):
        password = password.encode("utf-8")

    private_key, certificate, _ = pkcs12.load_key_and_certificates(pfx_bytes, password)
    if private_key is None or certificate is None:
        raise ValueError("El .pfx no contiene clave privada o certificado válidos")

    # Certificado en DER → base64 (para <X509Certificate>)
    cert_der = certificate.public_bytes(serialization.Encoding.DER)
    cert_der_b64 = base64.b64encode(cert_der).decode("ascii")

    # Módulo y exponente de la clave pública (para <RSAKeyValue>)
    pub_numbers = certificate.public_key().public_numbers()
    n = pub_numbers.n
    e = pub_numbers.e
    modulus_b64 = base64.b64encode(n.to_bytes((n.bit_length() + 7) // 8, "big")).decode("ascii")
    exponent_b64 = base64.b64encode(e.to_bytes((e.bit_length() + 7) // 8, "big")).decode("ascii")

    return private_key, certificate, cert_der_b64, modulus_b64, exponent_b64


def _c14n(elemento) -> bytes:
    """Canonicaliza un elemento XML según C14N (el método que pide el SII)."""
    return etree.tostring(elemento, method="c14n", exclusive=False, with_comments=False)


def _digest_sha1_b64(data: bytes) -> str:
    """Calcula SHA1 de data y lo devuelve en base64."""
    h = hashlib.sha1(data).digest()
    return base64.b64encode(h).decode("ascii")


def firmar_documento(dte_xml: bytes, pfx_bytes: bytes, password: str, reference_uri: str) -> bytes:
    """Firma un nodo <Documento> dentro de un <DTE> con XMLDSig enveloped.

    Args:
        dte_xml: XML del DTE completo (con <Documento ID="...">)
        pfx_bytes: .pfx binario
        password: contraseña del .pfx
        reference_uri: el ID del Documento a firmar, ej "F11T39" (sin #)

    Returns:
        bytes del <DTE> ahora con su <Signature> agregada antes de </DTE>
    """
    private_key, cert, cert_b64, mod_b64, exp_b64 = cargar_pfx(pfx_bytes, password)

    # Parsear el DTE
    parser = etree.XMLParser(remove_blank_text=False)
    root = etree.fromstring(dte_xml, parser)

    # Encontrar el <Documento> a firmar (por su ID)
    documento = None
    for el in root.iter():
        if el.get("ID") == reference_uri:
            documento = el
            break
    if documento is None:
        raise ValueError(f"No se encontró elemento con ID='{reference_uri}' para firmar")

    # 1. Canonicalizar el <Documento> y calcular su digest.
    doc_c14n = _c14n(documento)
    digest_value = _digest_sha1_b64(doc_c14n)

    # 2. Construir el <Signature> COMPLETO con SignatureValue vacío e insertarlo
    #    en el árbol PRIMERO. Esto asegura que la canonicalización de SignedInfo
    #    en firma y en verificación sea idéntica (mismo contexto de namespaces).
    signature_xml = (
        f'<Signature xmlns="{NS_DSIG}">'
        f'<SignedInfo>'
        f'<CanonicalizationMethod Algorithm="{C14N_METHOD}"/>'
        f'<SignatureMethod Algorithm="http://www.w3.org/2000/09/xmldsig#rsa-sha1"/>'
        f'<Reference URI="#{reference_uri}">'
        f'<Transforms>'
        f'<Transform Algorithm="http://www.w3.org/2000/09/xmldsig#enveloped-signature"/>'
        f'</Transforms>'
        f'<DigestMethod Algorithm="http://www.w3.org/2000/09/xmldsig#sha1"/>'
        f'<DigestValue>{digest_value}</DigestValue>'
        f'</Reference>'
        f'</SignedInfo>'
        f'<SignatureValue></SignatureValue>'
        f'<KeyInfo>'
        f'<KeyValue><RSAKeyValue>'
        f'<Modulus>{mod_b64}</Modulus>'
        f'<Exponent>{exp_b64}</Exponent>'
        f'</RSAKeyValue></KeyValue>'
        f'<X509Data><X509Certificate>{cert_b64}</X509Certificate></X509Data>'
        f'</KeyInfo>'
        f'</Signature>'
    )
    signature_el = etree.fromstring(signature_xml.encode("utf-8"))
    root.append(signature_el)

    # 3. Canonicalizar el SignedInfo YA DENTRO del árbol y firmarlo (RSA-SHA1)
    signed_info_en_arbol = None
    for e in signature_el.iter():
        if e.tag.endswith("}SignedInfo"):
            signed_info_en_arbol = e
            break
    signed_info_c14n = _c14n(signed_info_en_arbol)
    firma = private_key.sign(signed_info_c14n, padding.PKCS1v15(), hashes.SHA1())
    signature_value = base64.b64encode(firma).decode("ascii")

    # 4. Poner el SignatureValue calculado en el árbol
    for e in signature_el.iter():
        if e.tag.endswith("}SignatureValue"):
            e.text = signature_value
            break

    return etree.tostring(root, xml_declaration=True, encoding="ISO-8859-1")


def firmar_envio(envio_xml: bytes, pfx_bytes: bytes, password: str, set_dte_id: str) -> bytes:
    """Firma el sobre <EnvioBOLETA> o <EnvioDTE> completo (firma el <SetDTE>).

    Args:
        envio_xml: XML del EnvioBOLETA/EnvioDTE con <SetDTE ID="...">
        pfx_bytes: .pfx binario
        password: contraseña
        set_dte_id: el ID del SetDTE a firmar

    Returns:
        bytes del envío con la <Signature> del sobre agregada.
    """
    private_key, cert, cert_b64, mod_b64, exp_b64 = cargar_pfx(pfx_bytes, password)

    parser = etree.XMLParser(remove_blank_text=False)
    root = etree.fromstring(envio_xml, parser)

    # Encontrar el <SetDTE>
    set_dte = None
    for el in root.iter():
        if el.get("ID") == set_dte_id:
            set_dte = el
            break
    if set_dte is None:
        raise ValueError(f"No se encontró SetDTE con ID='{set_dte_id}'")

    set_c14n = _c14n(set_dte)
    digest_value = _digest_sha1_b64(set_c14n)

    # Insertar Signature completo (con value vacío) primero, firmar desde el árbol
    signature_xml = (
        f'<Signature xmlns="{NS_DSIG}">'
        f'<SignedInfo>'
        f'<CanonicalizationMethod Algorithm="{C14N_METHOD}"/>'
        f'<SignatureMethod Algorithm="http://www.w3.org/2000/09/xmldsig#rsa-sha1"/>'
        f'<Reference URI="#{set_dte_id}">'
        f'<Transforms>'
        f'<Transform Algorithm="http://www.w3.org/2000/09/xmldsig#enveloped-signature"/>'
        f'</Transforms>'
        f'<DigestMethod Algorithm="http://www.w3.org/2000/09/xmldsig#sha1"/>'
        f'<DigestValue>{digest_value}</DigestValue>'
        f'</Reference>'
        f'</SignedInfo>'
        f'<SignatureValue></SignatureValue>'
        f'<KeyInfo>'
        f'<KeyValue><RSAKeyValue>'
        f'<Modulus>{mod_b64}</Modulus>'
        f'<Exponent>{exp_b64}</Exponent>'
        f'</RSAKeyValue></KeyValue>'
        f'<X509Data><X509Certificate>{cert_b64}</X509Certificate></X509Data>'
        f'</KeyInfo>'
        f'</Signature>'
    )
    signature_el = etree.fromstring(signature_xml.encode("utf-8"))
    root.append(signature_el)

    signed_info_en_arbol = None
    for e in signature_el.iter():
        if e.tag.endswith("}SignedInfo"):
            signed_info_en_arbol = e
            break
    signed_info_c14n = _c14n(signed_info_en_arbol)
    firma = private_key.sign(signed_info_c14n, padding.PKCS1v15(), hashes.SHA1())
    signature_value = base64.b64encode(firma).decode("ascii")

    for e in signature_el.iter():
        if e.tag.endswith("}SignatureValue"):
            e.text = signature_value
            break

    # El SII (parser estricto) requiere la declaración con comillas DOBLES.
    # lxml genera comillas simples, que el SII rechaza con CHR-00001.
    cuerpo = etree.tostring(root, xml_declaration=False, encoding="ISO-8859-1")
    return b'<?xml version="1.0" encoding="ISO-8859-1"?>\n' + cuerpo


def verificar_firma_propia(dte_firmado_xml: bytes) -> dict:
    """Auto-test: verifica que la firma XMLDSig sea válida contra el cert embebido.

    Verifica DOS cosas (como hace el SII):
      1. Que el DigestValue del <Reference> coincida con el digest real del elemento referenciado
      2. Que la firma de <SignedInfo> sea válida con la clave pública del certificado

    Returns:
        dict {ok: bool, mensaje: str}
    """
    try:
        root = etree.fromstring(dte_firmado_xml)

        def _find_directo(parent, local):
            """Busca SOLO en hijos directos del parent (no desciende al árbol completo)."""
            for e in parent:
                if e.tag.endswith("}" + local) or e.tag == local:
                    return e
            return None

        def _find_en_subarbol(parent, local):
            """Busca en el subárbol pero SIN entrar a otros <Signature> anidados."""
            for e in parent.iter():
                if e.tag.endswith("}" + local) or e.tag == local:
                    return e
            return None

        # La firma a verificar es la que es hijo DIRECTO del root
        # (la firma del envío/sobre, o la del DTE si es documento individual).
        sig = _find_directo(root, "Signature")
        if sig is None:
            # Fallback: primera firma del árbol (caso DTE individual donde root=DTE)
            for el in root.iter():
                if el.tag.endswith("}Signature"):
                    sig = el
                    break
        if sig is None:
            return {"ok": False, "mensaje": "No se encontró <Signature>"}

        # Buscar elementos DENTRO de este Signature específico (no de otros)
        signed_info = _find_en_subarbol(sig, "SignedInfo")
        sig_value_el = _find_en_subarbol(sig, "SignatureValue")
        cert_el = _find_en_subarbol(sig, "X509Certificate")
        ref_el = _find_en_subarbol(sig, "Reference")
        digest_el = _find_en_subarbol(sig, "DigestValue")
        if None in (signed_info, sig_value_el, cert_el, ref_el, digest_el):
            return {"ok": False, "mensaje": "Firma incompleta"}

        # ── PASO 1: verificar el DigestValue del elemento referenciado ──
        ref_uri = (ref_el.get("URI") or "").lstrip("#")
        referenciado = None
        for el in root.iter():
            if el.get("ID") == ref_uri:
                referenciado = el
                break
        if referenciado is None:
            return {"ok": False, "mensaje": f"No se encontró elemento ID='{ref_uri}'"}

        # Para "enveloped", hay que remover el Signature del subárbol antes de digerir.
        # Como el Signature es hermano (no hijo) del Documento, el subárbol del
        # referenciado no lo contiene → digest directo.
        digest_real = _digest_sha1_b64(_c14n(referenciado))
        digest_en_firma = digest_el.text.strip()
        if digest_real != digest_en_firma:
            return {"ok": False,
                    "mensaje": f"DigestValue no coincide. Real={digest_real[:16]}... Firma={digest_en_firma[:16]}..."}

        # ── PASO 2: verificar la firma de SignedInfo ──
        from cryptography import x509
        cert_der = base64.b64decode(cert_el.text.strip())
        cert = x509.load_der_x509_certificate(cert_der)
        pub_key = cert.public_key()

        signed_info_c14n = _c14n(signed_info)
        firma = base64.b64decode(sig_value_el.text.strip())
        pub_key.verify(firma, signed_info_c14n, padding.PKCS1v15(), hashes.SHA1())

        return {"ok": True, "mensaje": "Firma XMLDSig válida (digest + firma verificados)"}
    except Exception as e:
        return {"ok": False, "mensaje": f"Firma inválida: {str(e)[:200]}"}
