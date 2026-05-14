"""
Emisión de DTEs (Documentos Tributarios Electrónicos).

⚠ ESTE MÓDULO ES PLACEHOLDER ⚠
Se construye en Fase 2 (próxima sesión).

Responsabilidades futuras:
    - generar_xml_dte(): construye el XML del DTE según el tipo
    - generar_ted(): genera el Timbre Electrónico Datada (TED) firmado con CAF
    - firmar_dte(): firma el XML con el certificado .pfx (xmldsig)
    - empaquetar_envio(): arma EnvioDTE o EnvioBOLETA
    - enviar_al_sii(): autentica con seed/token, hace POST al endpoint correcto
    - consultar_estado(): consulta estado en SII usando track_id
    - generar_pdf(): convierte DTE a PDF para mostrar al cliente

Estructura aproximada del XML por tipo:
    33 (Factura)        → DTE con MntNeto, IVA, Receptor con RUT empresa
    39 (Boleta)         → DTE con MntTotal incluyendo IVA, Receptor opcional
    52 (Guía Despacho)  → DTE con destino físico, sin valores en algunos casos
    56 (Nota Débito)    → DTE referenciando otro documento (RefDoc)
    61 (Nota Crédito)   → DTE con motivo (anula, corrige, devolución parcial)
"""


def emitir_dte(*args, **kwargs):
    """Placeholder. Se implementa en Fase 2."""
    return {
        "ok": False,
        "error": "Emisión de DTEs no implementada todavía. Estamos en Fase 1 (configuración)."
    }


def consultar_estado_sii(*args, **kwargs):
    """Placeholder. Se implementa en Fase 2."""
    return {
        "ok": False,
        "error": "Consulta de estado SII no implementada todavía."
    }
