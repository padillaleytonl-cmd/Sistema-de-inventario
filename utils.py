"""
Utilidades de facturación: validación RUT chileno, constantes,
ambientes SII, tipos de DTE.
"""

# ─────────────────────────────────────────────────────────────────────────────
# AMBIENTES SII
# ─────────────────────────────────────────────────────────────────────────────
AMBIENTE_CERTIFICACION = "certificacion"
AMBIENTE_PRODUCCION = "produccion"

# URLs base SII (sin endpoint específico, eso lo arma dtes.py)
SII_URLS = {
    AMBIENTE_CERTIFICACION: {
        "seed":       "https://maullin.sii.cl/DTEWS/CrSeed.jws",
        "token":      "https://maullin.sii.cl/DTEWS/GetTokenFromSeed.jws",
        "upload":     "https://maullin.sii.cl/cgi_dte/UPL/DTEUpload",
        "estado":     "https://maullin.sii.cl/DTEWS/QueryEstUp.jws",
        "rce":        "https://palena.sii.cl/recursos/v1/boleta.electronica.envio",  # boletas (mismo endpoint en cert/prod)
    },
    AMBIENTE_PRODUCCION: {
        "seed":       "https://palena.sii.cl/DTEWS/CrSeed.jws",
        "token":      "https://palena.sii.cl/DTEWS/GetTokenFromSeed.jws",
        "upload":     "https://palena.sii.cl/cgi_dte/UPL/DTEUpload",
        "estado":     "https://palena.sii.cl/DTEWS/QueryEstUp.jws",
        "rce":        "https://palena.sii.cl/recursos/v1/boleta.electronica.envio",
    },
}

# ─────────────────────────────────────────────────────────────────────────────
# TIPOS DE DTE (códigos oficiales SII)
# ─────────────────────────────────────────────────────────────────────────────
TIPOS_DTE = {
    33:  {"nombre": "Factura Electrónica",             "afecto_iva": True,  "tipo": "venta"},
    34:  {"nombre": "Factura Exenta",                   "afecto_iva": False, "tipo": "venta"},
    39:  {"nombre": "Boleta Electrónica",               "afecto_iva": True,  "tipo": "venta"},
    41:  {"nombre": "Boleta Exenta",                    "afecto_iva": False, "tipo": "venta"},
    46:  {"nombre": "Factura de Compra",                "afecto_iva": True,  "tipo": "compra"},
    52:  {"nombre": "Guía de Despacho",                 "afecto_iva": True,  "tipo": "guia"},
    56:  {"nombre": "Nota de Débito",                   "afecto_iva": True,  "tipo": "nota"},
    61:  {"nombre": "Nota de Crédito",                  "afecto_iva": True,  "tipo": "nota"},
    110: {"nombre": "Factura de Exportación",           "afecto_iva": False, "tipo": "venta"},
    111: {"nombre": "Nota de Débito de Exportación",    "afecto_iva": False, "tipo": "nota"},
    112: {"nombre": "Nota de Crédito de Exportación",   "afecto_iva": False, "tipo": "nota"},
}

# Subconjunto que vamos a emitir en este SaaS (todos los importantes)
TIPOS_DTE_HABILITADOS = [33, 34, 39, 41, 52, 56, 61]


# ─────────────────────────────────────────────────────────────────────────────
# VALIDACIÓN RUT CHILENO
# ─────────────────────────────────────────────────────────────────────────────
def _calcular_dv(rut_num):
    """Calcula dígito verificador del RUT (algoritmo módulo 11)."""
    rut_str = str(rut_num)
    reversed_digits = map(int, reversed(rut_str))
    factors = [2, 3, 4, 5, 6, 7]
    s = sum(d * factors[i % 6] for i, d in enumerate(reversed_digits))
    mod = (-s) % 11
    if mod == 10:
        return "K"
    return str(mod)


def validar_rut(rut):
    """Valida un RUT chileno. Acepta formatos: 12345678-K, 12.345.678-K, 12345678K.
    Devuelve True/False.
    """
    if not rut:
        return False
    # Limpiar: quitar puntos, espacios, guion
    rut_limpio = str(rut).replace(".", "").replace("-", "").replace(" ", "").upper()
    if len(rut_limpio) < 2:
        return False
    cuerpo = rut_limpio[:-1]
    dv = rut_limpio[-1]
    if not cuerpo.isdigit():
        return False
    try:
        return _calcular_dv(int(cuerpo)) == dv
    except Exception:
        return False


def formatear_rut(rut, con_puntos=True):
    """Formatea RUT como '12.345.678-K' (default) o '12345678-K' (sin puntos)."""
    rut_limpio = str(rut).replace(".", "").replace("-", "").replace(" ", "").upper()
    if len(rut_limpio) < 2:
        return rut
    cuerpo = rut_limpio[:-1]
    dv = rut_limpio[-1]
    if con_puntos:
        # Agrega puntos cada 3 dígitos desde la derecha
        partes = []
        while len(cuerpo) > 3:
            partes.insert(0, cuerpo[-3:])
            cuerpo = cuerpo[:-3]
        partes.insert(0, cuerpo)
        return ".".join(partes) + "-" + dv
    return cuerpo + "-" + dv


def rut_sin_dv(rut):
    """Devuelve solo el número del RUT (sin dígito verificador) como int.
    Útil para XML del SII donde el RUT va como número en algunos campos.
    """
    rut_limpio = str(rut).replace(".", "").replace("-", "").replace(" ", "").upper()
    if len(rut_limpio) < 2:
        return None
    cuerpo = rut_limpio[:-1]
    try:
        return int(cuerpo)
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS GENERALES
# ─────────────────────────────────────────────────────────────────────────────
def obtener_url_sii(ambiente, endpoint):
    """Devuelve URL del endpoint SII según ambiente.
    ambiente: 'certificacion' | 'produccion'
    endpoint: 'seed' | 'token' | 'upload' | 'estado' | 'rce'
    """
    return SII_URLS.get(ambiente, {}).get(endpoint)


def normalizar_ambiente(ambiente):
    """Normaliza el string del ambiente a uno de los 2 valores válidos."""
    if not ambiente:
        return AMBIENTE_CERTIFICACION  # default: certificación (más seguro)
    a = str(ambiente).lower().strip()
    if a in ("prod", "produccion", "producción", "production"):
        return AMBIENTE_PRODUCCION
    return AMBIENTE_CERTIFICACION
