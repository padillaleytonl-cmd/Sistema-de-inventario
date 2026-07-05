"""
facturacion/dtes/exportacion.py
─────────────────────────────────────────────────────────────
Generador del XML de Factura de Exportación (110), Nota de Crédito
de Exportación (112) y Nota de Débito de Exportación (111).

Diferencias críticas vs Factura nacional (33):
  • Elemento raíz <Exportaciones ID="..."> (no <Documento>)
  • Receptor EXTRANJERO: <Extranjero><Nacionalidad>NNN</Nacionalidad></Extranjero>
  • Sección <Transporte><Aduana> con códigos de Aduana (cláusula, vía, puertos, país)
  • Cuerpo principal en MONEDA EXTRANJERA: <Totales><TpoMoneda>EURO</TpoMoneda>...
  • Zona <OtraMoneda> OBLIGATORIA con conversión a pesos (TpoCambio + MntExeOtrMnda + MntTotOtrMnda)
  • Exportación = EXENTA de IVA → todo va en MntExe (no MntNeto/IVA)
  • Flete y Seguro: campos informativos en Aduana Y como recargos globales (DscRcgGlobal)
  • Recargos/descuentos por línea: RecargoMonto / DescuentoMonto

Reglas de cálculo (exportación, montos en moneda extranjera):
  Para cada ítem:
    linea = PrcItem × QtyItem  (o ValorLinea directo si no hay cantidad)
    +recargo línea / -descuento línea según corresponda
    MontoItem = linea ajustada
  MntExe = suma de MontoItem (todo exento, es exportación)
  + recargos globales (flete, seguro, comisiones)
  MntTotal = MntExe + recargos globales - descuentos globales
  OtraMoneda: MntExeOtrMnda = round(MntExe × TpoCambio), MntTotOtrMnda = round(MntTotal × TpoCambio)
"""
from __future__ import annotations
from datetime import datetime
from typing import List, Dict, Optional

from .caf_parser import CAFParsed
from .ted import construir_ted


def _round(valor) -> int:
    # Redondeo estándar (medio hacia arriba), como el SII. El round() nativo de
    # Python usa banker's rounding (4852.5→4852), que descuadra con el SII (→4853).
    from decimal import Decimal, ROUND_HALF_UP
    return int(Decimal(str(valor)).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def _limpiar_rut(rut) -> str:
    """Quita puntos y espacios del RUT, deja formato NNNNNNNN-D que exige el schema."""
    if rut is None:
        return ''
    return str(rut).replace('.', '').replace(' ', '').upper()


def _limpiar_latin1(s: str) -> str:
    """Deja el texto compatible con ISO-8859-1 (codificación que exige el SII),
    sin generar '?' feos. Translitera los caracteres tipográficos más comunes
    a su equivalente ASCII y descarta lo que Latin-1 no puede representar
    (emojis, símbolos raros). Mantiene tildes, ñ, ü, etc., que Latin-1 sí soporta.
    """
    if not s:
        return ''
    reemplazos = {
        '\u2013': '-', '\u2014': '-', '\u2012': '-', '\u2212': '-',
        '\u2018': "'", '\u2019': "'", '\u201A': "'", '\u2032': "'",
        '\u201C': '"', '\u201D': '"', '\u201E': '"', '\u2033': '"',
        '\u2026': '...',
        '\u00A0': ' ',
        '\u2022': '-', '\u25CF': '-', '\u00B7': '.',
        '\u2122': '(TM)', '\u00AE': '(R)', '\u00A9': '(C)',
        '\u20A9': '', '\u20AC': 'EUR',
    }
    for k, v in reemplazos.items():
        s = s.replace(k, v)
    s = s.encode('iso-8859-1', errors='ignore').decode('iso-8859-1')
    while '  ' in s:
        s = s.replace('  ', ' ')
    return s.strip()


def _escape_xml(s) -> str:
    if s is None:
        return ''
    s = _limpiar_latin1(str(s))
    return (s.replace('&', '&amp;')
             .replace('<', '&lt;')
             .replace('>', '&gt;')
             .replace('"', '&quot;')
             .replace("'", '&apos;'))


def _fmt_cant(qty) -> str:
    if qty == int(qty):
        return str(int(qty))
    return ('{:.6f}'.format(qty)).rstrip('0').rstrip('.')


def _fmt_dec(valor, dec=2) -> str:
    """Formatea un decimal con hasta `dec` decimales, sin ceros sobrantes."""
    s = ('{:.{}f}'.format(float(valor), dec)).rstrip('0').rstrip('.')
    return s if s else '0'


def generar_exportacion_xml(
    caf: CAFParsed,
    folio: int,
    fecha_emision: str,
    emisor: Dict,
    receptor: Dict,                 # receptor extranjero: {rut, razon_social, giro, direccion, comuna, nacionalidad}
    items: List[Dict],              # [{nombre, cantidad, precio_unitario, unidad?, valor_linea?, descuento_pct?, recargo_pct?}]
    moneda: str,                    # 'EURO' o 'DOLAR USA'
    tipo_cambio: float,             # tipo de cambio moneda→CLP (Banco Central)
    aduana: Optional[Dict] = None,  # {cod_mod_venta, cod_clau_venta, tot_clau_venta, cod_via_transp, cod_pto_embarque, cod_pto_desemb, cod_pais_recep, cod_pais_destin, cod_unid_tara, unid_peso_bruto, unid_peso_neto, cod_tpo_bultos, tot_bultos, peso_bruto, peso_neto, tara}
    fma_pag_exp: Optional[int] = None,   # forma pago exportación (código aduana)
    flete: Optional[float] = None,       # informativo + recargo global
    seguro: Optional[float] = None,      # informativo + recargo global
    recargos_globales: Optional[List[Dict]] = None,  # [{glosa, valor, tipo_valor='$'|'%'}]
    referencias: Optional[List[Dict]] = None,
    ind_servicio: Optional[int] = None,  # 3=Servicios, etc.
    tipo_dte: int = 110,
    timestamp_firma: Optional[str] = None,
) -> Dict:
    """Genera el XML de un DTE de exportación (110/111/112). SIN XMLDSig (lo hereda el sobre)."""

    aduana = aduana or {}
    recargos_globales = list(recargos_globales or [])

    # ─── 1. Calcular líneas de detalle ───
    detalles = []
    suma_lineas = 0
    for i, it in enumerate(items, start=1):
        nombre = it.get('nombre', 'Item')[:80]
        if 'valor_linea' in it and it.get('cantidad') is None:
            # Línea con valor directo (sin cantidad/precio)
            linea = float(it['valor_linea'])
            qty = None
            prc = None
        else:
            qty = float(it.get('cantidad', 1))
            prc = float(it.get('precio_unitario', 0))
            linea = qty * prc
        # Descuento / recargo por línea. El SII valida en exportación:
        # MontoItem = PrcItem×QtyItem − DescuentoMonto (+ RecargoMonto).
        # Es decir, MontoItem es NETO y el DescuentoPct/Monto se declara en la
        # línea. El MntExe usa la suma de los MontoItem netos (no se vuelve a
        # restar el descuento de línea).
        desc_pct = float(it.get('descuento_pct') or 0)
        recargo_pct = float(it.get('recargo_pct') or 0)
        desc_monto = _round(linea * desc_pct / 100) if desc_pct else 0
        recargo_monto = _round(linea * recargo_pct / 100) if recargo_pct else 0
        monto_item_fmt = _round(linea - desc_monto + recargo_monto)   # NETO
        suma_lineas += monto_item_fmt
        detalles.append({
            'nro': i, 'nombre': nombre, 'qty': qty, 'prc': prc,
            'unidad': it.get('unidad'),
            'desc_pct': desc_pct, 'recargo_pct': recargo_pct,
            'desc_monto': desc_monto, 'recargo_monto': recargo_monto,
            'monto_item': monto_item_fmt,
        })

    # ─── 2. Recargos globales (flete, seguro, comisiones) ───
    # El flete y seguro van como recargos globales además de informativos en Aduana.
    dsc_rcg = []
    nro_dr = 1
    if flete is not None:
        dsc_rcg.append({'nro': nro_dr, 'tipo': 'R', 'glosa': 'FLETE',
                        'tipo_valor': '$', 'valor': flete}); nro_dr += 1
    if seguro is not None:
        dsc_rcg.append({'nro': nro_dr, 'tipo': 'R', 'glosa': 'SEGURO',
                        'tipo_valor': '$', 'valor': seguro}); nro_dr += 1
    for rg in recargos_globales:
        dsc_rcg.append({'nro': nro_dr, 'tipo': rg.get('tipo', 'R'),
                        'glosa': rg.get('glosa', 'RECARGO'),
                        'tipo_valor': rg.get('tipo_valor', '$'),
                        'valor': rg['valor']}); nro_dr += 1

    # MntExe = suma de MontoItem (netos) + recargos globales.
    # El descuento de línea YA está restado en cada MontoItem neto.
    mnt_exe = suma_lineas
    for dr in dsc_rcg:
        if dr['tipo_valor'] == '$':
            if dr['tipo'] == 'R':
                mnt_exe += dr['valor']
            else:
                mnt_exe -= dr['valor']
        else:  # %
            monto = round(suma_lineas * float(dr['valor']) / 100, 2)
            if dr['tipo'] == 'R':
                mnt_exe += monto
            else:
                mnt_exe -= monto

    # En exportación el MntExe/MntTotal del documento deben ser ENTEROS
    # (el TED no acepta decimales y debe coincidir exactamente con MntTotal).
    # El flete/seguro se expresan con decimales solo en Aduana (informativos).
    mnt_exe_fmt = _round(mnt_exe)
    mnt_total = mnt_exe_fmt

    # ─── 3. OtraMoneda: conversión a pesos chilenos ───
    if tipo_cambio is not None:
        mnt_exe_clp = _round(mnt_exe_fmt * tipo_cambio)
        mnt_tot_clp = _round(mnt_total * tipo_cambio)
    else:
        mnt_exe_clp = mnt_tot_clp = 0

    # ─── 4. Encabezado ───
    enc = ['<Encabezado>']
    # IdDoc
    iddoc = [f'<TipoDTE>{tipo_dte}</TipoDTE>', f'<Folio>{folio}</Folio>',
             f'<FchEmis>{fecha_emision}</FchEmis>']
    if ind_servicio is not None:
        iddoc.append(f'<IndServicio>{ind_servicio}</IndServicio>')
    if fma_pag_exp is not None:
        iddoc.append(f'<FmaPagExp>{fma_pag_exp}</FmaPagExp>')
    enc.append('<IdDoc>' + ''.join(iddoc) + '</IdDoc>')
    # Emisor
    em = [f'<RUTEmisor>{_limpiar_rut(emisor["rut"])}</RUTEmisor>',
          f'<RznSoc>{_escape_xml(emisor["razon_social"])}</RznSoc>',
          f'<GiroEmis>{_escape_xml(emisor.get("giro", "Exportacion"))}</GiroEmis>']
    # Teléfono y correo del emisor (opcionales). Schema SII: van DESPUÉS de
    # GiroEmis y ANTES de Acteco. Sin esto, el membrete del PDF sale sin contacto.
    tel_em = emisor.get('telefono') or emisor.get('fono')
    if tel_em:
        if not isinstance(tel_em, (list, tuple)):
            tel_em = [tel_em]
        for t in tel_em[:2]:
            t = str(t).strip()[:20]
            if t:
                em.append(f'<Telefono>{_escape_xml(t)}</Telefono>')
    correo_em = emisor.get('correo') or emisor.get('correo_emisor') or emisor.get('email')
    if correo_em:
        em.append(f'<CorreoEmisor>{_escape_xml(str(correo_em).strip()[:80])}</CorreoEmisor>')
    # Acteco es OBLIGATORIO en exportación (debe ir antes de DirOrigen)
    acteco_val = emisor.get('acteco') or 620100
    em.append(f'<Acteco>{acteco_val}</Acteco>')
    em.append(f'<DirOrigen>{_escape_xml(emisor.get("dir_origen", "Sin direccion"))}</DirOrigen>')
    em.append(f'<CmnaOrigen>{_escape_xml(emisor.get("cmna_origen", "Santiago"))}</CmnaOrigen>')
    enc.append('<Emisor>' + ''.join(em) + '</Emisor>')
    # Receptor extranjero
    re = [f'<RUTRecep>{_limpiar_rut(receptor.get("rut", "55555555-5"))}</RUTRecep>',
          f'<RznSocRecep>{_escape_xml(receptor["razon_social"])}</RznSocRecep>']
    if receptor.get('nacionalidad'):
        re.append(f'<Extranjero><Nacionalidad>{receptor["nacionalidad"]}</Nacionalidad></Extranjero>')
    re.append(f'<GiroRecep>{_escape_xml(receptor.get("giro", "Importador"))}</GiroRecep>')
    re.append(f'<DirRecep>{_escape_xml(receptor.get("direccion", "Sin direccion"))}</DirRecep>')
    re.append(f'<CmnaRecep>{_escape_xml(receptor.get("comuna", "Exterior"))}</CmnaRecep>')
    enc.append('<Receptor>' + ''.join(re) + '</Receptor>')
    # Transporte + Aduana
    if aduana:
        ad = ['<Aduana>']
        if aduana.get('cod_mod_venta') is not None:
            ad.append(f'<CodModVenta>{aduana["cod_mod_venta"]}</CodModVenta>')
        if aduana.get('cod_clau_venta') is not None:
            ad.append(f'<CodClauVenta>{aduana["cod_clau_venta"]}</CodClauVenta>')
        if aduana.get('tot_clau_venta') is not None:
            ad.append(f'<TotClauVenta>{_fmt_dec(aduana["tot_clau_venta"], 2)}</TotClauVenta>')
        if aduana.get('cod_via_transp') is not None:
            ad.append(f'<CodViaTransp>{aduana["cod_via_transp"]}</CodViaTransp>')
        if aduana.get('cod_pto_embarque') is not None:
            ad.append(f'<CodPtoEmbarque>{aduana["cod_pto_embarque"]}</CodPtoEmbarque>')
        if aduana.get('cod_pto_desemb') is not None:
            ad.append(f'<CodPtoDesemb>{aduana["cod_pto_desemb"]}</CodPtoDesemb>')
        if aduana.get('tara'):
            ad.append(f'<Tara>{aduana["tara"]}</Tara>')
        if aduana.get('cod_unid_tara') is not None:
            ad.append(f'<CodUnidMedTara>{aduana["cod_unid_tara"]}</CodUnidMedTara>')
        if aduana.get('peso_bruto') is not None:
            ad.append(f'<PesoBruto>{_fmt_dec(aduana["peso_bruto"], 2)}</PesoBruto>')
            if aduana.get('unid_peso_bruto') is not None:
                ad.append(f'<CodUnidPesoBruto>{aduana["unid_peso_bruto"]}</CodUnidPesoBruto>')
        if aduana.get('peso_neto') is not None:
            ad.append(f'<PesoNeto>{_fmt_dec(aduana["peso_neto"], 2)}</PesoNeto>')
            if aduana.get('unid_peso_neto') is not None:
                ad.append(f'<CodUnidPesoNeto>{aduana["unid_peso_neto"]}</CodUnidPesoNeto>')
        if aduana.get('tot_items') is not None:
            ad.append(f'<TotItems>{aduana["tot_items"]}</TotItems>')
        if aduana.get('tot_bultos') is not None:
            ad.append(f'<TotBultos>{aduana["tot_bultos"]}</TotBultos>')
        if aduana.get('cod_tpo_bultos') is not None:
            bultos = ['<TipoBultos>',
                      f'<CodTpoBultos>{aduana["cod_tpo_bultos"]}</CodTpoBultos>']
            if aduana.get('cant_bultos') is not None:
                bultos.append(f'<CantBultos>{aduana["cant_bultos"]}</CantBultos>')
            # Marcas: el SII lo exige obligatorio en exportación (HED-2-804)
            marcas = aduana.get('marcas', 'SIN MARCAS')
            bultos.append(f'<Marcas>{_escape_xml(marcas)}</Marcas>')
            # Para contenedores, el SII exige IdContainer y Sello (HED-2-804)
            if aduana.get('id_container'):
                bultos.append(f'<IdContainer>{_escape_xml(aduana["id_container"])}</IdContainer>')
            if aduana.get('sello'):
                bultos.append(f'<Sello>{_escape_xml(aduana["sello"])}</Sello>')
            if aduana.get('emisor_sello'):
                bultos.append(f'<EmisorSello>{_escape_xml(aduana["emisor_sello"])}</EmisorSello>')
            bultos.append('</TipoBultos>')
            ad.append(''.join(bultos))
        if flete is not None:
            ad.append(f'<MntFlete>{_fmt_dec(flete, 2)}</MntFlete>')
        if seguro is not None:
            ad.append(f'<MntSeguro>{_fmt_dec(seguro, 2)}</MntSeguro>')
        if aduana.get('cod_pais_recep') is not None:
            ad.append(f'<CodPaisRecep>{aduana["cod_pais_recep"]}</CodPaisRecep>')
        if aduana.get('cod_pais_destin') is not None:
            ad.append(f'<CodPaisDestin>{aduana["cod_pais_destin"]}</CodPaisDestin>')
        ad.append('</Aduana>')
        enc.append('<Transporte>' + ''.join(ad) + '</Transporte>')
    # Totales (en moneda extranjera)
    tot = [f'<TpoMoneda>{moneda}</TpoMoneda>',
           f'<MntExe>{mnt_exe_fmt}</MntExe>',
           f'<MntTotal>{mnt_total}</MntTotal>']
    enc.append('<Totales>' + ''.join(tot) + '</Totales>')
    # OtraMoneda (conversión a pesos) — va al final del Encabezado.
    # Solo se incluye si hay tipo_cambio (es opcional en el schema; LibreDTE
    # tampoco la agrega salvo que el emisor la informe explícitamente).
    if tipo_cambio is not None:
        otra = [f'<TpoMoneda>PESO CL</TpoMoneda>',
                f'<TpoCambio>{_fmt_dec(tipo_cambio, 4)}</TpoCambio>',
                f'<MntExeOtrMnda>{mnt_exe_clp}</MntExeOtrMnda>',
                f'<MntTotOtrMnda>{mnt_tot_clp}</MntTotOtrMnda>']
        enc.append('<OtraMoneda>' + ''.join(otra) + '</OtraMoneda>')
    enc.append('</Encabezado>')
    encabezado_xml = '\n'.join(enc)

    # ─── 5. Detalle ───
    detalles_xml = ''
    for d in detalles:
        partes = [f'<NroLinDet>{d["nro"]}</NroLinDet>',
                  '<IndExe>1</IndExe>',  # exportación: cada línea es exenta
                  f'<NmbItem>{_escape_xml(d["nombre"])}</NmbItem>']
        if d['qty'] is not None:
            partes.append(f'<QtyItem>{_fmt_cant(d["qty"])}</QtyItem>')
            if d['unidad']:
                partes.append(f'<UnmdItem>{_escape_xml(d["unidad"])}</UnmdItem>')
            partes.append(f'<PrcItem>{_fmt_dec(d["prc"], 4)}</PrcItem>')
        # Descuento/recargo de línea: el Set Básico certificado emite
        # DescuentoPct + DescuentoMonto juntos (orden schema: Pct antes de Monto;
        # bloque descuento completo antes del bloque recargo). El Monto solo se
        # emite si es > 0 (MntImpType no acepta 0).
        if d['desc_pct']:
            partes.append(f'<DescuentoPct>{d["desc_pct"]:g}</DescuentoPct>')
            if d['desc_monto']:
                partes.append(f'<DescuentoMonto>{d["desc_monto"]}</DescuentoMonto>')
        if d['recargo_pct']:
            partes.append(f'<RecargoPct>{d["recargo_pct"]:g}</RecargoPct>')
            if d['recargo_monto']:
                partes.append(f'<RecargoMonto>{d["recargo_monto"]}</RecargoMonto>')
        partes.append(f'<MontoItem>{d["monto_item"]}</MontoItem>')
        detalles_xml += '<Detalle>' + ''.join(partes) + '</Detalle>\n'

    # ─── 6. Descuentos/Recargos globales ───
    dsc_rcg_xml = ''
    for dr in dsc_rcg:
        partes = [f'<NroLinDR>{dr["nro"]}</NroLinDR>',
                  f'<TpoMov>{dr["tipo"]}</TpoMov>',
                  f'<GlosaDR>{_escape_xml(dr["glosa"])}</GlosaDR>',
                  f'<TpoValor>{dr["tipo_valor"]}</TpoValor>',
                  f'<ValorDR>{_fmt_dec(dr["valor"], 2)}</ValorDR>',
                  '<IndExeDR>1</IndExeDR>']  # exportación es exenta
        dsc_rcg_xml += '<DscRcgGlobal>' + ''.join(partes) + '</DscRcgGlobal>\n'

    # ─── 7. Referencias ───
    referencia_xml = ''
    if referencias:
        for idx, ref in enumerate(referencias, start=1):
            partes = [f'<NroLinRef>{idx}</NroLinRef>']
            if ref.get('tpo_doc_ref'):
                partes.append(f'<TpoDocRef>{ref["tpo_doc_ref"]}</TpoDocRef>')
            if ref.get('folio_ref'):
                partes.append(f'<FolioRef>{ref["folio_ref"]}</FolioRef>')
            if ref.get('fecha_ref'):
                partes.append(f'<FchRef>{ref["fecha_ref"]}</FchRef>')
            if ref.get('cod_ref'):
                partes.append(f'<CodRef>{ref["cod_ref"]}</CodRef>')
            if ref.get('razon_ref'):
                partes.append(f'<RazonRef>{_escape_xml(ref["razon_ref"])}</RazonRef>')
            referencia_xml += '<Referencia>' + ''.join(partes) + '</Referencia>\n'

    # ─── 8. TED (el monto del TED es MntTotal en moneda extranjera) ───
    if timestamp_firma is None:
        timestamp_firma = datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
    primer_item = detalles[0]['nombre'][:40] if detalles else 'Item'
    ted = construir_ted(
        caf=caf, folio=folio, fecha_emision=fecha_emision,
        rut_receptor=_limpiar_rut(receptor.get('rut', '55555555-5')),
        razon_social_receptor=_escape_xml(receptor['razon_social']),
        monto_total=mnt_total,
        detalle_primer_item=_escape_xml(primer_item),
        timestamp_emision=timestamp_firma,
    )

    # ─── 9. DTE completo: raíz <Exportaciones> ───
    documento_id = f'F{folio}T{tipo_dte}'
    dte_xml = (
        '<?xml version="1.0" encoding="ISO-8859-1"?>\n'
        '<DTE version="1.0">\n'
        f'<Exportaciones ID="{documento_id}">\n'
        f'{encabezado_xml}\n'
        f'{detalles_xml}\n'
        f'{dsc_rcg_xml}\n'
        f'{referencia_xml}\n'
        f'{ted.decode("iso-8859-1")}\n'
        f'<TmstFirma>{timestamp_firma}</TmstFirma>\n'
        f'</Exportaciones>\n'
        '</DTE>'
    )

    return {
        'xml': dte_xml.encode('iso-8859-1', errors='replace'),
        'folio': folio,
        'tipo_dte': tipo_dte,
        'documento_id': documento_id,
        'totales': {
            'mnt_exe': mnt_exe_fmt, 'mnt_total': mnt_total,
            'mnt_exe_clp': mnt_exe_clp, 'mnt_tot_clp': mnt_tot_clp,
        },
        'ted': ted,
    }
