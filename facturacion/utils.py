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
# precio_uf: cuánto cuesta al tenant tener este DTE habilitado (add-on mensual)
# Estrategia comercial: boletas y NC gratis (volumen alto, masivo);
# factura premium (cliente B2B paga más); exportación cara (cliente serio).
TIPOS_DTE = {
    33:  {"nombre": "Factura Electrónica",             "afecto_iva": True,  "tipo": "venta", "precio_uf": 0.2},
    34:  {"nombre": "Factura Exenta",                   "afecto_iva": False, "tipo": "venta", "precio_uf": 0.2},
    39:  {"nombre": "Boleta Electrónica",               "afecto_iva": True,  "tipo": "venta", "precio_uf": 0.0},
    41:  {"nombre": "Boleta Exenta",                    "afecto_iva": False, "tipo": "venta", "precio_uf": 0.1},
    43:  {"nombre": "Liquidación de Factura",           "afecto_iva": True,  "tipo": "venta", "precio_uf": 0.1},
    46:  {"nombre": "Factura de Compra",                "afecto_iva": True,  "tipo": "compra", "precio_uf": 0.1},
    52:  {"nombre": "Guía de Despacho",                 "afecto_iva": True,  "tipo": "guia",  "precio_uf": 0.1},
    56:  {"nombre": "Nota de Débito",                   "afecto_iva": True,  "tipo": "nota",  "precio_uf": 0.1},
    61:  {"nombre": "Nota de Crédito",                  "afecto_iva": True,  "tipo": "nota",  "precio_uf": 0.0},
    110: {"nombre": "Factura de Exportación",           "afecto_iva": False, "tipo": "venta", "precio_uf": 0.4},
    111: {"nombre": "Nota de Débito de Exportación",    "afecto_iva": False, "tipo": "nota",  "precio_uf": 0.2},
    112: {"nombre": "Nota de Crédito de Exportación",   "afecto_iva": False, "tipo": "nota",  "precio_uf": 0.2},
}

# Mapeo: campo de BD en facturacion_config_tenant → código DTE
# Útil para calcular el costo tributario a partir de los flags emite_*
CAMPO_BD_A_TIPO_DTE = {
    "emite_boleta":         39,
    "emite_boleta_exenta":  41,
    "emite_factura":        33,
    "emite_factura_exenta": 34,
    "emite_factura_compra": 46,
    "emite_liquidacion":    43,
    "emite_nota_credito":   61,
    "emite_nota_debito":    56,
    "emite_guia_despacho":  52,
    "emite_fact_exportacion": 110,
    "emite_nc_exportacion":   112,
    "emite_nd_exportacion":   111,
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


# ─────────────────────────────────────────────────────────────────────────────
# RESOLUCIÓN SII DE LA CARÁTULA (FchResol / NroResol)
# ─────────────────────────────────────────────────────────────────────────────
# El SII no autoriza "al contribuyente" de una sola vez: autoriza cada familia de
# documentos por separado, y cada sobre debe declarar LA SUYA. El propio XSD lo
# dice en los campos de la carátula del EnvioBOLETA:
#
#   FchResol -> "Fecha de Resolucion que Autoriza la Emision de Boletas"
#   NroResol -> "Numero de Resolucion que Autoriza la Emision de Boletas"
#
# No es la resolución del Sistema de Factura Electrónica. Para Grupo PH SPA son
# dos fechas distintas: facturas desde el 27-08-2019 (resolución 80 del
# 22-08-2014) y boletas desde el 30-12-2020. Mientras las boletas viajaron con la
# resolución de facturas, el SII recibía el sobre, entregaba track id... y al
# validarlo no registraba el documento.
#
# El formato del sobre NO cambia: es exactamente el que pasó certificación
# (TrackID 249504588, set SOK). Lo único que cambia es de dónde sale este par.

# Par usado en certificacion. Solo se usa como ultimo recurso, cuando el tenant
# todavia no tiene ninguna resolucion cargada.
RESOLUCION_CERTIFICACION = ("2026-05-15", 0)


def _fecha_a_texto(v):
    """Normaliza una fecha (date, datetime o str) al 'YYYY-MM-DD' que pide el SII."""
    if not v:
        return None
    if isinstance(v, str):
        return v.strip() or None
    try:
        return v.isoformat()
    except AttributeError:
        return str(v)


def resolucion_para(config, tipo_dte=None):
    """Devuelve (fch_resol, nro_resol) para la caratula del sobre.

    Es la misma para todos los tipos de documento. El parametro `tipo_dte` se
    conserva porque lo pasan los llamadores, pero no cambia el resultado.

    Historia, para que no se vuelva a intentar: el XSD del EnvioBOLETA describe
    estos campos como "Resolucion que Autoriza la Emision de Boletas", asi que
    parecia que boletas y facturas debian declarar resoluciones distintas. Se
    implemento esa separacion y resulto ser falsa. La resolucion del contribuyente
    es una sola -para Grupo PH, la N 80 del 22-08-2014, la misma que usa Lioren en
    sus boletas- y la fecha que figura en "Consulta de contribuyentes autorizados"
    no es una resolucion: es desde cuando el contribuyente quedo habilitado a
    emitir ese documento.

    Lo que rechazaba las boletas era otra cosa: un RUT receptor con el digito
    verificador mal (codigo 110). La boleta 25211 salio con esta resolucion
    general y el SII la acepto con DOK.
    """
    config = config or {}
    fecha = _fecha_a_texto(config.get("resolucion_sii_fecha"))
    numero = config.get("resolucion_sii_numero")
    if fecha:
        return fecha, int(numero) if numero is not None else 0
    return RESOLUCION_CERTIFICACION


def resolucion_rcof(config):
    """Resolucion del Reporte de Consumo de Folios."""
    return resolucion_para(config)
