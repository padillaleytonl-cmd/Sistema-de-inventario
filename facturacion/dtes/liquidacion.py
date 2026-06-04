"""
facturacion/dtes/liquidacion.py
─────────────────────────────────────────────────────────────
Generador del XML de Liquidación-Factura Electrónica (DTE tipo 43).

La liquidación-factura la emite el MANDANTE (consignatario/comisionista que
recibe mercadería para vender por cuenta de un tercero) para rendir cuentas.
Estructura especial confirmada contra el XSD oficial DTE_v10.xsd:

  • Contenedor raíz: <Liquidacion> (NO <Documento>).
  • Detalle con items: netos (IndExe ausente) y exentos (IndExe=1).
    Los montos pueden ser NEGATIVOS (notas de crédito, liquidaciones previas).
  • Nodo <Comisiones> a nivel documento (después de Referencia):
      NroLinCom, TipoMovim (C/O), Glosa, [TasaComision], ValComNeto,
      ValComExe, ValComIVA.
  • <Totales> orden: MntNeto, MntExe, TasaIVA, IVA, [IVAProp, IVATerc,
      ImptoReten], Comisiones(ValComNeto, ValComExe, ValComIVA), MntTotal.
  • MntTotal = MntNeto + MntExe + IVA  (el neto de comisiones NO se suma al
    MntTotal del documento; las comisiones se totalizan aparte en su nodo).

Reusa los helpers de factura.py (escape, formato, IVA).
"""
from __future__ import annotations
from datetime import datetime
from typing import List, Dict, Optional

from .caf_parser import CAFParsed
from .ted import construir_ted
from .factura import _escape_xml, _fmt_cantidad, IVA_PORCENTAJE


def generar_liquidacion_xml(
    caf: CAFParsed,
    folio: int,
    fecha_emision: str,
    emisor: Dict,
    receptor: Dict,
    items: List[Dict],
    comisiones: Optional[List[Dict]] = None,
    referencias: Optional[List[Dict]] = None,
    forma_pago: int = 2,
    timestamp_firma: Optional[str] = None,
) -> Dict:
    """Genera el XML de una Liquidación-Factura Electrónica (DTE 43).

    items: lista de dicts {nombre, cantidad, monto, exento(bool)}.
           `monto` es el TOTAL de la línea (puede ser negativo). Si se entrega
           cantidad y precio, se calcula monto = cantidad*precio; si no, se usa
           `monto` directo (típico en liquidaciones, que totalizan por concepto).
    comisiones: lista de dicts {tipo_movim('C'|'O'), glosa, neto, exento, iva,
           tasa(opcional)}. Si no se entrega `iva`, se calcula 19% del neto.

    Returns: dict con xml(bytes), folio, totales, ted(bytes), documento_id.
    """
    tipo_dte = 43

    # ── 0. Totales del detalle ──
    mnt_neto = 0
    mnt_exe = 0
    iva_boletas = 0
    items_norm = []
    for it in items:
        es_exe = bool(it.get('exento'))
        es_bruto = bool(it.get('bruto'))  # monto incluye IVA (boletas a consumidor)
        if 'monto' in it and it['monto'] is not None:
            monto = int(round(float(it['monto'])))
            qty = it.get('cantidad')
            prc = it.get('precio_unitario')
        else:
            qty = float(it.get('cantidad', 1))
            prc = float(it.get('precio_unitario', 0))
            monto = int(round(qty * prc))
        # Items afectos brutos (p.ej. línea resumen de BOLETAS a consumidor final):
        # el monto del set incluye IVA. La LÍNEA muestra el MontoItem BRUTO tal cual
        # del set (como las demás líneas), pero el aporte a MntNeto es el neto y el
        # IVA incluido se acumula para el encabezado; el SII descompone boletas (39).
        monto_linea = monto          # lo que se muestra en <MontoItem>
        aporte_neto = monto          # lo que suma a MntNeto/MntExe
        iva_extra = 0
        if es_bruto and not es_exe:
            aporte_neto = int(round(monto / (1 + IVA_PORCENTAJE / 100.0)))
            iva_extra = monto - aporte_neto
        items_norm.append({
            'nombre': it.get('nombre', 'Item')[:80],
            'cantidad': qty, 'precio_unitario': prc,
            'monto': monto_linea, 'exento': es_exe,
            'unidad': it.get('unidad'),
            'tpo_doc_liq': it.get('tpo_doc_liq', 33),
        })
        if es_exe:
            mnt_exe += aporte_neto
        else:
            mnt_neto += aporte_neto
        iva_boletas += iva_extra

    # IVA del encabezado: el de las ventas afectas normales (neto*19%) más el IVA
    # ya incluido en las líneas brutas (boletas). Se mantiene la coherencia
    # IVA == round(MntNeto * 0.19) porque el neto de la boleta entró a MntNeto.
    mnt_iva = int(round(mnt_neto * IVA_PORCENTAJE / 100.0))
    # El MntTotal del DTE 43 DESCUENTA la comisión del liquidador (neto + IVA),
    # porque es lo que el liquidador retiene; al mandante se le paga el resto.
    # MntTotal = MntNeto + MntExe + IVA - ValComNeto - ValComIVA  (validación HED-2-260).
    # Se calcula más abajo, tras totalizar las comisiones.

    # ── Comisiones: totales para el resumen en <Totales> ──
    com_norm = []
    val_com_neto = 0
    val_com_exe = 0
    val_com_iva = 0
    if comisiones:
        for c in comisiones:
            c_neto = int(round(float(c.get('neto', 0))))
            c_exe = int(round(float(c.get('exento', 0))))
            if 'iva' in c and c['iva'] is not None:
                c_iva = int(round(float(c['iva'])))
            else:
                c_iva = int(round(c_neto * IVA_PORCENTAJE / 100.0))
            com_norm.append({
                'tipo_movim': c.get('tipo_movim', 'C'),
                'glosa': c.get('glosa', 'Comision')[:60],
                'tasa': c.get('tasa'),
                'neto': c_neto, 'exento': c_exe, 'iva': c_iva,
            })
            val_com_neto += c_neto
            val_com_exe += c_exe
            val_com_iva += c_iva

    # MntTotal del DTE 43 descuenta la comisión del liquidador (neto + IVA).
    mnt_total = mnt_neto + mnt_exe + mnt_iva - val_com_neto - val_com_iva

    # ── 1. IdDoc ──
    # TpoTranVenta=1 (ventas del giro): LibreDTE lo agrega siempre a los DTE que
    # no son boleta/exportación, incluido el 43. Orden del XSD: tras FchEmis.
    iddoc_parts = [
        f'<TipoDTE>{tipo_dte}</TipoDTE>',
        f'<Folio>{folio}</Folio>',
        f'<FchEmis>{fecha_emision}</FchEmis>',
        '<TpoTranVenta>1</TpoTranVenta>',
        f'<FmaPago>{forma_pago}</FmaPago>',
    ]
    iddoc_xml = '<IdDoc>' + ''.join(iddoc_parts) + '</IdDoc>'

    # ── 2. Emisor (mandante) ──
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

    # ── 3. Receptor (liquidador/consignatario) ──
    rut_r = str(receptor['rut']).replace('.', '').strip()
    rec_parts = [
        f'<RUTRecep>{rut_r}</RUTRecep>',
        f'<RznSocRecep>{_escape_xml(receptor["razon_social"][:100])}</RznSocRecep>',
        f'<GiroRecep>{_escape_xml(receptor.get("giro", "Sin giro")[:40])}</GiroRecep>',
        f'<DirRecep>{_escape_xml(receptor.get("direccion", "Sin dirección")[:70])}</DirRecep>',
        f'<CmnaRecep>{_escape_xml(receptor.get("comuna", "Santiago")[:20])}</CmnaRecep>',
    ]
    receptor_xml = '<Receptor>' + ''.join(rec_parts) + '</Receptor>'

    # ── 4. Totales (orden del XSD) ──
    tot_parts = []
    tot_parts.append(f'<MntNeto>{mnt_neto}</MntNeto>')
    if mnt_exe != 0:
        tot_parts.append(f'<MntExe>{mnt_exe}</MntExe>')
    tot_parts.append(f'<TasaIVA>{IVA_PORCENTAJE}.00</TasaIVA>')
    tot_parts.append(f'<IVA>{mnt_iva}</IVA>')
    # En liquidación-factura, el IVA se separa en IVA Propio (el de las comisiones
    # del liquidador) e IVA de Terceros (el del mandante = IVA total − IVA propio).
    # Orden XSD: IVA → IVAProp → IVATerc → ... → Comisiones.
    if com_norm:
        iva_prop = val_com_iva
        iva_terc = mnt_iva - iva_prop
        tot_parts.append(f'<IVAProp>{iva_prop}</IVAProp>')
        tot_parts.append(f'<IVATerc>{iva_terc}</IVATerc>')
    # Comisiones resumen (después de IVA/ImptoReten, antes de MntTotal)
    if com_norm:
        com_tot = ['<Comisiones>']
        com_tot.append(f'<ValComNeto>{val_com_neto}</ValComNeto>')
        if val_com_exe != 0:
            com_tot.append(f'<ValComExe>{val_com_exe}</ValComExe>')
        com_tot.append(f'<ValComIVA>{val_com_iva}</ValComIVA>')
        com_tot.append('</Comisiones>')
        tot_parts.append(''.join(com_tot))
    tot_parts.append(f'<MntTotal>{mnt_total}</MntTotal>')
    # MontoPeriodo: el SII valida (HED-3-265) que sea IGUAL al MntTotal del documento.
    # VlrPagar también coincide con el total a pagar. Orden XSD: MntTotal →
    # MontoPeriodo → SaldoAnterior → VlrPagar.
    tot_parts.append(f'<MontoPeriodo>{mnt_total}</MontoPeriodo>')
    tot_parts.append(f'<VlrPagar>{mnt_total}</VlrPagar>')
    totales_xml = '<Totales>' + ''.join(tot_parts) + '</Totales>'

    encabezado_xml = f'<Encabezado>{iddoc_xml}{emisor_xml}{receptor_xml}{totales_xml}</Encabezado>'

    # ── 5. Detalle ──
    # Estructura verificada: QtyItem con la CANTIDAD real del set y SIN PrcItem/
    # UnmdItem. Con esta forma las líneas cuyo TpoDocLiq coincide con el documento
    # real liquidado cuadran en la revisión del set. (Agregar PrcItem=monto con
    # Qty=1 hacía reparar todas las líneas; quitar QtyItem también.)
    # TpoDocLiq va tras NroLinDet, antes de IndExe.
    detalles_xml = ''
    for i, it in enumerate(items_norm, start=1):
        monto = it["monto"]
        linea_parts = [f'<NroLinDet>{i}</NroLinDet>']
        linea_parts.append(f'<TpoDocLiq>{it.get("tpo_doc_liq", 33)}</TpoDocLiq>')
        if it['exento']:
            linea_parts.append('<IndExe>1</IndExe>')
        linea_parts.append(f'<NmbItem>{_escape_xml(it["nombre"])}</NmbItem>')
        if it['cantidad'] is not None:
            linea_parts.append(f'<QtyItem>{_fmt_cantidad(float(it["cantidad"]))}</QtyItem>')
        linea_parts.append(f'<MontoItem>{monto}</MontoItem>')
        detalles_xml += '\n<Detalle>' + ''.join(linea_parts) + '</Detalle>'

    # ── 6. Referencias ──
    referencia_xml = ''
    if referencias and isinstance(referencias, list):
        for i, ref in enumerate(referencias[:40], start=1):
            tpo = str(ref.get('tpo_doc_ref') or ref.get('tipo_doc_ref') or 'SET').strip()
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

    # ── 7. Comisiones a nivel documento (después de Referencia, antes del TED) ──
    comisiones_xml = ''
    for i, c in enumerate(com_norm, start=1):
        c_parts = [
            f'<NroLinCom>{i}</NroLinCom>',
            f'<TipoMovim>{c["tipo_movim"]}</TipoMovim>',
            f'<Glosa>{_escape_xml(c["glosa"])}</Glosa>',
        ]
        if c.get('tasa') is not None:
            c_parts.append(f'<TasaComision>{c["tasa"]}</TasaComision>')
        c_parts.append(f'<ValComNeto>{c["neto"]}</ValComNeto>')
        c_parts.append(f'<ValComExe>{c["exento"]}</ValComExe>')
        c_parts.append(f'<ValComIVA>{c["iva"]}</ValComIVA>')
        comisiones_xml += '\n<Comisiones>' + ''.join(c_parts) + '</Comisiones>'

    # ── 8. TED ──
    if timestamp_firma is None:
        timestamp_firma = datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
    primer_item_nombre = items_norm[0]['nombre'][:40] if items_norm else 'Liquidacion'
    ted = construir_ted(
        caf=caf, folio=folio, fecha_emision=fecha_emision,
        rut_receptor=receptor['rut'],
        razon_social_receptor=_escape_xml(receptor['razon_social']),
        monto_total=mnt_total,
        detalle_primer_item=_escape_xml(primer_item_nombre),
        timestamp_emision=timestamp_firma,
    )

    # ── 9. DTE completo (contenedor <Liquidacion>) ──
    documento_id = f'F{folio}T{tipo_dte}'
    dte_xml = (
        '<?xml version="1.0" encoding="ISO-8859-1"?>'
        '<DTE version="1.0">'
        f'<Liquidacion ID="{documento_id}">'
        f'\n{encabezado_xml}'
        f'{detalles_xml}'
        f'\n{referencia_xml}'
        f'{comisiones_xml}'
        f'\n{ted.decode("iso-8859-1")}'
        f'\n<TmstFirma>{timestamp_firma}</TmstFirma>'
        f'</Liquidacion>'
        '</DTE>'
    )

    return {
        'xml': dte_xml.encode('iso-8859-1', errors='replace'),
        'folio': folio,
        'tipo_dte': tipo_dte,
        'documento_id': documento_id,
        'totales': {
            'mnt_neto': mnt_neto, 'mnt_exe': mnt_exe, 'mnt_iva': mnt_iva,
            'mnt_total': mnt_total,
            'val_com_neto': val_com_neto, 'val_com_exe': val_com_exe,
            'val_com_iva': val_com_iva,
        },
        'ted': ted,
    }
