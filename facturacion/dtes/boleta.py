"""
facturacion/dtes/boleta.py
─────────────────────────────────────────────────────────────
Generador del XML de Boleta Electrónica (DTE tipo 39).

Soporta:
  • Ítems afectos (con IVA)
  • Ítems exentos (IndExe=1)
  • Boletas mixtas (afecto + exento)
  • Unidad de medida (UnmdItem)
  • Cantidades con decimales
  • Referencia al Set de Pruebas (CodRef=SET, RazonRef=CASO-X)

Reglas de cálculo (boleta, precios INCLUYEN IVA):
  MntExe   = suma de MontoItem de ítems con IndExe=1
  bruto_af = suma de MontoItem de ítems con IndExe=0 (incluye IVA)
  MntNeto  = round(bruto_af / 1.19)
  IVA      = bruto_af - MntNeto
  MntTotal = MntNeto + IVA + MntExe   (== bruto_af + MntExe)
"""
from __future__ import annotations
from datetime import datetime
from typing import List, Dict, Optional

from .caf_parser import CAFParsed
from .ted import construir_ted


IVA_PORCENTAJE = 19  # IVA Chile


def _redondear_clp(valor: float) -> int:
    """Redondea al peso entero (sin decimales)."""
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


def _fmt_cantidad(qty: float) -> str:
    """Formatea cantidad: entera si no tiene decimales, sino hasta 6 decimales."""
    if qty == int(qty):
        return str(int(qty))
    # Hasta 6 decimales, quitando ceros sobrantes
    return f"{qty:.6f}".rstrip('0').rstrip('.')


def calcular_totales_boleta(items: List[Dict]) -> Dict:
    """Calcula los totales de una boleta (precios incluyen IVA).

    Args:
        items: lista de dicts con:
            {nombre, cantidad, precio_unitario, exento?(bool), unidad?(str)}
            precio_unitario = precio FINAL al consumidor (con IVA si es afecto).

    Returns:
        dict con mnt_neto, mnt_iva, mnt_exento, mnt_total, items_calc
    """
    items_calc = []
    bruto_afecto = 0   # suma de subtotales de ítems afectos (con IVA incluido)
    mnt_exento = 0     # suma de subtotales de ítems exentos

    for it in items:
        cantidad = float(it.get('cantidad', 1))
        precio_unit = float(it.get('precio_unitario', 0))
        es_exento = bool(it.get('exento', False))
        subtotal = _redondear_clp(cantidad * precio_unit)

        items_calc.append({
            'nombre': it.get('nombre', '(sin descripción)'),
            'cantidad': cantidad,
            'precio_unitario': _redondear_clp(precio_unit),
            'subtotal': subtotal,
            'exento': es_exento,
            'unidad': it.get('unidad'),  # ej: 'Kg', 'Un', None
        })

        if es_exento:
            mnt_exento += subtotal
        else:
            bruto_afecto += subtotal

    # Cálculo: la parte afecta incluye IVA, hay que separarlo
    mnt_neto = _redondear_clp(bruto_afecto / (1 + IVA_PORCENTAJE / 100)) if bruto_afecto > 0 else 0
    mnt_iva = bruto_afecto - mnt_neto if bruto_afecto > 0 else 0
    mnt_total = mnt_neto + mnt_iva + mnt_exento

    return {
        'mnt_neto': mnt_neto,
        'mnt_iva': mnt_iva,
        'mnt_exento': mnt_exento,
        'mnt_total': mnt_total,
        'tiene_afecto': bruto_afecto > 0,
        'tiene_exento': mnt_exento > 0,
        'items_calc': items_calc,
    }


def generar_boleta_xml(
    caf: CAFParsed,
    folio: int,
    fecha_emision: str,                # 'YYYY-MM-DD'
    emisor: Dict,                       # {rut, razon_social, giro, dir_origen, cmna_origen}
    items: List[Dict],                  # [{nombre, cantidad, precio_unitario, exento?, unidad?}, ...]
    receptor: Optional[Dict] = None,
    referencia: Optional[Dict] = None, # {cod_ref, razon_ref}
    timestamp_firma: Optional[str] = None,
) -> Dict:
    """Genera el XML de una Boleta Electrónica (tipo 39), sin firma XMLDSig.

    Returns:
        dict con xml(bytes), folio, totales, ted(bytes), documento_id
    """
    if timestamp_firma is None:
        timestamp_firma = datetime.now().strftime('%Y-%m-%dT%H:%M:%S')

    # 1. Totales
    totales = calcular_totales_boleta(items)

    # 2. TED (timbre) — el monto del TED es el MntTotal
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

    # 3. Detalles (uno por ítem)
    detalles_xml = ''
    for idx, it in enumerate(totales['items_calc'], start=1):
        linea = f'<Detalle>'
        linea += f'<NroLinDet>{idx}</NroLinDet>'
        # IndExe va ANTES de NmbItem según orden del schema
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

    # 4. Referencia (Set de Pruebas)
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

    # 5. Emisor
    emisor_xml = (
        f'<Emisor>'
        f'<RUTEmisor>{emisor["rut"]}</RUTEmisor>'
        f'<RznSocEmisor>{_escape_xml(emisor.get("razon_social", ""))}</RznSocEmisor>'
        f'<GiroEmisor>{_escape_xml(emisor.get("giro", "")[:80])}</GiroEmisor>'
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

    # 7. IdDoc
    iddoc_xml = (
        f'<IdDoc>'
        f'<TipoDTE>39</TipoDTE>'
        f'<Folio>{folio}</Folio>'
        f'<FchEmis>{fecha_emision}</FchEmis>'
        f'<IndServicio>3</IndServicio>'  # 3 = boleta de ventas y servicios
        f'</IdDoc>'
    )

    # 8. Totales — orden de campos según schema SII: MntNeto, MntExe, IVA, MntTotal
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
            'mnt_exento': totales['mnt_exento'],
        },
        'ted': ted,
        'documento_id': documento_id,
    }
