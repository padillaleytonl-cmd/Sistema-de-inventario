"""
facturacion/dtes/guia_despacho.py
─────────────────────────────────────────────────────────────
Generador de Guía de Despacho Electrónica (DTE tipo 52).

Diferencias críticas vs Factura (33):
  • <TipoDTE>52</TipoDTE>
  • <IndTraslado>X</IndTraslado> OBLIGATORIO en IdDoc (manual SII)
  • <TipoDespacho>X</TipoDespacho> OPCIONAL pero recomendado en IdDoc
  • Cuando IndTraslado=5 (traslado interno): Receptor debe ser = Emisor
  • Cuando IndTraslado distinto de 1/9 (no es venta): MntTotal puede ser 0
    (la guía solo certifica movimiento físico, no operación comercial)
  • Schema XML similar al de Factura

Códigos oficiales SII (manual_muestras_impresas.pdf):
  IndTraslado:
    1: Operación constituye VENTA              (CASO 2, CASO 3)
    2: Ventas por efectuar
    3: Consignaciones
    4: Entrega gratuita
    5: Traslados internos                       (CASO 1)
    6: Otros traslados no venta
    7: Guía de devolución
    8: Traslado para exportación (no venta)
    9: Venta para exportación

  TipoDespacho (quién traslada):
    1: Despacho por cuenta del receptor (cliente)
    2: Despacho por cuenta del emisor a instalaciones del cliente
    3: Despacho por cuenta del emisor a otras instalaciones

Casos oficiales del Set Guía de Despacho SII (4829125):
  CASO 1: Traslado materiales entre bodegas (interno)
          → IndTraslado=5, TipoDespacho=1, Receptor=Emisor, SIN precios
  CASO 2: Venta, despacho por emisor al local del cliente
          → IndTraslado=1, TipoDespacho=2, Receptor=Cliente, CON precios
  CASO 3: Venta, despacho por cliente
          → IndTraslado=1, TipoDespacho=1, Receptor=Cliente, CON precios

─────────────────────────────────────────────────────────────
RESOLUCIÓN EXENTA SII N°154/2025 + ANEXO TÉCNICO 2.5 (Transporte)
─────────────────────────────────────────────────────────────
La Res.154 (postergada al 1-nov-2026 por Res.52/2026) exige nuevos
campos de transporte en guías/facturas que trasladan bienes físicos.
El esquema XSD 2.5 (vigente desde 20-feb-2026) YA los acepta, por lo
que pueden enviarse de forma voluntaria desde ya sin riesgo de rechazo.

El área <Transporte> va DENTRO de <Encabezado>, DESPUÉS de </Receptor>
y ANTES de <Totales>. Orden EXACTO de sub-campos según manual 2.5:
  <Patente>       (67) placa patente vehículo
  <PatenteCarro>  (nuevo) placa carro/remolque (opcional)
  <RUTTrans>      (68) RUT transportista (si ≠ emisor)
  <RUTChofer>     (69) RUT chofer
  <NombreChofer>  (70) nombre chofer
  <DirDest>       (71) dirección destino
  <CmnaDest>      (72) comuna destino
  <CiudadDest>    (73) ciudad destino (opcional)
  <FchSalida>     (nuevo) fecha salida AAAA-MM-DD
  <HraSalida>     (nuevo) hora salida HH:MM:SS
  <FchLlegada>    (nuevo) fecha llegada (oblig. si traslado > 1 día)

IMPORTANTE: el bloque <Transporte> SOLO se incluye cuando hay traslado
físico de bienes (guías y facturas con traslado). NUNCA en servicios.
Si no se conoce un dato (ej. patente), la Res.154 permite indicarlo
expresamente; aquí se omite el sub-campo si viene vacío.
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


def _calcular_totales_guia(items: List[Dict], es_venta: bool) -> Dict:
    """Calcula totales para guía de despacho.

    Para guías NO VENTA (IndTraslado=5,6,7,8): no hay obligación de precios.
    Si los items vienen sin precio, MntTotal=0 (la guía solo movimiento físico).

    Para guías VENTA (IndTraslado=1,9): precios netos como factura.
    """
    bruto_af = 0  # suma items afectos
    bruto_ex = 0  # suma items exentos
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

    if es_venta:
        # Guía con venta: calcular IVA como factura
        mnt_neto = bruto_af
        mnt_iva = _redondear_clp(mnt_neto * IVA_PORCENTAJE / 100.0)
        mnt_exe = bruto_ex
        mnt_total = mnt_neto + mnt_iva + mnt_exe
    else:
        # Guía no venta: sin IVA. Los precios (si los hay) solo son valor referencial.
        # Manual SII: cuando IndTraslado=5 (traslado interno), puede no llevar MntNeto/IVA.
        mnt_neto = 0
        mnt_iva = 0
        mnt_exe = 0
        # MntTotal = 0 cuando es traslado puro sin precios
        # Pero si vienen precios (ej. para tener valor referencial de inventario),
        # podemos usar el bruto_af como MntTotal sin IVA.
        mnt_total = 0

    return {
        'mnt_neto': mnt_neto, 'mnt_iva': mnt_iva,
        'mnt_exe': mnt_exe, 'mnt_total': mnt_total,
        'items_calculados': items_calc,
    }


def _construir_transporte_xml(transporte: Optional[Dict]) -> str:
    """Construye el bloque <Transporte> según Anexo Técnico 2.5 (Res.154/2025).

    El orden de los sub-campos es ESTRICTO según el schema XSD 2.5.
    Cada sub-campo se omite si viene vacío/None (la Res.154 permite
    indicar expresamente la ausencia de un dato como la patente).

    Args:
        transporte: dict opcional con claves:
            patente, patente_carro, rut_transportista,
            rut_chofer, nombre_chofer, dir_dest, cmna_dest, ciudad_dest,
            fch_salida (AAAA-MM-DD), hra_salida (HH:MM:SS), fch_llegada

    Returns:
        '' si transporte es None/vacío, sino '<Transporte>...</Transporte>'
    """
    if not transporte or not isinstance(transporte, dict):
        return ''

    # Normaliza: limpia espacios, descarta vacíos
    def _v(clave, maxlen=None):
        val = transporte.get(clave)
        if val is None:
            return None
        val = str(val).strip()
        if not val:
            return None
        return val[:maxlen] if maxlen else val

    partes = []

    # Orden EXACTO del schema 2.5
    patente = _v('patente', 8)
    if patente:
        partes.append(f'<Patente>{_escape_xml(patente)}</Patente>')

    patente_carro = _v('patente_carro', 8)
    if patente_carro:
        partes.append(f'<PatenteCarro>{_escape_xml(patente_carro)}</PatenteCarro>')

    rut_trans = _v('rut_transportista')
    if rut_trans:
        rut_trans = rut_trans.replace('.', '')
        partes.append(f'<RUTTrans>{_escape_xml(rut_trans)}</RUTTrans>')

    rut_chofer = _v('rut_chofer')
    if rut_chofer:
        rut_chofer = rut_chofer.replace('.', '')
        partes.append(f'<RUTChofer>{_escape_xml(rut_chofer)}</RUTChofer>')

    nombre_chofer = _v('nombre_chofer', 30)
    if nombre_chofer:
        partes.append(f'<NombreChofer>{_escape_xml(nombre_chofer)}</NombreChofer>')

    dir_dest = _v('dir_dest', 70)
    if dir_dest:
        partes.append(f'<DirDest>{_escape_xml(dir_dest)}</DirDest>')

    cmna_dest = _v('cmna_dest', 20)
    if cmna_dest:
        partes.append(f'<CmnaDest>{_escape_xml(cmna_dest)}</CmnaDest>')

    ciudad_dest = _v('ciudad_dest', 20)
    if ciudad_dest:
        partes.append(f'<CiudadDest>{_escape_xml(ciudad_dest)}</CiudadDest>')

    # Campos nuevos Res.154 / Anexo 2.5
    fch_salida = _v('fch_salida')
    if fch_salida:
        partes.append(f'<FchSalida>{_escape_xml(fch_salida)}</FchSalida>')

    hra_salida = _v('hra_salida')
    if hra_salida:
        partes.append(f'<HraSalida>{_escape_xml(hra_salida)}</HraSalida>')

    fch_llegada = _v('fch_llegada')
    if fch_llegada:
        partes.append(f'<FchLlegada>{_escape_xml(fch_llegada)}</FchLlegada>')

    if not partes:
        return ''

    return '<Transporte>' + ''.join(partes) + '</Transporte>'


def generar_guia_despacho_xml(
    caf: CAFParsed,
    folio: int,
    fecha_emision: str,                # 'YYYY-MM-DD'
    emisor: Dict,                       # {rut, razon_social, giro, dir_origen, cmna_origen, [acteco]}
    receptor: Dict,                     # Si IndTraslado=5 → debe ser igual al emisor
    items: List[Dict],
    ind_traslado: int = 5,              # OBLIGATORIO: 1=venta, 5=interno, etc.
    tipo_despacho: int = 0,             # 0=no aplica, 1=receptor, 2=emisor a cliente, 3=emisor a otra
    transporte: Optional[Dict] = None,  # bloque <Transporte> Res.154 (patente, chofer, fechas...)
    referencias: Optional[List[Dict]] = None,  # set certificación, factura previa, etc.
    timestamp_firma: Optional[str] = None,
) -> Dict:
    """Genera XML de Guía de Despacho Electrónica (DTE 52).

    Returns:
        dict con xml(bytes), folio, totales, ted(bytes), documento_id
    """
    tipo_dte = 52

    if not ind_traslado:
        raise ValueError("Guía de Despacho requiere IndTraslado (1=venta, 5=interno, etc.)")

    # IndTraslado 1 (venta) o 9 (venta exportación) = es venta → lleva IVA
    es_venta = ind_traslado in (1, 9)

    # Si es traslado interno (5), receptor DEBE coincidir con emisor (regla SII)
    if ind_traslado == 5:
        receptor = {
            'rut': emisor['rut'],
            'razon_social': emisor['razon_social'],
            'giro': emisor.get('giro', 'Sin giro'),
            'direccion': emisor.get('dir_origen', 'Sin dirección'),
            'comuna': emisor.get('cmna_origen', 'Santiago'),
        }

    # 0. Calcular totales
    tot = _calcular_totales_guia(items, es_venta)
    items_calc = tot['items_calculados']
    mnt_neto = tot['mnt_neto']
    mnt_iva = tot['mnt_iva']
    mnt_exe = tot['mnt_exe']
    mnt_total = tot['mnt_total']

    # 1. IdDoc — con TipoDespacho e IndTraslado obligatorios para guía
    iddoc_parts = [
        f'<TipoDTE>{tipo_dte}</TipoDTE>',
        f'<Folio>{folio}</Folio>',
        f'<FchEmis>{fecha_emision}</FchEmis>',
    ]
    if tipo_despacho:
        iddoc_parts.append(f'<TipoDespacho>{tipo_despacho}</TipoDespacho>')
    iddoc_parts.append(f'<IndTraslado>{ind_traslado}</IndTraslado>')
    iddoc_xml = '<IdDoc>' + ''.join(iddoc_parts) + '</IdDoc>'

    # 2. Emisor (con Acteco obligatorio)
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

    # 3.5 Transporte (Res.154 / Anexo 2.5) — va DESPUÉS de Receptor, ANTES de Totales
    # Solo se incluye si se entregó info de transporte. Para traslado interno (5)
    # también aplica porque mueve bienes físicos.
    transporte_xml = _construir_transporte_xml(transporte)

    # 4. Totales — varía según si es venta o solo movimiento
    tot_parts = []
    if es_venta and mnt_total > 0:
        # Venta: MntNeto, IVA, etc. como factura
        if mnt_neto > 0:
            tot_parts.append(f'<MntNeto>{mnt_neto}</MntNeto>')
        if mnt_exe > 0:
            tot_parts.append(f'<MntExe>{mnt_exe}</MntExe>')
        if mnt_neto > 0:
            tot_parts.append(f'<TasaIVA>{IVA_PORCENTAJE}.00</TasaIVA>')
            tot_parts.append(f'<IVA>{mnt_iva}</IVA>')
        tot_parts.append(f'<MntTotal>{mnt_total}</MntTotal>')
    else:
        # No venta (traslado interno, etc.): MntTotal=0
        tot_parts.append(f'<MntTotal>0</MntTotal>')
    totales_xml = '<Totales>' + ''.join(tot_parts) + '</Totales>'

    encabezado_xml = f'<Encabezado>{iddoc_xml}{emisor_xml}{receptor_xml}{transporte_xml}{totales_xml}</Encabezado>'

    # 5. Detalles
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
        # Solo poner precio si > 0 (CASO 1 traslado interno = sin precio)
        if prc > 0:
            linea_parts.append(f'<PrcItem>{_fmt_cantidad(prc)}</PrcItem>')
            if it.get('_desc_pct'):
                linea_parts.append(f'<DescuentoPct>{it["_desc_pct"]:g}</DescuentoPct>')
                linea_parts.append(f'<DescuentoMonto>{it["_desc_aplicado"]}</DescuentoMonto>')
            elif it.get('descuento_monto'):
                linea_parts.append(f'<DescuentoMonto>{it["_desc_aplicado"]}</DescuentoMonto>')
            linea_parts.append(f'<MontoItem>{it["_monto_item"]}</MontoItem>')
        else:
            # CASO 1: items sin precio. SII permite MontoItem=0 cuando es traslado interno.
            linea_parts.append('<MontoItem>0</MontoItem>')
        detalles_xml += '<Detalle>' + ''.join(linea_parts) + '</Detalle>'

    # 6. Referencias (opcional)
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
                f'<FchRef>{fch_ref}</FchRef>',
            ]
            if cod_ref:
                partes.append(f'<CodRef>{_escape_xml(cod_ref)}</CodRef>')
            if razon:
                partes.append(f'<RazonRef>{_escape_xml(razon)}</RazonRef>')
            referencia_xml += '<Referencia>' + ''.join(partes) + '</Referencia>'

    # 7. TED
    if timestamp_firma is None:
        timestamp_firma = datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
    primer_item_nombre = items_calc[0].get('nombre', 'Producto')[:40] if items_calc else 'Producto'
    ted = construir_ted(
        caf=caf, folio=folio, fecha_emision=fecha_emision,
        rut_receptor=receptor['rut'],
        razon_social_receptor=_escape_xml(receptor['razon_social']),
        monto_total=mnt_total if mnt_total > 0 else 0,
        detalle_primer_item=_escape_xml(primer_item_nombre),
        timestamp_emision=timestamp_firma,
    )

    # 8. DTE
    # IMPORTANTE: construir_ted ya retorna <TED version="1.0">...</TED> COMPLETO
    # NO envolver de nuevo (sería <TED><TED>...</TED></TED> = inválido)
    documento_id = f'F{folio}T{tipo_dte}'
    dte_xml = (
        '<?xml version="1.0" encoding="ISO-8859-1"?>'
        f'<DTE version="1.0">'
        f'<Documento ID="{documento_id}">'
        f'{encabezado_xml}'
        f'{detalles_xml}'
        f'{referencia_xml}'
        f'{ted.decode("iso-8859-1") if isinstance(ted, bytes) else ted}'
        f'<TmstFirma>{timestamp_firma}</TmstFirma>'
        f'</Documento>'
        f'</DTE>'
    ).encode('iso-8859-1')

    return {
        'xml': dte_xml,
        'folio': folio,
        'documento_id': documento_id,
        'totales': {
            'mnt_neto': mnt_neto, 'mnt_iva': mnt_iva,
            'mnt_exe': mnt_exe, 'mnt_total': mnt_total,
        },
        'ted': ted,
    }
