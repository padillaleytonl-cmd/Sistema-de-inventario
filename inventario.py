import os
import psycopg2

def get_conn():
    return psycopg2.connect(os.environ.get("DATABASE_URL"))

def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS productos (
            sku TEXT PRIMARY KEY,
            nombre TEXT,
            stock INTEGER
        )
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
    conn.commit()
    cur.close()
    conn.close()

def cargar_productos():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT sku, nombre, stock FROM productos")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [{"sku": r[0], "nombre": r[1], "stock": r[2]} for r in rows]

def guardar_producto(p):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO productos (sku, nombre, stock)
        VALUES (%s, %s, %s)
        ON CONFLICT (sku) DO UPDATE SET stock = EXCLUDED.stock
    """, (p["sku"], p["nombre"], p["stock"]))
    conn.commit()
    cur.close()
    conn.close()

def guardar_productos(lista):
    for p in lista:
        guardar_producto(p)

def registrar_movimiento(tipo, sku, nombre, cantidad, motivo=""):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO movimientos (tipo, sku, nombre, cantidad, motivo)
        VALUES (%s, %s, %s, %s, %s)
    """, (tipo, sku, nombre, cantidad, motivo))
    conn.commit()
    cur.close()
    conn.close()

def cargar_movimientos(limite=20):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT tipo, nombre, cantidad, motivo, fecha
        FROM movimientos
        ORDER BY fecha DESC
        LIMIT %s
    """, (limite,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [
        f"{'➕' if r[0]=='entrada' else '➖'} {r[3]} | {r[1]} ({'+' if r[0]=='entrada' else '-'}{r[2]})"
        for r in rows
    ]