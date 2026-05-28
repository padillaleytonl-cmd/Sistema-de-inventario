"""
facturacion/dtes/nota_credito.py
─────────────────────────────────────────────────────────────
Generador de Nota de Crédito Electrónica (DTE tipo 61).

Una NC modifica un documento previo (factura o boleta). Soporta DOS modos
según el documento referenciado, controlados por el flag <MntBruto>:

  • MODO NETO (default, sin tag): precios sin IVA. Para NC sobre facturas (33/34).
    El SII espera: <MntNeto>, <IVA>, <MntTotal> calculados normalmente.

  • MODO BRUTO (<MntBruto>1</MntBruto> en IdDoc): precios INCLUYEN IVA.
    Para NC sobre boletas (39/41), porque las boletas son brutas.
    El SII espera: precios y MontoItem incluyen IVA; MntNeto se calcula
    sacando el IVA: MntNeto = round(bruto/1.19), IVA = bruto - MntNeto.

REGLA OFICIAL DEL SII (formato_dte):
  "MntBruto: Indica si las líneas de detalle, descuentos y recargos se expresan
   en montos brutos. Solamente se acepta el valor 1 (<MntBruto>1</MntBruto>).
   Si no se indica, se asume los valores en montos Netos."

CodRef:
  1 = anula documento completo
  2 = corrige texto
  3 = corrige montos (rebaja)

Para certificación (set 4829122):
  CASO 5: NC anula Factura del CASO 1 — modo NETO, sin items, replica monto
  CASO 6: NC rebaja Factura del CASO 2 — modo NETO, con items y cantidades
  CASO 7: NC anula Factura del CASO 3 — modo NETO, sin items, replica monto
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


def _calcular_totales_neto(items: List[Dict], descuento_global_pct: float = 0) -> Dict:
    """MODO NETO: precios netos (sin IVA). Para NC sobre facturas."""
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
        'tiene_afecto': bruto_af > 0,
        'tiene_exento': bruto_ex > 0,
    }


def _calcular_totales_bruto(items: List[Dict]) -> Dict:
    """MODO BRUTO: precios incluyen IVA. Para NC sobre boletas.

    MntNeto se calcula sacando el IVA: MntNeto = round(bruto / 1.19)
    """
    bruto_afecto = 0
    mnt_exento = 0
    items_calc = []
    for it in items:
        qty = float(it.get('cantidad', 1))
        prc = float(it.get('precio_unitario', 0))
        subtotal = _redondear_clp(qty * prc)
        es_exento = bool(it.get('exento', False))
        items_calc.append({
            **it,
            '_monto_item': subtotal,
        })
        if es_exento:
            mnt_exento += subtotal
        else:
            bruto_afecto += subtotal

    mnt_neto = _redondear_clp(bruto_afecto / (1 + IVA_PORCENTAJE / 100)) if bruto_afecto > 0 else 0
    mnt_iva = bruto_afecto - mnt_neto if bruto_afecto > 0 else 0
    mnt_total = mnt_neto + mnt_iva + mnt_exento
    return {
        'mnt_neto': mnt_neto, 'mnt_iva': mnt_iva,
        'mnt_exe': mnt_exento, 'mnt_total': mnt_total,
        'desc_global_monto': 0,
        'items_calculados': items_calc,
        'tiene_afecto': bruto_afecto > 0,
        'tiene_exento': mnt_exento > 0,
    }


def generar_nota_credito_xml(
    caf: CAFParsed,
    folio: int,
    fecha_emision: str,
    emisor: Dict,                        # {rut, razon_social, giro, dir_origen, cmna_origen, [acteco]}
    receptor: Dict,                      # OBLIGATORIO (NC factura) o {rut:66666666-6, ...} para NC boleta
    referencia: Dict,                    # OBLIGATORIO: {folio_ref, tipo_doc_ref, fecha_ref, cod_ref, razon_ref}
    items: Optional[List[Dict]] = None,  # opcional: si vacío y CodRef=1 → solo carátula (replica monto referido)
    monto_anulacion: Optional[int] = None,  # solo usado cuando CodRef=1 sin items (toma este como MntTotal)
    descuento_global_pct: float = 0,
    descuento_global_glosa: str = '',
    forma_pago: int = 2,                 # 1=Contado, 2=Crédito, 3=Sin costo (solo aplica modo neto)
    fecha_vencimiento: Optional[str] = None,
    timestamp_firma: Optional[str] = None,
) -> Dict:
    """Genera XML de Nota de Crédito Electrónica (DTE 61).

    El modo (bruto/neto) se DETECTA AUTOMÁTICAMENTE según el tipo del documento
    referenciado:
      • Referencia a boleta (39/41) → MODO BRUTO (<MntBruto>1</MntBruto>)
      • Referencia a factura (33/34/46) → MODO NETO (sin MntBruto)

    items puede ser:
      • Lista con items (CASO 6: corrige monto, devuelve mercadería) → calcula totales
      • Vacía o None (CASO 5, 7: anula completa por corrección de texto)
        En ese caso debe venir monto_anulacion para que la NC tenga MntTotal>0
        (el SII exige al menos MntTotal>0 incluso si "anula" por texto)

    Returns:
        dict con xml(bytes), folio, totales, ted(bytes), documento_id
    """
    tipo_dte = 61

    if not referencia or not referencia.get('folio_ref'):
        raise ValueError("Nota de Crédito requiere referencia obligatoria al documento que modifica")

    # 1. DETECTAR MODO según tipo de documento referenciado
    # Aceptar tanto 'tpo_doc_ref' como 'tipo_doc_ref' (alias) para robustez
    tpo_ref = int(referencia.get('tpo_doc_ref') or referencia.get('tipo_doc_ref') or 33)
    es_modo_bruto = tpo_ref in (39, 41)  # boletas

    # 2. Si no hay items y se da monto_anulacion (CASO 5, 7: anula por texto/giro)
    if not items:
        if not monto_anulacion or monto_anulacion <= 0:
            raise ValueError("Si la NC no lleva items, debes pasar monto_anulacion>0 (= MntTotal del doc referenciado)")
        # Crear un item ficticio cuyo MontoItem haga que MntTotal = monto_anulacion
        if es_modo_bruto:
            # modo bruto: precio incluye IVA → precio = monto_anulacion (es el total)
            precio_item = monto_anulacion
        else:
            # modo neto: precio es sin IVA. Necesitamos que neto + IVA = monto_anulacion
            # neto = monto_anulacion / 1.19; pero por redondeo, mejor:
            #   neto = round(monto_anulacion / 1.19)
            #   Luego IVA será round(neto * 0.19) — pueden no cuadrar exactos
            # Para que el TOTAL sea exacto = monto_anulacion, calculamos al revés
            precio_item = _redondear_clp(monto_anulacion / (1 + IVA_PORCENTAJE / 100.0))
        items = [{
            'nombre': 'Anulación documento referenciado',
            'cantidad': 1,
            'precio_unitario': precio_item,
            'exento': False,
        }]

    # 3. Calcular totales según modo
    if es_modo_bruto:
        tot = _calcular_totales_bruto(items)
    else:
        tot = _calcular_totales_neto(items, descuento_global_pct)
    items_calc = tot['items_calculados']
    mnt_neto = tot['mnt_neto']
    mnt_iva = tot['mnt_iva']
    mnt_exe = tot['mnt_exe']
    mnt_total = tot['mnt_total']

    # 4. IdDoc (con MntBruto=1 solo en modo bruto)
    iddoc_parts = [
        f'<TipoDTE>{tipo_dte}</TipoDTE>',
        f'<Folio>{folio}</Folio>',
        f'<FchEmis>{fecha_emision}</FchEmis>',
    ]
    if es_modo_bruto:
        iddoc_parts.append('<MntBruto>1</MntBruto>')
    else:
        # Modo neto (factura): incluir FmaPago
        iddoc_parts.append(f'<FmaPago>{forma_pago}</FmaPago>')
        if forma_pago == 2 and fecha_vencimiento:
            iddoc_parts.append(f'<FchVenc>{fecha_vencimiento}</FchVenc>')
    iddoc_xml = '<IdDoc>' + ''.join(iddoc_parts) + '</IdDoc>'

    # 5. Emisor
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

    # 6. Receptor
    rut_r = str(receptor['rut']).replace('.', '').strip()
    rec_parts = [
        f'<RUTRecep>{rut_r}</RUTRecep>',
        f'<RznSocRecep>{_escape_xml(receptor["razon_social"][:100])}</RznSocRecep>',
    ]
    # En modo neto (factura) los datos del receptor son más exigentes
    if not es_modo_bruto:
        rec_parts.append(f'<GiroRecep>{_escape_xml(receptor.get("giro", "Sin giro")[:40])}</GiroRecep>')
        if receptor.get('contacto'):
            rec_parts.append(f'<Contacto>{_escape_xml(receptor["contacto"][:80])}</Contacto>')
        rec_parts.append(f'<DirRecep>{_escape_xml(receptor.get("direccion", "Sin dirección")[:70])}</DirRecep>')
        rec_parts.append(f'<CmnaRecep>{_escape_xml(receptor.get("comuna", "Santiago")[:20])}</CmnaRecep>')
    receptor_xml = '<Receptor>' + ''.join(rec_parts) + '</Receptor>'

    # 7. Totales
    tot_parts = []
    if mnt_neto > 0:
        tot_parts.append(f'<MntNeto>{mnt_neto}</MntNeto>')
    if mnt_exe > 0:
        tot_parts.append(f'<MntExe>{mnt_exe}</MntExe>')
    if mnt_neto > 0:
        tot_parts.append(f'<TasaIVA>{IVA_PORCENTAJE}.00</TasaIVA>')
        tot_parts.append(f'<IVA>{mnt_iva}</IVA>')
    tot_parts.append(f'<MntTotal>{mnt_total}</MntTotal>')
    totales_xml = '<Totales>' + ''.join(tot_parts) + '</Totales>'

    encabezado_xml = f'<Encabezado>{iddoc_xml}{emisor_xml}{receptor_xml}{totales_xml}</Encabezado>'

    # 8. Detalles
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
        if not es_modo_bruto:
            if it.get('_desc_pct'):
                linea_parts.append(f'<DescuentoPct>{it["_desc_pct"]:g}</DescuentoPct>')
                linea_parts.append(f'<DescuentoMonto>{it["_desc_aplicado"]}</DescuentoMonto>')
            elif it.get('descuento_monto'):
                linea_parts.append(f'<DescuentoMonto>{it["_desc_aplicado"]}</DescuentoMonto>')
        linea_parts.append(f'<MontoItem>{it["_monto_item"]}</MontoItem>')
        detalles_xml += '<Detalle>' + ''.join(linea_parts) + '</Detalle>'

    # 9. Descuento global (solo modo neto)
    dsc_rcg_xml = ''
    if not es_modo_bruto and descuento_global_pct and tot['desc_global_monto'] > 0:
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

    # 10. Referencia OBLIGATORIA
    folio_ref = str(referencia.get('folio_ref') or '').strip()
    fch_ref = str(referencia.get('fecha_ref') or fecha_emision).strip()
    cod_ref = str(referencia.get('cod_ref') or '1').strip()
    razon = str(referencia.get('razon_ref') or 'ANULA DOCUMENTO').strip()[:90]
    ref_partes = [
        '<NroLinRef>1</NroLinRef>',
        f'<TpoDocRef>{tpo_ref}</TpoDocRef>',
        f'<FolioRef>{_escape_xml(folio_ref)}</FolioRef>',
        f'<FchRef>{fch_ref}</FchRef>',
        f'<CodRef>{_escape_xml(cod_ref)}</CodRef>',
        f'<RazonRef>{_escape_xml(razon)}</RazonRef>',
    ]
    referencia_xml = '<Referencia>' + ''.join(ref_partes) + '</Referencia>'

    # 11. TED
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

    # 12. DTE (sin namespace, lo hereda del EnvioDTE)
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
        'modo': 'bruto' if es_modo_bruto else 'neto',
        'totales': {
            'mnt_neto': mnt_neto, 'mnt_iva': mnt_iva,
            'mnt_exe': mnt_exe, 'mnt_total': mnt_total,
        },
        'ted': ted,
    }
