"""
rcof.py — Generador del Reporte de Consumo de Folios (RCOF / ConsumoFolios).

El RCOF es un resumen de los folios consumidos en un período (normalmente un día).
Para la CERTIFICACIÓN de boletas electrónicas, el SII exige enviar el RCOF
asociado a los documentos del Set de Pruebas (las 5 boletas + las 2 notas de crédito).

Estructura (formato confirmado del SII / ejemplo SimpleAPI):

    <ConsumoFolios version="1.0" xmlns="http://www.sii.cl/SiiDte" ...>
      <DocumentoConsumoFolios ID="RCOF_xxx">
        <Caratula version="1.0">
          <RutEmisor>...</RutEmisor>
          <RutEnvia>...</RutEnvia>
          <FchResol>...</FchResol>
          <NroResol>0</NroResol>
          <FchInicio>...</FchInicio>
          <FchFinal>...</FchFinal>
          <SecEnvio>1</SecEnvio>
          <TmstFirmaEnv>...</TmstFirmaEnv>
        </Caratula>
        <Resumen>                       (uno por cada TipoDocumento)
          <TipoDocumento>39</TipoDocumento>
          <MntNeto>...</MntNeto>
          <MntIva>...</MntIva>
          <TasaIVA>19</TasaIVA>
          <MntExento>...</MntExento>     (condicional)
          <MntTotal>...</MntTotal>
          <FoliosEmitidos>...</FoliosEmitidos>
          <FoliosAnulados>0</FoliosAnulados>
          <FoliosUtilizados>...</FoliosUtilizados>
          <RangoUtilizados><Inicial>..</Inicial><Final>..</Final></RangoUtilizados>
        </Resumen>
      </DocumentoConsumoFolios>
      <Signature>...</Signature>          (se agrega con firma.py)
    </ConsumoFolios>

La firma XMLDSig se aplica luego sobre <DocumentoConsumoFolios> (por su ID),
igual que la boleta se firma sobre <Documento>.
"""

from datetime import datetime
from typing import List, Dict, Optional


NS_SIIDTE = "http://www.sii.cl/SiiDte"
TASA_IVA = 19


def _fmt_fecha(fecha: str) -> str:
    """Asegura formato AAAA-MM-DD."""
    return fecha


def construir_resumen(
    tipo_documento: int,
    mnt_neto: int,
    mnt_iva: int,
    mnt_total: int,
    folios_emitidos: int,
    folios_utilizados: int,
    rangos_utilizados: List[Dict],     # [{inicial, final}, ...]
    mnt_exento: int = 0,
    folios_anulados: int = 0,
    rangos_anulados: Optional[List[Dict]] = None,
) -> str:
    """Construye un bloque <Resumen> para un tipo de documento.

    Args:
        tipo_documento: 39 (boleta), 41 (boleta exenta), 61 (nota de crédito)
        mnt_neto: total monto neto afecto
        mnt_iva: total IVA
        mnt_total: suma neto + iva + exento
        folios_emitidos: cantidad de documentos emitidos
        folios_utilizados: emitidos + anulados
        rangos_utilizados: lista de rangos consecutivos [{inicial, final}]
        mnt_exento: total monto exento/no afecto (condicional)
        folios_anulados: cantidad de folios anulados (no por NC, sino por anulación de folios)
        rangos_anulados: rangos de folios anulados [{inicial, final?}]

    Returns:
        str con el bloque <Resumen>...</Resumen>
    """
    partes = ["<Resumen>"]
    partes.append(f"<TipoDocumento>{tipo_documento}</TipoDocumento>")
    partes.append(f"<MntNeto>{mnt_neto}</MntNeto>")
    partes.append(f"<MntIva>{mnt_iva}</MntIva>")
    # TasaIVA solo si hay IVA (boletas afectas). Para exentas/NC sin IVA puede ir igual.
    if mnt_iva > 0 or tipo_documento == 39:
        partes.append(f"<TasaIVA>{TASA_IVA}</TasaIVA>")
    if mnt_exento > 0:
        partes.append(f"<MntExento>{mnt_exento}</MntExento>")
    partes.append(f"<MntTotal>{mnt_total}</MntTotal>")
    partes.append(f"<FoliosEmitidos>{folios_emitidos}</FoliosEmitidos>")
    partes.append(f"<FoliosAnulados>{folios_anulados}</FoliosAnulados>")
    partes.append(f"<FoliosUtilizados>{folios_utilizados}</FoliosUtilizados>")

    # Rangos de folios utilizados (uno o más)
    for r in rangos_utilizados:
        partes.append("<RangoUtilizados>")
        partes.append(f"<Inicial>{r['inicial']}</Inicial>")
        partes.append(f"<Final>{r['final']}</Final>")
        partes.append("</RangoUtilizados>")

    # Rangos de folios anulados (condicional)
    if rangos_anulados:
        for r in rangos_anulados:
            partes.append("<RangoAnulados>")
            partes.append(f"<Inicial>{r['inicial']}</Inicial>")
            if r.get("final") is not None:
                partes.append(f"<Final>{r['final']}</Final>")
            partes.append("</RangoAnulados>")

    partes.append("</Resumen>")
    return "".join(partes)


def generar_rcof_xml(
    rut_emisor: str,
    rut_envia: str,
    fecha: str,                        # 'YYYY-MM-DD' (FchInicio = FchFinal para diario)
    resumenes: List[str],              # lista de bloques <Resumen> ya construidos
    fch_resol: str = "2014-08-22",
    nro_resol: int = 0,
    sec_envio: int = 1,
    tmst_firma: Optional[str] = None,
    documento_id: Optional[str] = None,
) -> Dict:
    """Genera el XML del RCOF (ConsumoFolios) SIN firma todavía.

    Args:
        rut_emisor: RUT empresa (ej '76922862-4')
        rut_envia: RUT del representante que firma (ej '18849272-K')
        fecha: día del reporte 'YYYY-MM-DD'
        resumenes: bloques <Resumen> generados con construir_resumen()
        fch_resol: fecha de resolución SII
        nro_resol: número de resolución (0 en certificación)
        sec_envio: secuencia de envío (1 la primera vez, +1 si se corrige)
        tmst_firma: timestamp de firma
        documento_id: ID del DocumentoConsumoFolios (para la firma)

    Returns:
        dict con xml(bytes), documento_id
    """
    if tmst_firma is None:
        tmst_firma = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    if documento_id is None:
        # ID único para el RCOF
        documento_id = "RCOF_" + datetime.now().strftime("%Y%m%d%H%M%S")

    # El SII espera los RUT sin puntos, solo con guión (ej '76922862-4').
    def _normalizar_rut(rut: str) -> str:
        return str(rut).replace(".", "").strip()
    rut_emisor = _normalizar_rut(rut_emisor)
    rut_envia = _normalizar_rut(rut_envia)

    caratula = (
        '<Caratula version="1.0">'
        f"<RutEmisor>{rut_emisor}</RutEmisor>"
        f"<RutEnvia>{rut_envia}</RutEnvia>"
        f"<FchResol>{fch_resol}</FchResol>"
        f"<NroResol>{nro_resol}</NroResol>"
        f"<FchInicio>{fecha}</FchInicio>"
        f"<FchFinal>{fecha}</FchFinal>"
        f"<SecEnvio>{sec_envio}</SecEnvio>"
        f"<TmstFirmaEnv>{tmst_firma}</TmstFirmaEnv>"
        "</Caratula>"
    )

    documento = (
        f'<DocumentoConsumoFolios ID="{documento_id}">'
        + caratula
        + "".join(resumenes)
        + "</DocumentoConsumoFolios>"
    )

    xml = (
        '<?xml version="1.0" encoding="ISO-8859-1"?>\n'
        '<ConsumoFolios xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xmlns="http://www.sii.cl/SiiDte" '
        'xsi:schemaLocation="http://www.sii.cl/SiiDte ConsumoFolio_v10.xsd" '
        'version="1.0">'
        + documento
        + "</ConsumoFolios>"
    )

    return {
        "xml": xml.encode("ISO-8859-1"),
        "documento_id": documento_id,
    }
