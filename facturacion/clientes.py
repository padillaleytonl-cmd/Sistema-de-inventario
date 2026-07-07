# -*- coding: utf-8 -*-
"""Círculo de clientes por tenant.

Base de clientes/receptores frecuentes que cada tenant mantiene para emitir
documentos (boletas, facturas, guías, notas). Guarda TODOS los datos que el SII
puede exigir en un receptor; cada tipo de DTE toma solo los campos que su
esquema admite (la boleta usa rut+nombre; la factura usa además giro, dirección
y comuna; etc.).

La tabla se filtra por tenant_id de forma explícita (mismo enfoque que
facturacion_cafs). No expone datos entre tenants.
"""

from typing import Dict, List, Optional


def _crear_tabla(cur):
    """Crea la tabla de clientes si no existe. Idempotente."""
    cur.execute("""
        CREATE TABLE IF NOT EXISTS facturacion_clientes (
            id SERIAL PRIMARY KEY,
            tenant_id INTEGER NOT NULL,
            rut TEXT NOT NULL,
            razon_social TEXT NOT NULL,
            giro TEXT,
            direccion TEXT,
            comuna TEXT,
            ciudad TEXT,
            telefono TEXT,
            email TEXT,
            activo BOOLEAN DEFAULT TRUE,
            creado_en TIMESTAMP DEFAULT NOW(),
            actualizado_en TIMESTAMP DEFAULT NOW(),
            CONSTRAINT facturacion_cliente_tenant_rut UNIQUE (tenant_id, rut)
        )
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_facturacion_clientes_tenant
        ON facturacion_clientes(tenant_id, razon_social)
    """)


def init_clientes(get_conn, release_conn):
    """Inicializa la tabla (llamar al arranque, junto a las demás tablas)."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            _crear_tabla(cur)
        conn.commit()
    finally:
        release_conn(conn)


def _fila_a_dict(r) -> Dict:
    return {
        "id": r[0], "rut": r[1], "razon_social": r[2], "giro": r[3],
        "direccion": r[4], "comuna": r[5], "ciudad": r[6],
        "telefono": r[7], "email": r[8],
    }


_COLS = ("id, rut, razon_social, giro, direccion, comuna, ciudad, telefono, email")


def listar_clientes(get_conn, release_conn, tenant_id, q: str = None,
                    limite: int = 50) -> List[Dict]:
    """Lista los clientes del tenant. Si se pasa q, filtra por nombre o RUT."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            _crear_tabla(cur)
            if q:
                like = "%" + q.strip() + "%"
                cur.execute(
                    "SELECT " + _COLS + " FROM facturacion_clientes "
                    "WHERE tenant_id=%s AND activo=TRUE "
                    "AND (razon_social ILIKE %s OR rut ILIKE %s) "
                    "ORDER BY razon_social LIMIT %s",
                    (tenant_id, like, like, limite))
            else:
                cur.execute(
                    "SELECT " + _COLS + " FROM facturacion_clientes "
                    "WHERE tenant_id=%s AND activo=TRUE "
                    "ORDER BY razon_social LIMIT %s",
                    (tenant_id, limite))
            return [_fila_a_dict(r) for r in cur.fetchall()]
    finally:
        release_conn(conn)


def obtener_cliente(get_conn, release_conn, tenant_id, cliente_id) -> Optional[Dict]:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            _crear_tabla(cur)
            cur.execute(
                "SELECT " + _COLS + " FROM facturacion_clientes "
                "WHERE tenant_id=%s AND id=%s", (tenant_id, cliente_id))
            r = cur.fetchone()
            return _fila_a_dict(r) if r else None
    finally:
        release_conn(conn)


def _normalizar_rut(rut: str) -> str:
    """Normaliza el RUT: sin puntos, con guion, dígito verificador en mayúscula."""
    if not rut:
        return ""
    limpio = rut.replace(".", "").replace(" ", "").upper().strip()
    if "-" not in limpio and len(limpio) > 1:
        limpio = limpio[:-1] + "-" + limpio[-1]
    return limpio


def guardar_cliente(get_conn, release_conn, tenant_id, datos: Dict) -> Dict:
    """Crea o actualiza un cliente. Si datos trae 'id', actualiza; si no, crea.
    Valida los campos mínimos (rut y razón social). Devuelve {ok, cliente|error}."""
    rut = _normalizar_rut(datos.get("rut", ""))
    razon = (datos.get("razon_social") or "").strip()
    if not rut or not razon:
        return {"ok": False, "error": "RUT y razón social son obligatorios"}

    campos = {
        "rut": rut,
        "razon_social": razon,
        "giro": (datos.get("giro") or "").strip() or None,
        "direccion": (datos.get("direccion") or "").strip() or None,
        "comuna": (datos.get("comuna") or "").strip() or None,
        "ciudad": (datos.get("ciudad") or "").strip() or None,
        "telefono": (datos.get("telefono") or "").strip() or None,
        "email": (datos.get("email") or "").strip() or None,
    }

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            _crear_tabla(cur)
            cid = datos.get("id")
            if cid:
                cur.execute(
                    "UPDATE facturacion_clientes SET rut=%s, razon_social=%s, "
                    "giro=%s, direccion=%s, comuna=%s, ciudad=%s, telefono=%s, "
                    "email=%s, actualizado_en=NOW() "
                    "WHERE tenant_id=%s AND id=%s RETURNING " + _COLS,
                    (campos["rut"], campos["razon_social"], campos["giro"],
                     campos["direccion"], campos["comuna"], campos["ciudad"],
                     campos["telefono"], campos["email"], tenant_id, cid))
            else:
                cur.execute(
                    "INSERT INTO facturacion_clientes "
                    "(tenant_id, rut, razon_social, giro, direccion, comuna, "
                    " ciudad, telefono, email) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT (tenant_id, rut) DO UPDATE SET "
                    "razon_social=EXCLUDED.razon_social, giro=EXCLUDED.giro, "
                    "direccion=EXCLUDED.direccion, comuna=EXCLUDED.comuna, "
                    "ciudad=EXCLUDED.ciudad, telefono=EXCLUDED.telefono, "
                    "email=EXCLUDED.email, activo=TRUE, actualizado_en=NOW() "
                    "RETURNING " + _COLS,
                    (tenant_id, campos["rut"], campos["razon_social"],
                     campos["giro"], campos["direccion"], campos["comuna"],
                     campos["ciudad"], campos["telefono"], campos["email"]))
            r = cur.fetchone()
        conn.commit()
        return {"ok": True, "cliente": _fila_a_dict(r) if r else None}
    except Exception as e:
        conn.rollback()
        return {"ok": False, "error": str(e)[:200]}
    finally:
        release_conn(conn)


def eliminar_cliente(get_conn, release_conn, tenant_id, cliente_id) -> Dict:
    """Baja lógica del cliente (activo=FALSE). No borra para preservar historial."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            _crear_tabla(cur)
            cur.execute(
                "UPDATE facturacion_clientes SET activo=FALSE, actualizado_en=NOW() "
                "WHERE tenant_id=%s AND id=%s", (tenant_id, cliente_id))
        conn.commit()
        return {"ok": True}
    except Exception as e:
        conn.rollback()
        return {"ok": False, "error": str(e)[:200]}
    finally:
        release_conn(conn)
