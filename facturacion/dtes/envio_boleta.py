"""
facturacion/dtes/envio_boleta.py
─────────────────────────────────────────────────────────────
Arma el sobre <EnvioBOLETA> que agrupa las boletas firmadas para enviar al SII.

Estructura:
  <EnvioBOLETA version="1.0" xmlns=... xmlns:xsi=... xsi:schemaLocation=...>
    <SetDTE ID="SetDoc">
      <Caratula version="1.0">
        <RutEmisor>76922862-4</RutEmisor>
        <RutEnvia>18849272-K</RutEnvia>     ← representante legal (de la firma)
        <RutReceptor>60803000-K</RutReceptor> ← SII (constante)
        <FchResol>2014-08-22</FchResol>
        <NroResol>0</NroResol>                ← 0 en certificación
        <TmstFirmaEnv>2026-05-20T15:00:00</TmstFirmaEnv>
        <SubTotDTE><TpoDTE>39</TpoDTE><NroDTE>5</NroDTE></SubTotDTE>
      </Caratula>
      <DTE>...</DTE>   ← cada boleta YA firmada individualmente
      ...
    </SetDTE>
    <Signature>...</Signature>  ← firma del SetDTE completo
  </EnvioBOLETA>

RUT del SII (receptor) es constante: 60803000-K
"""
from __future__ import annotations
import re
from datetime import datetime
from typing import List

from lxml import etree

RUT_SII = "60803000-K"
NS_SII = "http://www.sii.cl/SiiDte"


def _extraer_dte_interno(dte_firmado_xml: bytes) -> str:
    """Extrae el contenido del <DTE>...</DTE> firmado, sin la declaración XML,
    para insertarlo dentro del SetDTE.
    """
    s = dte_firmado_xml.decode("iso-8859-1")
    # Quitar declaración <?xml ...?>
    s = re.sub(r'^<\?xml[^>]*\?>\s*', '', s)
    return s.strip()


def armar_envio_boleta(
    dtes_firmados: List[bytes],
    rut_emisor: str,
    rut_envia: str,
    fch_resol: str,           # 'YYYY-MM-DD'
    nro_resol: int = 0,       # 0 en certificación
    tipo_dte: int = 39,
    set_dte_id: str = "SetDoc",
    tmst_firma_env: str = None,
) -> bytes:
    """Arma el sobre EnvioBOLETA SIN firmar todavía (la firma se agrega con firmar_envio).

    Args:
        dtes_firmados: lista de bytes, cada uno un <DTE> ya firmado individualmente
        rut_emisor: RUT de la empresa (ej '76922862-4')
        rut_envia: RUT del representante legal que firma (ej '18849272-K')
        fch_resol: fecha de resolución SII
        nro_resol: número de resolución (0 en certificación)
        tipo_dte: 39 para boletas
        set_dte_id: ID del SetDTE (para la firma)
        tmst_firma_env: timestamp de firma del envío

    Returns:
        bytes del EnvioBOLETA sin firma (listo para firmar_envio)
    """
    if tmst_firma_env is None:
        tmst_firma_env = datetime.now().strftime('%Y-%m-%dT%H:%M:%S')

    nro_dte = len(dtes_firmados)

    # El schema SII exige RUT sin puntos: [0-9]+-([0-9]|K)
    rut_emisor = str(rut_emisor).replace('.', '').strip()
    rut_envia = str(rut_envia).replace('.', '').strip()

    # Carátula
    caratula = (
        f'<Caratula version="1.0">'
        f'<RutEmisor>{rut_emisor}</RutEmisor>'
        f'<RutEnvia>{rut_envia}</RutEnvia>'
        f'<RutReceptor>{RUT_SII}</RutReceptor>'
        f'<FchResol>{fch_resol}</FchResol>'
        f'<NroResol>{nro_resol}</NroResol>'
        f'<TmstFirmaEnv>{tmst_firma_env}</TmstFirmaEnv>'
        f'<SubTotDTE><TpoDTE>{tipo_dte}</TpoDTE><NroDTE>{nro_dte}</NroDTE></SubTotDTE>'
        f'</Caratula>'
    )

    # Concatenar los DTE internos
    dtes_xml = ''.join(_extraer_dte_interno(d) for d in dtes_firmados)

    # Sobre completo (sin Signature todavía)
    envio = (
        '<?xml version="1.0" encoding="ISO-8859-1"?>'
        '<EnvioBOLETA version="1.0" '
        'xmlns="http://www.sii.cl/SiiDte" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xsi:schemaLocation="http://www.sii.cl/SiiDte EnvioBOLETA_v11.xsd">'
        f'<SetDTE ID="{set_dte_id}">'
        f'{caratula}'
        f'{dtes_xml}'
        f'</SetDTE>'
        '</EnvioBOLETA>'
    )

    return envio.encode("iso-8859-1", errors="replace")
