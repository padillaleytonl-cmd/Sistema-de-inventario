# -*- coding: utf-8 -*-
"""
facturacion/dtes/pdf_dte.py
─────────────────────────────────────────────────────────────
Representación gráfica (PDF) UNIFICADA de Documentos Tributarios Electrónicos.

Reemplaza a pdf_boleta.py extendiéndolo a todos los DTE que Lusync emite,
detectando el tipo desde el <TipoDTE> del XML y dibujando los bloques que
correspondan a cada uno:

  39  Boleta Electrónica            → sin receptor completo, sin referencia
  41  Boleta Exenta Electrónica     → idem, montos exentos
  61  Nota de Crédito Electrónica   → receptor completo + Referencia obligatoria
  56  Nota de Débito Electrónica    → receptor completo + Referencia obligatoria
  33  Factura Electrónica           → receptor completo (giro/dir/comuna)
  34  Factura Exenta Electrónica    → receptor completo, sin IVA
  52  Guía de Despacho Electrónica  → receptor completo + tipo traslado

Dos formatos por documento (configurable por tenant):
  • "carta" : hoja carta, maquetación formal según instructivo SII.
  • "rollo" : ticket térmico 80mm (pensado para boletas en POS).

Cumple el instructivo SII para el timbre: PDF417 con ECL=5, relación 3:1,
ubicado abajo, leyenda "Timbre Electrónico SII" + sitio de verificación.
Desde 01-01-2026 el timbre impreso es OPCIONAL en boletas, pero se incluye
por defecto (permite verificación con la app e-factura del SII).

Uso:
    from facturacion.dtes.pdf_dte import generar_pdf_dte
    pdf_bytes = generar_pdf_dte(dte_xml, formato="carta")   # detecta el tipo solo

Compatibilidad:
    generar_pdf_boleta(xml, formato) sigue existiendo como alias.
"""
from __future__ import annotations

import io
import re
from typing import Optional

from reportlab.lib.pagesizes import letter
from reportlab.lib.units import mm, cm
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader

from pdf417gen import encode as _pdf417_encode, render_image as _pdf417_render


# ─────────────────────────────────────────────────────────────────────────────
# 0. Catálogo de tipos de DTE
# ─────────────────────────────────────────────────────────────────────────────
# nombre  : título que va en el recuadro rojo del SII
# receptor: si dibuja el bloque de receptor completo (giro/dir/comuna)
# referencia: si dibuja el bloque de Referencia al documento modificado
# es_boleta: si admite formato rollo y RUT genérico de consumidor
DTE_INFO = {
    33: {"nombre": "FACTURA ELECTRÓNICA",            "receptor": True,  "referencia": True,  "es_boleta": False},
    34: {"nombre": "FACTURA EXENTA ELECTRÓNICA",      "receptor": True,  "referencia": True,  "es_boleta": False},
    39: {"nombre": "BOLETA ELECTRÓNICA",              "receptor": False, "referencia": False, "es_boleta": True},
    41: {"nombre": "BOLETA EXENTA ELECTRÓNICA",       "receptor": False, "referencia": False, "es_boleta": True},
    52: {"nombre": "GUÍA DE DESPACHO ELECTRÓNICA",    "receptor": True,  "referencia": True,  "es_boleta": False},
    56: {"nombre": "NOTA DE DÉBITO ELECTRÓNICA",      "receptor": True,  "referencia": True,  "es_boleta": False},
    61: {"nombre": "NOTA DE CRÉDITO ELECTRÓNICA",     "receptor": True,  "referencia": True,  "es_boleta": False},
}

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
    "52": "Guía de Despacho",
    "56": "Nota de Débito",
    "61": "Nota de Crédito",
}


# ─────────────────────────────────────────────────────────────────────────────
# 1. Extracción de datos del XML
# ─────────────────────────────────────────────────────────────────────────────

def _txt(xml: str, tag: str) -> str:
    """Devuelve el contenido del primer <tag>...</tag>, o '' si no existe.
    Tolera atributos en la etiqueta de apertura (ej <TED version="1.0">)."""
    m = re.search(rf'<{tag}(?:\s[^>]*)?>(.*?)</{tag}>', xml, re.DOTALL)
    return m.group(1).strip() if m else ''


def _miles(valor) -> str:
    """Formatea un entero con separador de miles chileno (punto). 19900 -> 19.900"""
    try:
        return f"{int(round(float(valor))):,}".replace(",", ".")
    except (ValueError, TypeError):
        return str(valor)


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

    # Emisor (las boletas usan *Emisor; facturas/NC usan RznSoc/GiroEmis)
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
    }

    folio = _txt(encabezado, 'Folio') or _txt(s, 'Folio')
    fch_emis = _txt(encabezado, 'FchEmis') or _txt(s, 'FchEmis')

    # Receptor (relevante en factura/NC/ND/guía)
    receptor = {
        'rut': _txt(encabezado, 'RUTRecep'),
        'razon_social': _txt(encabezado, 'RznSocRecep'),
        'giro': _txt(encabezado, 'GiroRecep'),
        'direccion': _txt(encabezado, 'DirRecep'),
        'comuna': _txt(encabezado, 'CmnaRecep'),
        'ciudad': _txt(encabezado, 'CiudadRecep'),
        'contacto': _txt(encabezado, 'Contacto'),
    }

    totales = {
        'neto': _txt(encabezado, 'MntNeto'),
        'exento': _txt(encabezado, 'MntExe'),
        'iva': _txt(encabezado, 'IVA'),
        'tasa_iva': _txt(encabezado, 'TasaIVA') or '19',
        'total': _txt(encabezado, 'MntTotal'),
    }

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

    # Referencias (NC/ND/factura). Puede haber más de una.
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

    # TED (timbre) — exacto para el PDF417
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
        'ted': ted,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 2. Timbre PDF417
# ─────────────────────────────────────────────────────────────────────────────

def _generar_timbre_pdf417(ted: str) -> Optional[ImageReader]:
    """Imagen del timbre PDF417 desde el TED. ECL=5, relación 3:1 (instructivo SII)."""
    if not ted:
        return None
    ted_bytes = ted.encode('iso-8859-1', errors='replace')
    codes = _pdf417_encode(ted_bytes, columns=18, security_level=5)
    img = _pdf417_render(codes, scale=3, ratio=3, padding=2)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return ImageReader(buf)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Formato CARTA (estilo factura SII / Lioren)
# ─────────────────────────────────────────────────────────────────────────────

def _pdf_carta(d: dict, url_consulta: str) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
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
    c.drawCentredString(rec_x + rec_w / 2, rec_y + rec_h - 0.7 * cm, f"R.U.T.: {em['rut']}")
    # El título puede ser largo (ej GUÍA DE DESPACHO ELECTRÓNICA): ajustar fuente
    titulo = info['nombre']
    c.setFont("Helvetica-Bold", 13 if len(titulo) <= 20 else 10)
    c.drawCentredString(rec_x + rec_w / 2, rec_y + rec_h - 1.45 * cm, titulo)
    c.setFont("Helvetica-Bold", 13)
    c.drawCentredString(rec_x + rec_w / 2, rec_y + rec_h - 2.2 * cm, ("BORRADOR" if str(d['folio']) in ("0","") else f"N° {d['folio']}"))
    c.setFont("Helvetica-Bold", 10)
    c.drawCentredString(rec_x + rec_w / 2, rec_y + 0.4 * cm, "S.I.I. - SANTIAGO")
    c.setFillColorRGB(0, 0, 0)

    # ── Datos del emisor (arriba a la izquierda) ──
    x = 2 * cm
    y = H - 2.4 * cm
    c.setFont("Helvetica-Bold", 14)
    c.drawString(x, y, em['razon_social'][:45])
    c.setFont("Helvetica", 9)
    y -= 0.6 * cm
    for linea in [em['giro'][:65],
                  f"{em['direccion']}, {em['comuna']}".strip(', '),
                  f"Tel: {em['telefono']}" if em['telefono'] else '']:
        if linea:
            c.drawString(x, y, linea)
            y -= 0.45 * cm

    # ── Fecha de emisión ──
    y = rec_y - 0.8 * cm
    c.setFont("Helvetica-Bold", 9)
    c.drawString(x, y, f"Fecha Emisión: {d['fch_emis']}")

    # ── Bloque RECEPTOR (factura / NC / ND / guía) ──
    rec = d['receptor']
    if info['receptor'] and (rec.get('rut') or rec.get('razon_social')):
        y -= 0.7 * cm
        c.setStrokeColorRGB(0.7, 0.7, 0.7)
        c.setLineWidth(0.5)
        c.setFont("Helvetica-Bold", 9)
        c.drawString(x, y, "SEÑOR(ES):")
        c.setFont("Helvetica", 9)
        c.drawString(x + 2.2 * cm, y, (rec.get('razon_social') or '')[:55])
        y -= 0.42 * cm
        datos_rec = []
        if rec.get('rut'):
            datos_rec.append(("R.U.T.:", rec['rut']))
        if rec.get('giro'):
            datos_rec.append(("Giro:", rec['giro'][:50]))
        dir_rec = f"{rec.get('direccion','')}, {rec.get('comuna','')}".strip(', ')
        if dir_rec:
            datos_rec.append(("Dirección:", dir_rec[:55]))
        for etq, val in datos_rec:
            c.setFont("Helvetica-Bold", 9); c.drawString(x, y, etq)
            c.setFont("Helvetica", 9); c.drawString(x + 2.2 * cm, y, val)
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
        nombre = it['nombre'][:50]
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
        if y < 7 * cm:  # salto de seguridad
            break

    # ── Totales (abajo a la derecha) ──
    y = max(y, 7.5 * cm)
    tx = W - 7 * cm
    c.setLineWidth(0.5)
    c.line(tx, y + 0.3 * cm, W - 2 * cm, y + 0.3 * cm)
    t = d['totales']
    c.setFont("Helvetica", 9)
    if t.get('neto') and t['neto'] not in ('', '0'):
        c.drawString(tx, y, "Neto:")
        c.drawRightString(W - 2 * cm, y, f"$ {_miles(t['neto'])}")
        y -= 0.5 * cm
    if t.get('exento') and t['exento'] not in ('', '0'):
        c.drawString(tx, y, "Exento:")
        c.drawRightString(W - 2 * cm, y, f"$ {_miles(t['exento'])}")
        y -= 0.5 * cm
    if t.get('iva') and t['iva'] not in ('', '0'):
        c.drawString(tx, y, f"IVA ({t['tasa_iva']}%):")
        c.drawRightString(W - 2 * cm, y, f"$ {_miles(t['iva'])}")
        y -= 0.5 * cm
    c.setFont("Helvetica-Bold", 11)
    c.drawString(tx, y, "TOTAL:")
    c.drawRightString(W - 2 * cm, y, f"$ {_miles(t['total'])}")

    # ── Bloque REFERENCIA (NC / ND) ──
    if info['referencia'] and d['referencias']:
        y_ref = 6.2 * cm
        c.setFont("Helvetica-Bold", 8)
        c.drawString(x, y_ref, "Referencia:")
        c.setFont("Helvetica", 8)
        y_ref -= 0.4 * cm
        for r in d['referencias']:
            linea = f"{r['tpo_doc_ref_nombre']} N° {r['folio_ref']}"
            if r['fecha_ref']:
                linea += f" del {r['fecha_ref']}"
            if r['cod_ref_glosa']:
                linea += f" — {r['cod_ref_glosa']}"
            c.drawString(x, y_ref, linea[:90])
            y_ref -= 0.35 * cm
            if r['razon_ref']:
                c.setFont("Helvetica-Oblique", 8)
                c.drawString(x + 0.3 * cm, y_ref, f"Motivo: {r['razon_ref'][:80]}")
                c.setFont("Helvetica", 8)
                y_ref -= 0.35 * cm

    # ── Timbre PDF417 (abajo a la izquierda, según instructivo SII) ──
    timbre = _generar_timbre_pdf417(d['ted'])
    if timbre:
        tw, th = 7 * cm, 2.6 * cm  # dentro del rango SII (2x5 a 4x9 cm)
        c.drawImage(timbre, x, 2 * cm, width=tw, height=th, preserveAspectRatio=True, anchor='sw')
        c.setFont("Helvetica", 7)
        c.drawString(x, 1.7 * cm, "Timbre Electrónico SII")
        c.drawString(x, 1.4 * cm, f"Verifique en {url_consulta}")
    else:
        # Borrador/preview: placeholder donde irá el timbre real al emitir
        tw, th = 7 * cm, 2.6 * cm
        c.setStrokeColorRGB(0.7, 0.7, 0.7); c.setLineWidth(0.7)
        c.setDash(3, 3)
        c.rect(x, 2 * cm, tw, th); c.setDash()
        c.setFillColorRGB(0.6, 0.6, 0.6); c.setFont("Helvetica-Oblique", 8)
        c.drawCentredString(x + tw/2, 2 * cm + th/2 + 0.2*cm, "VISTA PREVIA — BORRADOR")
        c.drawCentredString(x + tw/2, 2 * cm + th/2 - 0.3*cm, "El timbre electrónico se genera al emitir")
        c.setFillColorRGB(0, 0, 0)

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
    c.drawCentredString(cx, y, em['razon_social'][:38]); y -= 4.5 * mm
    c.setFont("Helvetica", 7)
    for linea in [f"RUT: {em['rut']}", em['giro'][:42],
                  f"{em['direccion']}, {em['comuna']}".strip(', ')]:
        if linea:
            c.drawCentredString(cx, y, linea); y -= 3.5 * mm

    # Recuadro tipo documento
    y -= 2 * mm
    c.setStrokeColorRGB(0.8, 0, 0)
    c.setLineWidth(1)
    c.rect(6 * mm, y - 11 * mm, ancho - 12 * mm, 11 * mm)
    c.setFillColorRGB(0.8, 0, 0)
    c.setFont("Helvetica-Bold", 8 if len(info['nombre']) <= 22 else 7)
    c.drawCentredString(cx, y - 4 * mm, info['nombre'])
    c.drawCentredString(cx, y - 8.5 * mm, ("BORRADOR" if str(d['folio']) in ("0","") else f"N° {d['folio']}"))
    c.setFillColorRGB(0, 0, 0)
    y -= 15 * mm

    c.setFont("Helvetica", 7)
    c.drawString(6 * mm, y, f"Fecha: {d['fch_emis']}"); y -= 4 * mm

    # Receptor (si aplica)
    rec = d['receptor']
    if info['receptor'] and rec.get('rut'):
        c.drawString(6 * mm, y, f"Cliente: {rec.get('razon_social','')[:30]}"); y -= 3.5 * mm
        c.drawString(6 * mm, y, f"RUT: {rec['rut']}"); y -= 4 * mm

    c.line(6 * mm, y, ancho - 6 * mm, y); y -= 4 * mm

    # Items
    c.setFont("Helvetica", 7)
    for it in d['items']:
        nombre = it['nombre'][:34] + (" (E)" if it.get('exento') else "")
        c.drawString(6 * mm, y, nombre); y -= 3.5 * mm
        linea = f"  {it['cantidad']} x {_miles(it['precio'])}" if it['precio'] else f"  {it['cantidad']}"
        c.drawString(6 * mm, y, linea)
        c.drawRightString(ancho - 6 * mm, y, f"$ {_miles(it['monto'])}")
        y -= 4.5 * mm

    c.line(6 * mm, y, ancho - 6 * mm, y); y -= 4.5 * mm

    # Totales
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

    # Referencia (NC/ND en rollo)
    if info['referencia'] and d['referencias']:
        c.setFont("Helvetica", 6)
        for r in d['referencias']:
            c.drawString(6 * mm, y, f"Ref: {r['tpo_doc_ref_nombre']} N°{r['folio_ref']}"); y -= 3 * mm
            if r['cod_ref_glosa']:
                c.drawString(6 * mm, y, r['cod_ref_glosa'][:42]); y -= 3 * mm
        y -= 2 * mm

    # Timbre PDF417 centrado
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
        c.rect((ancho - tw)/2, y - th, tw, th); c.setDash()
        c.setFillColorRGB(0.6, 0.6, 0.6); c.setFont("Helvetica-Oblique", 6)
        c.drawCentredString(cx, y - th/2 + 1*mm, "VISTA PREVIA — BORRADOR"); 
        c.drawCentredString(cx, y - th/2 - 2.5*mm, "Timbre se genera al emitir")
        c.setFillColorRGB(0, 0, 0); y -= th + 2 * mm

    c.showPage()
    c.save()
    buf.seek(0)
    return buf.read()


# ─────────────────────────────────────────────────────────────────────────────
# 5. API pública
# ─────────────────────────────────────────────────────────────────────────────

def generar_pdf_dte(dte_xml: bytes, formato: str = "carta",
                    url_consulta: str = "www.sii.cl") -> bytes:
    """Genera el PDF de la representación gráfica de cualquier DTE.

    Args:
        dte_xml: XML del documento firmado (con TED).
        formato: "carta" (hoja, todos los tipos) o "rollo" (ticket 80mm, boletas).
        url_consulta: sitio donde el receptor verifica el documento
                      (ej "lusync.cl/consultadte"). Va impreso bajo el timbre.

    Returns:
        bytes del PDF.
    """
    d = parsear_dte_xml(dte_xml)
    # Rollo solo tiene sentido en boletas; otros tipos van a carta aunque pidan rollo
    if formato == "rollo" and d['info']['es_boleta']:
        return _pdf_rollo(d, url_consulta)
    return _pdf_carta(d, url_consulta)


# ─────────────────────────────────────────────────────────────────────────────
# 6. Compatibilidad con el módulo anterior (pdf_boleta.py)
# ─────────────────────────────────────────────────────────────────────────────

def generar_pdf_boleta(boleta_xml: bytes, formato: str = "carta",
                       url_consulta: str = "www.sii.cl") -> bytes:
    """Alias retrocompatible. El código que llamaba a pdf_boleta.generar_pdf_boleta
    sigue funcionando; internamente usa el generador unificado."""
    return generar_pdf_dte(boleta_xml, formato=formato, url_consulta=url_consulta)


# Alias del parser para compatibilidad
parsear_boleta_xml = parsear_dte_xml
