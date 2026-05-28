"""
facturacion/dtes/nota_debito.py
─────────────────────────────────────────────────────────────
Generador de Nota de Débito Electrónica (DTE tipo 56).

Una Nota de Débito INCREMENTA o reactiva valores de un documento previo.
Caso típico: anular una Nota de Crédito (CodRef=1, "ANULA NOTA DE CRÉDITO").

Estructura igual a factura (precios netos + IVA), con:
  • TipoDTE = 56
  • Referencia OBLIGATORIA al documento que modifica
  • Para "anular NC" (CASO 8 del set): solo referencia, sin items propios
    → en ese caso el documento referencia toda la NC anulada y replica sus
      montos en POSITIVO (revierte la NC que estaba en negativo conceptual)

CodRef:
  1 = anula documento completo (la NC referenciada queda sin efecto)
  2 = corrige texto
  3 = corrige montos

Para el SET CASO 4829122-8:
  - DOCUMENTO: NOTA DE DEBITO ELECTRONICA
  - REFERENCIA: NC del CASO-5
  - RAZON: "ANULA NOTA DE CREDITO ELECTRONICA"
  - CodRef = 1
  - Items: los mismos de la NC original (para que la ND iguale el monto a "reactivar")
"""
from __future__ import annotations
from datetime import datetime
from typing import List, Dict, Optional
from .caf_parser import CAFParsed
from .ted import construir_ted


IVA_PORCENTAJE = 19


def _redondear_clp(valor: float) -> int:
    return int(round(valor))


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


def _calcular_totales_neto(items: List[Dict], descuento_global_pct: float = 0) -> Dict:
    """Calcula totales con precios NETOS (igual que factura).

    Cada item: {nombre, cantidad, precio_unitario, exento?(bool), descuento_pct?, unidad?(str)}
    """
    bruto_af = 0
    bruto_ex = 0
    items_calc = []
    for it in items:
        qty = float(it.get('cantidad', 1))
        prc = float(it.get('precio_unitario', 0))
        es_exento = bool(it.get('exento', False))
        base = qty * prc
        desc_pct = float(it.get('descuento_pct', 0) or 0)
        desc_monto = float(it.get('descuento_monto', 0) or 0)
        if desc_pct:
            desc_aplicado = base * (desc_pct / 100.0)
        elif desc_monto:
            desc_aplicado = desc_monto
        else:
            desc_aplicado = 0
        monto_item = _redondear_clp(base - desc_aplicado)
        items_calc.append({
            **it,
            '_base': _redondear_clp(base),
            '_desc_aplicado': _redondear_clp(desc_aplicado),
            '_desc_pct': desc_pct,
            '_monto_item': monto_item,
        })
        if es_exento:
            bruto_ex += monto_item
        else:
            bruto_af += monto_item

    desc_global_monto = _redondear_clp(bruto_af * (descuento_global_pct / 100.0)) if descuento_global_pct else 0
    mnt_neto = bruto_af - desc_global_monto
    mnt_iva = _redondear_clp(mnt_neto * IVA_PORCENTAJE / 100.0)
    mnt_total = mnt_neto + mnt_iva + bruto_ex
    return {
        'mnt_neto': mnt_neto, 'mnt_iva': mnt_iva,
        'mnt_exe': bruto_ex, 'mnt_total': mnt_total,
        'desc_global_monto': desc_global_monto,
        'items_calculados': items_calc,
    }


def generar_nota_debito_xml(
    caf: CAFParsed,
    folio: int,
    fecha_emision: str,
    emisor: Dict,                    # {rut, razon_social, giro, dir_origen, cmna_origen, [acteco]}
    receptor: Dict,                  # OBLIGATORIO con datos completos
    referencia: Dict,                # OBLIGATORIO: {folio_ref, tipo_doc_ref, fecha_ref, cod_ref, razon_ref}
    items: List[Dict],
    descuento_global_pct: float = 0,
    descuento_global_glosa: str = '',
    forma_pago: int = 2,
    fecha_vencimiento: Optional[str] = None,
    timestamp_firma: Optional[str] = None,
) -> Dict:
    """Genera XML de Nota de Débito Electrónica (DTE 56).

    Igual estructura que factura (precios netos), con:
      - TipoDTE = 56
      - <Referencia> OBLIGATORIA al documento modificado

    Para CASO 8 del set (anula NC): items deben replicar los items de la NC
    para que el monto de la ND iguale al de la NC anulada.
    """
    tipo_dte = 56

    if not referencia or not referencia.get('folio_ref'):
        raise ValueError("Nota de Débito requiere referencia obligatoria al documento que modifica")

    # 0. Calcular totales
    tot = _calcular_totales_neto(items, descuento_global_pct)
    items_calc = tot['items_calculados']
    mnt_neto = tot['mnt_neto']
    mnt_iva = tot['mnt_iva']
    mnt_exe = tot['mnt_exe']
    mnt_total = tot['mnt_total']

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

    # 2. Emisor
    rut_e = str(emisor['rut']).replace('.', '').strip()
    emisor_parts = [
        f'<RUTEmisor>{rut_e}</RUTEmisor>',
        f'<RznSoc>{_escape_xml(emisor["razon_social"])}</RznSoc>',
        f'<GiroEmis>{_escape_xml(emisor.get("giro", "")[:80])}</GiroEmis>',
    ]
    if emisor.get('acteco'):
        emisor_parts.append(f'<Acteco>{emisor["acteco"]}</Acteco>')
    emisor_parts.extend([
        f'<DirOrigen>{_escape_xml(emisor["dir_origen"])}</DirOrigen>',
        f'<CmnaOrigen>{_escape_xml(emisor["cmna_origen"])}</CmnaOrigen>',
    ])
    emisor_xml = '<Emisor>' + ''.join(emisor_parts) + '</Emisor>'

    # 3. Receptor
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

    # 4. Totales (precios netos, igual que factura)
    tot_parts = [f'<MntNeto>{mnt_neto}</MntNeto>']
    if mnt_exe > 0:
        tot_parts.append(f'<MntExe>{mnt_exe}</MntExe>')
    tot_parts.append(f'<TasaIVA>{IVA_PORCENTAJE}.00</TasaIVA>')
    tot_parts.append(f'<IVA>{mnt_iva}</IVA>')
    tot_parts.append(f'<MntTotal>{mnt_total}</MntTotal>')
    totales_xml = '<Totales>' + ''.join(tot_parts) + '</Totales>'

    encabezado_xml = f'<Encabezado>{iddoc_xml}{emisor_xml}{receptor_xml}{totales_xml}</Encabezado>'

    # 5. Detalles (precios NETOS, igual que factura)
    detalles_xml = ''
    for i, it in enumerate(items_calc, start=1):
        qty = float(it.get('cantidad', 1))
        prc = float(it.get('precio_unitario', 0))
        unidad = it.get('unidad', 'Un')
        es_exe = bool(it.get('exento', False))
        nombre = it.get('nombre', 'Producto')[:80]

        linea_parts = [f'<NroLinDet>{i}</NroLinDet>']
        if es_exe:
            linea_parts.append('<IndExe>1</IndExe>')
        linea_parts.append(f'<NmbItem>{_escape_xml(nombre)}</NmbItem>')
        linea_parts.append(f'<QtyItem>{_fmt_cantidad(qty)}</QtyItem>')
        linea_parts.append(f'<UnmdItem>{_escape_xml(unidad)}</UnmdItem>')
        linea_parts.append(f'<PrcItem>{_fmt_cantidad(prc)}</PrcItem>')
        if it.get('_desc_pct'):
            linea_parts.append(f'<DescuentoPct>{it["_desc_pct"]:g}</DescuentoPct>')
            linea_parts.append(f'<DescuentoMonto>{it["_desc_aplicado"]}</DescuentoMonto>')
        elif it.get('descuento_monto'):
            linea_parts.append(f'<DescuentoMonto>{it["_desc_aplicado"]}</DescuentoMonto>')
        linea_parts.append(f'<MontoItem>{it["_monto_item"]}</MontoItem>')
        detalles_xml += '<Detalle>' + ''.join(linea_parts) + '</Detalle>'

    # 6. Descuento global
    dsc_rcg_xml = ''
    if descuento_global_pct and tot['desc_global_monto'] > 0:
        dsc_rcg_xml = (
            '<DscRcgGlobal>'
            '<NroLinDR>1</NroLinDR>'
            '<TpoMov>D</TpoMov>'
            f'<GlosaDR>{_escape_xml(descuento_global_glosa or "DESCUENTO GLOBAL")}</GlosaDR>'
            '<TpoValor>%</TpoValor>'
            f'<ValorDR>{descuento_global_pct:g}</ValorDR>'
            '<IndExeDR>2</IndExeDR>'
            '</DscRcgGlobal>'
        )

    # 7. Referencia OBLIGATORIA
    tpo = str(referencia.get('tpo_doc_ref') or '61').strip()  # default: 61 (NC)
    folio_ref = str(referencia.get('folio_ref') or '').strip()
    fch_ref = str(referencia.get('fecha_ref') or fecha_emision).strip()
    cod_ref = str(referencia.get('cod_ref') or '1').strip()  # default: anula
    razon = str(referencia.get('razon_ref') or 'ANULA NOTA DE CREDITO ELECTRONICA').strip()[:90]
    ref_partes = [
        '<NroLinRef>1</NroLinRef>',
        f'<TpoDocRef>{_escape_xml(tpo)}</TpoDocRef>',
        f'<FolioRef>{_escape_xml(folio_ref)}</FolioRef>',
        f'<FchRef>{fch_ref}</FchRef>',
        f'<CodRef>{_escape_xml(cod_ref)}</CodRef>',
        f'<RazonRef>{_escape_xml(razon)}</RazonRef>',
    ]
    referencia_xml = '<Referencia>' + ''.join(ref_partes) + '</Referencia>'

    # 8. TED
    if timestamp_firma is None:
        timestamp_firma = datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
    primer_item_nombre = items_calc[0].get('nombre', 'Producto')[:40] if items_calc else 'Producto'
    ted = construir_ted(
        caf=caf, folio=folio, fecha_emision=fecha_emision,
        rut_emisor=emisor['rut'], rut_receptor=receptor['rut'],
        razon_social_receptor=receptor['razon_social'],
        monto_total=mnt_total, primer_item_nombre=primer_item_nombre,
        timestamp_firma=timestamp_firma, tipo_dte=tipo_dte,
    )

    # 9. DTE completo (sin namespace, lo hereda del EnvioDTE)
    documento_id = f'F{folio}T{tipo_dte}'
    dte_xml = (
        '<?xml version="1.0" encoding="ISO-8859-1"?>'
        '<DTE version="1.0">'
        f'<Documento ID="{documento_id}">'
        f'{encabezado_xml}'
        f'{detalles_xml}'
        f'{dsc_rcg_xml}'
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
            'mnt_neto': mnt_neto, 'mnt_iva': mnt_iva,
            'mnt_exe': mnt_exe, 'mnt_total': mnt_total,
        },
        'ted': ted,
    }
