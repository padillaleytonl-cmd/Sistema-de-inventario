"""facturacion.dtes — generadores y parsers de DTEs del SII.

Módulos:
    caf_parser   — parsea CAFs (Códigos de Autorización de Folios) del SII
    ted          — genera el Timbre Electrónico (TED), firmado con la clave del CAF
    boleta       — genera el XML de Boleta Electrónica (tipo 39)
    firma        — firma XMLDSig de DTEs y del sobre EnvioBOLETA (usa el .pfx)
    envio_boleta — arma el sobre EnvioBOLETA con su Carátula

Uso típico:
    from facturacion.dtes.caf_parser import parsear_caf_xml
    from facturacion.dtes.boleta import generar_boleta_xml
    from facturacion.dtes.firma import firmar_documento, firmar_envio, verificar_firma_propia
    from facturacion.dtes.envio_boleta import armar_envio_boleta
"""
# Nota: no importamos los submódulos aquí para evitar fallos en el arranque
# si alguna dependencia (cryptography, lxml) no estuviera disponible.
# Cada módulo se importa explícitamente donde se usa.
