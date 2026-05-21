"""
facturacion/dtes/ted.py
─────────────────────────────────────────────────────────────
Generador del Timbre Electrónico Datada (TED).

El TED es el "código de barras" digital de cada DTE chileno. Contiene:
  • DD (datos del documento timbrado): RE, TD, F, FE, RR, RSR, MNT, IT1, TSTED
  • FRMT (firma del DD con la llave PRIVADA del CAF)
  • CAF embebido (con la firma del SII)

Proceso:
  1. Armar el bloque <DD>...</DD> con datos del documento
  2. Firmar el DD con SHA1withRSA usando la llave privada del CAF
  3. Resultado se pone en <FRMT algoritmo="SHA1withRSA">FIRMA_B64</FRMT>
  4. El TED completo es <TED version="1.0"><DD/><FRMT/></TED>

Referencia: SII Instructivo Técnico Timbre Electrónico.
"""
from __future__ import annotations
import base64
import re
from datetime import datetime

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from .caf_parser import CAFParsed, caf_node_para_ted


def _formatear_monto_entero(valor) -> str:
    """Los montos en el TED van como enteros, sin decimales ni separadores."""
    return str(int(round(float(valor))))


def _truncar(s: str, max_chars: int) -> str:
    """Trunca un string al máximo de caracteres permitido por el SII."""
    if not s:
        return ''
    s = str(s)
    return s[:max_chars]


def _formatear_rut_sin_puntos(rut: str) -> str:
    """RUT al formato del SII: NNNNNNNN-D (sin puntos)."""
    if not rut:
        return ''
    rut = rut.replace('.', '').replace(' ', '').upper()
    if '-' not in rut:
        # ej: '76922862K' → '76922862-K'
        rut = rut[:-1] + '-' + rut[-1]
    return rut


def construir_dd(
    caf: CAFParsed,
    folio: int,
    fecha_emision: str,
    rut_receptor: str,
    razon_social_receptor: str,
    monto_total: int,
    detalle_primer_item: str,
    timestamp_emision: str | None = None,
) -> bytes:
    """Construye el bloque <DD>...</DD> que se firmará con la llave privada del CAF.
    
    IMPORTANTE: el orden de los hijos importa. El SII valida el orden exacto.
    
    Args:
        caf: CAF parseado.
        folio: número de folio que se está usando.
        fecha_emision: 'YYYY-MM-DD'.
        rut_receptor: RUT del receptor (formato '12345678-9'). Para boletas a
                      consumidor final, se puede usar '66666666-6'.
        razon_social_receptor: descripción del receptor.
        monto_total: monto total con IVA, entero.
        detalle_primer_item: primera línea de descripción del documento.
        timestamp_emision: 'YYYY-MM-DDTHH:MM:SS' (ISO 8601 sin TZ). Si no se da,
                           usa el momento actual.
    
    Returns:
        bytes con el XML del <DD>, formato exacto requerido por SII.
    """
    if timestamp_emision is None:
        timestamp_emision = datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
    
    # Validación de formato fecha
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', fecha_emision):
        raise ValueError(f"fecha_emision debe ser YYYY-MM-DD, recibido: {fecha_emision}")
    
    rut_emisor = _formatear_rut_sin_puntos(caf.rut_emisor)
    rut_receptor = _formatear_rut_sin_puntos(rut_receptor) if rut_receptor else '66666666-6'
    
    # El detalle del primer item va truncado a 40 chars y sin caracteres especiales
    item_safe = _truncar(detalle_primer_item or 'Item', 40)
    
    # Razón social: máx 40 chars
    rsr_safe = _truncar(razon_social_receptor or 'Consumidor Final', 40)
    
    # Bloque DD: el orden y formato es CRÍTICO para que el SII valide.
    # NOTA: el atributo del CAF original (version="1.0" típicamente) lo extraemos tal cual
    caf_bloque = caf_node_para_ted(caf).decode('utf-8')
    
    dd_xml = (
        f'<DD>'
        f'<RE>{rut_emisor}</RE>'
        f'<TD>{caf.tipo_dte}</TD>'
        f'<F>{folio}</F>'
        f'<FE>{fecha_emision}</FE>'
        f'<RR>{rut_receptor}</RR>'
        f'<RSR>{rsr_safe}</RSR>'
        f'<MNT>{_formatear_monto_entero(monto_total)}</MNT>'
        f'<IT1>{item_safe}</IT1>'
        f'{caf_bloque}'
        f'<TSTED>{timestamp_emision}</TSTED>'
        f'</DD>'
    )
    return dd_xml.encode('iso-8859-1', errors='replace')


def firmar_dd_con_caf(dd_xml: bytes, caf: CAFParsed) -> str:
    """Firma el bloque <DD> con la llave privada del CAF.
    
    Algoritmo: SHA1withRSA (PKCS#1 v1.5).
    
    Returns:
        Firma en base64 (sin saltos de línea), lista para ir en <FRMT>.
    """
    # Cargar la llave privada (formato PEM viene dentro del CAF en <RSASK>)
    try:
        privada = serialization.load_pem_private_key(
            caf.rsa_privada_pem.encode('utf-8'),
            password=None,
        )
    except Exception as e:
        raise ValueError(f"No se pudo cargar la clave privada del CAF: {e}") from e
    
    # Firmar con SHA1 + PKCS#1 v1.5 (lo que pide el SII)
    firma_bytes = privada.sign(
        dd_xml,
        padding.PKCS1v15(),
        hashes.SHA1(),
    )
    
    # Base64 sin saltos
    return base64.b64encode(firma_bytes).decode('ascii')


def construir_ted(
    caf: CAFParsed,
    folio: int,
    fecha_emision: str,
    rut_receptor: str,
    razon_social_receptor: str,
    monto_total: int,
    detalle_primer_item: str,
    timestamp_emision: str | None = None,
) -> bytes:
    """Construye el TED completo (<TED>...</TED>) listo para insertar en el DTE.
    
    Returns:
        bytes con el XML del <TED> incluyendo DD y FRMT firmado.
    """
    dd_xml = construir_dd(
        caf=caf,
        folio=folio,
        fecha_emision=fecha_emision,
        rut_receptor=rut_receptor,
        razon_social_receptor=razon_social_receptor,
        monto_total=monto_total,
        detalle_primer_item=detalle_primer_item,
        timestamp_emision=timestamp_emision,
    )
    
    firma_b64 = firmar_dd_con_caf(dd_xml, caf)
    
    # TED completo
    ted_xml = (
        b'<TED version="1.0">'
        + dd_xml
        + b'<FRMT algoritmo="SHA1withRSA">'
        + firma_b64.encode('ascii')
        + b'</FRMT>'
        + b'</TED>'
    )
    return ted_xml
