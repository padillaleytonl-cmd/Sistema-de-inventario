"""
credenciales_canal.py — Credenciales de los canales del cliente que esta operando.

Cada integracion (Walmart, Paris, Ripley, Falabella, WooCommerce) leia sus
credenciales UNA vez al importarse, de las variables de entorno. Eso las ataba
a un solo cliente para siempre. Aca se resuelven en el momento de usarlas:

  - Cliente dueño de las integraciones (LUSYNC_TENANT_INTEGRACIONES): las
    variables de entorno de siempre. Para el no cambia nada.
  - Cualquier otro cliente: lo que tenga guardado, cifrado, en
    credenciales_marketplace (tenancy.guardar_credenciales_canal).

El cliente sale de la sesion o del hilo (inventario.tenant_actual). Sin
ninguno de los dos se asume el dueño, que es como funcionaba todo antes.

MercadoLibre no pasa por aca: la app (client_id/secret) es de Lusync y sirve a
todos, y el token de cada vendedor ya es por cliente (mercadolibre_auth).
"""
import os
import threading
import time

DUENO = int(os.environ.get("LUSYNC_TENANT_INTEGRACIONES", "1"))

# Dueño: (variable de entorno, valor por defecto), igual que las constantes
# que tenia cada modulo.
_ENV = {
    ("walmart", "client_id"): ("WALMART_CLIENT_ID", None),
    ("walmart", "client_secret"): ("WALMART_CLIENT_SECRET", None),
    ("paris", "api_key"): ("PARIS_API_KEY", None),
    ("ripley", "api_key"): ("RIPLEY_API_KEY", ""),
    ("falabella", "user_id"): ("FALABELLA_USER_ID", ""),
    ("falabella", "api_key"): ("FALABELLA_API_KEY", ""),
    ("web", "consumer_key"): ("WC_KEY", None),
    ("web", "consumer_secret"): ("WC_SECRET", None),
    ("web", "site_url"): (None, "https://www.babymine.cl"),
}

# Otros clientes: el formulario "conectar marketplace" guarda algunos campos
# con otro nombre. Falabella rotula "User ID (email)" al campo api_key y
# "API Key" al campo api_secret.
_CAMPO_GUARDADO = {
    ("falabella", "user_id"): "api_key",
    ("falabella", "api_key"): "api_secret",
}

_TTL = 300
_cache = {}
_lock = threading.Lock()


def cliente_en_curso():
    """El cliente que esta operando: sesion, hilo, o el dueño si no hay."""
    try:
        from inventario import tenant_actual
        ctx = tenant_actual()
        if ctx and ctx[0]:
            return int(ctx[0])
    except Exception:
        pass
    return DUENO


def _guardadas(tenant_id, canal):
    ahora = time.time()
    with _lock:
        c = _cache.get((tenant_id, canal))
        if c and ahora - c[0] < _TTL:
            return c[1]
    try:
        from tenancy import obtener_credenciales_canal
        creds = obtener_credenciales_canal(tenant_id, canal) or {}
    except Exception as e:
        print(f"[credenciales_canal] {canal} cliente {tenant_id}: {e}")
        creds = {}
    with _lock:
        _cache[(tenant_id, canal)] = (ahora, creds)
    return creds


def credencial(canal, campo):
    """Valor de una credencial del canal para el cliente en curso."""
    tid = cliente_en_curso()
    if tid == DUENO:
        env, defecto = _ENV.get((canal, campo), (None, None))
        return os.environ.get(env, defecto) if env else defecto
    return _guardadas(tid, canal).get(_CAMPO_GUARDADO.get((canal, campo), campo)) or ""


def tiene_canal(canal, tenant_id=None):
    """True si el cliente tiene credenciales para el canal."""
    tid = tenant_id or cliente_en_curso()
    if tid == DUENO:
        return True
    return bool(_guardadas(tid, canal))


def olvidar(tenant_id=None, canal=None):
    """Descarta lo cacheado (despues de guardar o cambiar credenciales)."""
    with _lock:
        for k in list(_cache):
            if (tenant_id is None or k[0] == tenant_id) and (canal is None or k[1] == canal):
                del _cache[k]


class CachePorCliente:
    """Un dict por cliente, con la misma forma que el dict que reemplaza.

    Los tokens de Walmart y Paris se guardaban en un solo dict de modulo: con
    dos clientes, uno usaria el token del otro. Esta clase se usa igual que ese
    dict (cache["token"], cache.get(...)) pero cada cliente ve el suyo.
    """

    def __init__(self, inicial):
        self._inicial = dict(inicial)
        self._por_cliente = {}

    def _d(self):
        return self._por_cliente.setdefault(cliente_en_curso(), dict(self._inicial))

    def __getitem__(self, k):
        return self._d()[k]

    def __setitem__(self, k, v):
        self._d()[k] = v

    def get(self, k, defecto=None):
        return self._d().get(k, defecto)
