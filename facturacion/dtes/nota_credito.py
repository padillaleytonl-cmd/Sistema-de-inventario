"""
nota_credito.py — Generador de Notas de Crédito Electrónicas (tipo 61).

Una NC modifica un documento previo (factura o boleta). Para certificación de
boletas, el SII pide:
  • Anular la boleta CASO-4 (CodRef=1)
  • Rebajar 10% la boleta CASO-1 (CodRef=3)

Diferencias clave con una boleta (formato confirmado SuperFactura + SII):
  • TipoDTE = 61
  • <MntBruto>1</MntBruto> dentro de <IdDoc> → los montos del detalle incluyen IVA
    (porque las boletas se expresan en montos brutos)
  • <Referencia> obligatoria apuntando al documento original:
      TpoDocRef=39 (boleta), FolioRef, FchRef, CodRef (1=anula, 3=corrige montos)
  • Lleva su propio TED, firmado con el CAF de NC (tipo 61), NO el de boletas.

CodRef:
  1 = anula documento completo
  2 = corrige texto
  3 = corrige montos (rebaja)
"""

from __future__ import annotations
from datetime import datetime
from typing import List, Dict, Optional
from .caf_parser import CAFParsed
from .ted import construir_ted

IVA_PORCENTAJE = 19


def _escape_xml(s: str) -> str:
    if s is None:
        return ''
    s = str(s)
    return (s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
             .replace('"', '&quot;').replace("'", '&apos;'))


def _fmt_cantidad(qty: float) -> str:
    if qty == int(qty):
        return str(int(qty))
    return f"{qty:.6f}".rstrip('0').rstrip('.')


def _redondear_clp(valor: float) -> int:
    return int(round(valor))


def _calcular_totales_bruto(items: List[Dict]) -> Dict:
    """Calcula totales para una NC con montos BRUTOS (precios incluyen IVA).

    Cada item: {nombre, cantidad, precio_unitario, exento?(bool), unidad?(str)}

    Returns:
        dict con mnt_neto, mnt_iva, mnt_exento, mnt_total, items_calc,
        tiene_afecto, tiene_exento
    """
    bruto_afecto = 0
    mnt_exento = 0
    items_calc = []
    for it in items:
        cantidad = it['cantidad']
        precio = it['precio_unitario']
        subtotal = _redondear_clp(cantidad * precio)
        es_exento = bool(it.get('exento', False))
        items_calc.append({
            'nombre': it['nombre'], 'cantidad': cantidad,
            'precio_unitario': precio, 'subtotal': subtotal,
            'exento': es_exento, 'unidad': it.get('unidad'),
        })
        if es_exento:
            mnt_exento += subtotal
        else:
            bruto_afecto += subtotal

    mnt_neto = _redondear_clp(bruto_afecto / (1 + IVA_PORCENTAJE / 100)) if bruto_afecto > 0 else 0
    mnt_iva = bruto_afecto - mnt_neto if bruto_afecto > 0 else 0
    mnt_total = mnt_neto + mnt_iva + mnt_exento
    return {
        'mnt_neto': mnt_neto, 'mnt_iva': mnt_iva, 'mnt_exento': mnt_exento,
        'mnt_total': mnt_total, 'items_calc': items_calc,
        'tiene_afecto': bruto_afecto > 0, 'tiene_exento': mnt_exento > 0,
    }


def generar_nota_credito_xml(
    caf: CAFParsed,                    # CAF de NC (tipo 61)
    folio: int,
    fecha_emision: str,                # 'YYYY-MM-DD'
    emisor: Dict,                      # {rut, razon_social, giro, dir_origen, cmna_origen}
    items: List[Dict],                 # items de la NC (montos brutos)
    referencia: Dict,                  # {folio_ref, fecha_ref, cod_ref, razon_ref, tipo_doc_ref?}
    receptor: Optional[Dict] = None,
    timestamp_firma: Optional[str] = None,
) -> Dict:
    """Genera el XML de una Nota de Crédito Electrónica (tipo 61), sin firma XMLDSig.

    Args:
        caf: CAF de Nota de Crédito (tipo 61)
        folio: folio de la NC
        fecha_emision: 'YYYY-MM-DD'
        emisor: datos del emisor
        items: items de la NC con montos brutos (incluyen IVA)
        referencia: {folio_ref, fecha_ref, cod_ref(1/2/3), razon_ref, tipo_doc_ref(default 39)}
        receptor: datos del receptor (default consumidor final)
        timestamp_firma: timestamp

    Returns:
        dict con xml(bytes), folio, totales, ted(bytes), documento_id
    """
    if timestamp_firma is None:
        timestamp_firma = datetime.now().strftime('%Y-%m-%dT%H:%M:%S')

    # 1. Totales (en bruto)
    totales = _calcular_totales_bruto(items)

    # 2. TED (timbre) — firmado con el CAF de NC
    receptor_rut = (receptor or {}).get('rut', '66666666-6')
    receptor_rs = (receptor or {}).get('razon_social', 'Consumidor Final')
    ted = construir_ted(
        caf=caf, folio=folio, fecha_emision=fecha_emision,
        rut_receptor=receptor_rut, razon_social_receptor=receptor_rs,
        monto_total=totales['mnt_total'],
        detalle_primer_item=items[0].get('nombre', 'Item') if items else 'Item',
        timestamp_emision=timestamp_firma,
    )

    # 3. Detalles (montos brutos)
    detalles_xml = ''
    for idx, it in enumerate(totales['items_calc'], start=1):
        linea = f'<Detalle>'
        linea += f'<NroLinDet>{idx}</NroLinDet>'
        if it['exento']:
            linea += f'<IndExe>1</IndExe>'
        linea += f'<NmbItem>{_escape_xml(it["nombre"])}</NmbItem>'
        linea += f'<QtyItem>{_fmt_cantidad(it["cantidad"])}</QtyItem>'
        if it.get('unidad'):
            linea += f'<UnmdItem>{_escape_xml(it["unidad"])}</UnmdItem>'
        linea += f'<PrcItem>{it["precio_unitario"]}</PrcItem>'
        linea += f'<MontoItem>{it["subtotal"]}</MontoItem>'
        linea += f'</Detalle>'
        detalles_xml += linea

    # 4. Referencia (OBLIGATORIA — apunta al documento original)
    tipo_doc_ref = referencia.get('tipo_doc_ref', 39)  # 39 = boleta
    referencia_xml = (
        f'<Referencia>'
        f'<NroLinRef>1</NroLinRef>'
        f'<TpoDocRef>{tipo_doc_ref}</TpoDocRef>'
        f'<FolioRef>{referencia["folio_ref"]}</FolioRef>'
        f'<FchRef>{referencia["fecha_ref"]}</FchRef>'
        f'<CodRef>{referencia["cod_ref"]}</CodRef>'
        f'<RazonRef>{_escape_xml(referencia.get("razon_ref", ""))}</RazonRef>'
        f'</Referencia>'
    )

    # 5. Emisor
    emisor_xml = (
        f'<Emisor>'
        f'<RUTEmisor>{emisor["rut"]}</RUTEmisor>'
        f'<RznSoc>{_escape_xml(emisor.get("razon_social", ""))}</RznSoc>'
        f'<GiroEmis>{_escape_xml(emisor.get("giro", "")[:80])}</GiroEmis>'
        f'<DirOrigen>{_escape_xml(emisor.get("dir_origen", ""))}</DirOrigen>'
        f'<CmnaOrigen>{_escape_xml(emisor.get("cmna_origen", ""))}</CmnaOrigen>'
        f'</Emisor>'
    )

    # 6. Receptor
    receptor_xml = (
        f'<Receptor>'
        f'<RUTRecep>{receptor_rut}</RUTRecep>'
        f'<RznSocRecep>{_escape_xml(receptor_rs)}</RznSocRecep>'
        f'</Receptor>'
    )

    # 7. IdDoc — con MntBruto=1 (montos del detalle incluyen IVA)
    iddoc_xml = (
        f'<IdDoc>'
        f'<TipoDTE>61</TipoDTE>'
        f'<Folio>{folio}</Folio>'
        f'<FchEmis>{fecha_emision}</FchEmis>'
        f'<MntBruto>1</MntBruto>'
        f'</IdDoc>'
    )

    # 8. Totales — orden schema: MntNeto, MntExe, IVA, MntTotal
    tot = '<Totales>'
    if totales['tiene_afecto']:
        tot += f'<MntNeto>{totales["mnt_neto"]}</MntNeto>'
    if totales['tiene_exento']:
        tot += f'<MntExe>{totales["mnt_exento"]}</MntExe>'
    if totales['tiene_afecto']:
        tot += f'<IVA>{totales["mnt_iva"]}</IVA>'
    tot += f'<MntTotal>{totales["mnt_total"]}</MntTotal>'
    tot += '</Totales>'

    # 9. Encabezado
    encabezado_xml = f'<Encabezado>{iddoc_xml}{emisor_xml}{receptor_xml}{tot}</Encabezado>'

    # 10. DTE completo (sin XMLDSig todavía)
    # IMPORTANTE: el <DTE> NO declara namespace (igual que boletas). Lo hereda del
    # sobre EnvioDTE al insertarse. Con quitar_ns_heredado=True en la firma, el
    # digest del <Documento> es idéntico aislado y dentro del sobre. Declarar el
    # namespace aquí rompería la firma (mismo problema que DTE-3-505 en boletas).
    documento_id = f'F{folio}T61'
    dte_xml = (
        '<?xml version="1.0" encoding="ISO-8859-1"?>'
        '<DTE version="1.0">'
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
            'mnt_exento': totales['mnt_exento'],
        },
        'ted': ted,
        'documento_id': documento_id,
    }
