import os
import psycopg2
from datetime import datetime, timezone, timedelta
import pytz

# Zona horaria Chile — se ajusta automáticamente entre GMT-3 y GMT-4
TZ_CHILE = pytz.timezone('America/Santiago')

def now_chile():
    return datetime.now(TZ_CHILE)

def get_conn():
    return psycopg2.connect(os.environ.get("DATABASE_URL"))

def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS productos (
            sku TEXT PRIMARY KEY,
            nombre TEXT,
            stock INTEGER,
            precio_normal NUMERIC(12,2) DEFAULT 0,
            precio_oferta NUMERIC(12,2) DEFAULT 0
        )
    """)
    # Agregar columnas si ya existe la tabla sin ellas
    cur.execute("""
        ALTER TABLE productos
        ADD COLUMN IF NOT EXISTS precio_normal NUMERIC(12,2) DEFAULT 0
    """)
    cur.execute("""
        ALTER TABLE productos
        ADD COLUMN IF NOT EXISTS precio_oferta NUMERIC(12,2) DEFAULT 0
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS movimientos (
            id SERIAL PRIMARY KEY,
            tipo TEXT,
            sku TEXT,
            nombre TEXT,
            cantidad INTEGER,
            motivo TEXT,
            fecha TIMESTAMP DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ordenes_procesadas (
            orden_id BIGINT PRIMARY KEY,
            fecha TIMESTAMP DEFAULT NOW()
        )
    """)

    cur.execute("CREATE TABLE IF NOT EXISTS configuracion (clave TEXT PRIMARY KEY, valor TEXT)")
    cur.execute("ALTER TABLE productos ADD COLUMN IF NOT EXISTS lead_time INTEGER DEFAULT 45")
    cur.execute("ALTER TABLE productos ADD COLUMN IF NOT EXISTS ventas_dia NUMERIC(10,4) DEFAULT 0")
    conn.commit()
    cur.close()
    conn.close()

def get_configuracion():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT clave, valor FROM configuracion")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return {r[0]: r[1] for r in rows}

def set_configuracion(data):
    conn = get_conn()
    cur = conn.cursor()
    for clave, valor in data.items():
        cur.execute("INSERT INTO configuracion (clave, valor) VALUES (%s, %s) ON CONFLICT (clave) DO UPDATE SET valor = EXCLUDED.valor", (clave, str(valor)))
    conn.commit()
    cur.close()
    conn.close()

def set_lead_time(sku, dias):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE productos SET lead_time = %s WHERE sku = %s", (dias, sku))
    conn.commit()
    cur.close()
    conn.close()

def cargar_productos():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT sku, nombre, stock, precio_normal, precio_oferta FROM productos")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [{"sku": r[0], "nombre": r[1], "stock": r[2],
             "precio_normal": float(r[3] or 0), "precio_oferta": float(r[4] or 0)} for r in rows]

def guardar_producto(p):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO productos (sku, nombre, stock, precio_normal, precio_oferta)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (sku) DO UPDATE SET
            stock = EXCLUDED.stock,
            precio_normal = EXCLUDED.precio_normal,
            precio_oferta = EXCLUDED.precio_oferta
    """, (p["sku"], p["nombre"], p["stock"],
          p.get("precio_normal", 0), p.get("precio_oferta", 0)))
    conn.commit()
    cur.close()
    conn.close()

def guardar_productos(lista):
    for p in lista:
        guardar_producto(p)

def actualizar_precios(sku, precio_normal, precio_oferta):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        UPDATE productos SET precio_normal = %s, precio_oferta = %s WHERE sku = %s
    """, (precio_normal, precio_oferta, sku))
    conn.commit()
    cur.close()
    conn.close()

def registrar_movimiento(tipo, sku, nombre, cantidad, motivo="", usuario="Sistema", canal="Sistema"):
    conn = get_conn()
    cur = conn.cursor()
    # Agregar columnas usuario y canal si no existen
    try:
        cur.execute("ALTER TABLE movimientos ADD COLUMN IF NOT EXISTS usuario TEXT DEFAULT 'Sistema'")
        cur.execute("ALTER TABLE movimientos ADD COLUMN IF NOT EXISTS canal TEXT DEFAULT 'Sistema'")
        conn.commit()
    except:
        conn.rollback()
    fecha_chile = now_chile()
    cur.execute("""
        INSERT INTO movimientos (tipo, sku, nombre, cantidad, motivo, usuario, canal, fecha)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    """, (tipo, sku, nombre, cantidad, motivo, usuario, canal, fecha_chile))
    conn.commit()
    cur.close()
    conn.close()

def cargar_movimientos(limite=20):
    conn = get_conn()
    cur = conn.cursor()
    # Asegurar que las columnas existen antes de leerlas
    try:
        cur.execute("ALTER TABLE movimientos ADD COLUMN IF NOT EXISTS usuario TEXT DEFAULT 'Sistema'")
        cur.execute("ALTER TABLE movimientos ADD COLUMN IF NOT EXISTS canal TEXT DEFAULT 'Sistema'")
        conn.commit()
    except:
        conn.rollback()
    cur.execute("""
        SELECT tipo, sku, nombre, cantidad, motivo,
               TO_CHAR(fecha AT TIME ZONE 'America/Santiago', 'DD/MM/YYYY') as fecha_fmt,
               TO_CHAR(fecha AT TIME ZONE 'America/Santiago', 'HH24:MI') as hora,
               COALESCE(usuario, 'Sistema') as usuario,
               COALESCE(canal, 'Sistema') as canal
        FROM movimientos
        ORDER BY fecha DESC
        LIMIT %s
    """, (limite,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [
        {
            "tipo": r[0],
            "sku": r[1],
            "nombre": r[2],
            "cantidad": r[3],
            "motivo": r[4],
            "fecha": r[5],
            "hora": r[6],
            "usuario": r[7],
            "canal": r[8]
        }
        for r in rows
    ]


def cargar_movimientos_hoy():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT tipo, sku, nombre, cantidad, motivo, "
        "TO_CHAR(fecha AT TIME ZONE 'America/Santiago', 'HH24:MI') as hora, "
        "COALESCE(canal, 'Sistema') as canal "
        "FROM movimientos "
        "WHERE DATE(fecha AT TIME ZONE 'America/Santiago') = CURRENT_DATE AND tipo = 'salida' "
        "ORDER BY fecha DESC"
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [{"tipo": r[0], "sku": r[1], "nombre": r[2],
             "cantidad": r[3], "motivo": r[4], "hora": r[5], "canal": r[6]} for r in rows]

def eliminar_producto(sku):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM productos WHERE sku = %s", (sku,))
    conn.commit()
    cur.close()
    conn.close()

def orden_ya_procesada(orden_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM ordenes_procesadas WHERE orden_id = %s", (orden_id,))
    existe = cur.fetchone() is not None
    cur.close()
    conn.close()
    return existe

def marcar_orden_procesada(orden_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO ordenes_procesadas (orden_id)
        VALUES (%s) ON CONFLICT (orden_id) DO NOTHING
    """, (orden_id,))
    conn.commit()
    cur.close()
    conn.close()
