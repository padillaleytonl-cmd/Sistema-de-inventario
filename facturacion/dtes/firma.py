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


def _c14n(elemento, quitar_ns_heredado: bool = True) -> bytes:
    """Canonicaliza un elemento XML según C14N inclusive.

    VERIFICADO byte a byte contra un EnvioDTE real de Lioren que el SII aceptó.

    quitar_ns_heredado=True (para <Documento>): quita el namespace SiiDte heredado
      antes de canonicalizar. Lioren firma cada Documento aislado, sin el namespace
      del sobre. Verificado: reproduce el DigestValue del DTE de Lioren.

    quitar_ns_heredado=False (para <SetDTE>): conserva el namespace pero quita los
      xmlns="" espurios que lxml inyecta en los hijos. Verificado: reproduce el
      DigestValue del SetDTE de Lioren.

    Sin esto, los digests NO coincidían con los del SII → "Error en Firma".
    """
    import re as _re
    serial = etree.tostring(elemento, encoding="unicode")
    if quitar_ns_heredado:
        serial = _re.sub(r'\sxmlns="http://www\.sii\.cl/SiiDte"', "", serial, count=1)
        serial = _re.sub(r'\sxmlns:xsi="http://www\.w3\.org/2001/XMLSchema-instance"', "", serial, count=1)
        serial = _re.sub(r'\sxsi:schemaLocation="[^"]*"', "", serial, count=1)
    reparsed = etree.fromstring(serial.encode("utf-8"))
    c = etree.tostring(reparsed, method="c14n", exclusive=False, with_comments=False)
    return c.replace(b' xmlns=""', b"")


def _b64_multilinea(b64: str, ancho: int = 76) -> str:
    """Quiebra una cadena base64 en líneas de 'ancho' caracteres.

    El instructivo técnico del SII (pág 19/23) exige que los campos base64
    del certificado y claves (X509Certificate, Modulus) se impriman a lo más
    76 caracteres por línea, o el envío puede ser rechazado con CHR-00002
    (Line too long). Los saltos en base64 se ignoran al decodificar.

    NOTA: NO se aplica al SignatureValue. Aunque el estándar lo permitiría,
    el validador del SII rechazó la firma al quebrar ese campo, así que se
    deja en una sola línea (su largo ~344 chars para RSA-2048 está bajo el
    límite de 4090 de todos modos).
    """
    return "\n".join(b64[i:i + ancho] for i in range(0, len(b64), ancho))


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
    cert_b64 = _b64_multilinea(cert_b64)
    mod_b64 = _b64_multilinea(mod_b64)

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
    signed_info_c14n = _c14n(signed_info_en_arbol, quitar_ns_heredado=False)
    firma = private_key.sign(signed_info_c14n, padding.PKCS1v15(), hashes.SHA1())
    signature_value = _b64_multilinea(base64.b64encode(firma).decode("ascii"))

    # 4. Poner el SignatureValue calculado en el árbol
    for e in signature_el.iter():
        if e.tag.endswith("}SignatureValue"):
            e.text = signature_value
            break

    # El SII (parser estricto) requiere la declaración con comillas DOBLES.
    # lxml genera comillas simples. Para documentos que se envían directo al SII
    # (como el RCOF), esto importa. Para boletas dentro del sobre no afecta,
    # porque _extraer_dte_interno quita la declaración de todos modos.
    cuerpo = etree.tostring(root, xml_declaration=False, encoding="ISO-8859-1")
    return b'<?xml version="1.0" encoding="ISO-8859-1"?>\n' + cuerpo


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
    cert_b64 = _b64_multilinea(cert_b64)
    mod_b64 = _b64_multilinea(mod_b64)

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

    set_c14n = _c14n(set_dte, quitar_ns_heredado=False)
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
    signed_info_c14n = _c14n(signed_info_en_arbol, quitar_ns_heredado=False)
    firma = private_key.sign(signed_info_c14n, padding.PKCS1v15(), hashes.SHA1())
    signature_value = _b64_multilinea(base64.b64encode(firma).decode("ascii"))

    for e in signature_el.iter():
        if e.tag.endswith("}SignatureValue"):
            e.text = signature_value
            break

    # El SII (parser estricto) requiere la declaración con comillas DOBLES.
    # lxml genera comillas simples, que el SII rechaza con CHR-00001.
    cuerpo = etree.tostring(root, xml_declaration=False, encoding="ISO-8859-1")
    return b'<?xml version="1.0" encoding="ISO-8859-1"?>\n' + cuerpo


def _firmar_id_en_arbol(root, elemento_id, private_key, cert_b64, mod_b64, exp_b64):
    """Firma (XMLDSig enveloped) el elemento con ID dado, YA dentro de su árbol.

    La <Signature> se inserta como hijo del elemento referenciado y el SignedInfo
    se canonicaliza EN CONTEXTO (heredando namespaces de los ancestros, como el
    xmlns:xsi del EnvioBOLETA). Así firma y validación del SII coinciden.

    Inserta la Signature como hermano siguiente del elemento (igual que Lioren:
    la firma del DTE va después de </Documento>, dentro del <DTE>).
    """
    # Buscar el elemento por ID y su padre
    elemento = None
    for el in root.iter():
        if el.get("ID") == elemento_id:
            elemento = el
            break
    if elemento is None:
        raise ValueError(f"No se encontró elemento ID='{elemento_id}' en el árbol")
    padre = elemento.getparent()

    # 1. Digest del elemento referenciado (en su contexto)
    digest_value = _digest_sha1_b64(_c14n(elemento))

    # 2. Construir Signature con SignatureValue vacío
    signature_xml = (
        f'<Signature xmlns="{NS_DSIG}">'
        f'<SignedInfo>'
        f'<CanonicalizationMethod Algorithm="{C14N_METHOD}"/>'
        f'<SignatureMethod Algorithm="http://www.w3.org/2000/09/xmldsig#rsa-sha1"/>'
        f'<Reference URI="#{elemento_id}">'
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
    # Insertar como hijo del padre, justo después del elemento referenciado
    idx = list(padre).index(elemento)
    padre.insert(idx + 1, signature_el)

    # 3. Firmar el SignedInfo del Documento. quitar_ns_heredado=True: verificado
    #    contra el DTE individual de Lioren — su SignedInfo valida sólo así.
    signed_info = None
    for e in signature_el.iter():
        if e.tag.endswith("}SignedInfo"):
            signed_info = e
            break
    firma = private_key.sign(_c14n(signed_info, quitar_ns_heredado=True), padding.PKCS1v15(), hashes.SHA1())
    signature_value = _b64_multilinea(base64.b64encode(firma).decode("ascii"))
    for e in signature_el.iter():
        if e.tag.endswith("}SignatureValue"):
            e.text = signature_value
            break


def firmar_envio_completo(envio_sin_firmar_xml: bytes, pfx_bytes: bytes, password: str,
                          set_dte_id: str, documento_ids: list) -> bytes:
    """Firma un EnvioBOLETA completo de la forma que el SII valida:

    1. Arma el árbol del sobre (con los DTE SIN firmar dentro).
    2. Firma cada <Documento> EN CONTEXTO (dentro del sobre, viendo el xmlns:xsi).
    3. Firma el <SetDTE> en contexto.

    Esto resuelve el "Error en Firma": el SignedInfo de cada firma se canonicaliza
    con los mismos namespaces heredados que el SII ve al validar.

    Args:
        envio_sin_firmar_xml: EnvioBOLETA con DTEs sin firma de Documento
        pfx_bytes, password: certificado
        set_dte_id: ID del SetDTE
        documento_ids: lista de IDs de los <Documento> a firmar (ej ['F11T39', ...])

    Returns:
        bytes del EnvioBOLETA totalmente firmado.
    """
    private_key, cert, cert_b64, mod_b64, exp_b64 = cargar_pfx(pfx_bytes, password)
    cert_b64 = _b64_multilinea(cert_b64)
    mod_b64 = _b64_multilinea(mod_b64)

    parser = etree.XMLParser(remove_blank_text=False)
    root = etree.fromstring(envio_sin_firmar_xml, parser)

    # 1. Firmar cada documento EN CONTEXTO
    for doc_id in documento_ids:
        _firmar_id_en_arbol(root, doc_id, private_key, cert_b64, mod_b64, exp_b64)

    # 2. Firmar el SetDTE EN CONTEXTO (la firma va como hijo del EnvioBOLETA,
    #    después del SetDTE)
    set_dte = None
    for el in root.iter():
        if el.get("ID") == set_dte_id:
            set_dte = el
            break
    if set_dte is None:
        raise ValueError(f"No se encontró SetDTE con ID='{set_dte_id}'")
    digest_set = _digest_sha1_b64(_c14n(set_dte, quitar_ns_heredado=False))
    sig_set_xml = (
        f'<Signature xmlns="{NS_DSIG}">'
        f'<SignedInfo>'
        f'<CanonicalizationMethod Algorithm="{C14N_METHOD}"/>'
        f'<SignatureMethod Algorithm="http://www.w3.org/2000/09/xmldsig#rsa-sha1"/>'
        f'<Reference URI="#{set_dte_id}">'
        f'<Transforms>'
        f'<Transform Algorithm="http://www.w3.org/2000/09/xmldsig#enveloped-signature"/>'
        f'</Transforms>'
        f'<DigestMethod Algorithm="http://www.w3.org/2000/09/xmldsig#sha1"/>'
        f'<DigestValue>{digest_set}</DigestValue>'
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
    sig_set = etree.fromstring(sig_set_xml.encode("utf-8"))
    root.append(sig_set)  # hermano del SetDTE, dentro de EnvioBOLETA
    si_set = None
    for e in sig_set.iter():
        if e.tag.endswith("}SignedInfo"):
            si_set = e
            break
    firma_set = private_key.sign(_c14n(si_set, quitar_ns_heredado=False), padding.PKCS1v15(), hashes.SHA1())
    for e in sig_set.iter():
        if e.tag.endswith("}SignatureValue"):
            e.text = _b64_multilinea(base64.b64encode(firma_set).decode("ascii"))
            break

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
        # El SetDTE conserva su namespace (quitar_ns_heredado=False); el Documento se
        # canonicaliza sin el namespace heredado (True), como hace el SII/Lioren.
        es_setdte = referenciado.tag.endswith("}SetDTE") or referenciado.tag == "SetDTE"
        digest_real = _digest_sha1_b64(_c14n(referenciado, quitar_ns_heredado=not es_setdte))
        digest_en_firma = digest_el.text.strip()
        if digest_real != digest_en_firma:
            return {"ok": False,
                    "mensaje": f"DigestValue no coincide. Real={digest_real[:16]}... Firma={digest_en_firma[:16]}..."}

        # ── PASO 2: verificar la firma de SignedInfo ──
        from cryptography import x509
        cert_der = base64.b64decode(cert_el.text.strip())
        cert = x509.load_der_x509_certificate(cert_der)
        pub_key = cert.public_key()

        signed_info_c14n = _c14n(signed_info, quitar_ns_heredado=not es_setdte)
        firma = base64.b64decode(sig_value_el.text.strip().replace("\n", "").replace(" ", ""))
        pub_key.verify(firma, signed_info_c14n, padding.PKCS1v15(), hashes.SHA1())

        return {"ok": True, "mensaje": "Firma XMLDSig válida (digest + firma verificados)"}
    except Exception as e:
        return {"ok": False, "mensaje": f"Firma inválida: {str(e)[:200]}"}
