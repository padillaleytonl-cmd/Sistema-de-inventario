# ════════════════════════════════════════════════════════════════════════════
# tenant_rls.py — Row Level Security para multi-tenancy
# ════════════════════════════════════════════════════════════════════════════
# Estrategia:
#   1. Crear políticas RLS en las 22 tablas con tenant_id (idempotente)
#   2. Las políticas filtran por current_setting('app.tenant_id')
#   3. Bypass para super-admin con current_setting('app.is_admin')
#   4. RLS queda DESHABILITADO por defecto → no afecta a Babymine
#   5. Activación gradual con habilitar_rls(tabla) y deshabilitar_rls(tabla)
#
# Comportamiento durante transición:
#   - RLS deshabilitado: queries ven todo (= código actual)
#   - RLS habilitado + sin context: queries ven NADA (defensivo)
#   - RLS habilitado + con context: queries ven solo el tenant
#   - RLS habilitado + admin context: queries ven todo (bypass)
#
# IMPORTANTE: Esto NO modifica las 283 queries existentes. Solo agrega seguridad
# a nivel BD que se aplica automáticamente.

from inventario import get_conn, release_conn

# Tablas que llevan tenant_id (debe coincidir con tenancy.py)
TABLAS_TENANT = [
    "productos",
    "movimientos",
    "stock_bodega",
    "bodegas",
    "sku_mapeo_canal",
    "ordenes_procesadas",
    "devoluciones",
    "documentos_compra",
    "movimientos_documento",
    "ajustes_inventario",
    "pos_sesiones",
    "alertas",
    "audit_log",
]

POLICY_NAME = "tenant_isolation"


# ════════════════════════════════════════════════════════════════════════════
# CREAR POLÍTICAS — idempotente, NO habilita RLS todavía
# ════════════════════════════════════════════════════════════════════════════

def init_rls_policies():
    """Crea las políticas RLS en todas las tablas con tenant_id.
    Las políticas quedan creadas pero RLS NO se habilita aún.
    Idempotente: se puede correr múltiples veces.
    """
    conn = get_conn(); cur = conn.cursor()
    creadas, ya_existian, errores = [], [], []

    for tabla in TABLAS_TENANT:
        try:
            # Verificar que la tabla existe
            cur.execute("""
                SELECT EXISTS(SELECT FROM information_schema.tables WHERE table_name = %s)
            """, (tabla,))
            if not cur.fetchone()[0]:
                continue

            # Verificar que tiene columna tenant_id
            cur.execute("""
                SELECT EXISTS(
                    SELECT FROM information_schema.columns
                    WHERE table_name = %s AND column_name = 'tenant_id'
                )
            """, (tabla,))
            if not cur.fetchone()[0]:
                errores.append(f"{tabla}: sin columna tenant_id")
                continue

            # Verificar si la política ya existe
            cur.execute("""
                SELECT EXISTS(
                    SELECT FROM pg_policies
                    WHERE schemaname = 'public' AND tablename = %s AND policyname = %s
                )
            """, (tabla, POLICY_NAME))
            ya_existe = cur.fetchone()[0]

            if ya_existe:
                ya_existian.append(tabla)
                continue

            # Crear política con bypass para admin
            # USING: aplica a SELECT/UPDATE/DELETE — restringe LECTURA por tenant
            # WITH CHECK: aplica a INSERT/UPDATE — permisivo (true) durante transición
            #   esto permite que schedulers sin sesión Flask sigan creando registros.
            # Cuando todas las funciones llamadas por schedulers seteen tenant context,
            # se puede endurecer WITH CHECK para validar tenant_id explícitamente.
            cur.execute(f"""
                CREATE POLICY {POLICY_NAME} ON {tabla}
                AS PERMISSIVE
                FOR ALL
                USING (
                    tenant_id = COALESCE(NULLIF(current_setting('app.tenant_id', true), ''), '0')::int
                    OR COALESCE(NULLIF(current_setting('app.is_admin', true), ''), 'false') = 'true'
                )
                WITH CHECK (true)
            """)
            conn.commit()
            creadas.append(tabla)

        except Exception as e:
            errores.append(f"{tabla}: {e}")
            conn.rollback()

    cur.close()
    release_conn(conn)

    print(f"[RLS] Políticas creadas: {len(creadas)} | Ya existían: {len(ya_existian)} | Errores: {len(errores)}")
    if errores:
        for e in errores:
            print(f"[RLS]   ⚠ {e}")
    return {"creadas": creadas, "ya_existian": ya_existian, "errores": errores}


# ════════════════════════════════════════════════════════════════════════════
# HABILITAR / DESHABILITAR RLS por tabla
# ════════════════════════════════════════════════════════════════════════════

def recrear_policies():
    """DROP + CREATE de todas las políticas RLS.
    Útil cuando cambias la definición de la política y necesitas que tome efecto.
    """
    conn = get_conn(); cur = conn.cursor()
    resultado = {"dropeadas": [], "recreadas": [], "errores": []}

    for tabla in TABLAS_TENANT:
        try:
            cur.execute("""
                SELECT EXISTS(SELECT FROM information_schema.tables WHERE table_name = %s)
            """, (tabla,))
            if not cur.fetchone()[0]:
                continue

            # Drop policy si existe
            try:
                cur.execute(f"DROP POLICY IF EXISTS {POLICY_NAME} ON {tabla}")
                conn.commit()
                resultado["dropeadas"].append(tabla)
            except Exception as e:
                resultado["errores"].append(f"{tabla} (drop): {e}")
                conn.rollback()

            # Recrear con la definición actual (USING estricto + WITH CHECK permisivo)
            try:
                cur.execute(f"""
                    CREATE POLICY {POLICY_NAME} ON {tabla}
                    AS PERMISSIVE
                    FOR ALL
                    USING (
                        tenant_id = COALESCE(NULLIF(current_setting('app.tenant_id', true), ''), '0')::int
                        OR COALESCE(NULLIF(current_setting('app.is_admin', true), ''), 'false') = 'true'
                    )
                    WITH CHECK (true)
                """)
                conn.commit()
                resultado["recreadas"].append(tabla)
            except Exception as e:
                resultado["errores"].append(f"{tabla} (create): {e}")
                conn.rollback()

        except Exception as e:
            resultado["errores"].append(f"{tabla}: {e}")
            conn.rollback()

    cur.close()
    release_conn(conn)
    return resultado


def habilitar_rls(tabla):
    """Activa RLS en una tabla específica.
    También activa FORCE para que el OWNER de la tabla también respete RLS
    (sin FORCE, los OWNERs bypasean RLS automáticamente en Postgres).
    """
    if tabla not in TABLAS_TENANT:
        return {"error": f"{tabla} no está en la lista de tablas tenant"}

    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute(f"ALTER TABLE {tabla} ENABLE ROW LEVEL SECURITY")
        cur.execute(f"ALTER TABLE {tabla} FORCE ROW LEVEL SECURITY")
        conn.commit()
        return {"ok": True, "tabla": tabla, "estado": "RLS habilitado + forzado (owner también filtra)"}
    except Exception as e:
        conn.rollback()
        return {"error": str(e)}
    finally:
        cur.close()
        release_conn(conn)


def deshabilitar_rls(tabla):
    """Desactiva RLS en una tabla. ROLLBACK rápido si algo falla.
    Desactiva tanto ENABLE como FORCE.
    """
    if tabla not in TABLAS_TENANT:
        return {"error": f"{tabla} no está en la lista de tablas tenant"}

    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute(f"ALTER TABLE {tabla} NO FORCE ROW LEVEL SECURITY")
        cur.execute(f"ALTER TABLE {tabla} DISABLE ROW LEVEL SECURITY")
        conn.commit()
        return {"ok": True, "tabla": tabla, "estado": "RLS deshabilitado"}
    except Exception as e:
        conn.rollback()
        return {"error": str(e)}
    finally:
        cur.close()
        release_conn(conn)


def deshabilitar_rls_todas():
    """ROLLBACK de emergencia: deshabilita RLS en TODAS las tablas.
    Las políticas quedan creadas pero dormidas.
    """
    resultados = {}
    for t in TABLAS_TENANT:
        resultados[t] = deshabilitar_rls(t)
    return resultados


def estado_rls():
    """Reporta qué tablas tienen RLS habilitado actualmente."""
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("""
            SELECT c.relname AS tabla,
                   c.relrowsecurity AS rls_habilitado,
                   c.relforcerowsecurity AS rls_forzado,
                   (SELECT COUNT(*) FROM pg_policies p
                    WHERE p.schemaname = 'public' AND p.tablename = c.relname) AS policies
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public'
              AND c.relname = ANY(%s)
            ORDER BY c.relname
        """, (TABLAS_TENANT,))
        rows = cur.fetchall()
        resultado = {}
        for tabla, habilitado, forzado, policies in rows:
            resultado[tabla] = {
                "rls_habilitado": habilitado,
                "rls_forzado": forzado,
                "politicas_count": policies
            }
        return resultado
    finally:
        cur.close()
        release_conn(conn)


# ════════════════════════════════════════════════════════════════════════════
# SETEAR CONTEXTO DE TENANT EN UNA CONEXIÓN
# ════════════════════════════════════════════════════════════════════════════

def set_tenant_context(conn, tenant_id, is_admin=False):
    """Asigna el tenant_id activo a la conexión actual.
    Las queries siguientes en esta conexión filtrarán por este tenant
    (si la tabla tiene RLS habilitado).

    Args:
        conn: conexión psycopg2
        tenant_id: int del tenant
        is_admin: True para bypass (super-admin Lusync)
    """
    if not conn or conn.closed:
        return False
    try:
        cur = conn.cursor()
        # SET LOCAL solo aplica a la transacción actual — más seguro
        # Pero requiere estar dentro de transacción. SET sin LOCAL persiste en la sesión.
        cur.execute("SELECT set_config('app.tenant_id', %s, false)", (str(int(tenant_id)),))
        cur.execute("SELECT set_config('app.is_admin', %s, false)", ('true' if is_admin else 'false',))
        cur.close()
        return True
    except Exception as e:
        print(f"[RLS] Error set_tenant_context: {e}")
        return False


def clear_tenant_context(conn):
    """Limpia el contexto. Útil antes de devolver conexión al pool."""
    if not conn or conn.closed:
        return False
    try:
        cur = conn.cursor()
        cur.execute("SELECT set_config('app.tenant_id', '', false)")
        cur.execute("SELECT set_config('app.is_admin', 'false', false)")
        cur.close()
        return True
    except Exception:
        return False


# ════════════════════════════════════════════════════════════════════════════
# TEST DE AISLAMIENTO — sin habilitar RLS aún
# ════════════════════════════════════════════════════════════════════════════

def test_aislamiento_dry_run():
    """Prueba SIN habilitar RLS: simula qué pasaría.
    Cuenta filas por tenant en cada tabla.
    """
    conn = get_conn(); cur = conn.cursor()
    resultado = {}
    try:
        for tabla in TABLAS_TENANT:
            cur.execute("""
                SELECT EXISTS(SELECT FROM information_schema.tables WHERE table_name = %s)
            """, (tabla,))
            if not cur.fetchone()[0]:
                continue

            cur.execute("""
                SELECT EXISTS(
                    SELECT FROM information_schema.columns
                    WHERE table_name = %s AND column_name = 'tenant_id'
                )
            """, (tabla,))
            if not cur.fetchone()[0]:
                continue

            cur.execute(f"""
                SELECT tenant_id, COUNT(*) FROM {tabla}
                GROUP BY tenant_id ORDER BY tenant_id
            """)
            resultado[tabla] = {str(r[0]): r[1] for r in cur.fetchall()}
        return resultado
    finally:
        cur.close()
        release_conn(conn)
