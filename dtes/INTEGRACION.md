# Módulos Backend SII — Fase 2A

## Ubicación en el repo
Estos archivos van en: `facturacion/dtes/`

```
facturacion/
  dtes/
    __init__.py
    caf_parser.py      # parsea CAFs del SII
    ted.py             # genera Timbre Electrónico (firma con clave del CAF)
    boleta.py          # genera XML Boleta 39 (afecto/exento/Kg/mixto)
    firma.py           # XMLDSig: firma boletas y sobre con .pfx
    envio_boleta.py    # arma sobre EnvioBOLETA con Carátula
```

## Estado: VALIDADO
- Parser: probado con los 3 CAFs reales
- TED: firma verificada matemáticamente contra clave pública del CAF
- Boleta: los 5 casos del Set BE calculan correcto
- Firma XMLDSig: firma+verifica boletas individuales y sobre completo
- Test integral: 5 boletas → sobre firmado → todo verifica OK

## Pendiente
- Endpoint auto-test (validar .pfx real en Lusync)
- sii_client.py (seed/token/upload mTLS)
- Notas de Crédito (anular boleta 4, rebajar boleta 1)
- RCOF (Reporte Consumo de Folios)

## Dependencias
- cryptography
- lxml
(ambas ya en requirements.txt o instalables)
