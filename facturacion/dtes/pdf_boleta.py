# -*- coding: utf-8 -*-
"""
Representación gráfica (PDF) de Boletas Electrónicas (DTE tipo 39/41).

Genera el documento visual que se entrega al cliente, a partir del XML de la
boleta ya firmada (de donde se extraen los datos y el TED para el timbre PDF417).

Dos formatos, configurables por tenant desde el panel de Lusync:
  • "carta"  : hoja carta, maquetación formal tipo factura (estilo Lioren).
  • "rollo"  : ticket térmico 80mm, para punto de venta.

El timbre electrónico PDF417 se genera con ECL=5 y relación 3:1, según el
instructivo del SII. Desde el 01-01-2026 el timbre impreso es OPCIONAL, pero se
incluye por defecto porque permite la verificación con la app e-factura del SII.

Uso:
    from facturacion.dtes.pdf_boleta import generar_pdf_boleta
    pdf_bytes = generar_pdf_boleta(boleta_xml, formato="carta")  # o "rollo"
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
# 1. Extracción de datos del XML de la boleta
# ─────────────────────────────────────────────────────────────────────────────

def _txt(xml: str, tag: str) -> str:
    """Devuelve el contenido del primer <tag>...</tag>, o '' si no existe."""
    m = re.search(rf'<{tag}>(.*?)</{tag}>', xml, re.DOTALL)
    return m.group(1).strip() if m else ''


def _miles(valor) -> str:
    """Formatea un entero con separador de miles chileno (punto). 19900 -> 19.900"""
    try:
        return f"{int(round(float(valor))):,}".replace(",", ".")
    except (ValueError, TypeError):
        return str(valor)


def parsear_boleta_xml(boleta_xml: bytes) -> dict:
    """Extrae los datos necesarios para la representación gráfica desde el XML."""
    s = boleta_xml.decode('iso-8859-1', errors='replace')

    # Emisor (algunos tags varían: RznSocEmisor/RznSoc, GiroEmisor/GiroEmis)
    emisor = {
        'rut': _txt(s, 'RUTEmisor'),
        'razon_social': _txt(s, 'RznSocEmisor') or _txt(s, 'RznSoc'),
        'giro': _txt(s, 'GiroEmisor') or _txt(s, 'GiroEmis'),
        'direccion': _txt(s, 'DirOrigen'),
        'comuna': _txt(s, 'CmnaOrigen'),
        'ciudad': _txt(s, 'CiudadOrigen'),
    }

    # Documento
    tipo_dte = _txt(s, 'TipoDTE')
    folio = _txt(s, 'Folio')
    fch_emis = _txt(s, 'FchEmis')

    # Receptor (en boletas suele ir mínimo o vacío)
    receptor = {
        'rut': _txt(s, 'RUTRecep'),
        'razon_social': _txt(s, 'RznSocRecep'),
    }

    # Totales
    totales = {
        'neto': _txt(s, 'MntNeto'),
        'exento': _txt(s, 'MntExe'),
        'iva': _txt(s, 'IVA'),
        'tasa_iva': _txt(s, 'TasaIVA') or '19',
        'total': _txt(s, 'MntTotal'),
    }

    # Items (Detalle, puede repetirse)
    items = []
    for det in re.findall(r'<Detalle>(.*?)</Detalle>', s, re.DOTALL):
        items.append({
            'nombre': _txt(det, 'NmbItem'),
            'cantidad': _txt(det, 'QtyItem'),
            'unidad': _txt(det, 'UnmdItem'),
            'precio': _txt(det, 'PrcItem'),
            'monto': _txt(det, 'MontoItem'),
            'exento': bool(re.search(r'<IndExe>', det)),
        })

    # TED (timbre) — se toma EXACTO como está en el XML, para el PDF417
    ted_match = re.search(r'<TED.*?</TED>', s, re.DOTALL)
    ted = ted_match.group(0) if ted_match else ''

    return {
        'tipo_dte': tipo_dte,
        'folio': folio,
        'fch_emis': fch_emis,
        'emisor': emisor,
        'receptor': receptor,
        'totales': totales,
        'items': items,
        'ted': ted,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 2. Timbre PDF417
# ─────────────────────────────────────────────────────────────────────────────

def _generar_timbre_pdf417(ted: str) -> Optional[ImageReader]:
    """Genera la imagen del timbre PDF417 a partir del TED.

    Según instructivo SII: ECL (security_level)=5, relación 3:1, contenido en
    ISO-8859-1. Devuelve un ImageReader listo para drawImage, o None si no hay TED.
    """
    if not ted:
        return None
    # El TED debe codificarse en ISO-8859-1 (bytes), como exige el SII.
    ted_bytes = ted.encode('iso-8859-1', errors='replace')
    codes = _pdf417_encode(ted_bytes, columns=18, security_level=5)
    img = _pdf417_render(codes, scale=3, ratio=3, padding=2)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return ImageReader(buf)


def _tipo_doc_nombre(tipo_dte: str) -> str:
    return {
        '39': 'BOLETA ELECTRÓNICA',
        '41': 'BOLETA EXENTA ELECTRÓNICA',
    }.get(str(tipo_dte), 'BOLETA ELECTRÓNICA')


# ─────────────────────────────────────────────────────────────────────────────
# 3. Formato CARTA (estilo factura, similar a Lioren)
# ─────────────────────────────────────────────────────────────────────────────

def _pdf_carta(d: dict) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    W, H = letter
    em = d['emisor']

    # ── Recuadro rojo del SII (arriba a la derecha) ──
    rec_w, rec_h = 8 * cm, 3.2 * cm
    rec_x, rec_y = W - rec_w - 2 * cm, H - rec_h - 2 * cm
    c.setStrokeColorRGB(0.8, 0, 0)
    c.setLineWidth(2)
    c.rect(rec_x, rec_y, rec_w, rec_h)
    c.setFillColorRGB(0.8, 0, 0)
    c.setFont("Helvetica-Bold", 13)
    c.drawCentredString(rec_x + rec_w / 2, rec_y + rec_h - 0.7 * cm, f"R.U.T.: {em['rut']}")
    c.drawCentredString(rec_x + rec_w / 2, rec_y + rec_h - 1.4 * cm, _tipo_doc_nombre(d['tipo_dte']))
    c.drawCentredString(rec_x + rec_w / 2, rec_y + rec_h - 2.1 * cm, f"N° {d['folio']}")
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
    for linea in [em['giro'][:60], f"{em['direccion']}, {em['comuna']}".strip(', ')]:
        if linea:
            c.drawString(x, y, linea)
            y -= 0.45 * cm

    # ── Fecha de emisión ──
    y = rec_y - 0.8 * cm
    c.setFont("Helvetica-Bold", 9)
    c.drawString(x, y, f"Fecha Emisión: {d['fch_emis']}")
    if d['receptor'].get('rut'):
        c.drawString(x + 8 * cm, y, f"Cliente: {d['receptor']['rut']}")

    # ── Tabla de items ──
    y -= 0.9 * cm
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
        c.drawString(col_desc, y, it['nombre'][:50])
        c.drawString(col_qty, y, it['cantidad'])
        c.drawString(col_prc, y, _miles(it['precio']) if it['precio'] else '')
        c.drawRightString(col_tot, y, _miles(it['monto']))
        y -= 0.55 * cm
        if y < 6 * cm:  # salto de seguridad
            break

    # ── Totales (abajo a la derecha) ──
    y = max(y, 7 * cm)
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

    # ── Timbre PDF417 (abajo a la izquierda) ──
    timbre = _generar_timbre_pdf417(d['ted'])
    if timbre:
        tw, th = 7 * cm, 2.6 * cm  # dentro del rango SII (máx 4x9cm)
        c.drawImage(timbre, x, 2 * cm, width=tw, height=th, preserveAspectRatio=True, anchor='sw')
        c.setFont("Helvetica", 7)
        c.drawString(x, 1.7 * cm, "Timbre Electrónico SII")
        c.drawString(x, 1.4 * cm, "Verifique documento: www.sii.cl")

    c.showPage()
    c.save()
    buf.seek(0)
    return buf.read()


# ─────────────────────────────────────────────────────────────────────────────
# 4. Formato ROLLO 80mm (ticket térmico)
# ─────────────────────────────────────────────────────────────────────────────

def _pdf_rollo(d: dict) -> bytes:
    em = d['emisor']
    ancho = 80 * mm
    # Alto dinámico: encabezado + items + totales + timbre. Ajustado para no
    # dejar espacio en blanco excesivo al final del ticket.
    alto = (52 + len(d['items']) * 8 + 28 + 30) * mm
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
    c.setFont("Helvetica-Bold", 8)
    c.drawCentredString(cx, y - 4 * mm, _tipo_doc_nombre(d['tipo_dte']))
    c.drawCentredString(cx, y - 8.5 * mm, f"N° {d['folio']}")
    c.setFillColorRGB(0, 0, 0)
    y -= 15 * mm

    c.setFont("Helvetica", 7)
    c.drawString(6 * mm, y, f"Fecha: {d['fch_emis']}"); y -= 4 * mm
    c.line(6 * mm, y, ancho - 6 * mm, y); y -= 4 * mm

    # Items
    c.setFont("Helvetica", 7)
    for it in d['items']:
        c.drawString(6 * mm, y, it['nombre'][:34]); y -= 3.5 * mm
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
    y -= 7 * mm

    # Timbre PDF417 centrado
    timbre = _generar_timbre_pdf417(d['ted'])
    if timbre:
        tw, th = 66 * mm, 22 * mm
        c.drawImage(timbre, (ancho - tw) / 2, y - th, width=tw, height=th,
                    preserveAspectRatio=True, anchor='n')
        y -= th + 2 * mm
        c.setFont("Helvetica", 6)
        c.drawCentredString(cx, y, "Timbre Electrónico SII"); y -= 3 * mm
        c.drawCentredString(cx, y, "Verifique documento: www.sii.cl")

    c.showPage()
    c.save()
    buf.seek(0)
    return buf.read()


# ─────────────────────────────────────────────────────────────────────────────
# 5. API pública
# ─────────────────────────────────────────────────────────────────────────────

def generar_pdf_boleta(boleta_xml: bytes, formato: str = "carta") -> bytes:
    """Genera el PDF de la representación gráfica de una boleta.

    Args:
        boleta_xml: XML de la boleta firmada (con TED).
        formato: "carta" (hoja, estilo factura) o "rollo" (ticket 80mm).

    Returns:
        bytes del PDF.
    """
    d = parsear_boleta_xml(boleta_xml)
    if formato == "rollo":
        return _pdf_rollo(d)
    return _pdf_carta(d)
