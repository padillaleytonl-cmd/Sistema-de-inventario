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

    cur.execute("ALTER TABLE movimientos ADD COLUMN IF NOT EXISTS fecha_importacion TIMESTAMP")
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

def registrar_movimiento(tipo, sku, nombre, cantidad, motivo="", usuario="Sistema", canal="Sistema", orden_id=None, fecha_override=None):
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("ALTER TABLE movimientos ADD COLUMN IF NOT EXISTS usuario TEXT DEFAULT 'Sistema'")
        cur.execute("ALTER TABLE movimientos ADD COLUMN IF NOT EXISTS canal TEXT DEFAULT 'Sistema'")
        cur.execute("ALTER TABLE movimientos ADD COLUMN IF NOT EXISTS orden_id TEXT DEFAULT NULL")
        cur.execute("ALTER TABLE movimientos ADD COLUMN IF NOT EXISTS fecha_importacion TIMESTAMP")
        conn.commit()
    except:
        conn.rollback()
    ahora = now_chile().replace(tzinfo=None)
    if fecha_override:
        if hasattr(fecha_override, 'tzinfo') and fecha_override.tzinfo:
            fecha = fecha_override.astimezone(TZ_CHILE).replace(tzinfo=None)
        else:
            fecha = fecha_override
    else:
        fecha = ahora
    fecha_importacion = ahora
    cur.execute("""
        INSERT INTO movimientos (tipo, sku, nombre, cantidad, motivo, usuario, canal, fecha, orden_id, fecha_importacion)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (tipo, sku, nombre, cantidad, motivo, usuario, canal, fecha, orden_id, fecha_importacion))
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
               TO_CHAR(fecha, 'DD/MM/YYYY') as fecha_fmt,
               TO_CHAR(fecha, 'HH24:MI') as hora,
               COALESCE(usuario, 'Sistema') as usuario,
               COALESCE(canal, 'Sistema') as canal,
               COALESCE(orden_id, '') as orden_id,
               TO_CHAR(fecha_importacion, 'DD/MM HH24:MI') as importado
        FROM movimientos
        ORDER BY fecha DESC
        LIMIT %s
    """, (limite,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [
        {
            "tipo": r[0], "sku": r[1], "nombre": r[2],
            "cantidad": r[3], "motivo": r[4], "fecha": r[5],
            "hora": r[6], "usuario": r[7], "canal": r[8],
            "orden_id": r[9], "importado": r[10] or ""
        }
        for r in rows
    ]


def cargar_movimientos_hoy():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT tipo, sku, nombre, cantidad, motivo,
               TO_CHAR(fecha, 'HH24:MI') as hora,
               COALESCE(canal, 'Sistema') as canal
        FROM movimientos
        WHERE DATE(fecha) = CURRENT_DATE
        AND tipo = 'salida'
        ORDER BY fecha DESC
    """)
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


# ── AUDIT LOG ──

def init_audit():
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id SERIAL PRIMARY KEY,
                fecha TIMESTAMP DEFAULT NOW(),
                usuario TEXT,
                ip TEXT,
                accion TEXT,
                entidad TEXT,
                entidad_id TEXT,
                detalle TEXT,
                resultado TEXT DEFAULT 'ok',
                dato_antes TEXT,
                dato_despues TEXT
            )
        """)
        # Asegurar columnas si tabla ya existía sin ellas
        for col in ['usuario','ip','accion','entidad','entidad_id','detalle','resultado','dato_antes','dato_despues']:
            try:
                cur.execute(f"ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS {col} TEXT")
            except:
                pass
        conn.commit()
        cur.close()
        conn.close()
        print("[Audit] Tabla audit_log lista")
    except Exception as e:
        print(f"[Audit] Error init_audit: {e}")

def registrar_audit(usuario, ip, accion, entidad='', entidad_id='', detalle='', resultado='ok', dato_antes='', dato_despues=''):
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO audit_log (usuario, ip, accion, entidad, entidad_id, detalle, resultado, dato_antes, dato_despues)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            str(usuario or 'Sistema'),
            str(ip or '—'),
            str(accion),
            str(entidad or ''),
            str(entidad_id or ''),
            str(detalle or '')[:500],
            str(resultado or 'ok'),
            str(dato_antes or '')[:500],
            str(dato_despues or '')[:500]
        ))
        conn.commit()
        cur.close()
        conn.close()
        print(f"[Audit] {accion} · {usuario} · {resultado}")
    except Exception as e:
        print(f"[Audit] ERROR registrando: {e}")
        # Reintentar creando la tabla si no existe
        try:
            init_audit()
            registrar_audit(usuario, ip, accion, entidad, entidad_id, detalle, resultado, dato_antes, dato_despues)
        except Exception as e2:
            print(f"[Audit] ERROR reintento: {e2}")

def listar_audit(limite=200, filtro_accion=None, filtro_usuario=None, filtro_resultado=None):
    conn = get_conn()
    cur = conn.cursor()
    where = []
    vals = []
    if filtro_accion:
        where.append("accion = %s"); vals.append(filtro_accion)
    if filtro_usuario:
        where.append("usuario ILIKE %s"); vals.append(f'%{filtro_usuario}%')
    if filtro_resultado:
        where.append("resultado = %s"); vals.append(filtro_resultado)
    w = ('WHERE ' + ' AND '.join(where)) if where else ''
    vals.append(limite)
    cur.execute(f"""
        SELECT id,
               TO_CHAR(fecha AT TIME ZONE 'America/Santiago', 'DD/MM/YYYY HH24:MI:SS') as fecha,
               usuario, ip, accion, entidad, entidad_id, detalle, resultado, dato_antes, dato_despues
        FROM audit_log {w}
        ORDER BY fecha DESC LIMIT %s
    """, vals)
    rows = cur.fetchall()
    cur.close(); conn.close()
    cols = ['id','fecha','usuario','ip','accion','entidad','entidad_id','detalle','resultado','dato_antes','dato_despues']
    return [dict(zip(cols, r)) for r in rows]

def limpiar_audit_antiguo(dias=90):
    """Nunca borra — solo archiva moviendo a audit_log_archivo"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS audit_log_archivo (LIKE audit_log INCLUDING ALL)
    """)
    cur.execute(f"""
        WITH moved AS (
            DELETE FROM audit_log WHERE fecha < NOW() - INTERVAL '{dias} days' RETURNING *
        )
        INSERT INTO audit_log_archivo SELECT * FROM moved
    """)
    conn.commit()
    cur.close(); conn.close()

# ── DEVOLUCIONES ──

def init_devoluciones():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS devoluciones (
            id SERIAL PRIMARY KEY,
            codigo TEXT UNIQUE,
            oc_origen TEXT NOT NULL,
            canal TEXT,
            sku TEXT,
            nombre TEXT,
            cantidad INTEGER DEFAULT 1,
            motivo_cliente TEXT,
            estado_producto TEXT,
            resolucion TEXT,
            observaciones TEXT,
            responsable TEXT DEFAULT 'Sistema',
            estado TEXT DEFAULT 'pendiente',
            fecha_solicitud TIMESTAMP DEFAULT NOW(),
            fecha_recepcion TIMESTAMP,
            fecha_resolucion TIMESTAMP,
            impacto_stock_reingresado BOOLEAN DEFAULT FALSE
        )
    """)
    conn.commit()
    cur.close()
    conn.close()

def generar_codigo_dev():
    from datetime import datetime
    conn = get_conn()
    cur = conn.cursor()
    hoy = datetime.now().strftime('%Y%m%d')
    cur.execute("SELECT COUNT(*) FROM devoluciones WHERE codigo LIKE %s", (f'DEV-{hoy}-%',))
    count = cur.fetchone()[0] + 1
    cur.close()
    conn.close()
    return f"DEV-{hoy}-{str(count).zfill(4)}"

def crear_devolucion(data):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO devoluciones (oc_origen, canal, sku, nombre, cantidad, motivo_cliente, responsable, estado, fecha_solicitud)
        VALUES (%s, %s, %s, %s, %s, %s, %s, 'pendiente', NOW())
        RETURNING id
    """, (data.get('oc_origen'), data.get('canal'), data.get('sku'), data.get('nombre'),
          data.get('cantidad', 1), data.get('motivo_cliente'), data.get('responsable', 'Sistema')))
    dev_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return dev_id

def asignar_codigo_dev(dev_id, codigo):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE devoluciones SET codigo = %s WHERE id = %s", (codigo, dev_id))
    conn.commit()
    cur.close()
    conn.close()

def actualizar_devolucion(dev_id, data):
    conn = get_conn()
    cur = conn.cursor()
    fields = []
    vals = []
    for k in ['motivo_cliente','estado_producto','resolucion','observaciones','responsable','estado']:
        if k in data:
            fields.append(f"{k} = %s")
            vals.append(data[k])
    if data.get('estado') == 'reingresada':
        fields.append("fecha_resolucion = NOW()")
        fields.append("impacto_stock_reingresado = TRUE")
    elif data.get('estado') in ['reenviado','dado_de_baja','reembolsado']:
        fields.append("fecha_resolucion = NOW()")
    if data.get('recibido'):
        fields.append("fecha_recepcion = NOW()")
    if not fields:
        return
    vals.append(dev_id)
    cur.execute(f"UPDATE devoluciones SET {', '.join(fields)} WHERE id = %s", vals)
    conn.commit()
    cur.close()
    conn.close()

def listar_devoluciones(estado=None):
    conn = get_conn()
    cur = conn.cursor()
    if estado and estado != 'todas':
        cur.execute("""
            SELECT id, codigo, oc_origen, canal, sku, nombre, cantidad, motivo_cliente,
                   estado_producto, resolucion, observaciones, responsable, estado,
                   TO_CHAR(fecha_solicitud, 'DD/MM/YYYY') as fecha_sol,
                   TO_CHAR(fecha_recepcion, 'DD/MM/YYYY HH24:MI') as fecha_rec,
                   TO_CHAR(fecha_resolucion, 'DD/MM/YYYY HH24:MI') as fecha_res,
                   impacto_stock_reingresado
            FROM devoluciones WHERE estado = %s ORDER BY fecha_solicitud DESC
        """, (estado,))
    else:
        cur.execute("""
            SELECT id, codigo, oc_origen, canal, sku, nombre, cantidad, motivo_cliente,
                   estado_producto, resolucion, observaciones, responsable, estado,
                   TO_CHAR(fecha_solicitud, 'DD/MM/YYYY') as fecha_sol,
                   TO_CHAR(fecha_recepcion, 'DD/MM/YYYY HH24:MI') as fecha_rec,
                   TO_CHAR(fecha_resolucion, 'DD/MM/YYYY HH24:MI') as fecha_res,
                   impacto_stock_reingresado
            FROM devoluciones ORDER BY fecha_solicitud DESC
        """)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    cols = ['id','codigo','oc_origen','canal','sku','nombre','cantidad','motivo_cliente',
            'estado_producto','resolucion','observaciones','responsable','estado',
            'fecha_solicitud','fecha_recepcion','fecha_resolucion','impacto_stock_reingresado']
    return [dict(zip(cols, r)) for r in rows]

def get_devolucion(dev_id=None, codigo=None):
    conn = get_conn()
    cur = conn.cursor()
    if codigo:
        cur.execute("SELECT * FROM devoluciones WHERE codigo = %s", (codigo,))
    else:
        cur.execute("SELECT * FROM devoluciones WHERE id = %s", (dev_id,))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close(); return None
    cols = [d[0] for d in cur.description]
    cur.close()
    conn.close()
    d = dict(zip(cols, row))
    for k in ['fecha_solicitud','fecha_recepcion','fecha_resolucion']:
        if d.get(k):
            d[k] = d[k].strftime('%d/%m/%Y %H:%M') if hasattr(d[k], 'strftime') else str(d[k])
    return d

def orden_ya_procesada_texto(order_id_texto):
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("ALTER TABLE ordenes_procesadas ADD COLUMN IF NOT EXISTS order_id_texto TEXT")
        conn.commit()
    except:
        conn.rollback()
    cur.execute("SELECT 1 FROM ordenes_procesadas WHERE order_id_texto = %s", (str(order_id_texto),))
    existe = cur.fetchone() is not None
    cur.close()
    conn.close()
    return existe

def marcar_orden_procesada_texto(order_id_texto):
    """Marca orden como procesada usando verificación previa para evitar duplicados."""
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("ALTER TABLE ordenes_procesadas ADD COLUMN IF NOT EXISTS order_id_texto TEXT")
        conn.commit()
    except Exception:
        conn.rollback()
    try:
        cur.execute("SELECT 1 FROM ordenes_procesadas WHERE order_id_texto = %s LIMIT 1", (str(order_id_texto),))
        if cur.fetchone():
            cur.close(); conn.close(); return
        import random
        cur.execute(
            "INSERT INTO ordenes_procesadas (orden_id, order_id_texto) VALUES (%s, %s)",
            (random.randint(1, 9007199254740991), str(order_id_texto))
        )
        conn.commit()
    except Exception as e:
        print(f"[Marcado orden] Error: {e}")
        conn.rollback()
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


def limpiar_movimientos_duplicados():
    """Elimina duplicados: misma orden_id + sku + canal + tipo, deja el más antiguo."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        DELETE FROM movimientos WHERE id IN (
            SELECT id FROM (
                SELECT id, ROW_NUMBER() OVER (
                    PARTITION BY orden_id, sku, canal, tipo ORDER BY fecha ASC, id ASC
                ) AS rn FROM movimientos
                WHERE orden_id IS NOT NULL AND orden_id != ''
            ) t WHERE rn > 1)
    """)
    eliminados = cur.rowcount
    conn.commit()
    cur.close(); conn.close()
    return eliminados

def borrar_movimientos_marketplace(desde_fecha=None):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM movimientos WHERE canal IN ('Walmart', 'WooCommerce', 'Paris')")
    mov_borrados = cur.rowcount
    cur.execute("DELETE FROM ordenes_procesadas")
    op_borradas = cur.rowcount
    conn.commit()
    cur.close(); conn.close()
    return mov_borrados, op_borradas

# ══════════════════════════════════════════════════
# MAPEO DE SKUs POR CANAL
# ══════════════════════════════════════════════════

CANAL_DISPLAY = {
    "web": "Web Propia", "walmart": "Walmart", "paris": "París",
    "falabella": "Falabella", "ripley": "Ripley",
    "mercadolibre": "Mercado Libre", "hites": "Hites",
}

def init_sku_mapeo():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sku_mapeo (
            id SERIAL PRIMARY KEY,
            sku_lusync TEXT UNIQUE NOT NULL,
            sku_web TEXT, sku_walmart TEXT, sku_paris TEXT,
            sku_falabella TEXT, sku_ripley TEXT, sku_mercadolibre TEXT, sku_hites TEXT,
            actualizado TIMESTAMP DEFAULT NOW()
        )
    """)
    for col in ["sku_web","sku_walmart","sku_paris","sku_falabella","sku_ripley","sku_mercadolibre","sku_hites"]:
        try:
            cur.execute(f"ALTER TABLE sku_mapeo ADD COLUMN IF NOT EXISTS {col} TEXT")
        except: pass
    cur.execute("INSERT INTO configuracion (clave, valor) VALUES ('plataforma_web', 'WooCommerce') ON CONFLICT (clave) DO NOTHING")
    conn.commit()
    cur.close(); conn.close()

def listar_sku_mapeo():
    init_sku_mapeo()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT p.sku, p.nombre,
               COALESCE(m.sku_web,''), COALESCE(m.sku_walmart,''), COALESCE(m.sku_paris,''),
               COALESCE(m.sku_falabella,''), COALESCE(m.sku_ripley,''),
               COALESCE(m.sku_mercadolibre,''), COALESCE(m.sku_hites,'')
        FROM productos p
        LEFT JOIN sku_mapeo m ON m.sku_lusync = p.sku
        ORDER BY p.nombre
    """)
    rows = cur.fetchall()
    cur.close(); conn.close()
    return [{"sku_lusync":r[0],"nombre":r[1],"sku_web":r[2],"sku_walmart":r[3],
             "sku_paris":r[4],"sku_falabella":r[5],"sku_ripley":r[6],
             "sku_mercadolibre":r[7],"sku_hites":r[8]} for r in rows]

def guardar_sku_mapeo_fila(sku_lusync, skus):
    init_sku_mapeo()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO sku_mapeo (sku_lusync, sku_web, sku_walmart, sku_paris, sku_falabella,
                               sku_ripley, sku_mercadolibre, sku_hites, actualizado)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NOW())
        ON CONFLICT (sku_lusync) DO UPDATE SET
            sku_web=EXCLUDED.sku_web, sku_walmart=EXCLUDED.sku_walmart,
            sku_paris=EXCLUDED.sku_paris, sku_falabella=EXCLUDED.sku_falabella,
            sku_ripley=EXCLUDED.sku_ripley, sku_mercadolibre=EXCLUDED.sku_mercadolibre,
            sku_hites=EXCLUDED.sku_hites, actualizado=NOW()
    """, (sku_lusync,
          (skus.get("web") or "").strip() or None,
          (skus.get("walmart") or "").strip() or None,
          (skus.get("paris") or "").strip() or None,
          (skus.get("falabella") or "").strip() or None,
          (skus.get("ripley") or "").strip() or None,
          (skus.get("mercadolibre") or "").strip() or None,
          (skus.get("hites") or "").strip() or None))
    conn.commit()
    cur.close(); conn.close()

def get_sku_canal(sku_lusync, canal):
    init_sku_mapeo()
    canal_lower = canal.lower()
    plataformas_web = ["woocommerce","shopify","vtex","prestashop","jumpseller","web"]
    if canal_lower in plataformas_web: col = "sku_web"
    elif canal_lower in ["mercadolibre","mercado libre"]: col = "sku_mercadolibre"
    elif canal_lower in ["walmart","paris","falabella","ripley","hites"]: col = f"sku_{canal_lower}"
    else: return sku_lusync
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(f"SELECT {col} FROM sku_mapeo WHERE sku_lusync = %s", (sku_lusync,))
    row = cur.fetchone()
    cur.close(); conn.close()
    return row[0].strip() if row and row[0] and row[0].strip() else sku_lusync

def get_plataforma_web():
    return get_configuracion("plataforma_web") or "WooCommerce"

def set_plataforma_web(plataforma):
    set_configuracion("plataforma_web", plataforma)

def registrar_importacion_mapeo(usuario, archivo, importados, errores):
    """Registra en BD el historial de importaciones de mapeo."""
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sku_mapeo_historial (
                id SERIAL PRIMARY KEY,
                fecha TIMESTAMP DEFAULT NOW(),
                usuario TEXT,
                archivo TEXT,
                importados INTEGER,
                errores INTEGER,
                detalle_errores TEXT
            )
        """)
        cur.execute("""
            INSERT INTO sku_mapeo_historial (usuario, archivo, importados, errores, detalle_errores)
            VALUES (%s, %s, %s, %s, %s)
        """, (usuario, archivo, importados, len(errores), str(errores[:5]) if errores else ""))
        conn.commit()
    except Exception as e:
        print(f"[Mapeo historial] Error: {e}")
        conn.rollback()
    cur.close(); conn.close()

def listar_historial_mapeo(limite=10):
    """Retorna el historial de importaciones de mapeo."""
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sku_mapeo_historial (
                id SERIAL PRIMARY KEY, fecha TIMESTAMP DEFAULT NOW(),
                usuario TEXT, archivo TEXT, importados INTEGER,
                errores INTEGER, detalle_errores TEXT
            )
        """)
        cur.execute("""
            SELECT id, TO_CHAR(fecha, 'DD/MM/YYYY HH24:MI') as fecha,
                   usuario, archivo, importados, errores, detalle_errores
            FROM sku_mapeo_historial
            ORDER BY fecha DESC LIMIT %s
        """, (limite,))
        rows = cur.fetchall()
        conn.commit()
    except:
        rows = []
    cur.close(); conn.close()
    return [{"id":r[0],"fecha":r[1],"usuario":r[2],"archivo":r[3],
             "importados":r[4],"errores":r[5],"detalle":r[6]} for r in rows]
