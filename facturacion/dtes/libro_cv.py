# -*- coding: utf-8 -*-
"""
libro_cv.py — Generación de Libros de Compra y Venta Electrónicos (IECV) para el SII.

Formato: FORMATO DE INFORMACIÓN ELECTRÓNICA DE COMPRAS Y VENTAS v3.0 (Marzo 2016)
Schema:  LibroCV_v10.xsd

Estructura del envío:
  <LibroCompraVenta>
    <EnvioLibro ID="...">
      <Caratula>...</Caratula>
      [<ResumenPeriodo>...</ResumenPeriodo>]   (obligatorio si TipoEnvio=TOTAL)
      [<Detalle>...</Detalle>...]               (uno por documento; ver reglas abajo)
    </EnvioLibro>
    <Signature>...</Signature>                  (XMLDSig sobre EnvioLibro por su ID)
  </LibroCompraVenta>

REGLAS CLAVE (del formato oficial, sección 1.3):
  - LIBRO DE VENTAS mensual: en el Detalle solo van documentos NO electrónicos y
    Facturas de Compra. Los DTE electrónicos emitidos NO se detallan (si se incluyen
    no se consideran). El ResumenPeriodo lleva los totales de TODOS los documentos.
    → Para un emisor 100% electrónico, el Libro de Ventas va SIN detalle.
  - LIBRO DE COMPRAS: SIEMPRE lleva Detalle (todos los documentos, electrónicos o no)
    + ResumenPeriodo. Incluye factor de proporcionalidad para IVA uso común.

La firma XMLDSig se aplica sobre <EnvioLibro> por su ID (igual que el RCOF sobre
DocumentoConsumoFolios), conservando el namespace.
"""

from datetime import datetime
from typing import Dict, List, Optional


def _normalizar_rut(rut: str) -> str:
    """RUT sin puntos, con guión y DV en mayúscula (ej '76922862-4')."""
    return str(rut).replace(".", "").strip().upper()


def construir_totales_periodo_venta(
    tpo_doc: int,
    tot_doc: int,
    tot_mnt_exe: int = 0,
    tot_mnt_neto: int = 0,
    tot_mnt_iva: int = 0,
    tot_mnt_total: int = 0,
    tot_iva_ret_total: int = 0,
    tot_iva_ret_parcial: int = 0,
    tot_iva_no_retenido: int = 0,
) -> str:
    """Construye un bloque <TotalesPeriodo> para el Libro de VENTAS.

    El orden de los tags es CRÍTICO para validar contra el schema.
    Solo se emiten los campos con valor (los condicionales en 0 se omiten,
    salvo Exe/Neto/IVA/Total que son obligatorios).
    """
    partes = [f"<TpoDoc>{tpo_doc}</TpoDoc>", f"<TotDoc>{tot_doc}</TotDoc>"]
    # Obligatorios: Exento, Neto, IVA (van siempre, aunque sean 0)
    partes.append(f"<TotMntExe>{tot_mnt_exe}</TotMntExe>")
    partes.append(f"<TotMntNeto>{tot_mnt_neto}</TotMntNeto>")
    partes.append(f"<TotMntIVA>{tot_mnt_iva}</TotMntIVA>")
    # IVA retenido (solo facturas de compra / NC-ND asociadas)
    if tot_iva_ret_total:
        partes.append(f"<TotIVARetTotal>{tot_iva_ret_total}</TotIVARetTotal>")
    if tot_iva_ret_parcial:
        partes.append(f"<TotIVARetParcial>{tot_iva_ret_parcial}</TotIVARetParcial>")
    # MontoTotal obligatorio
    partes.append(f"<TotMntTotal>{tot_mnt_total}</TotMntTotal>")
    if tot_iva_no_retenido:
        partes.append(f"<TotIVANoRetenido>{tot_iva_no_retenido}</TotIVANoRetenido>")
    return "<TotalesPeriodo>" + "".join(partes) + "</TotalesPeriodo>"


def construir_totales_periodo_compra(
    tpo_doc: int,
    tot_doc: int,
    tot_mnt_exe: int = 0,
    tot_mnt_neto: int = 0,
    tot_mnt_iva: int = 0,
    tot_mnt_total: int = 0,
    tot_op_iva_uso_comun: int = 0,
    tot_iva_uso_comun: int = 0,
    fct_prop: Optional[float] = None,
    tot_cred_iva_uso_comun: int = 0,
    tot_iva_no_rec: Optional[List[Dict]] = None,
    tot_iva_ret_total: int = 0,
    tot_mnt_iva_no_rec: int = 0,
    cod_iva_no_rec: Optional[int] = None,
) -> str:
    """Construye un bloque <TotalesPeriodo> para el Libro de COMPRAS.

    Soporta IVA uso común (con factor de proporcionalidad) e IVA no recuperable.
    El orden de tags sigue el schema (sección 3.3 del formato IECV).
    """
    partes = [f"<TpoDoc>{tpo_doc}</TpoDoc>", f"<TotDoc>{tot_doc}</TotDoc>"]
    partes.append(f"<TotMntExe>{tot_mnt_exe}</TotMntExe>")
    partes.append(f"<TotMntNeto>{tot_mnt_neto}</TotMntNeto>")
    partes.append(f"<TotMntIVA>{tot_mnt_iva}</TotMntIVA>")
    # IVA no recuperable (tabla con código + monto)
    if cod_iva_no_rec is not None and tot_mnt_iva_no_rec:
        partes.append(
            "<TotIVANoRec>"
            f"<CodIVANoRec>{cod_iva_no_rec}</CodIVANoRec>"
            f"<TotOpIVANoRec>1</TotOpIVANoRec>"
            f"<TotMntIVANoRec>{tot_mnt_iva_no_rec}</TotMntIVANoRec>"
            "</TotIVANoRec>"
        )
    # IVA uso común
    if tot_op_iva_uso_comun:
        partes.append(f"<TotOpIVAUsoComun>{tot_op_iva_uso_comun}</TotOpIVAUsoComun>")
    if tot_iva_uso_comun:
        partes.append(f"<TotIVAUsoComun>{tot_iva_uso_comun}</TotIVAUsoComun>")
    if fct_prop is not None:
        # Factor con 2 decimales (ej '0.60' → el SII acepta '0.6')
        partes.append(f"<FctProp>{fct_prop}</FctProp>")
    if tot_cred_iva_uso_comun:
        partes.append(f"<TotCredIVAUsoComun>{tot_cred_iva_uso_comun}</TotCredIVAUsoComun>")
    # IVA retenido total (facturas de compra)
    if tot_iva_ret_total:
        partes.append(f"<TotIVARetTotal>{tot_iva_ret_total}</TotIVARetTotal>")
    partes.append(f"<TotMntTotal>{tot_mnt_total}</TotMntTotal>")
    return "<TotalesPeriodo>" + "".join(partes) + "</TotalesPeriodo>"


def construir_detalle_compra(
    tpo_doc: int,
    nro_doc: int,
    fch_doc: str,
    rut_doc: str,
    mnt_total: int,
    tasa_imp: float = 19.0,
    mnt_exe: int = 0,
    mnt_neto: int = 0,
    mnt_iva: int = 0,
    rzn_soc: Optional[str] = None,
    iva_uso_comun: int = 0,
    cod_iva_no_rec: Optional[int] = None,
    mnt_iva_no_rec: int = 0,
    iva_ret_total: int = 0,
    emisor_nc_nd_fc: Optional[int] = None,
) -> str:
    """Construye un bloque <Detalle> para el Libro de COMPRAS.

    El orden de tags sigue la sección 3.4 del formato IECV.
    """
    def esc(s):
        return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    partes = [f"<TpoDoc>{tpo_doc}</TpoDoc>"]
    if emisor_nc_nd_fc is not None:
        partes.append(f"<Emisor>{emisor_nc_nd_fc}</Emisor>")
    partes.append(f"<NroDoc>{nro_doc}</NroDoc>")
    partes.append(f"<TasaImp>{tasa_imp}</TasaImp>")
    partes.append(f"<FchDoc>{fch_doc}</FchDoc>")
    partes.append(f"<RUTDoc>{_normalizar_rut(rut_doc)}</RUTDoc>")
    if rzn_soc:
        partes.append(f"<RznSoc>{esc(rzn_soc)[:50]}</RznSoc>")
    if mnt_exe:
        partes.append(f"<MntExe>{mnt_exe}</MntExe>")
    partes.append(f"<MntNeto>{mnt_neto}</MntNeto>")
    partes.append(f"<MntIVA>{mnt_iva}</MntIVA>")
    # IVA no recuperable
    if cod_iva_no_rec is not None and mnt_iva_no_rec:
        partes.append(
            "<IVANoRec>"
            f"<CodIVANoRec>{cod_iva_no_rec}</CodIVANoRec>"
            f"<MntIVANoRec>{mnt_iva_no_rec}</MntIVANoRec>"
            "</IVANoRec>"
        )
    # IVA uso común
    if iva_uso_comun:
        partes.append(f"<IVAUsoComun>{iva_uso_comun}</IVAUsoComun>")
    # IVA retenido total (facturas de compra)
    if iva_ret_total:
        partes.append(f"<OtrosImp><CodImp>15</CodImp><TasaImp>{tasa_imp}</TasaImp>"
                      f"<MntImp>{iva_ret_total}</MntImp></OtrosImp>")
    partes.append(f"<MntTotal>{mnt_total}</MntTotal>")
    return "<Detalle>" + "".join(partes) + "</Detalle>"


def generar_libro_xml(
    rut_emisor: str,
    rut_envia: str,
    periodo_tributario: str,            # 'AAAA-MM'
    tipo_operacion: str,                # 'VENTA' | 'COMPRA'
    totales_periodo: List[str],         # bloques <TotalesPeriodo> ya construidos
    detalles: Optional[List[str]] = None,  # bloques <Detalle> (solo compras / no-electrónicos)
    fch_resol: str = "2026-05-15",
    nro_resol: int = 0,
    tipo_libro: str = "ESPECIAL",       # ESPECIAL para certificación
    tipo_envio: str = "TOTAL",
    folio_notificacion: int = 1,
    cod_aut_rec: Optional[str] = None,
    libro_id: Optional[str] = None,
    tmst_firma: Optional[str] = None,
) -> Dict:
    """Genera el XML de un Libro de Compra/Venta (IECV) SIN firma todavía.

    Returns:
        dict con xml(bytes), libro_id (para la firma sobre EnvioLibro por su ID).
    """
    rut_emisor = _normalizar_rut(rut_emisor)
    rut_envia = _normalizar_rut(rut_envia)
    if libro_id is None:
        libro_id = "LIBRO_" + datetime.now().strftime("%Y%m%d%H%M%S")
    if tmst_firma is None:
        tmst_firma = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    # Carátula — el orden de tags es CRÍTICO para el schema
    car_partes = [
        f"<RutEmisorLibro>{rut_emisor}</RutEmisorLibro>",
        f"<RutEnvia>{rut_envia}</RutEnvia>",
        f"<PeriodoTributario>{periodo_tributario}</PeriodoTributario>",
        f"<FchResol>{fch_resol}</FchResol>",
        f"<NroResol>{nro_resol}</NroResol>",
        f"<TipoOperacion>{tipo_operacion}</TipoOperacion>",
        f"<TipoLibro>{tipo_libro}</TipoLibro>",
        f"<TipoEnvio>{tipo_envio}</TipoEnvio>",
    ]
    if folio_notificacion is not None:
        car_partes.append(f"<FolioNotificacion>{folio_notificacion}</FolioNotificacion>")
    if cod_aut_rec:
        car_partes.append(f"<CodAutRec>{cod_aut_rec}</CodAutRec>")
    caratula = "<Caratula>" + "".join(car_partes) + "</Caratula>"

    # ResumenPeriodo (obligatorio en TOTAL)
    resumen = "<ResumenPeriodo>" + "".join(totales_periodo) + "</ResumenPeriodo>"

    # Detalle (solo si hay; en ventas electrónicas NO va)
    detalle_xml = ""
    if detalles:
        detalle_xml = "".join(detalles)

    envio_libro = (
        f'<EnvioLibro ID="{libro_id}">'
        + caratula
        + resumen
        + detalle_xml
        + f"<TmstFirma>{tmst_firma}</TmstFirma>"
        + "</EnvioLibro>"
    )

    xml = (
        '<?xml version="1.0" encoding="ISO-8859-1"?>\n'
        '<LibroCompraVenta xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xmlns="http://www.sii.cl/SiiDte" '
        'xsi:schemaLocation="http://www.sii.cl/SiiDte LibroCV_v10.xsd" '
        'version="1.0">'
        + envio_libro
        + "</LibroCompraVenta>"
    )

    return {
        "xml": xml.encode("ISO-8859-1"),
        "libro_id": libro_id,
    }
