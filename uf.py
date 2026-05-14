"""
Consulta y cache del valor UF diario.

Fuente: mindicador.cl (API pública gratuita)
Fallback: último valor guardado en BD

Estrategia:
    1. Consulta mindicador.cl al pedir UF de hoy
    2. Guarda en tabla uf_diaria(fecha, valor, fuente)
    3. Si ya hay valor de hoy en cache, no vuelve a consultar
    4. Si falla la API, devuelve el último valor disponible con warning

Uso:
    >>> from facturacion.uf import obtener_uf_actual
    >>> uf = obtener_uf_actual(get_conn, release_conn)
    >>> # {"valor": 39247.85, "fecha": "2026-05-14", "fuente": "mindicador.cl"}
"""

import json
from datetime import date, datetime, timedelta
from urllib import request as urlrequest
from urllib.error import URLError


MINDICADOR_URL = "https://mindicador.cl/api/uf"
TIMEOUT_SEGUNDOS = 5


def init_uf_table(get_conn_func, release_conn_func=None):
    """Crea la tabla uf_diaria si no existe."""
    conn = get_conn_func(is_admin=True); cur = conn.cursor()
    try:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS uf_diaria (
                fecha DATE PRIMARY KEY,
                valor NUMERIC(10, 2) NOT NULL,
                fuente TEXT DEFAULT 'mindicador.cl',
                actualizado_en TIMESTAMP DEFAULT NOW()
            )
        """)
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"[UF] Error creando tabla uf_diaria: {e}")
    finally:
        cur.close()
        if release_conn_func:
            release_conn_func(conn)
        else:
            conn.close()


def _consultar_mindicador():
    """Consulta API mindicador.cl. Devuelve (valor, fecha) o (None, None) si falla."""
    try:
        req = urlrequest.Request(
            MINDICADOR_URL,
            headers={"User-Agent": "Lusync/1.0 (contacto@lusync.cl)"}
        )
        with urlrequest.urlopen(req, timeout=TIMEOUT_SEGUNDOS) as response:
            data = json.loads(response.read().decode("utf-8"))

        # Estructura: serie = [{"fecha": "2026-05-14T03:00:00.000Z", "valor": 39247.85}, ...]
        serie = data.get("serie", [])
        if not serie:
            return None, None

        # El primer elemento de la serie es el más reciente
        ultimo = serie[0]
        valor = float(ultimo.get("valor", 0))
        fecha_str = ultimo.get("fecha", "")
        # Convertir ISO 8601 a date
        try:
            fecha = datetime.strptime(fecha_str[:10], "%Y-%m-%d").date()
        except Exception:
            fecha = date.today()

        return valor, fecha
    except (URLError, json.JSONDecodeError, ValueError, KeyError) as e:
        print(f"[UF] Error consultando mindicador.cl: {e}")
        return None, None
    except Exception as e:
        print(f"[UF] Error inesperado: {e}")
        return None, None


def obtener_uf_actual(get_conn_func, release_conn_func, forzar_refresh=False):
    """Obtiene el valor UF de hoy. Primero busca en cache, si no consulta API.

    Args:
        forzar_refresh: si True, ignora cache y consulta API
                        (útil si el cliente sospecha que está obsoleto)

    Returns:
        dict con:
            valor: float (CLP por UF)
            fecha: ISO YYYY-MM-DD
            fuente: 'mindicador.cl' | 'cache' | 'fallback'
            warning: str opcional si hubo problema
    """
    hoy = date.today()
    conn = get_conn_func(); cur = conn.cursor()

    try:
        # 1. Si no forzamos refresh, revisamos cache de hoy
        if not forzar_refresh:
            cur.execute("SELECT valor, fuente FROM uf_diaria WHERE fecha = %s", (hoy,))
            row = cur.fetchone()
            if row:
                return {
                    "valor": float(row[0]),
                    "fecha": hoy.isoformat(),
                    "fuente": "cache",
                    "fuente_original": row[1],
                }

        # 2. Consultar API mindicador.cl
        valor_api, fecha_api = _consultar_mindicador()

        if valor_api is not None and fecha_api is not None:
            # Guardar en cache
            try:
                cur.execute("""
                    INSERT INTO uf_diaria (fecha, valor, fuente, actualizado_en)
                    VALUES (%s, %s, 'mindicador.cl', NOW())
                    ON CONFLICT (fecha) DO UPDATE SET
                        valor = EXCLUDED.valor,
                        actualizado_en = NOW()
                """, (fecha_api, valor_api))
                conn.commit()
            except Exception as e:
                conn.rollback()
                print(f"[UF] Error guardando en cache: {e}")

            return {
                "valor": valor_api,
                "fecha": fecha_api.isoformat(),
                "fuente": "mindicador.cl",
            }

        # 3. Fallback: último valor disponible en cache
        cur.execute("""
            SELECT valor, fecha, fuente FROM uf_diaria
            ORDER BY fecha DESC LIMIT 1
        """)
        row = cur.fetchone()
        if row:
            return {
                "valor": float(row[0]),
                "fecha": row[1].isoformat(),
                "fuente": "fallback",
                "warning": "No se pudo consultar mindicador.cl, usando último valor en cache",
            }

        # 4. Sin nada: error
        return {
            "valor": 37000.0,  # valor conservador fijo de emergencia
            "fecha": hoy.isoformat(),
            "fuente": "default",
            "warning": "No hay UF disponible (ni cache ni API). Usando $37.000 como fallback.",
        }
    finally:
        cur.close()
        release_conn_func(conn)


def obtener_uf_fecha(get_conn_func, release_conn_func, fecha_consulta):
    """Obtiene el valor UF de una fecha específica (para retroactivos / facturación pasada).

    Args:
        fecha_consulta: date object o string ISO YYYY-MM-DD

    Returns:
        dict como obtener_uf_actual, o None si no hay dato
    """
    if isinstance(fecha_consulta, str):
        fecha_consulta = datetime.strptime(fecha_consulta, "%Y-%m-%d").date()

    conn = get_conn_func(); cur = conn.cursor()
    try:
        cur.execute("SELECT valor, fuente FROM uf_diaria WHERE fecha = %s", (fecha_consulta,))
        row = cur.fetchone()
        if row:
            return {
                "valor": float(row[0]),
                "fecha": fecha_consulta.isoformat(),
                "fuente": "cache",
                "fuente_original": row[1],
            }

        # Si no hay dato, devolver el más cercano anterior
        cur.execute("""
            SELECT valor, fecha, fuente FROM uf_diaria
            WHERE fecha <= %s
            ORDER BY fecha DESC LIMIT 1
        """, (fecha_consulta,))
        row = cur.fetchone()
        if row:
            return {
                "valor": float(row[0]),
                "fecha": row[1].isoformat(),
                "fuente": "aproximado",
                "warning": f"No hay UF para {fecha_consulta.isoformat()}, usando valor del {row[1].isoformat()}",
            }

        return None
    finally:
        cur.close()
        release_conn_func(conn)
