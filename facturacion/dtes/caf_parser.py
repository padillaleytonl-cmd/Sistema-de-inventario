"""
facturacion/dtes/caf_parser.py
─────────────────────────────────────────────────────────────
Parser del XML del CAF (Código de Autorización de Folios).

El CAF contiene:
  • CAF/DA       : Datos de Autorización (RUT, tipo DTE, rango, FA, RSAPK)
  • CAF/FRMA     : Firma del SII sobre /CAF/DA (incluida tal cual al timbrar)
  • RSASK        : Clave PRIVADA RSA que usa el contribuyente para timbrar
  • RSAPUBK      : Clave PÚBLICA RSA correspondiente (debe coincidir con RSAPK)
"""
from __future__ import annotations
import re
from dataclasses import dataclass
from typing import Optional
from lxml import etree


@dataclass
class CAFParsed:
    """Datos extraídos de un CAF."""
    rut_emisor: str            # ej: "76922862-4"
    razon_social: str          # ej: "GRUPO PH SPA"
    tipo_dte: int              # ej: 39, 33, 61
    rango_desde: int           # ej: 11
    rango_hasta: int           # ej: 60
    fecha_autorizacion: str    # ej: "2026-05-17"
    idk: int                   # ej: 100 (certificación)
    rsa_publica_modulo: str    # base64 sin saltos
    rsa_publica_exponente: str # base64 sin saltos
    firma_sii: str             # base64, va dentro del TED como <FRMT>
    rsa_privada_pem: str       # PEM completo de la llave privada del contribuyente
    rsa_publica_pem: str       # PEM completo de la llave pública
    da_xml: bytes              # XML EXACTO del bloque <DA>...</DA> (para timbrar el TED)
    caf_xml_completo: bytes    # XML completo del CAF (para incluir en el TED)


def parsear_caf_xml(caf_xml: bytes | str) -> CAFParsed:
    """Parsea un XML de CAF y devuelve los datos relevantes para emitir DTEs.
    
    Args:
        caf_xml: bytes o str con el XML completo del CAF (descargado del SII).
    
    Returns:
        CAFParsed con todos los datos necesarios.
    
    Raises:
        ValueError: si el XML no es válido o falta algún campo requerido.
    """
    if isinstance(caf_xml, str):
        caf_xml = caf_xml.encode('utf-8')
    
    # Parseamos sin namespaces (los CAFs del SII no usan namespaces)
    parser = etree.XMLParser(remove_blank_text=False, recover=False)
    try:
        root = etree.fromstring(caf_xml, parser)
    except etree.XMLSyntaxError as e:
        raise ValueError(f"CAF XML inválido: {e}") from e
    
    # El root puede ser <AUTORIZACION> o <CAF> directamente
    if root.tag == 'AUTORIZACION':
        caf_node = root.find('CAF')
        if caf_node is None:
            raise ValueError("CAF inválido: falta nodo <CAF> dentro de <AUTORIZACION>")
        rsa_priv_node = root.find('RSASK')
        rsa_pub_node = root.find('RSAPUBK')
    elif root.tag == 'CAF':
        caf_node = root
        rsa_priv_node = None  # No siempre vienen, pero los CAFs nuevos del SII sí
        rsa_pub_node = None
    else:
        raise ValueError(f"CAF inválido: tag raíz inesperado '{root.tag}'")
    
    da_node = caf_node.find('DA')
    if da_node is None:
        raise ValueError("CAF inválido: falta nodo <DA>")
    
    frma_node = caf_node.find('FRMA')
    if frma_node is None or not frma_node.text:
        raise ValueError("CAF inválido: falta nodo <FRMA> (firma SII)")
    
    def _txt(node, tag):
        sub = node.find(tag)
        if sub is None or sub.text is None:
            raise ValueError(f"CAF inválido: falta <{tag}>")
        return sub.text.strip()
    
    rut_emisor = _txt(da_node, 'RE')
    razon_social = _txt(da_node, 'RS')
    tipo_dte = int(_txt(da_node, 'TD'))
    
    rng = da_node.find('RNG')
    if rng is None:
        raise ValueError("CAF inválido: falta <RNG>")
    rango_desde = int(_txt(rng, 'D'))
    rango_hasta = int(_txt(rng, 'H'))
    
    fecha_autorizacion = _txt(da_node, 'FA')
    idk = int(_txt(da_node, 'IDK'))
    
    rsapk_node = da_node.find('RSAPK')
    if rsapk_node is None:
        raise ValueError("CAF inválido: falta <RSAPK> (clave pública del SII)")
    rsa_pub_mod = _txt(rsapk_node, 'M')
    rsa_pub_exp = _txt(rsapk_node, 'E')
    
    firma_sii = frma_node.text.strip().replace('\n', '').replace('\r', '').replace(' ', '')
    
    # Llave privada/pública en PEM (parte del XML del CAF)
    if rsa_priv_node is None:
        rsa_priv_node = root.find('RSASK')
    if rsa_pub_node is None:
        rsa_pub_node = root.find('RSAPUBK')
    
    rsa_priv_pem = (rsa_priv_node.text or '').strip() if rsa_priv_node is not None else ''
    rsa_pub_pem = (rsa_pub_node.text or '').strip() if rsa_pub_node is not None else ''
    
    if not rsa_priv_pem:
        raise ValueError("CAF inválido: falta <RSASK> (clave privada del contribuyente)")
    
    # DA serializado EXACTO para timbrar (sin pretty-print, sin re-canonicalización)
    # Se debe usar el contenido raw entre <DA>...</DA> tal como vino en el CAF
    # extraemos del XML original usando regex (más confiable que serializar lxml)
    caf_xml_str = caf_xml.decode('utf-8', errors='replace')
    m = re.search(r'(<DA>.*?</DA>)', caf_xml_str, re.DOTALL)
    if not m:
        raise ValueError("CAF inválido: no se pudo extraer <DA> exacto")
    da_xml_exact = m.group(1).encode('utf-8')
    
    return CAFParsed(
        rut_emisor=rut_emisor,
        razon_social=razon_social,
        tipo_dte=tipo_dte,
        rango_desde=rango_desde,
        rango_hasta=rango_hasta,
        fecha_autorizacion=fecha_autorizacion,
        idk=idk,
        rsa_publica_modulo=rsa_pub_mod,
        rsa_publica_exponente=rsa_pub_exp,
        firma_sii=firma_sii,
        rsa_privada_pem=rsa_priv_pem,
        rsa_publica_pem=rsa_pub_pem,
        da_xml=da_xml_exact,
        caf_xml_completo=caf_xml,
    )


def caf_node_para_ted(caf_parsed: CAFParsed) -> bytes:
    """Devuelve el bloque <CAF version="1.0">...</CAF> tal como debe ir
    incrustado dentro del <TED> de un DTE.
    
    Importante: el bloque CAF dentro del TED es EXACTAMENTE el que vino del SII,
    incluyendo <DA>, <FRMA>, sin la sección <RSASK>/<RSAPUBK> (esas son privadas).
    """
    caf_xml_str = caf_parsed.caf_xml_completo.decode('utf-8', errors='replace')
    
    # Buscar el bloque <CAF ...>...</CAF> (con atributos como version="1.0")
    m = re.search(r'(<CAF\s+[^>]*?>.*?</CAF>)', caf_xml_str, re.DOTALL)
    if not m:
        # Fallback: sin atributos
        m = re.search(r'(<CAF>.*?</CAF>)', caf_xml_str, re.DOTALL)
        if not m:
            raise ValueError("No se encontró bloque <CAF> en el XML")
    
    return m.group(1).encode('utf-8')
