"""
facturacion/dtes/factura.py
─────────────────────────────────────────────────────────────
Generador del XML de Factura Electrónica (DTE tipo 33) y Factura Exenta (34).

Diferencias críticas vs Boleta (39):
  • Receptor OBLIGATORIO con datos completos (RUT, RznSoc, Giro, Dir, Cmna)
  • SIN <IndServicio>
  • Precios de ítems son NETOS (sin IVA), no brutos como boleta
  • <Totales> incluye <TasaIVA>19.00</TasaIVA>
  • Soporta descuentos por línea: <DescuentoPct> o <DescuentoMonto>
  • Soporta descuentos globales: <DscRcgGlobal>
  • Factura electrónica es CEDIBLE (vale como título de crédito)

Reglas de cálculo (factura, precios NETOS):
  Para cada ítem afecto:
    base = PrcItem × QtyItem
    base_con_desc = base - (base × DescuentoPct/100) o (base - DescuentoMonto)
    MontoItem = round(base_con_desc) — neto
  bruto_af  = suma de MontoItem de afectos
  desc_glob = descuento global (% sobre afectos)
  MntNeto   = bruto_af - desc_glob
  IVA       = round(MntNeto × 0.19)
  MntExe    = suma MontoItem exentos
  MntTotal  = MntNeto + IVA + MntExe

Soporta los 4 casos del Set Básico SII:
  CASO 1: 2 ítems afectos simples
  CASO 2: 2 ítems afectos con DescuentoPct por línea
  CASO 3: 2 afectos + 1 exento mezclado (IndExe=1)
  CASO 4: 2 afectos + 1 exento + DescuentoGlobal 23% solo sobre afectos
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
    return (s.replace('&', '&amp;')
             .replace('<', '&lt;')
             .replace('>', '&gt;')
             .replace('"', '&quot;')
             .replace("'", '&apos;'))


def _fmt_cantidad(qty: float) -> str:
    """Entera si no tiene decimales, sino hasta 6 decimales."""
    if qty == int(qty):
        return str(int(qty))
    return ('{:.6f}'.format(qty)).rstrip('0').rstrip('.')


def _calcular_totales_factura(items: List[Dict], descuento_global_pct: float = 0) -> Dict:
    """Calcula totales de factura. Items con precios NETOS.

    Args:
        items: [{nombre, cantidad, precio_unitario, exento?, descuento_pct?, descuento_monto?, unidad?}]
        descuento_global_pct: descuento % sobre items afectos (solo afectos, según SII)

    Returns:
        {mnt_neto, mnt_iva, mnt_exe, mnt_total, desc_global_monto, items_calculados}
    """
    bruto_af = 0  # suma de items afectos (después de descuento por línea)
    bruto_ex = 0  # suma de items exentos
    items_calc = []
    for it in items:
        qty = float(it.get('cantidad', 1))
        prc = float(it.get('precio_unitario', 0))
        es_exento = bool(it.get('exento', False))
        base = qty * prc
        # Descuento por línea: % tiene prioridad sobre monto
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

    # Manual oficial SII (formato_dte 2026-02 pág 23, campo 107):
    #   MntNeto = Suma items afectos - descuentos globales + recargos globales
    #             (asignados a items afectos)
    # IMPORTANTE: el tag <IndExeDR> se OMITE cuando el descuento aplica
    # solo a afectos (manual pág 37 sección D). Cuando se omite, el SII
    # aplica el descuento al neto correctamente.
    desc_global_monto = _redondear_clp(bruto_af * (descuento_global_pct / 100.0)) if descuento_global_pct else 0
    mnt_neto = bruto_af - desc_global_monto  # Neto con descuento global aplicado
    mnt_iva = _redondear_clp(mnt_neto * IVA_PORCENTAJE / 100.0)
    mnt_exe = bruto_ex
    # MntTotal = MntNeto + IVA + Exento (manual pág 26, campo 120)
    mnt_total = mnt_neto + mnt_iva + mnt_exe
    return {
        'mnt_neto': mnt_neto, 'mnt_iva': mnt_iva,
        'mnt_exe': mnt_exe, 'mnt_total': mnt_total,
        'desc_global_monto': desc_global_monto,
        'items_calculados': items_calc,
    }


def generar_factura_xml(
    caf: CAFParsed,
    folio: int,
    fecha_emision: str,                # 'YYYY-MM-DD'
    emisor: Dict,                       # {rut, razon_social, giro, dir_origen, cmna_origen, [acteco]}
    receptor: Dict,                     # OBLIGATORIO: {rut, razon_social, giro, direccion, comuna, [contacto]}
    items: List[Dict],
    descuento_global_pct: float = 0,    # 0 = sin desc global
    descuento_global_glosa: str = '',   # ej "DESCUENTO PROMOCIONAL"
    referencias: Optional[List[Dict]] = None,  # NC/ND referencias
    forma_pago: int = 2,                # 1=Contado, 2=Crédito, 3=Sin costo
    fecha_vencimiento: Optional[str] = None,  # 'YYYY-MM-DD', si forma_pago=2
    es_exenta: bool = False,            # True → DTE 34
    timestamp_firma: Optional[str] = None,
) -> Dict:
    """Genera el XML de una Factura Electrónica (33) o Factura Exenta (34).

    Returns:
        dict con xml(bytes), folio, totales, ted(bytes), documento_id
    """
    tipo_dte = 34 if es_exenta else 33

    # 0. Calcular totales
    tot = _calcular_totales_factura(items, descuento_global_pct)
    items_calc = tot['items_calculados']
    mnt_neto = tot['mnt_neto']
    mnt_iva = tot['mnt_iva']
    mnt_exe = tot['mnt_exe']
    mnt_total = tot['mnt_total']

    # Factura exenta no lleva MntNeto ni IVA — solo MntExe y MntTotal
    if es_exenta:
        mnt_neto = 0
        mnt_iva = 0
        # Si la factura es exenta, todos los items se tratan como exentos en MntExe
        mnt_exe = sum(it['_monto_item'] for it in items_calc)
        mnt_total = mnt_exe

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

    # 2. Emisor (más completo que en boleta)
    # Schema SII para factura/NC/ND exige <Acteco> ANTES de <DirOrigen>
    rut_e = str(emisor['rut']).replace('.', '').strip()
    emisor_parts = [
        f'<RUTEmisor>{rut_e}</RUTEmisor>',
        f'<RznSoc>{_escape_xml(emisor["razon_social"])}</RznSoc>',
        f'<GiroEmis>{_escape_xml(emisor.get("giro", "")[:80])}</GiroEmis>',
    ]
    # Acteco OBLIGATORIO en facturas/NC/ND. Si no se pasa, usar 620100
    # (Actividades de programación informática). Puede pasarse uno o varios.
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

    # 3. Receptor (OBLIGATORIO con datos completos en factura)
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

    # 4. Totales (orden EXACTO según schema SII DTE_v10)
    tot_parts = []
    if not es_exenta:
        tot_parts.append(f'<MntNeto>{mnt_neto}</MntNeto>')
        if mnt_exe > 0:
            tot_parts.append(f'<MntExe>{mnt_exe}</MntExe>')
        tot_parts.append(f'<TasaIVA>{IVA_PORCENTAJE}.00</TasaIVA>')
        tot_parts.append(f'<IVA>{mnt_iva}</IVA>')
    else:
        # Factura exenta: solo MntExe + MntTotal
        tot_parts.append(f'<MntExe>{mnt_exe}</MntExe>')
    tot_parts.append(f'<MntTotal>{mnt_total}</MntTotal>')
    totales_xml = '<Totales>' + ''.join(tot_parts) + '</Totales>'

    encabezado_xml = f'<Encabezado>{iddoc_xml}{emisor_xml}{receptor_xml}{totales_xml}</Encabezado>'

    # 5. Detalles (precios netos en factura, no brutos como boleta)
    detalles_xml = ''
    for i, it in enumerate(items_calc, start=1):
        qty = float(it.get('cantidad', 1))
        prc = float(it.get('precio_unitario', 0))
        unidad = it.get('unidad', 'Un')
        es_exe = bool(it.get('exento', False)) or es_exenta
        nombre = it.get('nombre', 'Producto')[:80]

        linea_parts = [f'<NroLinDet>{i}</NroLinDet>']
        if es_exe and not es_exenta:
            # Solo se marca IndExe=1 en factura afecta cuando el item específico es exento
            linea_parts.append('<IndExe>1</IndExe>')
        linea_parts.append(f'<NmbItem>{_escape_xml(nombre)}</NmbItem>')
        linea_parts.append(f'<QtyItem>{_fmt_cantidad(qty)}</QtyItem>')
        linea_parts.append(f'<UnmdItem>{_escape_xml(unidad)}</UnmdItem>')
        linea_parts.append(f'<PrcItem>{_fmt_cantidad(prc)}</PrcItem>')

        # Descuentos por línea (después de PrcItem, según schema)
        if it.get('_desc_pct'):
            linea_parts.append(f'<DescuentoPct>{it["_desc_pct"]:g}</DescuentoPct>')
            linea_parts.append(f'<DescuentoMonto>{it["_desc_aplicado"]}</DescuentoMonto>')
        elif it.get('descuento_monto'):
            linea_parts.append(f'<DescuentoMonto>{it["_desc_aplicado"]}</DescuentoMonto>')

        linea_parts.append(f'<MontoItem>{it["_monto_item"]}</MontoItem>')
        detalles_xml += '<Detalle>' + ''.join(linea_parts) + '</Detalle>'

    # 6. Descuento global (DscRcgGlobal) — después de detalles, antes de referencias
    dsc_rcg_xml = ''
    if descuento_global_pct and tot['desc_global_monto'] > 0:
        dsc_rcg_xml = (
            '<DscRcgGlobal>'
            '<NroLinDR>1</NroLinDR>'
            '<TpoMov>D</TpoMov>'  # D=descuento
            f'<GlosaDR>{_escape_xml(descuento_global_glosa or "DESCUENTO GLOBAL")}</GlosaDR>'
            '<TpoValor>%</TpoValor>'
            f'<ValorDR>{descuento_global_pct:g}</ValorDR>'
            # SII manual pág 37 sección D: cuando el descuento aplica solo
            # a items afectos, NO se debe llevar el <IndExeDR>. Solo se
            # incluye con valor 1 (exentos) o 2 (no facturables).
            '</DscRcgGlobal>'
        )

    # 7. Referencias (opcional)
    referencia_xml = ''
    if referencias and isinstance(referencias, list):
        for i, ref in enumerate(referencias[:40], start=1):
            tpo = str(ref.get('tpo_doc_ref') or '33').strip()
            folio_ref = str(ref.get('folio_ref') or '').strip()
            fch_ref = str(ref.get('fecha_ref') or fecha_emision).strip()
            cod_ref = str(ref.get('cod_ref') or '').strip()
            razon = str(ref.get('razon_ref') or '').strip()[:90]
            partes = [
                f'<NroLinRef>{i}</NroLinRef>',
                f'<TpoDocRef>{_escape_xml(tpo)}</TpoDocRef>',
                f'<FolioRef>{_escape_xml(folio_ref)}</FolioRef>',
                f'<FchRef>{fch_ref}</FchRef>',  # OBLIGATORIO en facturas (diferente a boletas)
            ]
            if cod_ref:
                partes.append(f'<CodRef>{_escape_xml(cod_ref)}</CodRef>')
            if razon:
                partes.append(f'<RazonRef>{_escape_xml(razon)}</RazonRef>')
            referencia_xml += '<Referencia>' + ''.join(partes) + '</Referencia>'

    # 8. TED (timbre del SII) - el monto del TED es MntTotal
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

    # 9. DTE completo (sin XMLDSig todavía). Sin namespace declarado: lo hereda
    #    del sobre EnvioDTE. Mismo patrón que certificó boletas.
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
            'desc_global_monto': tot['desc_global_monto'],
        },
        'ted': ted,
    }
