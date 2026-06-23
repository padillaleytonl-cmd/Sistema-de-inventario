# -*- coding: utf-8 -*-
"""
facturacion/dtes/pdf_dte.py
─────────────────────────────────────────────────────────────
Representación gráfica (PDF) UNIFICADA de Documentos Tributarios Electrónicos.

Detecta el tipo desde el <TipoDTE> del XML y dibuja los bloques que
correspondan a cada uno:

  33  Factura Electrónica            61  Nota de Crédito Electrónica
  34  Factura Exenta Electrónica     56  Nota de Débito Electrónica
  39  Boleta Electrónica             52  Guía de Despacho Electrónica
  41  Boleta Exenta Electrónica      43  Liquidación-Factura Electrónica
  46  Factura de Compra Electrónica  110 Factura de Exportación Electrónica
  111 Nota de Débito de Exportación  112 Nota de Crédito de Exportación

Dos formatos:
  • "carta" : hoja carta, maquetación formal según instructivo SII.
  • "rollo" : ticket térmico 80mm (boletas).

Cumple el Manual de Muestras Impresas del SII:
  - Timbre PDF417 (ECL=5, relación 3:1), abajo a ≥2cm del borde izquierdo,
    tamaño dentro del rango 2x5 a 4x9 cm.
  - Razón social destacada; giro sin abreviar.
  - Recuadro arriba-derecha: RUT emisor CON separador de miles, tipo, folio.
  - Copia CEDIBLE para los documentos que la requieren (facturas, NC, ND,
    guías, liquidación, factura de compra, exportaciones); NO para boletas.

Uso:
    from facturacion.dtes.pdf_dte import generar_pdf_dte
    pdf_bytes = generar_pdf_dte(dte_xml, formato="carta")
"""
from __future__ import annotations

import io
import re
import math
from html import unescape as _html_unescape
from typing import Optional

from reportlab.lib.pagesizes import letter
from reportlab.lib.units import mm, cm
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase.pdfmetrics import stringWidth

from pdf417gen import encode as _pdf417_encode, render_image as _pdf417_render


# ─────────────────────────────────────────────────────────────────────────────
# 0. Catálogo de tipos de DTE
# ─────────────────────────────────────────────────────────────────────────────
# nombre   : título que va en el recuadro rojo del SII
# receptor : si dibuja el bloque de receptor completo (giro/dir/comuna)
# referencia: si dibuja el bloque de Referencia al documento modificado
# es_boleta: si admite formato rollo y RUT genérico de consumidor
# cedible  : si genera copia CEDIBLE. Según Manual de Muestras Impresas:
#            SÍ → factura(33), exenta(34), guía(52), fact compra(46), liquidación(43)
#            NO → NC(61), ND(56), boletas, exportaciones
# acuse    : si lleva el recuadro de Acuse de Recibo (Ley 19.983).
#            Mismos que cedible (NC/ND NO llevan acuse).
# leyenda_cedible: texto de destino en la copia cedible.
#            Guía → "CEDIBLE CON SU FACTURA"; el resto → "CEDIBLE".
DTE_INFO = {
    33:  {"nombre": "FACTURA ELECTRÓNICA",                   "receptor": True,  "referencia": True,  "es_boleta": False, "cedible": True,  "acuse": True,  "leyenda_cedible": "CEDIBLE"},
    34:  {"nombre": "FACTURA NO AFECTA O EXENTA ELECTRÓNICA","receptor": True,  "referencia": True,  "es_boleta": False, "cedible": True,  "acuse": True,  "leyenda_cedible": "CEDIBLE"},
    39:  {"nombre": "BOLETA ELECTRÓNICA",                    "receptor": True,  "referencia": False, "es_boleta": True,  "cedible": False, "acuse": False, "leyenda_cedible": ""},
    41:  {"nombre": "BOLETA EXENTA ELECTRÓNICA",             "receptor": True,  "referencia": False, "es_boleta": True,  "cedible": False, "acuse": False, "leyenda_cedible": ""},
    43:  {"nombre": "LIQUIDACIÓN FACTURA ELECTRÓNICA",       "receptor": True,  "referencia": True,  "es_boleta": False, "cedible": True,  "acuse": True,  "leyenda_cedible": "CEDIBLE"},
    46:  {"nombre": "FACTURA DE COMPRA ELECTRÓNICA",         "receptor": True,  "referencia": True,  "es_boleta": False, "cedible": True,  "acuse": True,  "leyenda_cedible": "CEDIBLE"},
    52:  {"nombre": "GUÍA DE DESPACHO ELECTRÓNICA",          "receptor": True,  "referencia": True,  "es_boleta": False, "cedible": True,  "acuse": True,  "leyenda_cedible": "CEDIBLE CON SU FACTURA"},
    56:  {"nombre": "NOTA DE DÉBITO ELECTRÓNICA",            "receptor": True,  "referencia": True,  "es_boleta": False, "cedible": False, "acuse": False, "leyenda_cedible": ""},
    61:  {"nombre": "NOTA DE CRÉDITO ELECTRÓNICA",           "receptor": True,  "referencia": True,  "es_boleta": False, "cedible": False, "acuse": False, "leyenda_cedible": ""},
    110: {"nombre": "FACTURA DE EXPORTACIÓN ELECTRÓNICA",    "receptor": True,  "referencia": True,  "es_boleta": False, "cedible": False, "acuse": False, "leyenda_cedible": ""},
    111: {"nombre": "NOTA DE DÉBITO DE EXPORTACIÓN ELECTRÓNICA","receptor": True,"referencia": True, "es_boleta": False, "cedible": False, "acuse": False, "leyenda_cedible": ""},
    112: {"nombre": "NOTA DE CRÉDITO DE EXPORTACIÓN ELECTRÓNICA","receptor": True,"referencia": True,"es_boleta": False, "cedible": False, "acuse": False, "leyenda_cedible": ""},
}

# Resolución que autoriza al emisor (en certificación es 0 del año vigente)
RESOLUCION_NRO = 0
RESOLUCION_ANIO = 2026

# Glosa del código de referencia (para NC/ND)
COD_REF_GLOSA = {
    "1": "Anula documento de referencia",
    "2": "Corrige texto del documento de referencia",
    "3": "Corrige montos",
}

# Nombre legible del tipo de documento referenciado
TPO_DOC_REF_NOMBRE = {
    "33": "Factura Electrónica",
    "34": "Factura Exenta Electrónica",
    "39": "Boleta Electrónica",
    "41": "Boleta Exenta Electrónica",
    "43": "Liquidación-Factura",
    "46": "Factura de Compra",
    "52": "Guía de Despacho",
    "56": "Nota de Débito",
    "61": "Nota de Crédito",
    "110": "Factura de Exportación",
    "111": "Nota de Débito Exportación",
    "112": "Nota de Crédito Exportación",
    "812": "Resolución SNA",
    "SET": "Set de Pruebas",
}


# ─────────────────────────────────────────────────────────────────────────────
# 1. Extracción de datos del XML
# ─────────────────────────────────────────────────────────────────────────────

def _txt(xml: str, tag: str) -> str:
    """Devuelve el contenido del primer <tag>...</tag>, o '' si no existe.
    Tolera atributos en la etiqueta de apertura (ej <TED version="1.0">).
    Des-escapa las entidades XML (&quot; &amp; &lt; &gt; &apos; y numéricas)
    para que comillas, ampersands y acentos se impriman como caracteres reales
    en el PDF y no como su entidad literal."""
    m = re.search(rf'<{tag}(?:\s[^>]*)?>(.*?)</{tag}>', xml, re.DOTALL)
    if not m:
        return ''
    return _unescape_xml(m.group(1).strip())


def _unescape_xml(s: str) -> str:
    """Convierte entidades XML a su carácter real. Útil porque el texto se
    extrae con regex (no con un parser que lo haría automáticamente)."""
    if not s or '&' not in s:
        return s
    return _html_unescape(s)


def _miles(valor) -> str:
    """Formatea un entero con separador de miles chileno (punto). 19900 -> 19.900"""
    try:
        return f"{int(round(float(valor))):,}".replace(",", ".")
    except (ValueError, TypeError):
        return str(valor)


def _rut_con_miles(rut: str) -> str:
    """Formatea un RUT con separador de miles: 76922862-4 -> 76.922.862-4.
    Exigido por el Manual de Muestras Impresas del SII para el RUT del emisor."""
    if not rut:
        return rut
    rut = rut.strip().upper().replace(".", "")
    if "-" not in rut:
        return rut
    cuerpo, dv = rut.rsplit("-", 1)
    try:
        cuerpo_fmt = f"{int(cuerpo):,}".replace(",", ".")
    except ValueError:
        cuerpo_fmt = cuerpo
    return f"{cuerpo_fmt}-{dv}"


def _wrap(texto: str, max_chars: int) -> list:
    """Parte un texto en líneas de hasta max_chars sin cortar palabras."""
    if not texto:
        return []
    palabras = texto.split()
    lineas, linea = [], ""
    for pal in palabras:
        if len(linea) + len(pal) + 1 > max_chars:
            if linea:
                lineas.append(linea)
            linea = pal
        else:
            linea = (linea + " " + pal).strip()
    if linea:
        lineas.append(linea)
    return lineas


def _wrap_ancho(texto: str, max_ancho: float, font: str, size: float) -> list:
    """Parte un texto en líneas que no superen max_ancho (en puntos), midiendo
    el ancho real de cada palabra con la fuente y tamaño dados. Evita que el
    texto del emisor invada el recuadro rojo del SII. Si una palabra sola es
    más ancha que el máximo, la deja igual (no la corta a la mitad)."""
    from reportlab.pdfbase.pdfmetrics import stringWidth
    if not texto:
        return []
    palabras = str(texto).split()
    lineas, linea = [], ""
    for pal in palabras:
        prueba = (linea + " " + pal).strip()
        if stringWidth(prueba, font, size) > max_ancho and linea:
            lineas.append(linea)
            linea = pal
        else:
            linea = prueba
    if linea:
        lineas.append(linea)
    return lineas


def parsear_dte_xml(dte_xml: bytes) -> dict:
    """Extrae los datos para la representación gráfica desde el XML de cualquier DTE."""
    if isinstance(dte_xml, str):
        s = dte_xml
    else:
        s = dte_xml.decode('iso-8859-1', errors='replace')

    # Aislar el Encabezado para que los tags de Emisor/Receptor no se confundan
    encabezado = _txt(s, 'Encabezado') or s

    tipo_dte = _txt(s, 'TipoDTE')
    try:
        tipo_int = int(tipo_dte)
    except (ValueError, TypeError):
        tipo_int = 39
    info = DTE_INFO.get(tipo_int, DTE_INFO[39])

    # Emisor
    emisor = {
        'rut': _txt(encabezado, 'RUTEmisor'),
        'razon_social': _txt(encabezado, 'RznSocEmisor') or _txt(encabezado, 'RznSoc'),
        'giro': _txt(encabezado, 'GiroEmisor') or _txt(encabezado, 'GiroEmis') or _txt(encabezado, 'Giro'),
        'direccion': _txt(encabezado, 'DirOrigen'),
        'comuna': _txt(encabezado, 'CmnaOrigen'),
        'ciudad': _txt(encabezado, 'CiudadOrigen'),
        'telefono': _txt(encabezado, 'Telefono'),
        'correo': _txt(encabezado, 'CorreoEmisor'),
        'acteco': _txt(encabezado, 'Acteco'),
        # Sucursal (si se informa)
        'sucursal': _txt(encabezado, 'Sucursal'),
        'cdg_sii_sucursal': _txt(encabezado, 'CdgSIISucur'),
    }

    folio = _txt(encabezado, 'Folio') or _txt(s, 'Folio')
    fch_emis = _txt(encabezado, 'FchEmis') or _txt(s, 'FchEmis')

    # Guía de despacho: tipo de traslado y datos de transporte
    IND_TRASLADO_GLOSA = {
        "1": "Operación constituye venta", "2": "Ventas por efectuar",
        "3": "Consignaciones", "4": "Entrega gratuita", "5": "Traslados internos",
        "6": "Otros traslados no venta", "7": "Guía de devolución",
        "8": "Traslado para exportación (no venta)", "9": "Venta para exportación",
    }
    ind_tras = _txt(encabezado, 'IndTraslado')
    tipo_despacho = _txt(encabezado, 'TipoDespacho')
    transporte_xml = _txt(s, 'Transporte')
    TIPO_DESPACHO_GLOSA = {
        "1": "Por cuenta del receptor",
        "2": "Por cuenta del emisor a instalaciones del cliente",
        "3": "Por cuenta del emisor a otras instalaciones",
    }
    # Bultos (pueden ser varios)
    bultos = []
    if transporte_xml:
        for b in re.findall(r'<TipoBultos>(.*?)</TipoBultos>', transporte_xml, re.DOTALL):
            bultos.append({
                'cant': _txt(b, 'CantBultos'),
                'tipo': _txt(b, 'CodTipoBultos'),
                'marcas': _txt(b, 'Marcas'),
                'id_container': _txt(b, 'IdContainer'),
            })
    patente = _txt(transporte_xml, 'Patente') if transporte_xml else ''
    transporte = {
        'ind_traslado': ind_tras,
        'ind_traslado_glosa': IND_TRASLADO_GLOSA.get(ind_tras, ''),
        'tipo_despacho': tipo_despacho,
        'tipo_despacho_glosa': TIPO_DESPACHO_GLOSA.get(tipo_despacho, ''),
        'patente': patente,
        'rut_transportista': _txt(transporte_xml, 'RUTTrans') if transporte_xml else '',
        'rut_chofer': _txt(transporte_xml, 'RUTChofer') if transporte_xml else '',
        'nombre_chofer': _txt(transporte_xml, 'NombreChofer') if transporte_xml else '',
        'dir_dest': _txt(transporte_xml, 'DirDest') if transporte_xml else '',
        'cmna_dest': _txt(transporte_xml, 'CmnaDest') if transporte_xml else '',
        'ciudad_dest': _txt(transporte_xml, 'CiudadDest') if transporte_xml else '',
        'bultos': bultos,
        # Hay transporte declarado (para decidir si dibujar el bloque)
        'tiene_datos': bool(transporte_xml and (
            patente or _txt(transporte_xml, 'NombreChofer') or
            _txt(transporte_xml, 'RUTTrans') or _txt(transporte_xml, 'DirDest'))),
    }

    # Receptor
    receptor = {
        'rut': _txt(encabezado, 'RUTRecep'),
        'razon_social': _txt(encabezado, 'RznSocRecep'),
        'giro': _txt(encabezado, 'GiroRecep'),
        'direccion': _txt(encabezado, 'DirRecep'),
        'comuna': _txt(encabezado, 'CmnaRecep'),
        'ciudad': _txt(encabezado, 'CiudadRecep'),
        'contacto': _txt(encabezado, 'Contacto'),
    }
    _rut_rec_limpio = (receptor['rut'] or '').replace('.', '').replace('-', '').strip().upper()
    receptor['es_generico'] = _rut_rec_limpio in ('666666666', '66666666K', '')
    if receptor['es_generico']:
        receptor['rut'] = receptor['rut'] or '66666666-6'
        if not receptor['razon_social'] or receptor['razon_social'].lower() == 'consumidor final':
            receptor['razon_social'] = 'Cliente General'

    # Totales. Exportación lleva los montos en moneda extranjera (MntExe/MntTotal
    # están en USD); el peso va en OtraMoneda. Tomamos ambos.
    totales = {
        'neto': _txt(encabezado, 'MntNeto'),
        'exento': _txt(encabezado, 'MntExe'),
        'iva': _txt(encabezado, 'IVA'),
        'tasa_iva': _txt(encabezado, 'TasaIVA') or '19',
        'total': _txt(encabezado, 'MntTotal'),
        # Exportación / moneda
        'moneda': _txt(encabezado, 'TpoMoneda'),
        'tipo_cambio': _txt(encabezado, 'TpoCambio'),
        'total_otra_moneda': _txt(encabezado, 'MntTotOtrMnda'),
        # Factura de compra: IVA retenido
        'iva_retenido': _txt(encabezado, 'IVANoRet') or '',
    }
    # Impuesto retenido (factura de compra 46)
    imp_reten = _txt(encabezado, 'ImptoReten')
    if imp_reten:
        totales['monto_reten'] = _txt(imp_reten, 'MontoImp')

    # Items
    items = []
    for det in re.findall(r'<Detalle>(.*?)</Detalle>', s, re.DOTALL):
        items.append({
            'nombre': _txt(det, 'NmbItem'),
            'descripcion': _txt(det, 'DscItem'),
            'cantidad': _txt(det, 'QtyItem'),
            'unidad': _txt(det, 'UnmdItem'),
            'precio': _txt(det, 'PrcItem'),
            'descuento_pct': _txt(det, 'DescuentoPct'),
            'monto': _txt(det, 'MontoItem'),
            'exento': bool(re.search(r'<IndExe>', det)),
        })

    # Referencias
    referencias = []
    for ref in re.findall(r'<Referencia>(.*?)</Referencia>', s, re.DOTALL):
        cod_ref = _txt(ref, 'CodRef')
        tpo = _txt(ref, 'TpoDocRef')
        referencias.append({
            'tpo_doc_ref': tpo,
            'tpo_doc_ref_nombre': TPO_DOC_REF_NOMBRE.get(tpo, f"Doc. {tpo}"),
            'folio_ref': _txt(ref, 'FolioRef'),
            'fecha_ref': _txt(ref, 'FchRef'),
            'cod_ref': cod_ref,
            'cod_ref_glosa': COD_REF_GLOSA.get(cod_ref, ''),
            'razon_ref': _txt(ref, 'RazonRef'),
        })

    # TED (timbre)
    ted_match = re.search(r'<TED.*?</TED>', s, re.DOTALL)
    ted = ted_match.group(0) if ted_match else ''

    return {
        'tipo_dte': tipo_dte,
        'tipo_int': tipo_int,
        'info': info,
        'folio': folio,
        'fch_emis': fch_emis,
        'emisor': emisor,
        'receptor': receptor,
        'totales': totales,
        'items': items,
        'referencias': referencias,
        'transporte': transporte,
        'ted': ted,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 2. Timbre PDF417
# ─────────────────────────────────────────────────────────────────────────────

def _generar_timbre_pdf417(ted: str) -> Optional[ImageReader]:
    """Imagen del timbre PDF417 desde el TED.
    Parámetros exigidos por el Instructivo Técnico del SII (Anexo 2):
    18 columnas, ECL nivel 5, relación de módulo 3:1, encoding ISO-8859-1.
    Con el TED real (~700 bytes) el código genera ~24 filas, dando una
    figura compacta y legible (no excesivamente apaisada)."""
    if not ted:
        return None
    ted_bytes = ted.encode('iso-8859-1', errors='replace')
    # 18 columnas y ECL 5 son obligatorios. scale alto = módulos grandes (nítido).
    codes = _pdf417_encode(ted_bytes, columns=18, security_level=5)
    img = _pdf417_render(codes, scale=4, ratio=3, padding=2)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return ImageReader(buf)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Formato CARTA — una copia (tributaria o cedible)
# ─────────────────────────────────────────────────────────────────────────────

def _dibujar_copia_carta(c, d: dict, url_consulta: str, etiqueta_copia: str = ""):
    """Dibuja UNA copia del documento en la página actual del canvas.
    etiqueta_copia: '' = copia tributaria (sin leyenda), 'CEDIBLE' = copia cedible."""
    W, H = letter
    em = d['emisor']
    info = d['info']

    # ── Recuadro rojo del SII (arriba a la derecha) ──
    rec_w, rec_h = 8 * cm, 3.2 * cm
    rec_x, rec_y = W - rec_w - 2 * cm, H - rec_h - 2 * cm
    c.setStrokeColorRGB(0.8, 0, 0)
    c.setLineWidth(2)
    c.rect(rec_x, rec_y, rec_w, rec_h)
    c.setFillColorRGB(0.8, 0, 0)
    c.setFont("Helvetica-Bold", 13)
    # RUT del emisor CON separador de miles (exigido por el manual)
    c.drawCentredString(rec_x + rec_w / 2, rec_y + rec_h - 0.7 * cm, f"R.U.T.: {_rut_con_miles(em['rut'])}")
    titulo = info['nombre']
    c.setFont("Helvetica-Bold", 13 if len(titulo) <= 20 else (9 if len(titulo) <= 34 else 7.5))
    c.drawCentredString(rec_x + rec_w / 2, rec_y + rec_h - 1.45 * cm, titulo)
    c.setFont("Helvetica-Bold", 13)
    c.drawCentredString(rec_x + rec_w / 2, rec_y + rec_h - 2.2 * cm, ("BORRADOR" if str(d['folio']) in ("0", "") else f"N° {d['folio']}"))
    c.setFont("Helvetica-Bold", 10)
    c.drawCentredString(rec_x + rec_w / 2, rec_y + 0.4 * cm, "S.I.I. - SANTIAGO")
    c.setFillColorRGB(0, 0, 0)

    # ── Datos del emisor (arriba a la izquierda). Razón social DESTACADA ──
    # El texto NO debe invadir el recuadro rojo del SII. El ancho útil va
    # desde x hasta el borde izquierdo del recuadro, menos un margen de 0.4cm.
    x = 2 * cm
    y = H - 2.4 * cm
    ancho_util = rec_x - x - 0.4 * cm
    # Razón social destacada: si es muy ancha, se reduce el tamaño para que
    # quepa antes del recuadro (nunca se mete debajo del RUT).
    razon = (em['razon_social'] or '')
    rs_size = 15
    while rs_size > 9 and stringWidth(razon, "Helvetica-Bold", rs_size) > ancho_util:
        rs_size -= 0.5
    c.setFont("Helvetica-Bold", rs_size)
    c.drawString(x, y, razon)
    c.setFont("Helvetica", 9)
    y -= 0.55 * cm
    # Giro (se parte por ANCHO real, máx 2 líneas)
    giro = (em['giro'] or '')
    if giro:
        c.setFont("Helvetica-Oblique", 9)
        for linea_giro in _wrap_ancho(giro, ancho_util, "Helvetica-Oblique", 9)[:2]:
            c.drawString(x, y, linea_giro)
            y -= 0.42 * cm
        c.setFont("Helvetica", 9)
    # Dirección casa matriz con comuna y ciudad
    dir_cm = ", ".join([p for p in [em['direccion'], em['comuna'], em.get('ciudad', '')] if p])
    lineas_emisor = [f"Casa Matriz: {dir_cm}".strip(': ')]
    if em.get('sucursal'):
        lineas_emisor.append(f"Sucursal: {em['sucursal']}")
    # Contacto (teléfono / correo) en una línea si vienen
    contacto = []
    if em.get('telefono'):
        contacto.append(f"Tel: {em['telefono']}")
    if em.get('correo'):
        contacto.append(em['correo'])
    if contacto:
        lineas_emisor.append("  ·  ".join(contacto))
    # Cada línea del emisor se envuelve por ancho real para no tocar el recuadro
    for linea in lineas_emisor:
        if not linea:
            continue
        for sub in _wrap_ancho(linea, ancho_util, "Helvetica", 9):
            c.drawString(x, y, sub)
            y -= 0.42 * cm

    # ── Fecha de emisión ──
    y = rec_y - 0.8 * cm
    c.setFont("Helvetica-Bold", 9)
    c.drawString(x, y, f"Fecha Emisión: {d['fch_emis']}")

    # ── Bloque RECEPTOR (dentro de un recuadro) ──
    rec = d['receptor']
    if info['receptor'] and (rec.get('rut') or rec.get('razon_social')):
        y -= 0.6 * cm
        # Preparar filas de datos
        datos_rec = []
        if rec.get('rut'):
            datos_rec.append(("R.U.T.:", _rut_con_miles(rec['rut'])))
        if rec.get('giro'):
            datos_rec.append(("Giro:", rec['giro'][:55]))
        dir_rec = f"{rec.get('direccion', '')}, {rec.get('comuna', '')}".strip(', ')
        if rec.get('ciudad'):
            dir_rec = f"{dir_rec}, {rec['ciudad']}".strip(', ')
        if dir_rec:
            datos_rec.append(("Dirección:", dir_rec[:60]))
        # Altura del recuadro = línea del nombre + filas
        box_h = 0.55 * cm + len(datos_rec) * 0.42 * cm + 0.25 * cm
        box_w = W - 4 * cm
        box_top = y + 0.35 * cm
        c.setStrokeColorRGB(0.55, 0.55, 0.55)
        c.setLineWidth(0.6)
        c.rect(x, box_top - box_h, box_w, box_h)
        # Contenido
        ty = box_top - 0.5 * cm
        c.setFont("Helvetica-Bold", 9)
        c.drawString(x + 0.25 * cm, ty, "SEÑOR(ES):")
        c.setFont("Helvetica", 9)
        c.drawString(x + 2.5 * cm, ty, (rec.get('razon_social') or '')[:55])
        ty -= 0.42 * cm
        for etq, val in datos_rec:
            c.setFont("Helvetica-Bold", 9); c.drawString(x + 0.25 * cm, ty, etq)
            c.setFont("Helvetica", 9); c.drawString(x + 2.5 * cm, ty, val)
            ty -= 0.42 * cm
        c.setStrokeColorRGB(0, 0, 0)
        y = box_top - box_h

    # ── Bloque TRANSPORTE (Guía de Despacho 52) — Res. Ex. SII 154/52 ──
    # Desde 01-05-2026 es OBLIGATORIO en guías que trasladan bienes informar
    # chofer, transportista, patente y destino. Si la patente no se conoce al
    # emitir, la norma exige señalarlo expresamente.
    # Se dibuja en DOS COLUMNAS para no consumir espacio vertical del detalle.
    tr = d.get('transporte', {})
    if d['tipo_int'] == 52 and (tr.get('tiene_datos') or tr.get('ind_traslado_glosa')):
        y -= 0.45 * cm
        filas_tr = []
        if tr.get('ind_traslado_glosa'):
            filas_tr.append(("Traslado:", f"({tr['ind_traslado']}) {tr['ind_traslado_glosa']}"))
        if tr.get('tipo_despacho_glosa'):
            filas_tr.append(("Despacho:", tr['tipo_despacho_glosa']))
        if tr.get('patente'):
            filas_tr.append(("Patente:", tr['patente']))
        elif tr.get('tiene_datos'):
            filas_tr.append(("Patente:", "No informada a la emisión"))
        if tr.get('rut_transportista'):
            filas_tr.append(("Transportista:", _rut_con_miles(tr['rut_transportista'])))
        if tr.get('nombre_chofer'):
            chofer = tr['nombre_chofer']
            if tr.get('rut_chofer'):
                chofer += f" ({_rut_con_miles(tr['rut_chofer'])})"
            filas_tr.append(("Chofer:", chofer[:42]))
        dir_dest = f"{tr.get('dir_dest', '')}".strip()
        cmna_ciudad = ", ".join([p for p in [tr.get('cmna_dest', ''), tr.get('ciudad_dest', '')] if p])
        if dir_dest:
            destino = dir_dest + (f", {cmna_ciudad}" if cmna_ciudad else "")
            filas_tr.append(("Destino:", destino[:42]))
        elif cmna_ciudad:
            filas_tr.append(("Destino:", cmna_ciudad[:42]))
        if tr.get('bultos'):
            for b in tr['bultos']:
                partes = []
                if b.get('cant'):
                    partes.append(f"{b['cant']} bulto(s)")
                if b.get('marcas'):
                    partes.append(f"marcas {b['marcas']}")
                if b.get('id_container'):
                    partes.append(f"cont. {b['id_container']}")
                if partes:
                    filas_tr.append(("Bultos:", " · ".join(partes)[:42]))
        # Repartir en dos columnas
        n = len(filas_tr)
        mitad = math.ceil(n / 2)
        col_izq = filas_tr[:mitad]
        col_der = filas_tr[mitad:]
        n_filas = max(len(col_izq), len(col_der))
        # Recuadro: título + filas
        box_w = W - 4 * cm
        box_h = 0.55 * cm + n_filas * 0.38 * cm + 0.2 * cm
        box_top = y + 0.35 * cm
        c.setStrokeColorRGB(0.55, 0.55, 0.55)
        c.setLineWidth(0.6)
        c.rect(x, box_top - box_h, box_w, box_h)
        # Título dentro del recuadro
        c.setFont("Helvetica-Bold", 9)
        c.drawString(x + 0.25 * cm, box_top - 0.45 * cm, "Datos de Traslado y Transporte")
        c.setStrokeColorRGB(0, 0, 0)
        # Columnas
        col1_x = x + 0.25 * cm
        col2_x = x + 9.25 * cm
        ty_base = box_top - 0.9 * cm
        ty = ty_base
        for etq, val in col_izq:
            c.setFont("Helvetica-Bold", 8.5); c.drawString(col1_x, ty, etq)
            c.setFont("Helvetica", 8.5); c.drawString(col1_x + 2.4 * cm, ty, val)
            ty -= 0.38 * cm
        ty = ty_base
        for etq, val in col_der:
            c.setFont("Helvetica-Bold", 8.5); c.drawString(col2_x, ty, etq)
            c.setFont("Helvetica", 8.5); c.drawString(col2_x + 2.4 * cm, ty, val)
            ty -= 0.38 * cm
        y = box_top - box_h

    # ── Moneda (exportación) ──
    t = d['totales']
    if t.get('moneda') and t['moneda'] not in ('', 'PESO CL'):
        y -= 0.2 * cm
        c.setFont("Helvetica-Bold", 9)
        c.drawString(x, y, f"Moneda: {t['moneda']}")
        if t.get('tipo_cambio'):
            c.setFont("Helvetica", 9)
            c.drawString(x + 4 * cm, y, f"Tipo de cambio: {_miles(t['tipo_cambio'])}")
        y -= 0.42 * cm

    # ── Tabla de items ──
    y -= 0.4 * cm
    c.setFont("Helvetica-Bold", 9)
    c.setFillColorRGB(0.92, 0.92, 0.92)
    c.rect(x, y - 0.15 * cm, W - 4 * cm, 0.6 * cm, fill=1, stroke=0)
    c.setFillColorRGB(0, 0, 0)
    col_desc, col_qty, col_prc, col_tot = x + 0.2 * cm, x + 9 * cm, x + 12 * cm, W - 2 * cm
    c.drawString(col_desc, y, "Descripción")
    c.drawString(col_qty, y, "Cant.")
    c.drawString(col_prc, y, "P. Unit.")
    c.drawRightString(col_tot, y, "Monto")
    y -= 0.7 * cm

    c.setFont("Helvetica", 9)
    for it in d['items']:
        nombre = (it['nombre'] or '')[:50]
        if it.get('exento'):
            nombre += " (E)"
        c.drawString(col_desc, y, nombre)
        c.drawString(col_qty, y, it['cantidad'])
        c.drawString(col_prc, y, _miles(it['precio']) if it['precio'] else '')
        c.drawRightString(col_tot, y, _miles(it['monto']))
        y -= 0.55 * cm
        if it.get('descripcion'):
            c.setFont("Helvetica-Oblique", 8)
            c.drawString(col_desc + 0.3 * cm, y, it['descripcion'][:60])
            c.setFont("Helvetica", 9)
            y -= 0.45 * cm
        if y < 7 * cm:
            break

    # ── Totales ──
    y = max(y, 7.5 * cm)
    tx = W - 7 * cm
    c.setLineWidth(0.5)
    c.line(tx, y + 0.3 * cm, W - 2 * cm, y + 0.3 * cm)
    c.setFont("Helvetica", 9)
    es_export = t.get('moneda') and t['moneda'] not in ('', 'PESO CL')
    simbolo = "US$" if es_export else "$"
    if t.get('neto') and t['neto'] not in ('', '0'):
        c.drawString(tx, y, "Neto:")
        c.drawRightString(W - 2 * cm, y, f"{simbolo} {_miles(t['neto'])}")
        y -= 0.5 * cm
    if t.get('exento') and t['exento'] not in ('', '0'):
        etq_exe = "Monto Exento:" if not es_export else "Monto:"
        c.drawString(tx, y, etq_exe)
        c.drawRightString(W - 2 * cm, y, f"{simbolo} {_miles(t['exento'])}")
        y -= 0.5 * cm
    if t.get('iva') and t['iva'] not in ('', '0'):
        c.drawString(tx, y, f"IVA ({t['tasa_iva']}%):")
        c.drawRightString(W - 2 * cm, y, f"$ {_miles(t['iva'])}")
        y -= 0.5 * cm
    if t.get('monto_reten') and t['monto_reten'] not in ('', '0'):
        c.drawString(tx, y, "IVA Retenido:")
        c.drawRightString(W - 2 * cm, y, f"$ -{_miles(t['monto_reten'])}")
        y -= 0.5 * cm
    c.setFont("Helvetica-Bold", 11)
    c.drawString(tx, y, "TOTAL:")
    c.drawRightString(W - 2 * cm, y, f"{simbolo} {_miles(t['total'])}")
    # Total en pesos para exportación
    if es_export and t.get('total_otra_moneda'):
        y -= 0.5 * cm
        c.setFont("Helvetica", 8)
        c.drawString(tx, y, "Total (CLP):")
        c.drawRightString(W - 2 * cm, y, f"$ {_miles(t['total_otra_moneda'])}")

    # ── Bloque REFERENCIA ──
    if info['referencia'] and d['referencias']:
        # El acuse de recibo ocupa la franja 6.4–9.0cm a lo ancho completo.
        # La referencia va por encima de esa franja cuando hay acuse.
        if info.get('acuse'):
            y_ref = 11.0 * cm   # arranca bien arriba del acuse
            tope = 9.4 * cm     # no bajar al acuse
        else:
            y_ref = 6.6 * cm
            tope = 4.0 * cm
        c.setFont("Helvetica-Bold", 8)
        c.drawString(x, y_ref, "Referencia:")
        c.setFont("Helvetica", 8)
        y_ref -= 0.4 * cm
        for r in d['referencias']:
            if y_ref < tope:
                break
            linea = f"{r['tpo_doc_ref_nombre']} N° {r['folio_ref']}"
            if r['fecha_ref']:
                linea += f" del {r['fecha_ref']}"
            if r['cod_ref_glosa']:
                linea += f" — {r['cod_ref_glosa']}"
            c.drawString(x, y_ref, linea[:90])
            y_ref -= 0.35 * cm
            if r['razon_ref'] and y_ref >= tope:
                c.setFont("Helvetica-Oblique", 8)
                c.drawString(x + 0.3 * cm, y_ref, f"Motivo: {r['razon_ref'][:80]}")
                c.setFont("Helvetica", 8)
                y_ref -= 0.35 * cm

    # ── Recuadro ACUSE DE RECIBO (Ley 19.983) — ancho completo, estilo Lioren ──
    # Solo en documentos que lo requieren (facturas, fact compra, liquidación,
    # guía). NC/ND/boletas/exportaciones NO lo llevan.
    if info.get('acuse'):
        ar_x = x                      # margen izquierdo del contenido
        ar_w = W - 4 * cm             # ancho completo (de margen a margen)
        ar_h = 2.6 * cm
        ar_y = 6.4 * cm               # por encima del timbre (timbre llega a ~5.8cm)
        c.setStrokeColorRGB(0.35, 0.35, 0.35)
        c.setLineWidth(0.8)
        c.rect(ar_x, ar_y, ar_w, ar_h)
        # Título
        c.setFont("Helvetica-Bold", 9)
        c.drawString(ar_x + 0.3 * cm, ar_y + ar_h - 0.5 * cm, "Acuse de Recibo")
        # Texto legal (ancho completo, una/dos líneas)
        c.setFont("Helvetica", 7)
        texto_ley = ("El acuse de recibo que se declara en este acto, de acuerdo a lo dispuesto en la "
                     "letra b) del Art. 4°, y la letra c) del Art. 5° de la Ley 19.983, acredita que la "
                     "entrega de mercaderías o servicio(s) prestado(s) ha(n) sido recibido(s).")
        palabras = texto_ley.split()
        linea = ""
        ty = ar_y + ar_h - 0.85 * cm
        for pal in palabras:
            if len(linea) + len(pal) + 1 > 120:
                c.drawString(ar_x + 0.3 * cm, ty, linea)
                ty -= 0.32 * cm
                linea = pal
            else:
                linea = (linea + " " + pal).strip()
        if linea:
            c.drawString(ar_x + 0.3 * cm, ty, linea)
        # Campos de firma distribuidos en el ancho
        c.setFont("Helvetica", 8)
        fy1 = ar_y + 0.8 * cm
        fy2 = ar_y + 0.3 * cm
        c.drawString(ar_x + 0.3 * cm, fy1, "Nombre: ______________________________")
        c.drawString(ar_x + 8.5 * cm, fy1, "R.U.T.: __________________")
        c.drawString(ar_x + 13.0 * cm, fy1, "Fecha: ____________")
        c.drawString(ar_x + 0.3 * cm, fy2, "Recinto: ______________________________")
        c.drawString(ar_x + 8.5 * cm, fy2, "Firma: ________________________________")
        c.setStrokeColorRGB(0, 0, 0)

    # ── Timbre PDF417 (abajo a la izquierda, ≥2cm del borde) ──
    # Norma SII: mínimo 2x5 cm, máximo 4x9 cm (alto x ancho), margen blanco
    # alrededor, y bajo él la leyenda + Resolución + verifique www.sii.cl.
    timbre = _generar_timbre_pdf417(d['ted'])
    if timbre:
        # Recuadro 8x4 cm: alto al máximo de norma (4cm) para que no se vea
        # delgado; ancho 8cm dentro del máximo (9cm). preserveAspectRatio
        # evita distorsionar el código (riesgo de ilegibilidad).
        tw, th = 8 * cm, 4 * cm
        c.drawImage(timbre, x, 1.9 * cm, width=tw, height=th,
                    preserveAspectRatio=True, anchor='sw')
        # Leyenda exigida (letra ≥ 6): rótulo + Resolución + verifique www.sii.cl
        c.setFont("Helvetica", 7.5)
        c.drawString(x, 1.55 * cm, "Timbre Electrónico SII")
        c.setFont("Helvetica", 6.5)
        c.drawString(x, 1.25 * cm, f"Res. {d.get('nro_resol', RESOLUCION_NRO)} de {d.get('anio_resol', RESOLUCION_ANIO)} - Verifique documento: www.sii.cl")
        # Leyenda de destino (solo copia cedible), zona inferior derecha
        if etiqueta_copia:
            c.setFont("Helvetica-Bold", 13)
            c.drawRightString(W - 2 * cm, 2.6 * cm, etiqueta_copia)
    else:
        tw, th = 8 * cm, 4 * cm
        c.setStrokeColorRGB(0.7, 0.7, 0.7); c.setLineWidth(0.7)
        c.setDash(3, 3)
        c.rect(x, 1.9 * cm, tw, th); c.setDash()
        c.setFillColorRGB(0.6, 0.6, 0.6); c.setFont("Helvetica-Oblique", 8)
        c.drawCentredString(x + tw / 2, 1.9 * cm + th / 2 + 0.2 * cm, "VISTA PREVIA — BORRADOR")
        c.drawCentredString(x + tw / 2, 1.9 * cm + th / 2 - 0.3 * cm, "El timbre electrónico se genera al emitir")
        c.setFillColorRGB(0, 0, 0)


def _pdf_carta(d: dict, url_consulta: str) -> bytes:
    """Genera el PDF carta. Si el documento es cedible, agrega una 2da página
    con la copia CEDIBLE (exigida por el Manual de Muestras Impresas).
    La guía usa la leyenda 'CEDIBLE CON SU FACTURA'."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)

    # Copia 1: tributaria (sin leyenda de destino)
    _dibujar_copia_carta(c, d, url_consulta, etiqueta_copia="")
    c.showPage()

    # Copia 2: CEDIBLE (solo para documentos que la requieren; no NC/ND/boletas)
    if d['info'].get('cedible'):
        leyenda = d['info'].get('leyenda_cedible', 'CEDIBLE')
        _dibujar_copia_carta(c, d, url_consulta, etiqueta_copia=leyenda)
        c.showPage()

    c.save()
    buf.seek(0)
    return buf.read()


# ─────────────────────────────────────────────────────────────────────────────
# 4. Formato ROLLO 80mm (ticket térmico — boletas)
# ─────────────────────────────────────────────────────────────────────────────

def _pdf_rollo(d: dict, url_consulta: str) -> bytes:
    em = d['emisor']
    info = d['info']
    ancho = 80 * mm
    n_ref = len(d['referencias'])
    alto = (52 + len(d['items']) * 8 + 28 + 30 + n_ref * 10) * mm
    alto = max(alto, 120 * mm)

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(ancho, alto))
    cx = ancho / 2
    y = alto - 6 * mm

    c.setFont("Helvetica-Bold", 10)
    c.drawCentredString(cx, y, (em['razon_social'] or '')[:38]); y -= 4.5 * mm
    c.setFont("Helvetica", 7)
    for linea in [f"RUT: {_rut_con_miles(em['rut'])}", (em['giro'] or '')[:42],
                  f"{em['direccion']}, {em['comuna']}".strip(', ')]:
        if linea:
            c.drawCentredString(cx, y, linea); y -= 3.5 * mm

    y -= 2 * mm
    c.setStrokeColorRGB(0.8, 0, 0)
    c.setLineWidth(1)
    c.rect(6 * mm, y - 11 * mm, ancho - 12 * mm, 11 * mm)
    c.setFillColorRGB(0.8, 0, 0)
    c.setFont("Helvetica-Bold", 8 if len(info['nombre']) <= 22 else 7)
    c.drawCentredString(cx, y - 4 * mm, info['nombre'])
    c.drawCentredString(cx, y - 8.5 * mm, ("BORRADOR" if str(d['folio']) in ("0", "") else f"N° {d['folio']}"))
    c.setFillColorRGB(0, 0, 0)
    y -= 15 * mm

    c.setFont("Helvetica", 7)
    c.drawString(6 * mm, y, f"Fecha: {d['fch_emis']}"); y -= 4 * mm

    rec = d['receptor']
    if info['receptor'] and rec.get('rut'):
        c.drawString(6 * mm, y, f"Cliente: {rec.get('razon_social', '')[:30]}"); y -= 3.5 * mm
        c.drawString(6 * mm, y, f"RUT: {_rut_con_miles(rec['rut'])}"); y -= 4 * mm

    c.line(6 * mm, y, ancho - 6 * mm, y); y -= 4 * mm

    c.setFont("Helvetica", 7)
    for it in d['items']:
        nombre = (it['nombre'] or '')[:34] + (" (E)" if it.get('exento') else "")
        c.drawString(6 * mm, y, nombre); y -= 3.5 * mm
        linea = f"  {it['cantidad']} x {_miles(it['precio'])}" if it['precio'] else f"  {it['cantidad']}"
        c.drawString(6 * mm, y, linea)
        c.drawRightString(ancho - 6 * mm, y, f"$ {_miles(it['monto'])}")
        y -= 4.5 * mm

    c.line(6 * mm, y, ancho - 6 * mm, y); y -= 4.5 * mm

    t = d['totales']
    c.setFont("Helvetica", 7)
    for etq, val in [("Neto", t.get('neto')), ("Exento", t.get('exento')),
                     (f"IVA {t['tasa_iva']}%", t.get('iva'))]:
        if val and val not in ('', '0'):
            c.drawString(6 * mm, y, f"{etq}:")
            c.drawRightString(ancho - 6 * mm, y, f"$ {_miles(val)}")
            y -= 4 * mm
    c.setFont("Helvetica-Bold", 9)
    c.drawString(6 * mm, y, "TOTAL:")
    c.drawRightString(ancho - 6 * mm, y, f"$ {_miles(t['total'])}")
    y -= 6 * mm

    if info['referencia'] and d['referencias']:
        c.setFont("Helvetica", 6)
        for r in d['referencias']:
            c.drawString(6 * mm, y, f"Ref: {r['tpo_doc_ref_nombre']} N°{r['folio_ref']}"); y -= 3 * mm
            if r['cod_ref_glosa']:
                c.drawString(6 * mm, y, r['cod_ref_glosa'][:42]); y -= 3 * mm
        y -= 2 * mm

    timbre = _generar_timbre_pdf417(d['ted'])
    if timbre:
        tw, th = 66 * mm, 22 * mm
        c.drawImage(timbre, (ancho - tw) / 2, y - th, width=tw, height=th,
                    preserveAspectRatio=True, anchor='n')
        y -= th + 2 * mm
        c.setFont("Helvetica", 6)
        c.drawCentredString(cx, y, "Timbre Electrónico SII"); y -= 3 * mm
        c.drawCentredString(cx, y, f"Verifique en {url_consulta}")
    else:
        tw, th = 60 * mm, 20 * mm
        c.setStrokeColorRGB(0.7, 0.7, 0.7); c.setLineWidth(0.6); c.setDash(2, 2)
        c.rect((ancho - tw) / 2, y - th, tw, th); c.setDash()
        c.setFillColorRGB(0.6, 0.6, 0.6); c.setFont("Helvetica-Oblique", 6)
        c.drawCentredString(cx, y - th / 2 + 1 * mm, "VISTA PREVIA — BORRADOR")
        c.drawCentredString(cx, y - th / 2 - 2.5 * mm, "Timbre se genera al emitir")
        c.setFillColorRGB(0, 0, 0); y -= th + 2 * mm

    c.showPage()
    c.save()
    buf.seek(0)
    return buf.read()


# ─────────────────────────────────────────────────────────────────────────────
# 5. API pública
# ─────────────────────────────────────────────────────────────────────────────

def generar_pdf_dte(dte_xml: bytes, formato: str = "carta",
                    url_consulta: str = "www.sii.cl",
                    nro_resol: int = None, anio_resol: int = None) -> bytes:
    """Genera el PDF de la representación gráfica de cualquier DTE.

    Args:
        dte_xml: XML del documento firmado (con TED).
        formato: "carta" (hoja, todos los tipos) o "rollo" (ticket 80mm, boletas).
        url_consulta: sitio donde el receptor verifica el documento (opcional;
            la norma exige www.sii.cl, que siempre se imprime).
        nro_resol: número de la resolución que autoriza al emisor como
            facturador electrónico. En certificación es 0; en producción es el
            número real que el SII asigna a cada empresa (configurable por tenant).
        anio_resol: año de esa resolución. Por defecto el año vigente.

    Returns:
        bytes del PDF. Para documentos cedibles incluye 2 páginas
        (copia tributaria + copia CEDIBLE).
    """
    d = parsear_dte_xml(dte_xml)
    # Resolución del emisor (configurable; default = certificación)
    d['nro_resol'] = nro_resol if nro_resol is not None else RESOLUCION_NRO
    d['anio_resol'] = anio_resol if anio_resol is not None else RESOLUCION_ANIO
    if formato == "rollo" and d['info']['es_boleta']:
        return _pdf_rollo(d, url_consulta)
    return _pdf_carta(d, url_consulta)


# ─────────────────────────────────────────────────────────────────────────────
# 6. Compatibilidad con el módulo anterior (pdf_boleta.py)
# ─────────────────────────────────────────────────────────────────────────────

def generar_pdf_boleta(boleta_xml: bytes, formato: str = "carta",
                       url_consulta: str = "www.sii.cl") -> bytes:
    """Alias retrocompatible."""
    return generar_pdf_dte(boleta_xml, formato=formato, url_consulta=url_consulta)


parsear_boleta_xml = parsear_dte_xml
