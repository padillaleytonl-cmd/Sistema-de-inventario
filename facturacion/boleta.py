"""
facturacion/dtes/boleta.py
─────────────────────────────────────────────────────────────
Generador del XML de Boleta Electrónica (DTE tipo 39).

Estructura simplificada del DTE:
  <DTE version="1.0">
    <Documento ID="F11T39">
      <Encabezado>
        <IdDoc>...</IdDoc>            (tipo, folio, fecha)
        <Emisor>...</Emisor>           (RUT, razón social, giro, sucursal)
        <Receptor>...</Receptor>       (RUT, razón social — opcional para boleta)
        <Totales>...</Totales>         (MntNeto, IVA, MntTotal)
      </Encabezado>
      <Detalle>...</Detalle>           (1 a N items)
      <Detalle>...</Detalle>
      <Referencia>...</Referencia>     (opcional - obligatorio en Set de Pruebas)
      <TED>...</TED>                   (timbre)
      <TmstFirma>...</TmstFirma>
    </Documento>
  </DTE>
"""
from __future__ import annotations
import math
from datetime import datetime
from typing import List, Dict, Optional

from .caf_parser import CAFParsed
from .ted import construir_ted


IVA_PORCENTAJE = 19  # IVA Chile


def _redondear_clp(valor: float) -> int:
    """Redondea al peso entero (sin decimales). IVA chileno se calcula con int."""
    return int(round(valor))


def _escape_xml(s: str) -> str:
    """Escapa caracteres especiales para XML."""
    if s is None:
        return ''
    s = str(s)
    return (s.replace('&', '&amp;')
             .replace('<', '&lt;')
             .replace('>', '&gt;')
             .replace('"', '&quot;')
             .replace("'", '&apos;'))


def calcular_totales_boleta(items: List[Dict]) -> Dict:
    """Calcula los totales de una boleta.
    
    IMPORTANTE: en la BOLETA chilena, los precios al cliente incluyen IVA.
    El cálculo es desde el TOTAL hacia atrás: Neto = Total / 1.19
    
    Args:
        items: lista de dicts con {nombre, cantidad, precio_unitario}.
               El precio_unitario es el PRECIO FINAL al consumidor (con IVA incluido).
    
    Returns:
        dict con:
          mnt_total: int    (suma de subtotales con IVA, redondeado al peso)
          mnt_neto: int     (mnt_total / 1.19, redondeado)
          mnt_iva: int      (mnt_total - mnt_neto)
          items_calc: lista con precio + subtotal calculado por item
    """
    items_calc = []
    mnt_total = 0
    
    for it in items:
        cantidad = float(it.get('cantidad', 1))
        precio_unit = float(it.get('precio_unitario', 0))
        subtotal = _redondear_clp(cantidad * precio_unit)
        items_calc.append({
            'nombre': it.get('nombre', '(sin descripción)'),
            'cantidad': cantidad,
            'precio_unitario': _redondear_clp(precio_unit),
            'subtotal': subtotal,
        })
        mnt_total += subtotal
    
    # En boleta, el TOTAL ya incluye IVA. Neto = Total / 1.19
    mnt_neto = _redondear_clp(mnt_total / (1 + IVA_PORCENTAJE / 100))
    mnt_iva = mnt_total - mnt_neto
    
    return {
        'mnt_total': mnt_total,
        'mnt_neto': mnt_neto,
        'mnt_iva': mnt_iva,
        'items_calc': items_calc,
    }


def generar_boleta_xml(
    caf: CAFParsed,
    folio: int,
    fecha_emision: str,                # 'YYYY-MM-DD'
    emisor: Dict,                       # {rut, razon_social, giro, dir_origen, cmna_origen, sucursal_sii?}
    items: List[Dict],                  # [{nombre, cantidad, precio_unitario}, ...]
    receptor: Optional[Dict] = None,   # {rut, razon_social} - opcional para boleta
    referencia: Optional[Dict] = None, # {cod_ref, razon_ref} para Set de Pruebas
    forma_pago: int = 1,                # 1=Contado, 2=Crédito, 3=Sin pago
    timestamp_firma: Optional[str] = None,
) -> Dict:
    """Genera el XML de una Boleta Electrónica (tipo 39).
    
    Returns:
        dict con:
          xml: bytes del XML del DTE (sin firma XMLDSig todavía)
          folio: folio usado
          totales: dict con mnt_total, mnt_neto, mnt_iva
          ted: bytes del TED ya firmado
    """
    if timestamp_firma is None:
        timestamp_firma = datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
    
    # 1. Calcular totales
    totales = calcular_totales_boleta(items)
    
    # 2. Construir TED (timbre)
    receptor_rut = (receptor or {}).get('rut', '66666666-6')
    receptor_rs = (receptor or {}).get('razon_social', 'Consumidor Final')
    
    ted = construir_ted(
        caf=caf,
        folio=folio,
        fecha_emision=fecha_emision,
        rut_receptor=receptor_rut,
        razon_social_receptor=receptor_rs,
        monto_total=totales['mnt_total'],
        detalle_primer_item=items[0].get('nombre', 'Item') if items else 'Item',
        timestamp_emision=timestamp_firma,
    )
    
    # 3. Construir bloque <Detalle> (uno por item)
    detalles_xml = ''
    for idx, it in enumerate(totales['items_calc'], start=1):
        detalles_xml += (
            f'<Detalle>'
            f'<NroLinDet>{idx}</NroLinDet>'
            f'<NmbItem>{_escape_xml(it["nombre"])}</NmbItem>'
            f'<QtyItem>{it["cantidad"]:.0f}</QtyItem>'
            f'<PrcItem>{it["precio_unitario"]}</PrcItem>'
            f'<MontoItem>{it["subtotal"]}</MontoItem>'
            f'</Detalle>'
        )
    
    # 4. Construir bloque <Referencia> (opcional)
    referencia_xml = ''
    if referencia:
        cod_ref = referencia.get('cod_ref', 'SET')
        razon_ref = referencia.get('razon_ref', '')
        referencia_xml = (
            f'<Referencia>'
            f'<NroLinRef>1</NroLinRef>'
            f'<TpoDocRef>SET</TpoDocRef>'
            f'<FolioRef>{folio}</FolioRef>'
            f'<FchRef>{fecha_emision}</FchRef>'
            f'<CodRef>{cod_ref}</CodRef>'
            f'<RazonRef>{_escape_xml(razon_ref)}</RazonRef>'
            f'</Referencia>'
        )
    
    # 5. Encabezado: Emisor
    emisor_xml = (
        f'<Emisor>'
        f'<RUTEmisor>{emisor["rut"]}</RUTEmisor>'
        f'<RznSocEmisor>{_escape_xml(emisor.get("razon_social", ""))}</RznSocEmisor>'
        f'<GiroEmisor>{_escape_xml(emisor.get("giro", "")[:80])}</GiroEmisor>'
        f'<DirOrigen>{_escape_xml(emisor.get("dir_origen", ""))}</DirOrigen>'
        f'<CmnaOrigen>{_escape_xml(emisor.get("cmna_origen", ""))}</CmnaOrigen>'
        f'</Emisor>'
    )
    
    # 6. Encabezado: Receptor (en boleta es opcional pero conviene poner consumidor final)
    receptor_xml = (
        f'<Receptor>'
        f'<RUTRecep>{receptor_rut}</RUTRecep>'
        f'<RznSocRecep>{_escape_xml(receptor_rs)}</RznSocRecep>'
        f'</Receptor>'
    )
    
    # 7. Encabezado: IdDoc
    iddoc_xml = (
        f'<IdDoc>'
        f'<TipoDTE>39</TipoDTE>'
        f'<Folio>{folio}</Folio>'
        f'<FchEmis>{fecha_emision}</FchEmis>'
        f'<IndServicio>3</IndServicio>'  # 3 = boleta de venta y servicios
        f'</IdDoc>'
    )
    
    # 8. Encabezado: Totales
    totales_xml = (
        f'<Totales>'
        f'<MntNeto>{totales["mnt_neto"]}</MntNeto>'
        f'<IVA>{totales["mnt_iva"]}</IVA>'
        f'<MntTotal>{totales["mnt_total"]}</MntTotal>'
        f'</Totales>'
    )
    
    # 9. Encabezado completo
    encabezado_xml = (
        f'<Encabezado>'
        f'{iddoc_xml}'
        f'{emisor_xml}'
        f'{receptor_xml}'
        f'{totales_xml}'
        f'</Encabezado>'
    )
    
    # 10. DTE completo (sin firma XMLDSig todavía - eso se agrega después)
    documento_id = f'F{folio}T39'
    dte_xml = (
        '<?xml version="1.0" encoding="ISO-8859-1"?>'
        '<DTE version="1.0" xmlns="http://www.sii.cl/SiiDte">'
        f'<Documento ID="{documento_id}">'
        f'{encabezado_xml}'
        f'{detalles_xml}'
        f'{referencia_xml}'
        f'{ted.decode("iso-8859-1")}'
        f'<TmstFirma>{timestamp_firma}</TmstFirma>'
        f'</Documento>'
        '</DTE>'
    )
    
    return {
        'xml': dte_xml.encode('iso-8859-1', errors='replace'),
        'folio': folio,
        'totales': {
            'mnt_total': totales['mnt_total'],
            'mnt_neto': totales['mnt_neto'],
            'mnt_iva': totales['mnt_iva'],
        },
        'ted': ted,
        'documento_id': documento_id,
    }
