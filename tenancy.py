# ════════════════════════════════════════════════════════════════════════════
# tenancy.py — Multi-tenancy para Lusync
# ════════════════════════════════════════════════════════════════════════════
# Provee:
#   1. init_multitenancy(): crea tablas globales (tenants, planes, usuarios, credenciales_marketplace, marketplaces_catalogo)
#   2. ALTER TABLE para agregar tenant_id a todas las tablas existentes
#   3. Babymine queda como tenant_id=1 automáticamente
#   4. Helpers: get_tenant_actual(), get_tenant_or_die(), hash_password(), verify_password()
#   5. Encriptación de credenciales con Fernet (clave en env LUSYNC_FERNET_KEY)
#
# IMPORTANTE: este módulo NO modifica queries existentes. Solo prepara la BD.
# Modificar queries para filtrar por tenant_id es el siguiente paso (Semana 1 Fase 2).

import os
import hashlib
import secrets
from datetime import datetime
from flask import session
import psycopg2

from inventario import get_conn, release_conn, now_chile

# ════════════════════════════════════════════════════════════════════════════
# TABLAS QUE LLEVAN tenant_id (se les agrega en migración inicial)
# ════════════════════════════════════════════════════════════════════════════
# Lista exhaustiva de tablas del cliente (con sus datos) que deben filtrar por tenant.

TABLAS_CON_TENANT = [
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
    "config_canal",
    "stock_marketplaces",
    "sync_log",
    "ventas_marketplace_log",
    "shipments_procesados",
    "categorias",
    "etiquetas",
    "precios_canal",
    "comisiones_personalizadas",
]


# ════════════════════════════════════════════════════════════════════════════
# INICIALIZACIÓN — crea tablas globales y migra existentes
# ════════════════════════════════════════════════════════════════════════════

def init_multitenancy():
    """Inicializa multi-tenancy. Idempotente: se puede correr varias veces.

    Pasos:
    1. Crear tablas globales (tenants, planes, usuarios, credenciales_marketplace, marketplaces_catalogo)
    2. Insertar planes por defecto (Starter, Pro, Enterprise)
    3. Insertar Babymine como tenant_id=1 si no existe
    4. ALTER TABLE para agregar tenant_id INT NOT NULL DEFAULT 1 a cada tabla existente
    5. Crear índices compuestos (tenant_id + columnas clave)
    """
    conn = get_conn()
    cur = conn.cursor()
    try:
        # ───────────────────────────────────────────────────────────
        # 1. TABLAS GLOBALES (sin tenant_id)
        # ───────────────────────────────────────────────────────────

        # Tabla planes (catálogo de planes que Lusync ofrece)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS planes (
                id            SERIAL PRIMARY KEY,
                codigo        TEXT UNIQUE NOT NULL,     -- 'starter', 'pro', 'enterprise', 'trial'
                nombre        TEXT NOT NULL,            -- 'Starter', 'Pro', 'Enterprise'
                precio_uf     NUMERIC(6,2) NOT NULL,    -- 1.00, 2.00, 4.00
                max_ordenes_mes      INTEGER,           -- NULL = ilimitado
                max_skus             INTEGER,
                max_usuarios         INTEGER,
                max_marketplaces     INTEGER,
                features_json        JSONB DEFAULT '{}'::jsonb,  -- {motor_precios: true, ...}
                activo        BOOLEAN DEFAULT TRUE,
                creado        TIMESTAMP DEFAULT NOW()
            )
        """)

        # Insertar planes por defecto si no existen
        planes_default = [
            ("trial",      "Trial Gratuito", 0.00, 50,   100,  1,  2,  '{"motor_precios": false, "soporte": "estandar"}'),
            ("starter",    "Starter",        1.00, 300,  500,  2,  2,  '{"motor_precios": false, "soporte": "estandar"}'),
            ("pro",        "Pro",            2.00, 1500, 2000, 5,  5,  '{"motor_precios": true,  "soporte": "prioritario", "eventos_masivos": true}'),
            ("enterprise", "Enterprise",     4.00, 5000, None, None, None, '{"motor_precios": true, "soporte": "dedicado", "eventos_masivos": true, "multi_bodega_avanzado": true, "account_manager": true}'),
        ]
        for codigo, nombre, precio, max_o, max_s, max_u, max_m, features in planes_default:
            cur.execute("""
                INSERT INTO planes (codigo, nombre, precio_uf, max_ordenes_mes, max_skus, max_usuarios, max_marketplaces, features_json)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (codigo) DO NOTHING
            """, (codigo, nombre, precio, max_o, max_s, max_u, max_m, features))

        # Tabla tenants (clientes de Lusync)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS tenants (
                id              SERIAL PRIMARY KEY,
                nombre          TEXT NOT NULL,
                razon_social    TEXT,
                rut             TEXT UNIQUE,
                email_contacto  TEXT,
                telefono        TEXT,
                plan_id         INTEGER REFERENCES planes(id),
                estado          TEXT DEFAULT 'activo',     -- 'activo', 'suspendido', 'trial', 'cancelado'
                fecha_alta      TIMESTAMP DEFAULT NOW(),
                fecha_baja      TIMESTAMP,
                pais            TEXT DEFAULT 'CL',
                zona_horaria    TEXT DEFAULT 'America/Santiago',
                config_json     JSONB DEFAULT '{}'::jsonb,  -- preferencias del tenant
                notas_internas  TEXT
            )
        """)

        # Tabla usuarios (cada tenant tiene N usuarios)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS usuarios (
                id               SERIAL PRIMARY KEY,
                tenant_id        INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                email            TEXT NOT NULL,
                password_hash    TEXT NOT NULL,
                nombre           TEXT,
                rol              TEXT DEFAULT 'admin',     -- 'admin', 'operador', 'viewer'
                activo           BOOLEAN DEFAULT TRUE,
                ultimo_login     TIMESTAMP,
                fecha_creacion   TIMESTAMP DEFAULT NOW(),
                debe_cambiar_password BOOLEAN DEFAULT FALSE,
                UNIQUE (tenant_id, email)
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_usuarios_email ON usuarios(email)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_usuarios_tenant ON usuarios(tenant_id)")

        # Tabla super-admins (TÚ — gestiona todos los tenants)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS lusync_admins (
                id              SERIAL PRIMARY KEY,
                email           TEXT UNIQUE NOT NULL,
                password_hash   TEXT NOT NULL,
                nombre          TEXT,
                activo          BOOLEAN DEFAULT TRUE,
                ultimo_login    TIMESTAMP,
                fecha_creacion  TIMESTAMP DEFAULT NOW()
            )
        """)

        # Tabla credenciales de marketplace (encriptadas)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS credenciales_marketplace (
                id              SERIAL PRIMARY KEY,
                tenant_id       INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                canal           TEXT NOT NULL,            -- 'mercadolibre', 'falabella', 'paris', etc.
                nombre_alias    TEXT,                     -- nombre amigable que el cliente le pone
                credenciales_encriptadas TEXT NOT NULL,   -- JSON encriptado con Fernet
                activo          BOOLEAN DEFAULT TRUE,
                ultima_validacion TIMESTAMP,
                estado_validacion TEXT,                   -- 'ok', 'token_expirado', 'credenciales_invalidas'
                fecha_creacion  TIMESTAMP DEFAULT NOW(),
                UNIQUE (tenant_id, canal, nombre_alias)
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_cred_tenant_canal ON credenciales_marketplace(tenant_id, canal)")

        # Tabla catálogo de marketplaces soportados
        cur.execute("""
            CREATE TABLE IF NOT EXISTS marketplaces_catalogo (
                id              SERIAL PRIMARY KEY,
                codigo          TEXT UNIQUE NOT NULL,    -- 'mercadolibre', 'falabella', 'paris', etc.
                nombre          TEXT NOT NULL,
                color_hex       TEXT,                    -- '#FFE600', etc.
                color_texto     TEXT,                    -- '#1A1A1A' o 'white'
                label_corto     TEXT,                    -- 'ML', 'PA', 'FA'
                pais            TEXT DEFAULT 'CL',
                comision_base_pct NUMERIC(5,2),          -- 13.5, 18.0
                tipo_auth       TEXT,                    -- 'oauth', 'api_key', 'token'
                activo          BOOLEAN DEFAULT TRUE,
                orden_display   INTEGER DEFAULT 0
            )
        """)

        # Insertar marketplaces soportados
        marketplaces = [
            ("mercadolibre", "MercadoLibre",  "#FFE600", "#1A1A1A", "ML", "CL", 13.5, "oauth", 1),
            ("paris",        "Paris",         "#4F5AFF", "white",   "PA", "CL", 15.0, "api_key", 2),
            ("falabella",    "Falabella",     "#ADD500", "#1A1A1A", "FA", "CL", 18.0, "api_key", 3),
            ("walmart",      "Walmart",       "#0071DC", "white",   "WM", "CL", 12.0, "api_key", 4),
            ("ripley",       "Ripley",        "#5F3D76", "white",   "RP", "CL", 17.0, "api_key", 5),
            ("hites",        "Hites",         "#FF8200", "white",   "HI", "CL", 16.0, "api_key", 6),
            ("web",          "Web (Woo)",     "#873EFF", "white",   "WE", "CL", 0.0,  "api_key", 7),
            ("shopify",      "Shopify",       "#95BF47", "white",   "SH", "CL", 0.0,  "api_key", 8),
            ("vtex",         "VTEX",          "#F71963", "white",   "VT", "CL", 0.0,  "api_key", 9),
            ("jumseller",    "Jumseller",     "#FF6B35", "white",   "JU", "CL", 0.0,  "api_key", 10),
            ("prestashop",   "Prestashop",    "#DF0067", "white",   "PS", "CL", 0.0,  "api_key", 11),
            ("magento",      "Magento",       "#EE672F", "white",   "MG", "CL", 0.0,  "api_key", 12),
        ]
        for codigo, nombre, color, color_txt, label, pais, com, auth, orden in marketplaces:
            cur.execute("""
                INSERT INTO marketplaces_catalogo (codigo, nombre, color_hex, color_texto, label_corto, pais, comision_base_pct, tipo_auth, orden_display)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (codigo) DO UPDATE SET
                    color_hex=EXCLUDED.color_hex,
                    color_texto=EXCLUDED.color_texto,
                    label_corto=EXCLUDED.label_corto,
                    comision_base_pct=EXCLUDED.comision_base_pct
            """, (codigo, nombre, color, color_txt, label, pais, com, auth, orden))

        # Tabla facturación interna (Lusync cobra a sus clientes)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS facturacion_lusync (
                id              SERIAL PRIMARY KEY,
                tenant_id       INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                periodo         TEXT,                    -- '2026-05'
                plan_codigo     TEXT,
                monto_uf        NUMERIC(8,4),
                monto_clp       INTEGER,
                valor_uf_dia    NUMERIC(10,2),           -- valor UF al día de facturación
                ordenes_facturadas INTEGER,
                ordenes_extras_uf  NUMERIC(8,4) DEFAULT 0,
                estado          TEXT DEFAULT 'pendiente', -- 'pendiente', 'pagado', 'vencido', 'anulado'
                fecha_emision   TIMESTAMP DEFAULT NOW(),
                fecha_vencimiento DATE,
                fecha_pago      TIMESTAMP,
                metodo_pago     TEXT,
                folio_factura   TEXT,
                notas           TEXT
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_fact_tenant_periodo ON facturacion_lusync(tenant_id, periodo)")

        conn.commit()
        print("[init_multitenancy] Tablas globales creadas")

        # ───────────────────────────────────────────────────────────
        # 2. CREAR BABYMINE COMO TENANT 1 (si no existe)
        # ───────────────────────────────────────────────────────────
        cur.execute("SELECT id FROM tenants WHERE id = 1")
        if not cur.fetchone():
            # Asegurar que id=1 esté disponible
            cur.execute("SELECT id FROM planes WHERE codigo = 'enterprise'")
            plan_id = cur.fetchone()[0]

            cur.execute("""
                INSERT INTO tenants (id, nombre, razon_social, rut, email_contacto, plan_id, estado, pais, zona_horaria, notas_internas)
                VALUES (1, 'Babymine', 'Grupo PH SPA', '76.XXX.XXX-X', 'luis@babymine.cl', %s, 'activo', 'CL', 'America/Santiago', 'Tenant fundador - Lusync labs')
                ON CONFLICT (id) DO NOTHING
            """, (plan_id,))
            # Resetear secuencia para que próximo INSERT use id=2
            cur.execute("SELECT setval(pg_get_serial_sequence('tenants', 'id'), GREATEST(1, (SELECT MAX(id) FROM tenants)))")
            conn.commit()
            print("[init_multitenancy] Babymine creado como tenant_id=1")

        # ───────────────────────────────────────────────────────────
        # 3. ALTER TABLE: agregar tenant_id a cada tabla existente
        # ───────────────────────────────────────────────────────────
        for tabla in TABLAS_CON_TENANT:
            try:
                # Verificar que la tabla existe antes de alterarla
                cur.execute("""
                    SELECT EXISTS (
                        SELECT FROM information_schema.tables
                        WHERE table_name = %s
                    )
                """, (tabla,))
                if not cur.fetchone()[0]:
                    print(f"[init_multitenancy] Tabla {tabla} no existe, saltando")
                    continue

                # ADD COLUMN tenant_id (idempotente con IF NOT EXISTS)
                cur.execute(f"ALTER TABLE {tabla} ADD COLUMN IF NOT EXISTS tenant_id INTEGER NOT NULL DEFAULT 1")

                # CREATE INDEX (idempotente)
                cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{tabla}_tenant ON {tabla}(tenant_id)")

                conn.commit()
            except Exception as e:
                print(f"[init_multitenancy] Error en {tabla}: {e}")
                conn.rollback()

        print(f"[init_multitenancy] tenant_id agregado a {len(TABLAS_CON_TENANT)} tablas")

    except Exception as e:
        print(f"[init_multitenancy] ERROR: {e}")
        import traceback
        traceback.print_exc()
        conn.rollback()
    finally:
        cur.close()
        release_conn(conn)


# ════════════════════════════════════════════════════════════════════════════
# HELPERS de SESIÓN — leer tenant_id actual desde Flask session
# ════════════════════════════════════════════════════════════════════════════

DEFAULT_TENANT_ID = 1   # Babymine (legacy: si no hay sesión multi-tenant)

def get_tenant_actual():
    """Retorna el tenant_id del usuario logueado en la sesión actual.
    Si no hay sesión (modo legacy / scheduler), devuelve DEFAULT_TENANT_ID (Babymine).
    """
    try:
        tid = session.get("tenant_id")
        if tid:
            return int(tid)
    except RuntimeError:
        # Fuera del request context (ej: scheduler en background)
        pass
    return DEFAULT_TENANT_ID


def get_tenant_or_die():
    """Igual que get_tenant_actual pero falla si no hay tenant en sesión.
    Útil para endpoints que requieren tenant válido.
    """
    try:
        tid = session.get("tenant_id")
        if tid:
            return int(tid)
    except RuntimeError:
        pass
    raise ValueError("No hay tenant_id en sesión actual")


def es_lusync_admin():
    """True si el usuario actual es super-admin de Lusync."""
    try:
        return bool(session.get("is_lusync_admin"))
    except RuntimeError:
        return False


# ════════════════════════════════════════════════════════════════════════════
# PASSWORDS — hash con bcrypt o fallback a sha256+salt
# ════════════════════════════════════════════════════════════════════════════

def hash_password(password):
    """Hash una contraseña. Usa bcrypt si está disponible, sino sha256+salt."""
    try:
        import bcrypt
        return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    except ImportError:
        # Fallback: sha256 con salt (no ideal pero funcional)
        salt = secrets.token_hex(16)
        h = hashlib.sha256((salt + password).encode("utf-8")).hexdigest()
        return f"sha256${salt}${h}"


def verify_password(password, stored_hash):
    """Verifica contraseña contra el hash guardado."""
    if not stored_hash:
        return False
    if stored_hash.startswith("sha256$"):
        try:
            _, salt, expected = stored_hash.split("$", 2)
            actual = hashlib.sha256((salt + password).encode("utf-8")).hexdigest()
            return secrets.compare_digest(actual, expected)
        except Exception:
            return False
    # Asumir bcrypt
    try:
        import bcrypt
        return bcrypt.checkpw(password.encode("utf-8"), stored_hash.encode("utf-8"))
    except Exception:
        return False


# ════════════════════════════════════════════════════════════════════════════
# CRUD básico de TENANTS
# ════════════════════════════════════════════════════════════════════════════

def listar_tenants():
    """Devuelve todos los tenants con info básica."""
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("""
            SELECT t.id, t.nombre, t.razon_social, t.rut, t.email_contacto,
                   t.estado, t.fecha_alta,
                   p.codigo AS plan_codigo, p.nombre AS plan_nombre, p.precio_uf
            FROM tenants t
            LEFT JOIN planes p ON p.id = t.plan_id
            ORDER BY t.id ASC
        """)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        cur.close(); release_conn(conn)


def crear_tenant(nombre, razon_social=None, rut=None, email_contacto=None,
                 plan_codigo="pro", telefono=None, notas=None):
    """Crea un tenant nuevo y devuelve su ID."""
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("SELECT id FROM planes WHERE codigo = %s", (plan_codigo,))
        r = cur.fetchone()
        plan_id = r[0] if r else None

        cur.execute("""
            INSERT INTO tenants (nombre, razon_social, rut, email_contacto, telefono, plan_id, estado, notas_internas)
            VALUES (%s, %s, %s, %s, %s, %s, 'activo', %s)
            RETURNING id
        """, (nombre, razon_social, rut, email_contacto, telefono, plan_id, notas))
        tid = cur.fetchone()[0]
        conn.commit()
        return tid
    finally:
        cur.close(); release_conn(conn)


def crear_usuario(tenant_id, email, password, nombre=None, rol="admin"):
    """Crea un usuario para un tenant. Devuelve user_id."""
    conn = get_conn(); cur = conn.cursor()
    try:
        ph = hash_password(password)
        cur.execute("""
            INSERT INTO usuarios (tenant_id, email, password_hash, nombre, rol, debe_cambiar_password)
            VALUES (%s, %s, %s, %s, %s, TRUE)
            RETURNING id
        """, (tenant_id, email.lower().strip(), ph, nombre, rol))
        uid = cur.fetchone()[0]
        conn.commit()
        return uid
    finally:
        cur.close(); release_conn(conn)


def autenticar_usuario(email, password):
    """Intenta autenticar. Devuelve dict con tenant_id, user_id, rol si OK, None si falla."""
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("""
            SELECT u.id, u.tenant_id, u.email, u.password_hash, u.nombre, u.rol, u.activo,
                   t.estado AS tenant_estado, t.nombre AS tenant_nombre
            FROM usuarios u
            JOIN tenants t ON t.id = u.tenant_id
            WHERE u.email = %s
            LIMIT 1
        """, (email.lower().strip(),))
        r = cur.fetchone()
        if not r:
            return None
        uid, tid, em, ph, nombre, rol, activo, tenant_estado, tenant_nombre = r
        if not activo:
            return {"error": "Usuario desactivado"}
        if tenant_estado in ("suspendido", "cancelado"):
            return {"error": f"Cuenta {tenant_estado}"}
        if not verify_password(password, ph):
            return None
        # Update ultimo_login
        cur.execute("UPDATE usuarios SET ultimo_login = NOW() WHERE id = %s", (uid,))
        conn.commit()
        return {
            "user_id": uid,
            "tenant_id": tid,
            "email": em,
            "nombre": nombre,
            "rol": rol,
            "tenant_nombre": tenant_nombre,
        }
    finally:
        cur.close(); release_conn(conn)


def autenticar_lusync_admin(email, password):
    """Auth de super-admin Lusync (TÚ)."""
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("""
            SELECT id, email, password_hash, nombre, activo
            FROM lusync_admins
            WHERE email = %s
            LIMIT 1
        """, (email.lower().strip(),))
        r = cur.fetchone()
        if not r:
            return None
        aid, em, ph, nombre, activo = r
        if not activo or not verify_password(password, ph):
            return None
        cur.execute("UPDATE lusync_admins SET ultimo_login = NOW() WHERE id = %s", (aid,))
        conn.commit()
        return {"admin_id": aid, "email": em, "nombre": nombre}
    finally:
        cur.close(); release_conn(conn)


def crear_lusync_admin(email, password, nombre=None):
    """Bootstrap: crear el primer super-admin Lusync."""
    conn = get_conn(); cur = conn.cursor()
    try:
        ph = hash_password(password)
        cur.execute("""
            INSERT INTO lusync_admins (email, password_hash, nombre)
            VALUES (%s, %s, %s)
            ON CONFLICT (email) DO UPDATE SET password_hash = EXCLUDED.password_hash
            RETURNING id
        """, (email.lower().strip(), ph, nombre))
        aid = cur.fetchone()[0]
        conn.commit()
        return aid
    finally:
        cur.close(); release_conn(conn)


# ════════════════════════════════════════════════════════════════════════════
# CREDENCIALES de MARKETPLACE — encriptación con Fernet
# ════════════════════════════════════════════════════════════════════════════

def _get_fernet():
    """Devuelve instancia Fernet usando LUSYNC_FERNET_KEY del env.
    Si no existe la key, la genera y la muestra en log (admin debe agregarla a env permanentemente).
    """
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        raise RuntimeError("Falta paquete 'cryptography'. Instalar: pip install cryptography")

    key = os.environ.get("LUSYNC_FERNET_KEY")
    if not key:
        new_key = Fernet.generate_key().decode()
        print(f"[tenancy] ⚠️ LUSYNC_FERNET_KEY no configurada. Generada temporal: {new_key}")
        print(f"[tenancy] ⚠️ Agrégala a tu env permanentemente o las credenciales se perderán")
        key = new_key
    elif isinstance(key, str):
        pass
    return Fernet(key.encode() if isinstance(key, str) else key)


def guardar_credenciales_canal(tenant_id, canal, credenciales_dict, alias=None):
    """Guarda credenciales encriptadas para un canal de un tenant.

    Args:
        tenant_id: ID del tenant
        canal: 'mercadolibre', 'falabella', etc.
        credenciales_dict: dict con las creds. Ej: {'api_key': '...', 'api_secret': '...'}
        alias: nombre amigable (opcional, ej: 'Cuenta principal')
    """
    import json
    fernet = _get_fernet()
    encrypted = fernet.encrypt(json.dumps(credenciales_dict).encode()).decode()
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO credenciales_marketplace (tenant_id, canal, nombre_alias, credenciales_encriptadas, activo)
            VALUES (%s, %s, %s, %s, TRUE)
            ON CONFLICT (tenant_id, canal, nombre_alias) DO UPDATE SET
                credenciales_encriptadas = EXCLUDED.credenciales_encriptadas,
                activo = TRUE
            RETURNING id
        """, (tenant_id, canal, alias or "default", encrypted))
        cid = cur.fetchone()[0]
        conn.commit()
        return cid
    finally:
        cur.close(); release_conn(conn)


def obtener_credenciales_canal(tenant_id, canal, alias=None):
    """Lee y desencripta credenciales. Devuelve dict o None."""
    import json
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("""
            SELECT credenciales_encriptadas FROM credenciales_marketplace
            WHERE tenant_id = %s AND canal = %s AND activo = TRUE
              AND (%s::text IS NULL OR nombre_alias = %s)
            ORDER BY id DESC LIMIT 1
        """, (tenant_id, canal, alias, alias))
        r = cur.fetchone()
        if not r:
            return None
        fernet = _get_fernet()
        return json.loads(fernet.decrypt(r[0].encode()).decode())
    except Exception as e:
        print(f"[tenancy] Error obteniendo creds {canal} tenant {tenant_id}: {e}")
        return None
    finally:
        cur.close(); release_conn(conn)


# ════════════════════════════════════════════════════════════════════════════
# DIAGNÓSTICO — verificar estado del multi-tenancy
# ════════════════════════════════════════════════════════════════════════════

def diagnostico_tenancy():
    """Reporta el estado actual del sistema multi-tenant.
    Útil para verificar la migración inicial.
    """
    conn = get_conn(); cur = conn.cursor()
    try:
        resultado = {
            "tablas_globales": {},
            "tablas_con_tenant_id": {},
            "tenants": 0,
            "planes": 0,
        }

        # Verificar existencia de tablas globales
        tablas_globales = ["tenants", "planes", "usuarios", "lusync_admins",
                           "credenciales_marketplace", "marketplaces_catalogo",
                           "facturacion_lusync"]
        for t in tablas_globales:
            cur.execute(f"SELECT EXISTS(SELECT FROM information_schema.tables WHERE table_name = %s)", (t,))
            existe = cur.fetchone()[0]
            if existe:
                cur.execute(f"SELECT COUNT(*) FROM {t}")
                resultado["tablas_globales"][t] = cur.fetchone()[0]
            else:
                resultado["tablas_globales"][t] = "NO EXISTE"

        # Verificar tenant_id en tablas
        for tabla in TABLAS_CON_TENANT:
            cur.execute("""
                SELECT EXISTS(
                    SELECT FROM information_schema.columns
                    WHERE table_name = %s AND column_name = 'tenant_id'
                )
            """, (tabla,))
            tiene_col = cur.fetchone()[0]
            if tiene_col:
                cur.execute(f"SELECT COUNT(*), COUNT(DISTINCT tenant_id) FROM {tabla}")
                total, distintos = cur.fetchone()
                resultado["tablas_con_tenant_id"][tabla] = {"filas": total, "tenants_distintos": distintos}
            else:
                resultado["tablas_con_tenant_id"][tabla] = "SIN tenant_id"

        # Resumen
        cur.execute("SELECT COUNT(*) FROM tenants")
        resultado["tenants"] = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM planes")
        resultado["planes"] = cur.fetchone()[0]

        return resultado
    finally:
        cur.close(); release_conn(conn)
