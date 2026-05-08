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

    # ── COLUMNAS DE TRAZABILIDAD AVANZADA (no destructivas) ───────────────────
    # fecha_compra_marketplace: fecha REAL en que el cliente compró en el marketplace
    #                            (lo que viene en el payload: date_created/createdAt/etc).
    #                            NULL para movimientos antiguos y para movimientos no-marketplace.
    # origen_registro:           cómo entró el movimiento al sistema:
    #                            'sync_manual', 'webhook', 'scheduler', 'manual',
    #                            'import_excel', 'devolucion', 'pos', 'sistema'.
    # stock_antes / stock_despues: snapshot del stock de la bodega antes y después
    #                              del movimiento, para auditoría sin reconstruir.
    cur.execute("ALTER TABLE movimientos ADD COLUMN IF NOT EXISTS fecha_compra_marketplace TIMESTAMP")
    cur.execute("ALTER TABLE movimientos ADD COLUMN IF NOT EXISTS origen_registro TEXT DEFAULT 'sistema'")
    cur.execute("ALTER TABLE movimientos ADD COLUMN IF NOT EXISTS stock_antes INTEGER")
    cur.execute("ALTER TABLE movimientos ADD COLUMN IF NOT EXISTS stock_despues INTEGER")

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

    # ── SYNC UNIVERSAL: cualquier movimiento de stock sincroniza a todos los canales ──
    # Se ejecuta en background para no bloquear la respuesta
    try:
        import threading
        def _sync_bg():
            try:
                # Calcular stock disponible para canales no-fulfillment
                # = SUM de bodegas tipo 'propia' (CENTRAL, etc.)
                # NO se cuenta MELI_FULL, PARIS_CD ni ninguna fulfillment/transito
                conn2 = get_conn()
                cur2 = conn2.cursor()
                cur2.execute("""
                    SELECT COALESCE(SUM(sb.cantidad), 0)
                    FROM stock_bodega sb
                    JOIN bodegas b ON b.codigo = sb.bodega_codigo
                    WHERE sb.sku = %s AND b.tipo = 'propia'
                """, (sku,))
                row = cur2.fetchone()
                stock_disponible = int(row[0]) if row and row[0] is not None else 0
                cur2.close()
                conn2.close()

                # Importar y llamar sync con stock disponible
                from app import sincronizar_stock_marketplaces
                resultado = sincronizar_stock_marketplaces(sku, stock_disponible, contexto=f"{tipo}_{canal}_{motivo[:20] if motivo else 'manual'}")
                print(f"[SyncUniversal] {sku} stock_propio={stock_disponible} -> {resultado}")
            except Exception as e:
                import traceback
                print(f"[SyncUniversal] Error sincronizando {sku}: {e}")
                traceback.print_exc()

        threading.Thread(target=_sync_bg, daemon=True).start()
    except Exception:
        pass

def cargar_movimientos(limite=20):
    conn = get_conn()
    cur = conn.cursor()
    # Asegurar que las columnas existen antes de leerlas
    try:
        cur.execute("ALTER TABLE movimientos ADD COLUMN IF NOT EXISTS usuario TEXT DEFAULT 'Sistema'")
        cur.execute("ALTER TABLE movimientos ADD COLUMN IF NOT EXISTS canal TEXT DEFAULT 'Sistema'")
        cur.execute("ALTER TABLE movimientos ADD COLUMN IF NOT EXISTS bodega_codigo TEXT DEFAULT 'CENTRAL'")
        cur.execute("ALTER TABLE movimientos ADD COLUMN IF NOT EXISTS fecha_compra_marketplace TIMESTAMP")
        cur.execute("ALTER TABLE movimientos ADD COLUMN IF NOT EXISTS origen_registro TEXT DEFAULT 'sistema'")
        cur.execute("ALTER TABLE movimientos ADD COLUMN IF NOT EXISTS stock_antes INTEGER")
        cur.execute("ALTER TABLE movimientos ADD COLUMN IF NOT EXISTS stock_despues INTEGER")
        conn.commit()
    except:
        conn.rollback()
    cur.execute("""
        SELECT tipo, sku, nombre, cantidad, motivo,
               TO_CHAR(fecha, 'DD/MM/YYYY'), TO_CHAR(fecha, 'HH24:MI'),
               COALESCE(usuario, 'Sistema'), COALESCE(canal, 'Sistema'),
               COALESCE(orden_id, ''),
               TO_CHAR(fecha_importacion, 'DD/MM HH24:MI'),
               COALESCE(bodega_codigo, 'CENTRAL'),
               TO_CHAR(fecha_compra_marketplace, 'DD/MM/YYYY HH24:MI'),
               COALESCE(origen_registro, 'sistema'),
               stock_antes,
               stock_despues
        FROM movimientos ORDER BY fecha DESC LIMIT %s
    """, (limite,))
    rows = cur.fetchall()
    cur.close(); conn.close()
    return [{"tipo":r[0],"sku":r[1],"nombre":r[2],"cantidad":r[3],"motivo":r[4],
             "fecha":r[5],"hora":r[6],"usuario":r[7],"canal":r[8],
             "orden_id":r[9],"importado":r[10] or "",
             "bodega":r[11] or "CENTRAL",
             "fecha_compra":r[12] or "",
             "origen":r[13] or "sistema",
             "stock_antes": r[14] if r[14] is not None else None,
             "stock_despues": r[15] if r[15] is not None else None
             } for r in rows]


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
    # Columnas nuevas para sistema mejorado de devoluciones (sin romper datos existentes)
    columnas_nuevas = [
        ("tipificacion", "TEXT"),                  # buen_estado / reparable / dado_de_baja / reembolsado / reenviado
        ("motivo_texto", "TEXT"),                  # texto largo del motivo/notas
        ("usuario_revisor", "TEXT"),               # quién revisó la devolución
        ("fecha_deadline", "TIMESTAMP"),           # cuando vencen las 72h hábiles
        ("foto_url", "TEXT"),                      # URL opcional de foto evidencia
        ("etiqueta_generada", "BOOLEAN DEFAULT FALSE"),
        ("etiqueta_pdf_url", "TEXT"),              # URL del PDF generado
        ("origen_datos", "TEXT DEFAULT 'manual'"), # manual / webhook / sync
        ("orden_data_json", "TEXT")                # snapshot de los datos de la OC al momento del registro
    ]
    for nombre, tipo in columnas_nuevas:
        try:
            cur.execute(f"ALTER TABLE devoluciones ADD COLUMN IF NOT EXISTS {nombre} {tipo}")
        except Exception as e:
            print(f"[devoluciones] columna {nombre}: {e}")
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


def intentar_marcar_orden_atomic(order_id_texto):
    """Marca una orden como procesada DE FORMA ATÓMICA. 
    
    Devuelve:
    - True si fue marcada AHORA (esta llamada es la "primera")
    - False si ya estaba marcada (otra llamada concurrente la procesó primero)
    
    Resuelve race conditions cuando llegan 2 webhooks simultáneos para la misma orden
    (típico en MELI con multi-publicación).
    
    Estrategia: usa transacción con SELECT FOR UPDATE para bloquear la fila a nivel de BD.
    Solo el primer webhook que llegue puede insertar; los demás esperan y luego ven la fila.
    """
    conn = get_conn()
    cur = conn.cursor()
    marcada = False
    try:
        # Asegurar columna existe
        cur.execute("ALTER TABLE ordenes_procesadas ADD COLUMN IF NOT EXISTS order_id_texto TEXT")
        conn.commit()
        
        # Iniciar transacción explícita
        cur.execute("BEGIN")
        
        # Buscar si ya existe (con LOCK pesimista para evitar race conditions)
        cur.execute("""
            SELECT 1 FROM ordenes_procesadas 
            WHERE order_id_texto = %s 
            LIMIT 1 
            FOR UPDATE
        """, (str(order_id_texto),))
        
        if cur.fetchone():
            # Ya existe, otra request la procesó
            cur.execute("COMMIT")
            marcada = False
        else:
            # No existe, insertarla AHORA (mientras tenemos el lock)
            import random
            cur.execute("""
                INSERT INTO ordenes_procesadas (orden_id, order_id_texto) 
                VALUES (%s, %s)
            """, (random.randint(1, 9007199254740991), str(order_id_texto)))
            cur.execute("COMMIT")
            marcada = True
    except Exception as e:
        print(f"[Marcado atómico] Error: {e}")
        try: conn.rollback()
        except: pass
        # FALLBACK: usar el método clásico (no atómico pero funcional)
        try:
            cur.execute("SELECT 1 FROM ordenes_procesadas WHERE order_id_texto=%s LIMIT 1",
                       (str(order_id_texto),))
            if cur.fetchone():
                marcada = False
            else:
                import random
                cur.execute("""INSERT INTO ordenes_procesadas (orden_id, order_id_texto) 
                               VALUES (%s, %s)""",
                           (random.randint(1, 9007199254740991), str(order_id_texto)))
                conn.commit()
                marcada = True
        except Exception as e2:
            print(f"[Marcado atómico FALLBACK] Error: {e2}")
            try: conn.rollback()
            except: pass
            marcada = False
    cur.close(); conn.close()
    return marcada
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


# ═══════════════════════════════════════════════════════════════════════════
# MAPEO MULTI-PUBLICACIÓN (sku_mapeo_canal)
# ═══════════════════════════════════════════════════════════════════════════
# Modelo: 1 producto Lusync puede tener N publicaciones por marketplace
# Ejemplo: ODJM001 (Lusync) → 2 publicaciones MELI: MLC2710421490 y MLC1584290001
# Ambas se sincronizan al actualizar stock/precio.
# ═══════════════════════════════════════════════════════════════════════════

def init_sku_mapeo_canal():
    """Crea la tabla nueva sku_mapeo_canal (multi-publicación por canal).

    NO migra datos de la tabla vieja sku_mapeo automáticamente.
    Si quieres poblarla, usa el endpoint /admin/auto_mapeo_v2 que trae
    todas las publicaciones de cada marketplace y las inserta aquí.
    """
    conn = get_conn(); cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sku_mapeo_canal (
            id              SERIAL PRIMARY KEY,
            sku_lusync      TEXT NOT NULL,
            canal           TEXT NOT NULL,
            sku_canal       TEXT NOT NULL,
            item_id_canal   TEXT,
            es_catalogo     BOOLEAN DEFAULT FALSE,
            activo          BOOLEAN DEFAULT TRUE,
            creado_at       TIMESTAMP DEFAULT NOW(),
            actualizado_at  TIMESTAMP DEFAULT NOW(),
            notas           TEXT
        )
    """)
    # Índices únicos: una publicación de un canal solo puede mapear a un SKU Lusync
    # Permitimos que item_id_canal sea NULL (para canales que no usan item_id, como Walmart/Falabella)
    cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_smc_unique_item
        ON sku_mapeo_canal(canal, item_id_canal)
        WHERE item_id_canal IS NOT NULL
    """)
    # Si no hay item_id, prevenimos duplicados por (canal, sku_canal, sku_lusync)
    cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_smc_unique_sku
        ON sku_mapeo_canal(canal, sku_canal, sku_lusync)
        WHERE item_id_canal IS NULL
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_smc_lusync ON sku_mapeo_canal(sku_lusync, canal)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_smc_sku_canal ON sku_mapeo_canal(canal, sku_canal)")
    conn.commit(); cur.close(); conn.close()


def obtener_publicaciones_canal(sku_lusync, canal):
    """Devuelve TODAS las publicaciones de un SKU Lusync en un canal específico.

    Args:
        sku_lusync: el SKU local de Lusync (ej: "ODJM001")
        canal: 'mercadolibre' | 'paris' | 'walmart' | 'falabella' | 'ripley' | 'web' | 'hites'

    Returns:
        list[dict] con: {id, sku_canal, item_id_canal, es_catalogo, activo}
        Lista VACÍA si no hay mapeos.

    Esta función reemplaza a get_sku_canal() que devolvía solo un string.
    Las funciones actualizar_stock_X / actualizar_precio_X deben loopear por estas.
    """
    init_sku_mapeo_canal()
    canal = (canal or "").lower().strip()
    conn = get_conn(); cur = conn.cursor()
    cur.execute("""
        SELECT id, sku_canal, item_id_canal, es_catalogo, activo
        FROM sku_mapeo_canal
        WHERE sku_lusync = %s AND canal = %s AND activo = TRUE
        ORDER BY id
    """, (sku_lusync, canal))
    filas = cur.fetchall()
    cur.close(); conn.close()
    return [{
        "id": r[0],
        "sku_canal": r[1],
        "item_id_canal": r[2],
        "es_catalogo": r[3],
        "activo": r[4]
    } for r in filas]


def obtener_sku_lusync_por_canal(canal, sku_canal=None, item_id_canal=None):
    """Operación inversa: dado un SKU del marketplace o item_id, encuentra el SKU Lusync.
    Útil para webhooks y sync de órdenes.

    Args:
        canal: 'mercadolibre' | 'paris' | etc
        sku_canal: SKU del marketplace (opcional si se pasa item_id_canal)
        item_id_canal: item_id del marketplace (preferido cuando existe)

    Returns:
        str con el SKU Lusync, o None si no hay match.
    """
    if not (sku_canal or item_id_canal):
        return None
    init_sku_mapeo_canal()
    canal = (canal or "").lower().strip()
    conn = get_conn(); cur = conn.cursor()

    # Prioridad 1: buscar por item_id_canal (más específico)
    if item_id_canal:
        cur.execute("""
            SELECT sku_lusync FROM sku_mapeo_canal
            WHERE canal = %s AND item_id_canal = %s AND activo = TRUE
            LIMIT 1
        """, (canal, str(item_id_canal)))
        r = cur.fetchone()
        if r:
            cur.close(); conn.close()
            return r[0]

    # Prioridad 2: buscar por sku_canal
    if sku_canal:
        cur.execute("""
            SELECT sku_lusync FROM sku_mapeo_canal
            WHERE canal = %s AND sku_canal = %s AND activo = TRUE
            LIMIT 1
        """, (canal, sku_canal))
        r = cur.fetchone()
        if r:
            cur.close(); conn.close()
            return r[0]

    cur.close(); conn.close()
    return None


def agregar_publicacion(sku_lusync, canal, sku_canal, item_id_canal=None,
                        es_catalogo=False, notas=None):
    """Registra una publicación de un SKU Lusync en un canal.

    Si ya existe (canal + item_id_canal) o (canal + sku_canal + sku_lusync),
    actualiza el registro existente en vez de duplicar.

    Returns:
        int (id de la fila) o None si falló.
    """
    init_sku_mapeo_canal()
    canal = (canal or "").lower().strip()
    conn = get_conn(); cur = conn.cursor()

    try:
        # Primero verificar si ya existe (evitar conflicto de UNIQUE)
        if item_id_canal:
            cur.execute("""
                SELECT id FROM sku_mapeo_canal
                WHERE canal = %s AND item_id_canal = %s
            """, (canal, str(item_id_canal)))
        else:
            cur.execute("""
                SELECT id FROM sku_mapeo_canal
                WHERE canal = %s AND sku_canal = %s AND sku_lusync = %s
                  AND item_id_canal IS NULL
            """, (canal, sku_canal, sku_lusync))
        existe = cur.fetchone()

        if existe:
            mapeo_id = existe[0]
            cur.execute("""
                UPDATE sku_mapeo_canal
                SET sku_lusync = %s, sku_canal = %s, es_catalogo = %s,
                    activo = TRUE, actualizado_at = NOW(),
                    notas = COALESCE(%s, notas)
                WHERE id = %s
            """, (sku_lusync, sku_canal, bool(es_catalogo), notas, mapeo_id))
        else:
            cur.execute("""
                INSERT INTO sku_mapeo_canal
                (sku_lusync, canal, sku_canal, item_id_canal, es_catalogo, notas)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (sku_lusync, canal, sku_canal,
                  str(item_id_canal) if item_id_canal else None,
                  bool(es_catalogo), notas))
            mapeo_id = cur.fetchone()[0]

        conn.commit()
        cur.close(); conn.close()
        return mapeo_id
    except Exception as e:
        try: conn.rollback()
        except: pass
        try: cur.close()
        except: pass
        try: conn.close()
        except: pass
        print(f"[sku_mapeo_canal] Error agregando publicación: {e}")
        return None


def eliminar_publicacion(mapeo_id):
    """Marca como inactiva una publicación (soft delete)."""
    init_sku_mapeo_canal()
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("UPDATE sku_mapeo_canal SET activo = FALSE, actualizado_at = NOW() WHERE id = %s",
                    (mapeo_id,))
        conn.commit()
        cur.close(); conn.close()
        return True
    except Exception as e:
        try: conn.rollback()
        except: pass
        try: cur.close()
        except: pass
        try: conn.close()
        except: pass
        return False


def listar_mapeos_canal(canal=None, sku_lusync=None, solo_activos=True):
    """Lista los mapeos canal. Filtra opcionalmente por canal y/o sku_lusync."""
    init_sku_mapeo_canal()
    conn = get_conn(); cur = conn.cursor()
    where_clauses = []
    params = []
    if canal:
        where_clauses.append("canal = %s")
        params.append(canal.lower().strip())
    if sku_lusync:
        where_clauses.append("sku_lusync = %s")
        params.append(sku_lusync)
    if solo_activos:
        where_clauses.append("activo = TRUE")
    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    cur.execute(f"""
        SELECT id, sku_lusync, canal, sku_canal, item_id_canal,
               es_catalogo, activo,
               TO_CHAR(creado_at, 'DD/MM/YYYY HH24:MI'),
               TO_CHAR(actualizado_at, 'DD/MM/YYYY HH24:MI'),
               COALESCE(notas, '')
        FROM sku_mapeo_canal {where_sql}
        ORDER BY sku_lusync, canal, id
    """, tuple(params))
    rows = cur.fetchall()
    cur.close(); conn.close()
    return [{
        "id": r[0], "sku_lusync": r[1], "canal": r[2],
        "sku_canal": r[3], "item_id_canal": r[4],
        "es_catalogo": r[5], "activo": r[6],
        "creado_at": r[7], "actualizado_at": r[8],
        "notas": r[9]
    } for r in rows]


def contar_publicaciones_por_sku():
    """Devuelve un dict {sku_lusync: {canal: cantidad}} para mostrar en UI."""
    init_sku_mapeo_canal()
    conn = get_conn(); cur = conn.cursor()
    cur.execute("""
        SELECT sku_lusync, canal, COUNT(*)
        FROM sku_mapeo_canal
        WHERE activo = TRUE
        GROUP BY sku_lusync, canal
    """)
    resultado = {}
    for sku, canal, cnt in cur.fetchall():
        resultado.setdefault(sku, {})[canal] = cnt
    cur.close(); conn.close()
    return resultado


# Llamar init al cargar el módulo (idempotente)
try:
    init_sku_mapeo_canal()
except Exception as e:
    print(f"[inventario] No se pudo inicializar sku_mapeo_canal: {e}")

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
                   CASE LOWER(TRIM(canal))
                     WHEN 'mercadolibre' THEN 'MercadoLibre'
                     WHEN 'meli'         THEN 'MercadoLibre'
                     WHEN 'falabella'    THEN 'Falabella'
                     WHEN 'paris'        THEN 'Paris'
                     WHEN 'ripley'       THEN 'Ripley'
                     WHEN 'walmart'      THEN 'Walmart'
                     WHEN 'woocommerce'  THEN 'Web'
                     WHEN 'web'          THEN 'Web'
                     WHEN 'woo'          THEN 'Web'
                     ELSE INITCAP(canal)
                   END AS canal_norm,
                   COALESCE(SUM(cantidad), 0) AS total
            FROM movimientos
            WHERE tipo = 'salida'
              AND DATE(fecha) BETWEEN %s AND %s
              AND canal IS NOT NULL
              AND LOWER(TRIM(canal)) NOT IN ('manual', 'sistema', '')
            GROUP BY dia, canal_norm
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
    ("CENTRAL",          "Bodega Central",            "propia",      None),
    ("MELI_FULL",        "MercadoLibre Full",         "fulfillment", "mercadolibre"),
    ("MELI_FULL_TRANSITO","En camino a MELI Full",    "transito",    "mercadolibre"),
    ("PARIS_CD",         "París Fulfillment",         "fulfillment", "paris"),
    ("WALMART_FBM",      "Walmart Fulfillment",       "fulfillment", "walmart"),
    ("FALABELLA_FBM",    "Falabella Fulfillment",     "fulfillment", "falabella"),
    ("RIPLEY_FBM",       "Ripley Fulfillment",        "fulfillment", "ripley"),
    ("HITES_FBM",        "Hites Fulfillment",         "fulfillment", "hites"),
    ("WOO_DROP",         "WooCommerce Dropship",      "dropship",    "woocommerce"),
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

        # Tabla de historial de importaciones de stock por bodega
        cur.execute("""CREATE TABLE IF NOT EXISTS bodegas_imports (
            id SERIAL PRIMARY KEY,
            archivo TEXT NOT NULL,
            usuario TEXT,
            estado TEXT DEFAULT 'procesando',
            total_filas INTEGER DEFAULT 0,
            procesados INTEGER DEFAULT 0,
            advertencias INTEGER DEFAULT 0,
            errores INTEGER DEFAULT 0,
            log TEXT,
            created_at TIMESTAMP DEFAULT NOW(),
            finalizado_at TIMESTAMP
        )""")

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
    """Suma o resta delta al stock de un SKU en una bodega. Retorna nuevo total.
    
    Importante: si la fila no existe, se inserta con max(0, delta).
    Si ya existe, se suma delta (puede ser negativo) y se aplica GREATEST(0,...)
    para no permitir stock negativo.
    
    BUG FIX (2026-05-06): Antes el EXCLUDED.cantidad usaba GREATEST(0, %s) en el VALUES,
    lo que convertía deltas negativos a 0 y NO descontaba stock al UPDATE.
    Ahora pasamos el delta REAL y aplicamos GREATEST solo en la lógica de upsert.
    """
    conn = get_conn(); cur = conn.cursor()
    nuevo = 0
    try:
        delta_int = int(delta)
        # Para INSERT inicial: si la fila no existe y delta es negativo, no podemos
        # tener stock negativo, así que insertamos 0 (después se sumará delta si llega más).
        # Para UPDATE: sumamos el delta REAL (puede ser negativo) al stock existente.
        
        valor_inicial = max(0, delta_int)  # 0 si delta es negativo, delta si positivo
        
        cur.execute("""
            INSERT INTO stock_bodega (sku, bodega_codigo, cantidad, actualizado_at)
            VALUES (%s, %s, %s, NOW())
            ON CONFLICT (sku, bodega_codigo)
            DO UPDATE SET cantidad = GREATEST(0, stock_bodega.cantidad + %s),
                          actualizado_at = NOW()
            RETURNING cantidad
        """, (sku, bodega_codigo, valor_inicial, delta_int))
        # Pasamos delta_int como segundo parámetro para el UPDATE.
        # Así el UPDATE suma el delta REAL (positivo o negativo).
        
        r = cur.fetchone()
        if r: nuevo = int(r[0] or 0)
        conn.commit()
        _recalcular_stock_total(sku)
    except Exception as e:
        print(f"[Bodegas] ajustar_stock_bodega ERROR sku={sku} bodega={bodega_codigo} delta={delta}: {e}")
        conn.rollback()
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
                                 motivo=None, usuario="Sistema",
                                 fecha_compra_marketplace=None,
                                 origen_registro="sync_manual"):
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
        fecha_compra_marketplace: datetime real de la compra en el marketplace
                                  (opcional, si se pasa se guarda en columna nueva).
                                  Debe venir en zona Chile (sin tzinfo) o con tzinfo
                                  (se convierte automáticamente).
        origen_registro: cómo entró el movimiento al sistema:
                         'sync_manual' (default), 'webhook', 'scheduler',
                         'manual', 'import_excel', 'devolucion', 'pos', 'sistema'.

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

    # Normalizar fecha de compra del marketplace (si llegó con tz, pasar a Chile sin tz)
    fecha_compra_clean = None
    if fecha_compra_marketplace:
        try:
            if hasattr(fecha_compra_marketplace, 'tzinfo') and fecha_compra_marketplace.tzinfo:
                fecha_compra_clean = fecha_compra_marketplace.astimezone(TZ_CHILE).replace(tzinfo=None)
            else:
                fecha_compra_clean = fecha_compra_marketplace
        except Exception as e:
            print(f"[Bodegas] No se pudo normalizar fecha_compra_marketplace: {e}")
            fecha_compra_clean = None

    # Registrar movimiento con la bodega y trazabilidad completa
    try:
        conn = get_conn(); cur = conn.cursor()
        # Asegurar columnas (idempotente)
        cur.execute("ALTER TABLE movimientos ADD COLUMN IF NOT EXISTS fecha_importacion TIMESTAMP")
        cur.execute("ALTER TABLE movimientos ADD COLUMN IF NOT EXISTS fecha_compra_marketplace TIMESTAMP")
        cur.execute("ALTER TABLE movimientos ADD COLUMN IF NOT EXISTS origen_registro TEXT DEFAULT 'sistema'")
        cur.execute("ALTER TABLE movimientos ADD COLUMN IF NOT EXISTS stock_antes INTEGER")
        cur.execute("ALTER TABLE movimientos ADD COLUMN IF NOT EXISTS stock_despues INTEGER")
        conn.commit()

        # Buscar nombre del producto
        cur.execute("SELECT nombre FROM productos WHERE sku=%s LIMIT 1", (sku,))
        r = cur.fetchone()
        nombre = r[0] if r else sku

        motivo_final = motivo or f"Venta {canal}{' (Fulfillment)' if fulfillment else ''}"

        ahora_chile = now_chile().replace(tzinfo=None)

        cur.execute("""INSERT INTO movimientos
            (tipo, sku, nombre, cantidad, motivo, usuario, canal, fecha, orden_id,
             bodega_codigo, fecha_importacion, fecha_compra_marketplace,
             origen_registro, stock_antes, stock_despues)
            VALUES ('salida', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (sku, nombre, descontar, motivo_final, usuario, canal,
             ahora_chile, orden_id, bodega, ahora_chile,
             fecha_compra_clean, origen_registro, stock_antes, stock_despues))
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


# ════════════════════════════════════════════════════════════════════════════
# AJUSTAR STOCK POR DEVOLUCIÓN (usado por endpoints de devoluciones avanzadas)
# ════════════════════════════════════════════════════════════════════════════
# Cuando se procesa una devolución (cliente devuelve un producto en buen estado),
# el stock vuelve a la bodega CENTRAL y se registra el movimiento.
#
# También suma al campo `stock` total del producto (legacy) para mantener
# consistencia con la lógica vieja del sistema.
# ════════════════════════════════════════════════════════════════════════════

def ajustar_stock_dev(sku, cantidad, dev_id, motivo_codigo="reintegro_buen_estado"):
    """Ajusta stock por devolución: suma a CENTRAL + registra movimiento.
    
    Args:
        sku: SKU Lusync del producto
        cantidad: cantidad a reintegrar (positiva)
        dev_id: ID de la devolución (para trazabilidad)
        motivo_codigo: código de motivo (reintegro_buen_estado, etc.)
    
    Returns:
        dict con {ok: bool, stock_anterior, stock_nuevo, mensaje}
    """
    try:
        cantidad = int(cantidad)
        if cantidad <= 0:
            return {"ok": False, "error": "cantidad debe ser > 0"}
        
        # ── 1. Actualizar stock_bodega CENTRAL (modelo nuevo) ──
        try:
            ajustar_stock_bodega(sku, "CENTRAL", cantidad)
        except Exception as e:
            print(f"[ajustar_stock_dev] Error stock_bodega CENTRAL: {e}")
        
        # ── 2. Actualizar campo stock del producto (modelo legacy compatible) ──
        conn = get_conn(); cur = conn.cursor()
        cur.execute("SELECT stock, nombre FROM productos WHERE sku=%s LIMIT 1", (sku,))
        r = cur.fetchone()
        if not r:
            cur.close(); conn.close()
            return {"ok": False, "error": f"SKU '{sku}' no encontrado"}
        
        stock_anterior = int(r[0] or 0)
        nombre = r[1] or sku
        stock_nuevo = stock_anterior + cantidad
        
        cur.execute("UPDATE productos SET stock=%s WHERE sku=%s", (stock_nuevo, sku))
        
        # ── 3. Registrar movimiento ──
        motivo_texto = {
            "reintegro_buen_estado": f"Devolución {dev_id} - reintegrado a stock",
            "reintegro_reparable":   f"Devolución {dev_id} - reparable reintegrado",
            "ajuste":                f"Devolución {dev_id} - ajuste de stock",
        }.get(motivo_codigo, f"Devolución {dev_id}")
        
        cur.execute("""INSERT INTO movimientos
            (tipo, sku, nombre, cantidad, motivo, usuario, canal, fecha, bodega_codigo)
            VALUES ('entrada', %s, %s, %s, %s, %s, %s, NOW(), %s)""",
            (sku, nombre, cantidad, motivo_texto, "Sistema (Devolución)", "Devolución", "CENTRAL"))
        
        conn.commit()
        cur.close(); conn.close()
        
        # ── 4. Sincronizar a los 6 marketplaces (resiliente) ──
        # Importamos el helper desde app.py si está disponible
        try:
            from app import sincronizar_stock_marketplaces
            sincronizar_stock_marketplaces(sku, stock_nuevo, contexto=f"devolucion_{dev_id}")
        except Exception as e_sync:
            print(f"[ajustar_stock_dev] Warning: no se pudo sincronizar marketplaces: {e_sync}")
        
        print(f"[ajustar_stock_dev] OK — {sku} +{cantidad} (dev {dev_id}) → stock {stock_anterior}→{stock_nuevo}")
        return {
            "ok": True,
            "stock_anterior": stock_anterior,
            "stock_nuevo": stock_nuevo,
            "mensaje": f"Reintegrado +{cantidad} a CENTRAL (stock: {stock_anterior}→{stock_nuevo})"
        }
    except Exception as e:
        import traceback
        print(f"[ajustar_stock_dev] Error: {e}")
        print(traceback.format_exc())
        try: conn.rollback(); cur.close(); conn.close()
        except: pass
        return {"ok": False, "error": str(e)}


def detectar_fulfillment_meli(orden_data):
    """Detecta si una orden MercadoLibre es Full o Seller envía.
    AUTORITATIVO: consulta /shipments/{id} porque la orden no trae logistic_type."""
    try:
        # Path 1: campo directo (raro pero posible)
        if orden_data.get("fulfilled") is True:
            return True

        shipping = orden_data.get("shipping", {}) or {}
        logistic_type = (shipping.get("logistic_type") or "").lower()
        if logistic_type == "fulfillment":
            return True

        # Path 2: tags
        tags = orden_data.get("tags", []) or []
        if "fulfillment" in tags or "fbm" in tags:
            return True

        # Path 3 (AUTORITATIVO): consultar /shipments/{id}
        shipping_id = shipping.get("id")
        if shipping_id:
            try:
                from mercadolibre import meli_headers, MELI_API_URL
                import requests as _req
                res = _req.get(f"{MELI_API_URL}/shipments/{shipping_id}",
                               headers=meli_headers(), timeout=10)
                if res.status_code == 200:
                    ship = res.json()
                    if (ship.get("logistic_type") or "").lower() == "fulfillment":
                        return True
            except Exception as e:
                print(f"[inventario] No se pudo consultar shipment {shipping_id}: {e}")
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


def actualizar_nombres_bodegas():
    """Sobrescribe los nombres/tipos/canales de bodegas existentes con los valores
    actuales de BODEGAS_DEFAULT. NO toca el stock ni borra bodegas custom.
    Útil cuando se renombra una bodega (ej: Paris CrossDocking → Paris Fulfillment)."""
    conn = get_conn(); cur = conn.cursor()
    actualizadas = []
    try:
        for i, (codigo, nombre, tipo, canal) in enumerate(BODEGAS_DEFAULT):
            cur.execute("""UPDATE bodegas
                          SET nombre=%s, tipo=%s, canal=%s, orden=%s
                          WHERE codigo=%s
                          RETURNING codigo, nombre""",
                       (nombre, tipo, canal, i, codigo))
            r = cur.fetchone()
            if r:
                actualizadas.append({"codigo": r[0], "nombre": r[1]})
        conn.commit()
    except Exception as e:
        print(f"[Bodegas] actualizar_nombres error: {e}"); conn.rollback()
    cur.close(); conn.close()
    return actualizadas


# ── HISTORIAL DE IMPORTACIONES DE STOCK ────────────────────────────────────

def crear_import_log(archivo, usuario, total_filas):
    """Registra el inicio de una importación. Devuelve el id."""
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("""INSERT INTO bodegas_imports
                       (archivo, usuario, estado, total_filas)
                       VALUES (%s, %s, 'procesando', %s) RETURNING id""",
                    (archivo, usuario, total_filas))
        id_ = cur.fetchone()[0]
        conn.commit()
        return id_
    except Exception as e:
        print(f"[bodegas_imports] error: {e}"); conn.rollback()
        return None
    finally:
        cur.close(); conn.close()


def actualizar_import_log(import_id, procesados=None, advertencias=None,
                          errores=None, estado=None, log=None):
    """Actualiza el progreso de una importación."""
    if not import_id: return
    conn = get_conn(); cur = conn.cursor()
    try:
        sets = []
        vals = []
        if procesados is not None: sets.append("procesados=%s"); vals.append(procesados)
        if advertencias is not None: sets.append("advertencias=%s"); vals.append(advertencias)
        if errores is not None: sets.append("errores=%s"); vals.append(errores)
        if estado is not None: sets.append("estado=%s"); vals.append(estado)
        if log is not None: sets.append("log=%s"); vals.append(log)
        if estado in ("ok", "error", "advertencias"):
            sets.append("finalizado_at=NOW()")
        if sets:
            vals.append(import_id)
            cur.execute(f"UPDATE bodegas_imports SET {', '.join(sets)} WHERE id=%s", tuple(vals))
            conn.commit()
    except Exception as e:
        print(f"[bodegas_imports] update error: {e}"); conn.rollback()
    finally:
        cur.close(); conn.close()


def listar_imports_recientes(limit=20):
    """Lista las últimas importaciones."""
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("""SELECT id, archivo, usuario, estado, total_filas,
                              procesados, advertencias, errores, created_at, finalizado_at
                       FROM bodegas_imports
                       ORDER BY id DESC LIMIT %s""", (limit,))
        rows = cur.fetchall()
        return [{
            "id": r[0], "archivo": r[1], "usuario": r[2], "estado": r[3],
            "total_filas": r[4], "procesados": r[5], "advertencias": r[6],
            "errores": r[7], "created_at": r[8].isoformat() if r[8] else None,
            "finalizado_at": r[9].isoformat() if r[9] else None
        } for r in rows]
    except Exception as e:
        print(f"[bodegas_imports] listar error: {e}")
        return []
    finally:
        cur.close(); conn.close()


def obtener_import_log(import_id):
    """Devuelve el detalle completo de una importación incluyendo el log."""
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("""SELECT id, archivo, usuario, estado, total_filas,
                              procesados, advertencias, errores, log,
                              created_at, finalizado_at
                       FROM bodegas_imports WHERE id=%s""", (import_id,))
        r = cur.fetchone()
        if not r: return None
        return {
            "id": r[0], "archivo": r[1], "usuario": r[2], "estado": r[3],
            "total_filas": r[4], "procesados": r[5], "advertencias": r[6],
            "errores": r[7], "log": r[8],
            "created_at": r[9].isoformat() if r[9] else None,
            "finalizado_at": r[10].isoformat() if r[10] else None
        }
    except Exception as e:
        print(f"[bodegas_imports] detalle error: {e}")
        return None
    finally:
        cur.close(); conn.close()
