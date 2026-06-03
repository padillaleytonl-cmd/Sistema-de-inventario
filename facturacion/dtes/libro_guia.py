# -*- coding: utf-8 -*-
"""
libro_guia.py — Libro de Guías de Despacho Electrónicas para el SII.

Formato: FORMATO LIBRO DE GUIAS DE DESPACHO ELECTRONICAS v1.0 (2003-10-29)
Schema:  LibroGuia_v10.xsd  (DISTINTO al LibroCV de compra/venta)

Estructura:
  <LibroGuia>
    <EnvioLibro ID="...">
      <Caratula>...</Caratula>
      <Detalle>...</Detalle>     (una línea por guía)
      ...
      <ResumenPeriodo>...</ResumenPeriodo>
      <TmstFirma>...</TmstFirma>
    </EnvioLibro>
    <Signature>...</Signature>   (XMLDSig sobre EnvioLibro por su ID)
  </LibroGuia>

Campos del Detalle (orden del schema):
  Folio, TpoOper (indicador tipo operación), FchDoc, RUTDoc, RznSoc,
  Anulado/Modificado, MntNeto, TasaImp, IVA, MntTotal, y para guías de venta
  facturadas: TpoDocRef + FolioDocRef de la factura emitida.

Indicadores tipo de operación:
  1=venta, 2=ventas por efectuar, 3=consignaciones, 4=demostración,
  5=traslados internos, 6=otros traslados no venta, 7=guía de devolución.

ANULADO/MODIFICADO:
  1=folio anulado antes de envío al SII, 2=anulado posterior al envío,
  3=productos recibidos parcialmente.
"""

from datetime import datetime
from typing import Dict, List, Optional


def _normalizar_rut(rut: str) -> str:
    return str(rut).replace(".", "").strip().upper()


def construir_detalle_guia(
    folio: int,
    tpo_oper: int,                 # 1=venta, 5=traslado interno, etc.
    fch_doc: str,                  # AAAA-MM-DD
    rut_doc: Optional[str] = None,
    rzn_soc: Optional[str] = None,
    mnt_neto: int = 0,
    tasa_imp: float = 19.0,
    iva: int = 0,
    mnt_total: int = 0,
    anulado: Optional[int] = None,  # 1, 2 o 3
    tpo_doc_ref: Optional[int] = None,   # tipo de factura que facturó la guía (ej 33)
    folio_doc_ref: Optional[int] = None, # folio de la factura
    fch_doc_ref: Optional[str] = None,
) -> str:
    """Construye un bloque <Detalle> para el Libro de Guías.

    El orden de los tags sigue el formato oficial (sección 2.d).
    """
    def esc(s):
        return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    partes = [f"<Folio>{folio}</Folio>"]
    if anulado is not None:
        partes.append(f"<Anulado>{anulado}</Anulado>")
    partes.append(f"<TpoOper>{tpo_oper}</TpoOper>")
    partes.append(f"<FchDoc>{fch_doc}</FchDoc>")
    if rut_doc:
        partes.append(f"<RUTDoc>{_normalizar_rut(rut_doc)}</RUTDoc>")
    if rzn_soc:
        partes.append(f"<RznSoc>{esc(rzn_soc)[:50]}</RznSoc>")
    if mnt_neto:
        partes.append(f"<MntNeto>{mnt_neto}</MntNeto>")
        partes.append(f"<TasaImp>{tasa_imp}</TasaImp>")
    if iva:
        partes.append(f"<IVA>{iva}</IVA>")
    if mnt_total:
        partes.append(f"<MntTotal>{mnt_total}</MntTotal>")
    # Referencia a la factura emitida (guía de venta facturada)
    if tpo_doc_ref and folio_doc_ref:
        partes.append(f"<TpoDocRef>{tpo_doc_ref}</TpoDocRef>")
        partes.append(f"<FolioDocRef>{folio_doc_ref}</FolioDocRef>")
        if fch_doc_ref:
            partes.append(f"<FchDocRef>{fch_doc_ref}</FchDocRef>")
    return "<Detalle>" + "".join(partes) + "</Detalle>"


def construir_resumen_guia(
    tot_folios_anulados: int = 0,
    tot_guias_anuladas: int = 0,
    tot_guias_venta: int = 0,
    tot_mnt_guias_venta: int = 0,
    tot_mnt_modificado: int = 0,
    guias_no_venta: Optional[List[Dict]] = None,  # [{cod_traslado, cantidad, monto}]
) -> str:
    """Construye el <ResumenPeriodo> del Libro de Guías.

    guias_no_venta: lista de dicts con cod_traslado (2-7), cantidad y monto.
    """
    partes = []
    if tot_folios_anulados:
        partes.append(f"<TotFolAnulado>{tot_folios_anulados}</TotFolAnulado>")
    if tot_guias_anuladas:
        partes.append(f"<TotGuiaAnulada>{tot_guias_anuladas}</TotGuiaAnulada>")
    partes.append(f"<TotGuiaVenta>{tot_guias_venta}</TotGuiaVenta>")
    partes.append(f"<TotMntGuiaVta>{tot_mnt_guias_venta}</TotMntGuiaVta>")
    if tot_mnt_modificado:
        partes.append(f"<TotMntModificado>{tot_mnt_modificado}</TotMntModificado>")
    # Tabla de guías no venta (hasta 6 ocurrencias por código de traslado)
    if guias_no_venta:
        for g in guias_no_venta:
            sub = f"<TpoTraslado>{g['cod_traslado']}</TpoTraslado>"
            sub += f"<CantGuia>{g['cantidad']}</CantGuia>"
            if g.get("monto"):
                sub += f"<MntGuia>{g['monto']}</MntGuia>"
            partes.append("<TotGuiaNoVenta>" + sub + "</TotGuiaNoVenta>")
    return "<ResumenPeriodo>" + "".join(partes) + "</ResumenPeriodo>"


def generar_libro_guia_xml(
    rut_emisor: str,
    rut_envia: str,
    periodo_tributario: str,
    detalles: List[str],
    resumen: str,
    fch_resol: str = "2026-05-15",
    nro_resol: int = 0,
    tipo_libro: str = "ESPECIAL",
    tipo_envio: str = "TOTAL",
    folio_notificacion: int = 1,
    libro_id: Optional[str] = None,
    tmst_firma: Optional[str] = None,
) -> Dict:
    """Genera el XML del Libro de Guías (SIN firma todavía).

    Returns: dict con xml(bytes), libro_id (para firmar EnvioLibro por su ID).
    """
    rut_emisor = _normalizar_rut(rut_emisor)
    rut_envia = _normalizar_rut(rut_envia)
    if libro_id is None:
        libro_id = "LIBROGUIA_" + datetime.now().strftime("%Y%m%d%H%M%S")
    if tmst_firma is None:
        tmst_firma = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    car_partes = [
        f"<RutEmisorLibro>{rut_emisor}</RutEmisorLibro>",
        f"<RutEnvia>{rut_envia}</RutEnvia>",
        f"<PeriodoTributario>{periodo_tributario}</PeriodoTributario>",
        f"<FchResol>{fch_resol}</FchResol>",
        f"<NroResol>{nro_resol}</NroResol>",
        f"<TipoLibro>{tipo_libro}</TipoLibro>",
        f"<TipoEnvio>{tipo_envio}</TipoEnvio>",
        f"<FolioNotificacion>{folio_notificacion}</FolioNotificacion>",
    ]
    caratula = "<Caratula>" + "".join(car_partes) + "</Caratula>"

    # Orden del schema LibroGuia: Caratula → ResumenPeriodo → Detalle(s) → TmstFirma
    # (el error de schema indicó que tras los Detalle solo va TmstFirma, así que el
    # ResumenPeriodo debe ir ANTES de los Detalle).
    detalle_xml = "\n" + "\n".join(detalles)

    envio_libro = (
        f'<EnvioLibro ID="{libro_id}">\n'
        + caratula + "\n"
        + resumen + "\n"
        + detalle_xml + "\n"
        + f"<TmstFirma>{tmst_firma}</TmstFirma>"
        + "</EnvioLibro>"
    )

    xml = (
        '<?xml version="1.0" encoding="ISO-8859-1"?>\n'
        '<LibroGuia xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xmlns="http://www.sii.cl/SiiDte" '
        'xsi:schemaLocation="http://www.sii.cl/SiiDte LibroGuia_v10.xsd" '
        'version="1.0">'
        + envio_libro
        + "</LibroGuia>"
    )

    return {
        "xml": xml.encode("ISO-8859-1"),
        "libro_id": libro_id,
    }
