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
    cur.execute("""
        INSERT INTO movimientos (tipo, sku, nombre, cantidad, motivo, usuario, canal, fecha, orden_id, fecha_importacion)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (tipo, sku, nombre, cantidad, motivo, usuario, canal, fecha, orden_id, ahora))
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
               TO_CHAR(fecha, 'DD/MM/YYYY'), TO_CHAR(fecha, 'HH24:MI'),
               COALESCE(usuario, 'Sistema'), COALESCE(canal, 'Sistema'),
               COALESCE(orden_id, ''),
               TO_CHAR(fecha_importacion, 'DD/MM HH24:MI')
        FROM movimientos ORDER BY fecha DESC LIMIT %s
    """, (limite,))
    rows = cur.fetchall()
    cur.close(); conn.close()
    return [{"tipo":r[0],"sku":r[1],"nombre":r[2],"cantidad":r[3],"motivo":r[4],
             "fecha":r[5],"hora":r[6],"usuario":r[7],"canal":r[8],
             "orden_id":r[9],"importado":r[10] or ""} for r in rows]


def cargar_movimientos_hoy():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT tipo, sku, nombre, cantidad, motivo,
               TO_CHAR(fecha, 'HH24:MI'), COALESCE(canal, 'Sistema')
        FROM movimientos
        WHERE DATE(fecha) = CURRENT_DATE AND tipo = 'salida'
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
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("ALTER TABLE ordenes_procesadas ADD COLUMN IF NOT EXISTS order_id_texto TEXT")
        conn.commit()
    except Exception: conn.rollback()
    try:
        cur.execute("SELECT 1 FROM ordenes_procesadas WHERE order_id_texto=%s LIMIT 1",(str(order_id_texto),))
        if cur.fetchone(): cur.close(); conn.close(); return
        import random
        cur.execute("INSERT INTO ordenes_procesadas (orden_id,order_id_texto) VALUES (%s,%s)",
                    (random.randint(1,9007199254740991), str(order_id_texto)))
        conn.commit()
    except Exception as e:
        print(f"[Marcado] Error: {e}"); conn.rollback()
    cur.close(); conn.close()

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
    conn = get_conn(); cur = conn.cursor()
    cur.execute("""DELETE FROM movimientos WHERE id IN (
        SELECT id FROM (SELECT id, ROW_NUMBER() OVER (
            PARTITION BY orden_id,sku,canal,tipo ORDER BY fecha ASC,id ASC
        ) AS rn FROM movimientos WHERE orden_id IS NOT NULL AND orden_id!=\'\') t WHERE rn>1)""")
    n = cur.rowcount; conn.commit(); cur.close(); conn.close(); return n

def borrar_movimientos_marketplace(desde_fecha=None):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("DELETE FROM movimientos WHERE canal IN ('Walmart','WooCommerce','Paris')")
    m = cur.rowcount
    cur.execute("DELETE FROM ordenes_procesadas")
    o = cur.rowcount
    conn.commit(); cur.close(); conn.close(); return m, o

CANAL_DISPLAY = {"web":"Web Propia","walmart":"Walmart","paris":"París",
    "falabella":"Falabella","ripley":"Ripley","mercadolibre":"Mercado Libre","hites":"Hites"}

def init_sku_mapeo():
    conn = get_conn(); cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS sku_mapeo (
        id SERIAL PRIMARY KEY, sku_lusync TEXT UNIQUE NOT NULL,
        sku_web TEXT, sku_walmart TEXT, sku_paris TEXT,
        sku_falabella TEXT, sku_ripley TEXT, sku_mercadolibre TEXT, sku_hites TEXT)""")
    for col in ["sku_web","sku_walmart","sku_paris","sku_falabella","sku_ripley","sku_mercadolibre","sku_hites"]:
        try: cur.execute(f"ALTER TABLE sku_mapeo ADD COLUMN IF NOT EXISTS {col} TEXT")
        except: pass
    cur.execute("INSERT INTO configuracion (clave,valor) VALUES ('plataforma_web','WooCommerce') ON CONFLICT (clave) DO NOTHING")
    conn.commit(); cur.close(); conn.close()

def listar_sku_mapeo():
    init_sku_mapeo()
    conn = get_conn(); cur = conn.cursor()
    cur.execute("""SELECT p.sku, p.nombre,
        COALESCE(m.sku_web,''), COALESCE(m.sku_walmart,''), COALESCE(m.sku_paris,''),
        COALESCE(m.sku_falabella,''), COALESCE(m.sku_ripley,''),
        COALESCE(m.sku_mercadolibre,''), COALESCE(m.sku_hites,'')
        FROM productos p LEFT JOIN sku_mapeo m ON m.sku_lusync=p.sku ORDER BY p.nombre""")
    rows = cur.fetchall(); cur.close(); conn.close()
    return [{"sku_lusync":r[0],"nombre":r[1],"sku_web":r[2],"sku_walmart":r[3],
             "sku_paris":r[4],"sku_falabella":r[5],"sku_ripley":r[6],
             "sku_mercadolibre":r[7],"sku_hites":r[8]} for r in rows]

def guardar_sku_mapeo_fila(sku_lusync, skus):
    init_sku_mapeo()
    conn = get_conn(); cur = conn.cursor()
    cur.execute("""INSERT INTO sku_mapeo
        (sku_lusync,sku_web,sku_walmart,sku_paris,sku_falabella,sku_ripley,sku_mercadolibre,sku_hites)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (sku_lusync) DO UPDATE SET
            sku_web=EXCLUDED.sku_web, sku_walmart=EXCLUDED.sku_walmart,
            sku_paris=EXCLUDED.sku_paris, sku_falabella=EXCLUDED.sku_falabella,
            sku_ripley=EXCLUDED.sku_ripley, sku_mercadolibre=EXCLUDED.sku_mercadolibre,
            sku_hites=EXCLUDED.sku_hites""",
        (sku_lusync,
         (skus.get("web") or "").strip() or None,
         (skus.get("walmart") or "").strip() or None,
         (skus.get("paris") or "").strip() or None,
         (skus.get("falabella") or "").strip() or None,
         (skus.get("ripley") or "").strip() or None,
         (skus.get("mercadolibre") or "").strip() or None,
         (skus.get("hites") or "").strip() or None))
    conn.commit(); cur.close(); conn.close()

def get_sku_canal(sku_lusync, canal):
    init_sku_mapeo()
    c = canal.lower()
    webs = ["woocommerce","shopify","vtex","prestashop","jumpseller","web"]
    if c in webs: col = "sku_web"
    elif c in ["mercadolibre","mercado libre"]: col = "sku_mercadolibre"
    elif c in ["walmart","paris","falabella","ripley","hites"]: col = f"sku_{c}"
    else: return sku_lusync
    conn = get_conn(); cur = conn.cursor()
    cur.execute(f"SELECT {col} FROM sku_mapeo WHERE sku_lusync=%s", (sku_lusync,))
    row = cur.fetchone(); cur.close(); conn.close()
    return row[0].strip() if row and row[0] and row[0].strip() else sku_lusync

def get_plataforma_web():
    return get_configuracion("plataforma_web") or "WooCommerce"

def set_plataforma_web(p):
    set_configuracion("plataforma_web", p)

def registrar_importacion_mapeo(usuario, archivo, importados, errores):
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("""CREATE TABLE IF NOT EXISTS sku_mapeo_historial (
            id SERIAL PRIMARY KEY, fecha TIMESTAMP DEFAULT NOW(),
            usuario TEXT, archivo TEXT, importados INTEGER, errores INTEGER, detalle_errores TEXT)""")
        cur.execute("""INSERT INTO sku_mapeo_historial (usuario,archivo,importados,errores,detalle_errores)
            VALUES (%s,%s,%s,%s,%s)""",
            (usuario, archivo, importados, len(errores),
             str([f"F{e['fila']}:{e['error'][:50]}" for e in errores[:5]]) if errores else ""))
        conn.commit()
    except Exception as e:
        print(f"[Historial mapeo] {e}"); conn.rollback()
    cur.close(); conn.close()

def listar_historial_mapeo(limite=10):
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("""CREATE TABLE IF NOT EXISTS sku_mapeo_historial (
            id SERIAL PRIMARY KEY, fecha TIMESTAMP DEFAULT NOW(),
            usuario TEXT, archivo TEXT, importados INTEGER, errores INTEGER, detalle_errores TEXT)""")
        cur.execute("""SELECT id, TO_CHAR(fecha,'DD/MM/YYYY HH24:MI'),
            usuario, archivo, importados, errores, detalle_errores
            FROM sku_mapeo_historial ORDER BY fecha DESC LIMIT %s""", (limite,))
        rows = cur.fetchall(); conn.commit()
    except: rows = []
    cur.close(); conn.close()
    return [{"id":r[0],"fecha":r[1],"usuario":r[2],"archivo":r[3],
             "importados":r[4],"errores":r[5],"detalle":r[6]} for r in rows]

# ── ALERTAS ────────────────────────────────────────────────────────────────

def init_alertas():
    """Crea tablas de alertas y configuración de notificaciones."""
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("""CREATE TABLE IF NOT EXISTS alertas (
            id SERIAL PRIMARY KEY,
            fecha TIMESTAMP DEFAULT NOW(),
            tipo TEXT NOT NULL,
            canal TEXT,
            titulo TEXT NOT NULL,
            mensaje TEXT,
            orden_id TEXT,
            sku TEXT,
            leida BOOLEAN DEFAULT FALSE
        )""")
        cur.execute("""CREATE TABLE IF NOT EXISTS alertas_config (
            id SERIAL PRIMARY KEY,
            clave TEXT UNIQUE NOT NULL,
            valor TEXT
        )""")
        # Defaults de configuración SMTP (vacíos hasta que el usuario los configure)
        for clave, valor in [
            ("smtp_host", ""),
            ("smtp_port", "587"),
            ("smtp_user", ""),
            ("smtp_password", ""),
            ("smtp_from", ""),
            ("destinatarios", ""),  # CSV: "luis@x.com, otro@y.com"
            ("notif_cancelaciones", "true"),
            ("notif_errores_api", "false")
        ]:
            cur.execute("INSERT INTO alertas_config (clave, valor) VALUES (%s, %s) ON CONFLICT (clave) DO NOTHING",
                        (clave, valor))
        conn.commit()
    except Exception as e:
        print(f"[Alertas] init error: {e}"); conn.rollback()
    cur.close(); conn.close()


def crear_alerta(tipo, titulo, mensaje="", canal=None, orden_id=None, sku=None, enviar_email=True):
    """Registra una alerta en BD y opcionalmente envía email a destinatarios configurados."""
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("""INSERT INTO alertas (tipo, canal, titulo, mensaje, orden_id, sku)
                       VALUES (%s, %s, %s, %s, %s, %s)""",
                    (tipo, canal, titulo, mensaje, orden_id, sku))
        conn.commit()
    except Exception as e:
        print(f"[Alertas] crear error: {e}"); conn.rollback()
    cur.close(); conn.close()

    if enviar_email:
        try:
            _enviar_email_alerta(titulo, mensaje, canal, orden_id, sku)
        except Exception as e:
            print(f"[Alertas] email error: {e}")


def _enviar_email_alerta(titulo, mensaje, canal, orden_id, sku):
    """Envía email usando SMTP configurado. Si no hay config, solo loguea."""
    cfg = get_alertas_config()
    host = cfg.get("smtp_host", "").strip()
    user = cfg.get("smtp_user", "").strip()
    password = cfg.get("smtp_password", "").strip()
    from_addr = cfg.get("smtp_from", user).strip()
    destinatarios_raw = cfg.get("destinatarios", "").strip()

    if not host or not user or not password or not destinatarios_raw:
        print(f"[Alertas] SMTP no configurado, omitiendo email: {titulo}")
        return

    destinatarios = [d.strip() for d in destinatarios_raw.split(",") if d.strip()]
    if not destinatarios:
        return

    try:
        port = int(cfg.get("smtp_port", "587"))
    except:
        port = 587

    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    body = f"""
    <html><body style="font-family:Arial,sans-serif;color:#222">
    <h2 style="color:#c0392b">⚠️ {titulo}</h2>
    <p>{mensaje}</p>
    <hr>
    <table style="border-collapse:collapse">
    {"<tr><td><b>Canal:</b></td><td>%s</td></tr>" % canal if canal else ""}
    {"<tr><td><b>Orden:</b></td><td>%s</td></tr>" % orden_id if orden_id else ""}
    {"<tr><td><b>SKU:</b></td><td>%s</td></tr>" % sku if sku else ""}
    </table>
    <p style="color:#888;font-size:12px;margin-top:20px">
    Enviado automáticamente por Lusync ERP
    </p>
    </body></html>
    """
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[Lusync] {titulo}"
    msg["From"] = from_addr
    msg["To"] = ", ".join(destinatarios)
    msg.attach(MIMEText(body, "html"))

    try:
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=15)
        else:
            server = smtplib.SMTP(host, port, timeout=15)
            server.starttls()
        server.login(user, password)
        server.sendmail(from_addr, destinatarios, msg.as_string())
        server.quit()
        print(f"[Alertas] Email enviado a {len(destinatarios)} destinatarios: {titulo}")
    except Exception as e:
        print(f"[Alertas] Error SMTP: {e}")


def listar_alertas(limite=50, solo_no_leidas=False):
    conn = get_conn(); cur = conn.cursor()
    try:
        if solo_no_leidas:
            cur.execute("""SELECT id, TO_CHAR(fecha,'DD/MM/YYYY HH24:MI'), tipo, canal,
                           titulo, mensaje, orden_id, sku, leida
                           FROM alertas WHERE leida=FALSE ORDER BY fecha DESC LIMIT %s""", (limite,))
        else:
            cur.execute("""SELECT id, TO_CHAR(fecha,'DD/MM/YYYY HH24:MI'), tipo, canal,
                           titulo, mensaje, orden_id, sku, leida
                           FROM alertas ORDER BY fecha DESC LIMIT %s""", (limite,))
        rows = cur.fetchall()
    except Exception as e:
        print(f"[Alertas] listar error: {e}"); rows = []
    cur.close(); conn.close()
    return [{"id":r[0],"fecha":r[1],"tipo":r[2],"canal":r[3],"titulo":r[4],
             "mensaje":r[5],"orden_id":r[6],"sku":r[7],"leida":r[8]} for r in rows]


def contar_alertas_no_leidas():
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("SELECT COUNT(*) FROM alertas WHERE leida=FALSE")
        n = cur.fetchone()[0]
    except: n = 0
    cur.close(); conn.close()
    return n


def marcar_alerta_leida(alerta_id):
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("UPDATE alertas SET leida=TRUE WHERE id=%s", (alerta_id,))
        conn.commit()
    except Exception as e:
        print(f"[Alertas] marcar leida: {e}"); conn.rollback()
    cur.close(); conn.close()


def marcar_todas_leidas():
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("UPDATE alertas SET leida=TRUE WHERE leida=FALSE")
        conn.commit()
    except Exception as e:
        print(f"[Alertas] marcar todas: {e}"); conn.rollback()
    cur.close(); conn.close()


def get_alertas_config():
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("SELECT clave, valor FROM alertas_config")
        rows = cur.fetchall()
    except: rows = []
    cur.close(); conn.close()
    return {r[0]: (r[1] or "") for r in rows}


def set_alertas_config(data):
    conn = get_conn(); cur = conn.cursor()
    try:
        for clave, valor in data.items():
            cur.execute("""INSERT INTO alertas_config (clave, valor) VALUES (%s, %s)
                           ON CONFLICT (clave) DO UPDATE SET valor=EXCLUDED.valor""",
                        (clave, str(valor) if valor is not None else ""))
        conn.commit()
    except Exception as e:
        print(f"[Alertas] set config: {e}"); conn.rollback()
    cur.close(); conn.close()


# ── MERCADOLIBRE AUTH ──────────────────────────────────────────────────────

def init_meli_auth():
    """Crea tabla mercadolibre_auth para guardar tokens OAuth2."""
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("""CREATE TABLE IF NOT EXISTS mercadolibre_auth (
            id SERIAL PRIMARY KEY,
            user_id BIGINT,
            access_token TEXT,
            refresh_token TEXT,
            expires_at BIGINT,
            connected_at TIMESTAMP DEFAULT NOW()
        )""")
        conn.commit()
    except Exception as e:
        print(f"[MELI] init_meli_auth error: {e}"); conn.rollback()
    cur.close(); conn.close()


def get_meli_auth():
    """Retorna el token guardado más reciente."""
    conn = get_conn(); cur = conn.cursor()
    auth = None
    try:
        cur.execute("""SELECT user_id, access_token, refresh_token, expires_at
                       FROM mercadolibre_auth ORDER BY id DESC LIMIT 1""")
        row = cur.fetchone()
        if row:
            auth = {"user_id": row[0], "access_token": row[1],
                    "refresh_token": row[2], "expires_at": row[3]}
    except Exception as e:
        print(f"[MELI] get_meli_auth error: {e}")
    cur.close(); conn.close()
    return auth


def set_meli_auth(data):
    """Guarda/actualiza el token. Si hay registros previos, los reemplaza con uno nuevo."""
    conn = get_conn(); cur = conn.cursor()
    try:
        # Limpiar registros viejos para mantener solo el más reciente
        cur.execute("DELETE FROM mercadolibre_auth")
        cur.execute("""INSERT INTO mercadolibre_auth (user_id, access_token, refresh_token, expires_at)
                       VALUES (%s, %s, %s, %s)""",
                    (data.get("user_id"), data.get("access_token"),
                     data.get("refresh_token"), data.get("expires_at")))
        conn.commit()
    except Exception as e:
        print(f"[MELI] set_meli_auth error: {e}"); conn.rollback()
    cur.close(); conn.close()


def borrar_meli_auth():
    """Borra el token guardado (desconectar)."""
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("DELETE FROM mercadolibre_auth")
        conn.commit()
    except Exception as e:
        print(f"[MELI] borrar_meli_auth error: {e}"); conn.rollback()
    cur.close(); conn.close()


# ── DASHBOARD STATS (para gráficos del dashboard) ───────────────────────────

def stats_ventas_por_canal_dia(fecha_desde, fecha_hasta):
    """Ventas (salidas) agrupadas por día y canal. Para gráfico línea apilada.
    Solo cuenta canales reales de marketplace, excluye 'Manual', 'Sistema', NULL."""
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("""
            SELECT TO_CHAR(DATE(fecha), 'YYYY-MM-DD') AS dia,
                   canal,
                   COALESCE(SUM(cantidad), 0) AS total
            FROM movimientos
            WHERE tipo = 'salida'
              AND DATE(fecha) BETWEEN %s AND %s
              AND canal IS NOT NULL
              AND canal NOT IN ('Manual', 'Sistema', 'manual', 'sistema', '')
            GROUP BY dia, canal
            ORDER BY dia ASC
        """, (fecha_desde, fecha_hasta))
        rows = cur.fetchall()
    except Exception as e:
        print(f"[Stats] ventas_por_canal_dia: {e}"); rows = []
    cur.close(); conn.close()
    return [{"dia": r[0], "canal": r[1], "total": int(r[2])} for r in rows]


def stats_top_productos_vendidos(fecha_desde, fecha_hasta, limite=10):
    """Top N productos más vendidos en un rango. Para gráfico barras.
    Solo cuenta canales reales de marketplace, excluye Manual/Sistema."""
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("""
            SELECT sku, nombre, COALESCE(SUM(cantidad), 0) AS total
            FROM movimientos
            WHERE tipo = 'salida'
              AND DATE(fecha) BETWEEN %s AND %s
              AND canal IS NOT NULL
              AND canal NOT IN ('Manual', 'Sistema', 'manual', 'sistema', '')
            GROUP BY sku, nombre
            ORDER BY total DESC
            LIMIT %s
        """, (fecha_desde, fecha_hasta, limite))
        rows = cur.fetchall()
    except Exception as e:
        print(f"[Stats] top_productos: {e}"); rows = []
    cur.close(); conn.close()
    return [{"sku": r[0], "nombre": r[1], "total": int(r[2])} for r in rows]


def stats_movimientos_dia(fecha_desde, fecha_hasta):
    """Entradas vs salidas por día. Para gráfico líneas."""
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("""
            SELECT TO_CHAR(DATE(fecha), 'YYYY-MM-DD') AS dia,
                   tipo,
                   COALESCE(SUM(cantidad), 0) AS total
            FROM movimientos
            WHERE DATE(fecha) BETWEEN %s AND %s
            GROUP BY dia, tipo
            ORDER BY dia ASC
        """, (fecha_desde, fecha_hasta))
        rows = cur.fetchall()
    except Exception as e:
        print(f"[Stats] movimientos_dia: {e}"); rows = []
    cur.close(); conn.close()
    return [{"dia": r[0], "tipo": r[1], "total": int(r[2])} for r in rows]


def stats_distribucion_stock_canal():
    """Distribución de stock total por canal mapeado. Para gráfico donut."""
    conn = get_conn(); cur = conn.cursor()
    distribucion = {"WooCommerce": 0, "Walmart": 0, "Paris": 0,
                    "Falabella": 0, "Ripley": 0, "MercadoLibre": 0, "Hites": 0}
    try:
        # Productos con stock
        cur.execute("""
            SELECT p.sku, p.stock,
                   m.sku_web, m.sku_walmart, m.sku_paris, m.sku_falabella,
                   m.sku_ripley, m.sku_mercadolibre, m.sku_hites
            FROM productos p
            LEFT JOIN sku_mapeo m ON m.sku_lusync = p.sku
        """)
        rows = cur.fetchall()
        for r in rows:
            stock = int(r[1] or 0)
            if stock <= 0:
                continue
            if r[2]: distribucion["WooCommerce"]   += stock
            if r[3]: distribucion["Walmart"]       += stock
            if r[4]: distribucion["Paris"]         += stock
            if r[5]: distribucion["Falabella"]     += stock
            if r[6]: distribucion["Ripley"]        += stock
            if r[7]: distribucion["MercadoLibre"]  += stock
            if r[8]: distribucion["Hites"]         += stock
    except Exception as e:
        print(f"[Stats] distribucion_stock: {e}")
    cur.close(); conn.close()
    return [{"canal": k, "stock": v} for k, v in distribucion.items() if v > 0]


def stats_kpis_dashboard(fecha_desde, fecha_hasta):
    """KPIs para tarjetas superiores del dashboard."""
    conn = get_conn(); cur = conn.cursor()
    kpis = {
        "ventas_periodo": 0,
        "ordenes_periodo": 0,
        "productos_total": 0,
        "stock_total": 0,
        "stock_bajo": 0,
        "alertas_no_leidas": 0
    }
    try:
        # Ventas en el período (excluye Manual/Sistema, solo canales marketplace)
        cur.execute("""
            SELECT COALESCE(SUM(cantidad), 0), COUNT(DISTINCT orden_id)
            FROM movimientos
            WHERE tipo = 'salida'
              AND DATE(fecha) BETWEEN %s AND %s
              AND canal IS NOT NULL
              AND canal NOT IN ('Manual', 'Sistema', 'manual', 'sistema', '')
        """, (fecha_desde, fecha_hasta))
        r = cur.fetchone()
        kpis["ventas_periodo"]  = int(r[0] or 0)
        kpis["ordenes_periodo"] = int(r[1] or 0)

        # Productos / stock total
        cur.execute("SELECT COUNT(*), COALESCE(SUM(stock), 0) FROM productos")
        r = cur.fetchone()
        kpis["productos_total"] = int(r[0] or 0)
        kpis["stock_total"]     = int(r[1] or 0)

        # Stock bajo (umbral 10)
        cur.execute("SELECT COUNT(*) FROM productos WHERE stock < 10 AND stock > 0")
        r = cur.fetchone()
        kpis["stock_bajo"] = int(r[0] or 0)

        # Alertas
        try:
            cur.execute("SELECT COUNT(*) FROM alertas WHERE leida=FALSE")
            r = cur.fetchone()
            kpis["alertas_no_leidas"] = int(r[0] or 0)
        except: pass
    except Exception as e:
        print(f"[Stats] kpis: {e}")
    cur.close(); conn.close()
    return kpis


# ── BODEGAS Y STOCK MULTI-BODEGA ────────────────────────────────────────────

# Bodegas estándar de Lusync. Cada cliente tendrá estas mismas bodegas
# (independiente de si usa fulfillment o no).
BODEGAS_DEFAULT = [
    # (codigo, nombre, tipo, canal_asociado)
    ("CENTRAL",      "Bodega Central",         "propia",      None),
    ("MELI_FULL",    "MercadoLibre Full",      "fulfillment", "mercadolibre"),
    ("PARIS_CD",     "París Fulfillment",      "fulfillment", "paris"),
    ("WALMART_FBM",  "Walmart Fulfillment",    "fulfillment", "walmart"),
    ("FALABELLA_FBM","Falabella Fulfillment",  "fulfillment", "falabella"),
    ("RIPLEY_FBM",   "Ripley Fulfillment",     "fulfillment", "ripley"),
    ("HITES_FBM",    "Hites Fulfillment",      "fulfillment", "hites"),
    ("WOO_DROP",     "WooCommerce Dropship",   "dropship",    "woocommerce"),
]


def init_bodegas():
    """Crea tablas bodegas + stock_bodega y migra el stock actual a Bodega Central."""
    conn = get_conn(); cur = conn.cursor()
    try:
        # Tabla maestra de bodegas
        cur.execute("""CREATE TABLE IF NOT EXISTS bodegas (
            id SERIAL PRIMARY KEY,
            codigo TEXT UNIQUE NOT NULL,
            nombre TEXT NOT NULL,
            tipo TEXT NOT NULL DEFAULT 'propia',
            canal TEXT,
            activa BOOLEAN DEFAULT TRUE,
            orden INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT NOW()
        )""")

        # Tabla de stock por bodega (relación N:M producto-bodega)
        cur.execute("""CREATE TABLE IF NOT EXISTS stock_bodega (
            id SERIAL PRIMARY KEY,
            sku TEXT NOT NULL,
            bodega_codigo TEXT NOT NULL,
            cantidad INTEGER DEFAULT 0,
            actualizado_at TIMESTAMP DEFAULT NOW(),
            UNIQUE (sku, bodega_codigo)
        )""")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_stock_bodega_sku ON stock_bodega(sku)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_stock_bodega_codigo ON stock_bodega(bodega_codigo)")

        # Insertar bodegas default si no existen
        for i, (codigo, nombre, tipo, canal) in enumerate(BODEGAS_DEFAULT):
            cur.execute("""INSERT INTO bodegas (codigo, nombre, tipo, canal, orden)
                           VALUES (%s, %s, %s, %s, %s)
                           ON CONFLICT (codigo) DO NOTHING""",
                        (codigo, nombre, tipo, canal, i))

        # Agregar columna bodega_codigo a movimientos si no existe
        cur.execute("ALTER TABLE movimientos ADD COLUMN IF NOT EXISTS bodega_codigo TEXT DEFAULT 'CENTRAL'")

        conn.commit()

        # MIGRACIÓN: copiar stock actual de productos a Bodega Central
        cur.execute("""SELECT sku, stock FROM productos WHERE stock > 0""")
        productos = cur.fetchall()
        migrados = 0
        for sku, stock in productos:
            cur.execute("""INSERT INTO stock_bodega (sku, bodega_codigo, cantidad)
                           VALUES (%s, 'CENTRAL', %s)
                           ON CONFLICT (sku, bodega_codigo) DO NOTHING""",
                        (sku, stock or 0))
            if cur.rowcount > 0:
                migrados += 1
        conn.commit()
        if migrados > 0:
            print(f"[Bodegas] Migrados {migrados} productos a Bodega Central")
    except Exception as e:
        print(f"[Bodegas] init error: {e}"); conn.rollback()
    cur.close(); conn.close()


def listar_bodegas(solo_activas=True):
    """Devuelve todas las bodegas configuradas."""
    conn = get_conn(); cur = conn.cursor()
    try:
        if solo_activas:
            cur.execute("SELECT codigo, nombre, tipo, canal, activa FROM bodegas WHERE activa=TRUE ORDER BY orden, id")
        else:
            cur.execute("SELECT codigo, nombre, tipo, canal, activa FROM bodegas ORDER BY orden, id")
        rows = cur.fetchall()
    except Exception as e:
        print(f"[Bodegas] listar error: {e}"); rows = []
    cur.close(); conn.close()
    return [{"codigo":r[0],"nombre":r[1],"tipo":r[2],"canal":r[3],"activa":r[4]} for r in rows]


def stock_por_bodega(sku):
    """Devuelve {bodega_codigo: cantidad} para un SKU."""
    conn = get_conn(); cur = conn.cursor()
    result = {}
    try:
        cur.execute("SELECT bodega_codigo, cantidad FROM stock_bodega WHERE sku=%s", (sku,))
        result = {r[0]: int(r[1] or 0) for r in cur.fetchall()}
    except Exception as e:
        print(f"[Bodegas] stock_por_bodega error: {e}")
    cur.close(); conn.close()
    return result


def get_stock_bodega(sku, bodega_codigo):
    """Stock de un SKU en una bodega específica."""
    conn = get_conn(); cur = conn.cursor()
    cant = 0
    try:
        cur.execute("SELECT cantidad FROM stock_bodega WHERE sku=%s AND bodega_codigo=%s",
                    (sku, bodega_codigo))
        r = cur.fetchone()
        if r: cant = int(r[0] or 0)
    except Exception as e:
        print(f"[Bodegas] get_stock_bodega: {e}")
    cur.close(); conn.close()
    return cant


def set_stock_bodega(sku, bodega_codigo, cantidad):
    """Establece el stock de un SKU en una bodega (override)."""
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("""INSERT INTO stock_bodega (sku, bodega_codigo, cantidad, actualizado_at)
                       VALUES (%s, %s, %s, NOW())
                       ON CONFLICT (sku, bodega_codigo)
                       DO UPDATE SET cantidad=EXCLUDED.cantidad, actualizado_at=NOW()""",
                    (sku, bodega_codigo, max(0, int(cantidad))))
        conn.commit()
        # Sincronizar columna stock de productos (sumatoria de todas las bodegas)
        _recalcular_stock_total(sku)
    except Exception as e:
        print(f"[Bodegas] set_stock_bodega: {e}"); conn.rollback()
    cur.close(); conn.close()


def ajustar_stock_bodega(sku, bodega_codigo, delta):
    """Suma o resta delta al stock de un SKU en una bodega. Retorna nuevo total."""
    conn = get_conn(); cur = conn.cursor()
    nuevo = 0
    try:
        cur.execute("""INSERT INTO stock_bodega (sku, bodega_codigo, cantidad, actualizado_at)
                       VALUES (%s, %s, GREATEST(0, %s), NOW())
                       ON CONFLICT (sku, bodega_codigo)
                       DO UPDATE SET cantidad=GREATEST(0, stock_bodega.cantidad + EXCLUDED.cantidad),
                                     actualizado_at=NOW()
                       RETURNING cantidad""",
                    (sku, bodega_codigo, max(0, int(delta)) if delta > 0 else int(delta)))
        # NOTE: el INSERT inicial usa GREATEST(0, delta) por si se intenta restar de bodega vacía
        # En el UPDATE se respeta el GREATEST(0, current+delta)
        r = cur.fetchone()
        if r: nuevo = int(r[0] or 0)
        conn.commit()
        _recalcular_stock_total(sku)
    except Exception as e:
        print(f"[Bodegas] ajustar_stock_bodega: {e}"); conn.rollback()
    cur.close(); conn.close()
    return nuevo


def _recalcular_stock_total(sku):
    """Sincroniza productos.stock = SUM(stock_bodega.cantidad) para un SKU."""
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("""UPDATE productos SET stock = (
            SELECT COALESCE(SUM(cantidad), 0) FROM stock_bodega WHERE sku=%s
        ) WHERE sku=%s""", (sku, sku))
        conn.commit()
    except Exception as e:
        print(f"[Bodegas] _recalcular_stock_total: {e}"); conn.rollback()
    cur.close(); conn.close()


def listar_stock_completo():
    """Tabla completa producto × bodega para vista admin de bodegas."""
    conn = get_conn(); cur = conn.cursor()
    result = []
    try:
        cur.execute("""
            SELECT p.sku, p.nombre, p.stock,
                   COALESCE(json_object_agg(sb.bodega_codigo, sb.cantidad)
                            FILTER (WHERE sb.bodega_codigo IS NOT NULL), '{}'::json) AS por_bodega
            FROM productos p
            LEFT JOIN stock_bodega sb ON sb.sku = p.sku
            GROUP BY p.sku, p.nombre, p.stock
            ORDER BY p.nombre
        """)
        for row in cur.fetchall():
            result.append({
                "sku": row[0],
                "nombre": row[1],
                "stock_total": int(row[2] or 0),
                "por_bodega": row[3] if isinstance(row[3], dict) else {}
            })
    except Exception as e:
        print(f"[Bodegas] listar_stock_completo: {e}")
    cur.close(); conn.close()
    return result


def stock_total_por_bodega():
    """Resumen: total de unidades por bodega para dashboard."""
    conn = get_conn(); cur = conn.cursor()
    result = {}
    try:
        cur.execute("""SELECT b.codigo, b.nombre, b.tipo,
                              COALESCE(SUM(sb.cantidad), 0) AS total
                       FROM bodegas b
                       LEFT JOIN stock_bodega sb ON sb.bodega_codigo = b.codigo
                       WHERE b.activa = TRUE
                       GROUP BY b.codigo, b.nombre, b.tipo, b.orden
                       ORDER BY b.orden""")
        for row in cur.fetchall():
            result[row[0]] = {"nombre": row[1], "tipo": row[2], "total": int(row[3] or 0)}
    except Exception as e:
        print(f"[Bodegas] stock_total_por_bodega: {e}")
    cur.close(); conn.close()
    return result


def determinar_bodega_para_canal(canal, fulfillment=False):
    """Dado un canal y si es venta fulfillment, retorna el código de bodega."""
    if not fulfillment:
        return "CENTRAL"
    canal_l = (canal or "").lower()
    mapeo = {
        "mercadolibre": "MELI_FULL",
        "paris":        "PARIS_CD",
        "walmart":      "WALMART_FBM",
        "falabella":    "FALABELLA_FBM",
        "ripley":       "RIPLEY_FBM",
        "hites":        "HITES_FBM",
        "woocommerce":  "WOO_DROP",
    }
    return mapeo.get(canal_l, "CENTRAL")


# ── DESCUENTO INTELIGENTE POR BODEGA ────────────────────────────────────────

def descontar_venta_inteligente(sku, cantidad, canal, fulfillment, orden_id=None,
                                 motivo=None, usuario="Sistema"):
    """
    Función central que descuenta stock de la bodega correcta según el canal y tipo.

    Args:
        sku: SKU del producto
        cantidad: unidades a descontar (positivo)
        canal: nombre canal (WooCommerce, Walmart, Paris, MercadoLibre, etc.)
        fulfillment: True si es venta fulfillment del marketplace, False si seller envía
        orden_id: número de orden del marketplace
        motivo: texto descriptivo de la venta
        usuario: quien registra el movimiento (default Sistema)

    Returns:
        dict con: {ok, bodega_codigo, stock_bodega_antes, stock_bodega_despues, sku, cantidad}
    """
    bodega = determinar_bodega_para_canal(canal, fulfillment=fulfillment)
    stock_antes = get_stock_bodega(sku, bodega)

    # Si la bodega no tiene stock suficiente, descontar lo que se pueda
    # y registrar advertencia
    descontar = min(cantidad, stock_antes)
    advertencia = None
    if stock_antes < cantidad:
        advertencia = f"Bodega {bodega} sin stock suficiente: pedidas {cantidad}, había {stock_antes}"
        print(f"[Bodegas] WARN {advertencia}")

    if descontar > 0:
        ajustar_stock_bodega(sku, bodega, -descontar)

    stock_despues = get_stock_bodega(sku, bodega)

    # Registrar movimiento con la bodega
    try:
        conn = get_conn(); cur = conn.cursor()
        # Buscar nombre del producto
        cur.execute("SELECT nombre FROM productos WHERE sku=%s LIMIT 1", (sku,))
        r = cur.fetchone()
        nombre = r[0] if r else sku

        motivo_final = motivo or f"Venta {canal}{' (Fulfillment)' if fulfillment else ''}"

        cur.execute("""INSERT INTO movimientos
            (tipo, sku, nombre, cantidad, motivo, usuario, canal, fecha, orden_id, bodega_codigo)
            VALUES ('salida', %s, %s, %s, %s, %s, %s, NOW(), %s, %s)""",
            (sku, nombre, descontar, motivo_final, usuario, canal, orden_id, bodega))
        conn.commit()
        cur.close(); conn.close()
    except Exception as e:
        print(f"[Bodegas] Error registrando movimiento: {e}")

    return {
        "ok": True,
        "sku": sku,
        "cantidad_solicitada": cantidad,
        "cantidad_descontada": descontar,
        "bodega": bodega,
        "stock_antes": stock_antes,
        "stock_despues": stock_despues,
        "advertencia": advertencia
    }


def reintegrar_stock_bodega(sku, cantidad, bodega_codigo, motivo, canal=None, orden_id=None,
                             usuario="Sistema"):
    """Reintegra stock a una bodega específica (para cancelaciones/devoluciones)."""
    ajustar_stock_bodega(sku, bodega_codigo, cantidad)

    try:
        conn = get_conn(); cur = conn.cursor()
        cur.execute("SELECT nombre FROM productos WHERE sku=%s LIMIT 1", (sku,))
        r = cur.fetchone()
        nombre = r[0] if r else sku

        cur.execute("""INSERT INTO movimientos
            (tipo, sku, nombre, cantidad, motivo, usuario, canal, fecha, orden_id, bodega_codigo)
            VALUES ('entrada', %s, %s, %s, %s, %s, %s, NOW(), %s, %s)""",
            (sku, nombre, cantidad, motivo, usuario, canal, orden_id, bodega_codigo))
        conn.commit()
        cur.close(); conn.close()
    except Exception as e:
        print(f"[Bodegas] Error registrando entrada: {e}")


def detectar_fulfillment_meli(orden_data):
    """Detecta si una orden de MercadoLibre es Full o Seller envía."""
    try:
        # En orders/{id}, MELI incluye shipping_id; consultar shipment da logistic_type
        shipping = orden_data.get("shipping", {})
        # logistic_type: 'fulfillment' = MELI Full, 'self_service'/'cross_docking'/'drop_off' = seller
        logistic_type = shipping.get("logistic_type", "")
        if logistic_type == "fulfillment":
            return True
        # En el payload de la orden a veces viene en shipping.id y hay que consultar /shipments/{id}
        # Por ahora, asumimos que si NO viene logistic_type explícito, es seller envía
        return False
    except: return False


def detectar_fulfillment_paris(orden_data):
    """Detecta si una orden París es CrossDocking o Seller normal."""
    try:
        # En el payload de Paris, los shipments tienen 'flow' o 'carrier'
        # Si flow == 'CROSSDOCKING' → es CD
        shipments = orden_data.get("shipments", [])
        for ship in shipments:
            flow = (ship.get("flow") or ship.get("flowType") or "").upper()
            if "CROSS" in flow or flow == "CD":
                return True
        # También puede estar en orden_data.shippingType
        shipping_type = (orden_data.get("shippingType") or orden_data.get("shipping_type") or "").upper()
        if "CROSS" in shipping_type or shipping_type == "CD":
            return True
        return False
    except: return False


def sincronizar_stock_a_bodega_central(sku):
    """Helper: Si productos.stock cambió por código antiguo, sincroniza a bodega CENTRAL.
    Útil para mantener consistencia mientras se migra todo el código a bodegas."""
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("SELECT stock FROM productos WHERE sku=%s", (sku,))
        r = cur.fetchone()
        if r:
            stock_total = int(r[0] or 0)
            # Calcular cuánto hay en otras bodegas
            cur.execute("""SELECT COALESCE(SUM(cantidad), 0)
                          FROM stock_bodega WHERE sku=%s AND bodega_codigo != 'CENTRAL'""", (sku,))
            otras = int(cur.fetchone()[0] or 0)
            # Lo que va en CENTRAL es el total menos lo de otras bodegas
            central = max(0, stock_total - otras)
            cur.execute("""INSERT INTO stock_bodega (sku, bodega_codigo, cantidad, actualizado_at)
                          VALUES (%s, 'CENTRAL', %s, NOW())
                          ON CONFLICT (sku, bodega_codigo)
                          DO UPDATE SET cantidad=EXCLUDED.cantidad, actualizado_at=NOW()""",
                       (sku, central))
            conn.commit()
    except Exception as e:
        print(f"[Bodegas] sincronizar_stock_a_bodega_central: {e}"); conn.rollback()
    cur.close(); conn.close()


def detectar_fulfillment_walmart(orden_data):
    """Detecta si una orden Walmart es WFS (Walmart Fulfillment Services) o Seller envía.
    Walmart usa varios campos según versión del API:
      - fulfillmentInfo.fulfillmentMethod = 'wfs' o 'WFS'
      - shippingInfo.shipMethod
      - purchaseOrderType
    """
    try:
        # Path 1: fulfillmentInfo
        fi = orden_data.get("fulfillmentInfo", {}) or orden_data.get("fulfillment_info", {})
        method = (fi.get("fulfillmentMethod") or fi.get("fulfillment_method") or "").upper()
        if "WFS" in method or "FULFILLED_BY_WALMART" in method:
            return True

        # Path 2: orderType o purchaseOrderType
        order_type = (orden_data.get("orderType") or orden_data.get("purchaseOrderType") or "").upper()
        if "WFS" in order_type or "FULFILLED" in order_type:
            return True

        # Path 3: shippingInfo / lines (a veces el shipNode tiene "WFS")
        ship_info = orden_data.get("shippingInfo", {}) or orden_data.get("shipping_info", {})
        ship_method = (ship_info.get("shipMethod") or ship_info.get("methodCode") or "").upper()
        if "WFS" in ship_method:
            return True

        # Path 4: en orderLines hay fulfillment indicator
        order_lines = orden_data.get("orderLines", {}).get("orderLine", [])
        if isinstance(order_lines, dict):
            order_lines = [order_lines]
        for line in order_lines:
            line_fi = line.get("fulfillment", {}) or {}
            if (line_fi.get("fulfillmentOption") or "").upper() in ("WFS", "FULFILLED_BY_WALMART"):
                return True

        return False
    except: return False
