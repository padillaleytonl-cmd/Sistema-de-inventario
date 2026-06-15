"""
facturacion/dtes/envio_dte.py
─────────────────────────────────────────────────────────────
Arma el sobre <EnvioDTE> que agrupa DTE tradicionales (Notas de Crédito tipo 61,
facturas 33/34, notas de débito 56, etc.) para enviar al SII.

A diferencia de <EnvioBOLETA> (que va a pangal/boletas), el <EnvioDTE> se sube a
maullin/palena vía DTEUpload (POST tradicional). Estructura:

  <EnvioDTE version="1.0" xmlns=... xmlns:xsi=... xsi:schemaLocation=...>
    <SetDTE ID="SetDoc">
      <Caratula version="1.0">
        <RutEmisor>76922862-4</RutEmisor>
        <RutEnvia>18849272-K</RutEnvia>      ← representante legal (de la firma)
        <RutReceptor>60803000-K</RutReceptor> ← SII (constante en certificación)
        <FchResol>2026-05-15</FchResol>
        <NroResol>0</NroResol>                 ← 0 en certificación
        <TmstFirmaEnv>2026-05-28T15:00:00</TmstFirmaEnv>
        <SubTotDTE><TpoDTE>61</TpoDTE><NroDTE>2</NroDTE></SubTotDTE>
      </Caratula>
      <DTE>...</DTE>   ← cada NC YA firmada individualmente
      ...
    </SetDTE>
    <Signature>...</Signature>  ← firma del SetDTE completo
  </EnvioDTE>

Diferencias clave con EnvioBOLETA:
  • tag raíz <EnvioDTE> (no <EnvioBOLETA>)
  • schema EnvioDTE_v10.xsd (no EnvioBOLETA_v11.xsd)
  • puede agrupar varios TpoDTE distintos en varios <SubTotDTE>
"""
from __future__ import annotations
import re
from datetime import datetime
from typing import List, Dict

RUT_SII = "60803000-K"
NS_SII = "http://www.sii.cl/SiiDte"


def _extraer_dte_interno(dte_firmado_xml: bytes) -> str:
    """Extrae el <DTE>...</DTE> firmado, sin la declaración XML, para insertarlo
    dentro del SetDTE sin modificarlo (NO reindentar, NO tocar)."""
    s = dte_firmado_xml.decode("iso-8859-1")
    s = re.sub(r'^<\?xml[^>]*\?>\s*', '', s)
    return s.strip()


def armar_envio_dte(
    dtes_firmados: List[bytes],
    rut_emisor: str,
    rut_envia: str,
    fch_resol: str,            # 'YYYY-MM-DD'
    nro_resol: int = 0,        # 0 en certificación
    subtotales: Dict[int, int] = None,  # {tipo_dte: cantidad}; si None se infiere
    tipo_dte: int = 61,        # usado solo si subtotales es None
    set_dte_id: str = "SetDoc",
    tmst_firma_env: str = None,
) -> bytes:
    """Arma el sobre EnvioDTE SIN firmar todavía (la firma se agrega con firmar_envio_completo).

    Args:
        dtes_firmados: lista de bytes, cada uno un <DTE> ya firmado individualmente
        rut_emisor: RUT de la empresa (ej '76922862-4')
        rut_envia: RUT del representante legal que firma (ej '18849272-K')
        fch_resol: fecha de resolución SII
        nro_resol: número de resolución (0 en certificación)
        subtotales: dict {tipo_dte: cantidad}. Si None, todos los DTE son `tipo_dte`.
        tipo_dte: tipo por defecto si no se pasa subtotales (61 = NC)
        set_dte_id: ID del SetDTE (para la firma)
        tmst_firma_env: timestamp de firma del envío

    Returns:
        bytes del EnvioDTE sin firma (listo para firmar_envio_completo)
    """
    if tmst_firma_env is None:
        tmst_firma_env = datetime.now().strftime('%Y-%m-%dT%H:%M:%S')

    # El schema SII exige RUT sin puntos: [0-9]+-([0-9]|K)
    rut_emisor = str(rut_emisor).replace('.', '').strip()
    rut_envia = str(rut_envia).replace('.', '').strip()

    # Subtotales por tipo de DTE
    if subtotales is None:
        subtotales = {tipo_dte: len(dtes_firmados)}
    subtot_xml = ''.join(
        f'<SubTotDTE><TpoDTE>{t}</TpoDTE><NroDTE>{n}</NroDTE></SubTotDTE>'
        for t, n in subtotales.items()
    )

    # Carátula (misma estructura que EnvioBOLETA)
    caratula = (
        f'<Caratula version="1.0">'
        f'<RutEmisor>{rut_emisor}</RutEmisor>'
        f'<RutEnvia>{rut_envia}</RutEnvia>'
        f'<RutReceptor>{RUT_SII}</RutReceptor>'
        f'<FchResol>{fch_resol}</FchResol>'
        f'<NroResol>{nro_resol}</NroResol>'
        f'<TmstFirmaEnv>{tmst_firma_env}</TmstFirmaEnv>'
        f'{subtot_xml}'
        f'</Caratula>'
    )

    # Concatenar los DTE internos sin modificar (versión certificada SOK 3-jun:
    # sin saltos de línea entre DTEs, que provocaban HED-2-302 en factura compra).
    dtes_xml = ''.join(_extraer_dte_interno(d) for d in dtes_firmados)

    # Sobre completo (sin Signature todavía) — tag raíz EnvioDTE, schema v10.
    # CRÍTICO: orden de atributos EXACTO como lo exige el SII:
    #   xmlns → xmlns:xsi → xsi:schemaLocation → version
    # Si "version" va antes de "xsi:schemaLocation", el SII devuelve SCH-00001.
    envio = (
        '<?xml version="1.0" encoding="ISO-8859-1"?>'
        '<EnvioDTE '
        'xmlns="http://www.sii.cl/SiiDte" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xsi:schemaLocation="http://www.sii.cl/SiiDte EnvioDTE_v10.xsd" '
        'version="1.0">'
        f'<SetDTE ID="{set_dte_id}">'
        f'{caratula}'
        f'{dtes_xml}'
        f'</SetDTE>'
        '</EnvioDTE>'
    )

    return envio.encode("iso-8859-1", errors="replace")
