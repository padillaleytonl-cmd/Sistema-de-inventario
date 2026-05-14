"""
Módulo de Facturación Electrónica SII Chile para Lusync.

Estructura:
    facturacion/
        __init__.py           ← este archivo (orquestación)
        certificados.py       ← subir/leer/encriptar .pfx (PKCS#12)
        cafs.py               ← gestionar archivos CAF del SII (folios)
        dtes.py               ← generar XML, firmar, enviar al SII (Fase 2)
        utils.py              ← helpers (RUT, validaciones, ambientes)
        db.py                 ← init_facturacion_tables y queries específicas

Fases:
    F1 (HOY)   → certificados.py + db.py + UI subir .pfx
    F2 (next)  → dtes.py: generador XML Boleta(39) + firma digital
    F3         → Set de pruebas SII (certificación)
    F4         → Factura(33) + NC(61) + ND(56) + Guía(52)
    F5         → UI emisión automática post-venta
"""

# Exports públicos
from .db import (
    init_facturacion_tables,
    obtener_config_facturacion,
    guardar_config_facturacion,
)
from .certificados import (
    subir_certificado,
    obtener_certificado,
    listar_certificados_tenant,
    eliminar_certificado,
    validar_pfx,
)
from .cafs import (
    subir_caf,
    listar_cafs_tenant,
    obtener_folio_disponible,
    marcar_folio_usado,
)
from .utils import (
    validar_rut,
    formatear_rut,
    AMBIENTE_CERTIFICACION,
    AMBIENTE_PRODUCCION,
    TIPOS_DTE,
)

__all__ = [
    "init_facturacion_tables",
    "obtener_config_facturacion",
    "guardar_config_facturacion",
    "subir_certificado",
    "obtener_certificado",
    "listar_certificados_tenant",
    "eliminar_certificado",
    "validar_pfx",
    "subir_caf",
    "listar_cafs_tenant",
    "obtener_folio_disponible",
    "marcar_folio_usado",
    "validar_rut",
    "formatear_rut",
    "AMBIENTE_CERTIFICACION",
    "AMBIENTE_PRODUCCION",
    "TIPOS_DTE",
]
