"""
facturacion/dtes/factura_compra.py
─────────────────────────────────────────────────────────────
Generador del XML de Factura de Compra Electrónica (DTE tipo 46).

La factura de compra la emite el COMPRADOR (agente retenedor) cuando opera
con cambio de sujeto del IVA. Diferencias clave vs factura normal (33):
  • TipoDTE = 46
  • <Totales> incluye <ImptoReten> con la retención de IVA:
      <ImptoReten><TipoImp>15</TipoImp><TasaImp>19</TasaImp><MontoImp>...</MontoImp></ImptoReten>
    TipoImp 15 = IVA Retenido Total (retención total del IVA).
  • MntTotal = MntNeto + IVA - retención. En retención TOTAL, IVA = retención,
    por lo que MntTotal = MntNeto.

Reusa los helpers de factura.py (escape, formato, cálculo de totales).
"""
from __future__ import annotations
from datetime import datetime
from typing import List, Dict, Optional

from .caf_parser import CAFParsed
from .ted import construir_ted
from .factura import (
    _escape_xml, _fmt_cantidad, _calcular_totales_factura, IVA_PORCENTAJE,
)


def generar_factura_compra_xml(
    caf: CAFParsed,
    folio: int,
    fecha_emision: str,
    emisor: Dict,
    receptor: Dict,
    items: List[Dict],
    referencias: Optional[List[Dict]] = None,
    forma_pago: int = 2,
    fecha_vencimiento: Optional[str] = None,
    cod_imp_reten: int = 15,            # 15 = IVA Retenido Total
    timestamp_firma: Optional[str] = None,
) -> Dict:
    """Genera el XML de una Factura de Compra Electrónica (DTE 46) con
    retención total del IVA. SIN XMLDSig todavía (lo hereda del sobre).

    Returns: dict con xml(bytes), folio, totales, ted(bytes), documento_id.
    """
    tipo_dte = 46

    # 0. Totales (igual que factura afecta: items con precio neto)
    tot = _calcular_totales_factura(items, 0)
    items_calc = tot['items_calculados']
    mnt_neto = tot['mnt_neto']
    mnt_iva = tot['mnt_iva']
    mnt_exe = tot['mnt_exe']
    # Retención TOTAL del IVA: el monto retenido es el IVA completo
    mnt_reten = mnt_iva
    # MntTotal = Neto + IVA - retención (en retención total → Neto)
    mnt_total = mnt_neto + mnt_iva + mnt_exe - mnt_reten

    # 1. IdDoc
    iddoc_parts = [
        f'<TipoDTE>{tipo_dte}</TipoDTE>',
        f'<Folio>{folio}</Folio>',
        f'<FchEmis>{fecha_emision}</FchEmis>',
        f'<FmaPago>{forma_pago}</FmaPago>',
    ]
    if forma_pago == 2 and fecha_vencimiento:
        iddoc_parts.append(f'<FchVenc>{fecha_vencimiento}</FchVenc>')
    iddoc_xml = '<IdDoc>' + ''.join(iddoc_parts) + '</IdDoc>'

    # 2. Emisor (el comprador-retenedor). Acteco antes de DirOrigen.
    rut_e = str(emisor['rut']).replace('.', '').strip()
    emisor_parts = [
        f'<RUTEmisor>{rut_e}</RUTEmisor>',
        f'<RznSoc>{_escape_xml(emisor["razon_social"])}</RznSoc>',
        f'<GiroEmis>{_escape_xml(emisor.get("giro", "")[:80])}</GiroEmis>',
    ]
    actecos = emisor.get('acteco') or emisor.get('actecos') or [620100]
    if not isinstance(actecos, (list, tuple)):
        actecos = [actecos]
    for ac in actecos:
        ac_clean = str(ac).replace('.', '').strip()
        if ac_clean:
            emisor_parts.append(f'<Acteco>{ac_clean}</Acteco>')
    emisor_parts.extend([
        f'<DirOrigen>{_escape_xml(emisor["dir_origen"])}</DirOrigen>',
        f'<CmnaOrigen>{_escape_xml(emisor["cmna_origen"])}</CmnaOrigen>',
    ])
    emisor_xml = '<Emisor>' + ''.join(emisor_parts) + '</Emisor>'

    # 3. Receptor (el proveedor / vendedor)
    rut_r = str(receptor['rut']).replace('.', '').strip()
    rec_parts = [
        f'<RUTRecep>{rut_r}</RUTRecep>',
        f'<RznSocRecep>{_escape_xml(receptor["razon_social"][:100])}</RznSocRecep>',
        f'<GiroRecep>{_escape_xml(receptor.get("giro", "Sin giro")[:40])}</GiroRecep>',
    ]
    if receptor.get('contacto'):
        rec_parts.append(f'<Contacto>{_escape_xml(receptor["contacto"][:80])}</Contacto>')
    rec_parts.extend([
        f'<DirRecep>{_escape_xml(receptor.get("direccion", "Sin dirección")[:70])}</DirRecep>',
        f'<CmnaRecep>{_escape_xml(receptor.get("comuna", "Santiago")[:20])}</CmnaRecep>',
    ])
    receptor_xml = '<Receptor>' + ''.join(rec_parts) + '</Receptor>'

    # 4. Totales con ImptoReten (orden EXACTO del schema DTE_v10):
    #    MntNeto, [MntExe], TasaIVA, IVA, ImptoReten, MntTotal
    tot_parts = [f'<MntNeto>{mnt_neto}</MntNeto>']
    if mnt_exe > 0:
        tot_parts.append(f'<MntExe>{mnt_exe}</MntExe>')
    tot_parts.append(f'<TasaIVA>{IVA_PORCENTAJE}.00</TasaIVA>')
    tot_parts.append(f'<IVA>{mnt_iva}</IVA>')
    tot_parts.append(
        '<ImptoReten>'
        f'<TipoImp>{cod_imp_reten}</TipoImp>'
        f'<TasaImp>{IVA_PORCENTAJE}.00</TasaImp>'
        f'<MontoImp>{mnt_reten}</MontoImp>'
        '</ImptoReten>'
    )
    # Validación SII N°37: IVA = IVANoRet + IVARetParcial + IVARetTotal.
    # En retención TOTAL, IVANoRet = IVA - retención total = 0. El campo debe ir
    # presente para que el SII complete la validación (si no, HED-2-300).
    iva_no_ret = mnt_iva - mnt_reten
    tot_parts.append(f'<IVANoRet>{iva_no_ret}</IVANoRet>')
    tot_parts.append(f'<MntTotal>{mnt_total}</MntTotal>')
    totales_xml = '<Totales>' + ''.join(tot_parts) + '</Totales>'

    encabezado_xml = f'<Encabezado>{iddoc_xml}{emisor_xml}{receptor_xml}{totales_xml}</Encabezado>'

    # 5. Detalles (precios netos). El ejemplo oficial de FC46 NO lleva CodImpAdic
    #    en el detalle: la retención se valida solo por el ImptoReten + IVANoRet
    #    del encabezado (validación N°37 del SII).
    detalles_xml = ''
    for i, it in enumerate(items_calc, start=1):
        qty = float(it.get('cantidad', 1))
        prc = float(it.get('precio_unitario', 0))
        unidad = it.get('unidad', 'Un')
        nombre = it.get('nombre', 'Producto')[:80]
        linea_parts = [
            f'<NroLinDet>{i}</NroLinDet>',
            f'<NmbItem>{_escape_xml(nombre)}</NmbItem>',
            f'<QtyItem>{_fmt_cantidad(qty)}</QtyItem>',
            f'<UnmdItem>{_escape_xml(unidad)}</UnmdItem>',
            f'<PrcItem>{_fmt_cantidad(prc)}</PrcItem>',
            f'<MontoItem>{it["_monto_item"]}</MontoItem>',
        ]
        detalles_xml += '<Detalle>' + ''.join(linea_parts) + '</Detalle>'

    # 6. Referencias (para NC/ND y SET)
    referencia_xml = ''
    if referencias and isinstance(referencias, list):
        for i, ref in enumerate(referencias[:40], start=1):
            tpo = str(ref.get('tpo_doc_ref') or ref.get('tipo_doc_ref') or '46').strip()
            folio_ref = str(ref.get('folio_ref') or '').strip()
            fch_ref = str(ref.get('fecha_ref') or fecha_emision).strip()
            cod_ref = str(ref.get('cod_ref') or '').strip()
            razon = str(ref.get('razon_ref') or '').strip()[:90]
            partes = [
                f'<NroLinRef>{i}</NroLinRef>',
                f'<TpoDocRef>{_escape_xml(tpo)}</TpoDocRef>',
                f'<FolioRef>{_escape_xml(folio_ref)}</FolioRef>',
                f'<FchRef>{fch_ref}</FchRef>',
            ]
            if cod_ref:
                partes.append(f'<CodRef>{_escape_xml(cod_ref)}</CodRef>')
            if razon:
                partes.append(f'<RazonRef>{_escape_xml(razon)}</RazonRef>')
            referencia_xml += '<Referencia>' + ''.join(partes) + '</Referencia>'

    # 7. TED (el monto del TED es MntTotal)
    if timestamp_firma is None:
        timestamp_firma = datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
    primer_item_nombre = items_calc[0].get('nombre', 'Producto')[:40] if items_calc else 'Producto'
    ted = construir_ted(
        caf=caf, folio=folio, fecha_emision=fecha_emision,
        rut_receptor=receptor['rut'],
        razon_social_receptor=_escape_xml(receptor['razon_social']),
        monto_total=mnt_total,
        detalle_primer_item=_escape_xml(primer_item_nombre),
        timestamp_emision=timestamp_firma,
    )

    # 8. DTE completo (sin XMLDSig; hereda namespace del sobre)
    documento_id = f'F{folio}T{tipo_dte}'
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
        'tipo_dte': tipo_dte,
        'documento_id': documento_id,
        'totales': {
            'mnt_neto': mnt_neto, 'mnt_iva': mnt_iva, 'mnt_exe': mnt_exe,
            'mnt_reten': mnt_reten, 'mnt_total': mnt_total,
        },
        'ted': ted,
    }
