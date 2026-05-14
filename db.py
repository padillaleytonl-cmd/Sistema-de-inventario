"""
Gestión de tablas BD para facturación electrónica.

Tablas creadas:
  - facturacion_config_tenant: config por tenant (RUT, razón social, ambiente, etc)
  - facturacion_certificados:  certificados .pfx encriptados con Fernet
  - facturacion_cafs:          archivos CAF (folios autorizados SII)
  - facturacion_dtes:          DTEs emitidos (con XML firmado + estado SII)
  - facturacion_folios_consumidos: tracking de folios usados (para no repetir)

Todas las tablas tienen tenant_id NOT NULL y RLS forzado para multi-tenancy.
"""

import json
from datetime import datetime


def init_facturacion_tables(get_conn_func, release_conn_func=None, enable_rls_func=None):
    """Crea las tablas de facturación si no existen.

    Args:
        get_conn_func: función para obtener conexión (típicamente inventario.get_conn)
        release_conn_func: función para liberar (típicamente inventario.release_conn)
        enable_rls_func: función opcional para habilitar RLS en cada tabla
    """
    conn = get_conn_func(is_admin=True)  # admin para crear tablas globales
    cur = conn.cursor()

    try:
        # ─────────────────────────────────────────────────────────────────
        # 1. CONFIG por tenant: datos del emisor + ambiente + folios siguientes
        # ─────────────────────────────────────────────────────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS facturacion_config_tenant (
                tenant_id INTEGER PRIMARY KEY,
                rut_emisor TEXT NOT NULL,
                razon_social TEXT NOT NULL,
                giro TEXT,
                direccion TEXT,
                comuna TEXT,
                ciudad TEXT,
                telefono TEXT,
                email TEXT,
                resolucion_sii_fecha DATE,
                resolucion_sii_numero INTEGER,
                ambiente TEXT DEFAULT 'certificacion',
                emite_boleta BOOLEAN DEFAULT TRUE,
                emite_boleta_exenta BOOLEAN DEFAULT FALSE,
                emite_factura BOOLEAN DEFAULT TRUE,
                emite_factura_exenta BOOLEAN DEFAULT FALSE,
                emite_factura_compra BOOLEAN DEFAULT FALSE,
                emite_liquidacion BOOLEAN DEFAULT FALSE,
                emite_nota_credito BOOLEAN DEFAULT TRUE,
                emite_nota_debito BOOLEAN DEFAULT FALSE,
                emite_guia_despacho BOOLEAN DEFAULT FALSE,
                emite_fact_exportacion BOOLEAN DEFAULT FALSE,
                emite_nc_exportacion BOOLEAN DEFAULT FALSE,
                emite_nd_exportacion BOOLEAN DEFAULT FALSE,
                activo BOOLEAN DEFAULT FALSE,
                fecha_creacion TIMESTAMP DEFAULT NOW(),
                fecha_actualizacion TIMESTAMP DEFAULT NOW()
            )
        """)

        # Migración para tablas existentes: agregar columnas nuevas si no están
        cur.execute("""
            DO $$
            BEGIN
              BEGIN ALTER TABLE facturacion_config_tenant ADD COLUMN emite_boleta_exenta BOOLEAN DEFAULT FALSE; EXCEPTION WHEN duplicate_column THEN NULL; END;
              BEGIN ALTER TABLE facturacion_config_tenant ADD COLUMN emite_factura_exenta BOOLEAN DEFAULT FALSE; EXCEPTION WHEN duplicate_column THEN NULL; END;
              BEGIN ALTER TABLE facturacion_config_tenant ADD COLUMN emite_factura_compra BOOLEAN DEFAULT FALSE; EXCEPTION WHEN duplicate_column THEN NULL; END;
              BEGIN ALTER TABLE facturacion_config_tenant ADD COLUMN emite_liquidacion BOOLEAN DEFAULT FALSE; EXCEPTION WHEN duplicate_column THEN NULL; END;
              BEGIN ALTER TABLE facturacion_config_tenant ADD COLUMN emite_fact_exportacion BOOLEAN DEFAULT FALSE; EXCEPTION WHEN duplicate_column THEN NULL; END;
              BEGIN ALTER TABLE facturacion_config_tenant ADD COLUMN emite_nc_exportacion BOOLEAN DEFAULT FALSE; EXCEPTION WHEN duplicate_column THEN NULL; END;
              BEGIN ALTER TABLE facturacion_config_tenant ADD COLUMN emite_nd_exportacion BOOLEAN DEFAULT FALSE; EXCEPTION WHEN duplicate_column THEN NULL; END;
            END $$;
        """)

        # Forzar TRUE en DTEs gratuitos (boleta 39 + NC 61) para tenants existentes
        # Estos son gratis y deben estar siempre activos por defecto
        cur.execute("""
            UPDATE facturacion_config_tenant
            SET emite_boleta = TRUE, emite_nota_credito = TRUE
            WHERE emite_boleta IS FALSE OR emite_nota_credito IS FALSE
                OR emite_boleta IS NULL OR emite_nota_credito IS NULL
        """)

        # ─────────────────────────────────────────────────────────────────
        # 2. CERTIFICADOS .pfx encriptados (uno por tenant, pueden tener varios pero solo 1 activo)
        # ─────────────────────────────────────────────────────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS facturacion_certificados (
                id SERIAL PRIMARY KEY,
                tenant_id INTEGER NOT NULL,
                nombre_archivo TEXT NOT NULL,
                pfx_encriptado BYTEA NOT NULL,
                password_encriptado TEXT NOT NULL,
                rut_certificado TEXT,
                titular TEXT,
                emisor_cert TEXT,
                fecha_emision_cert DATE,
                fecha_expiracion_cert DATE NOT NULL,
                activo BOOLEAN DEFAULT FALSE,
                fecha_subida TIMESTAMP DEFAULT NOW(),
                fecha_desactivacion TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_facturacion_cert_tenant
            ON facturacion_certificados(tenant_id, activo)
        """)

        # ─────────────────────────────────────────────────────────────────
        # 3. CAFs (Códigos de Autorización de Folios) — XMLs del SII
        # ─────────────────────────────────────────────────────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS facturacion_cafs (
                id SERIAL PRIMARY KEY,
                tenant_id INTEGER NOT NULL,
                tipo_dte INTEGER NOT NULL,
                folio_desde INTEGER NOT NULL,
                folio_hasta INTEGER NOT NULL,
                folio_actual INTEGER NOT NULL,
                xml_caf TEXT NOT NULL,
                rut_emisor_caf TEXT NOT NULL,
                fecha_autorizacion DATE NOT NULL,
                ambiente TEXT DEFAULT 'certificacion',
                agotado BOOLEAN DEFAULT FALSE,
                fecha_subida TIMESTAMP DEFAULT NOW(),
                fecha_agotamiento TIMESTAMP,
                CONSTRAINT facturacion_cafs_rango_valido CHECK (folio_hasta >= folio_desde),
                CONSTRAINT facturacion_cafs_actual_en_rango CHECK (folio_actual >= folio_desde AND folio_actual <= folio_hasta + 1)
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_facturacion_caf_tenant_tipo
            ON facturacion_cafs(tenant_id, tipo_dte, agotado)
        """)

        # ─────────────────────────────────────────────────────────────────
        # 4. DTEs emitidos: histórico completo
        # ─────────────────────────────────────────────────────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS facturacion_dtes (
                id SERIAL PRIMARY KEY,
                tenant_id INTEGER NOT NULL,
                tipo_dte INTEGER NOT NULL,
                folio INTEGER NOT NULL,
                rut_receptor TEXT,
                razon_social_receptor TEXT,
                monto_neto NUMERIC(14, 0) DEFAULT 0,
                monto_iva NUMERIC(14, 0) DEFAULT 0,
                monto_total NUMERIC(14, 0) NOT NULL,
                xml_firmado TEXT,
                ted_xml TEXT,
                pdf_base64 TEXT,
                estado TEXT DEFAULT 'pendiente',
                track_id_sii TEXT,
                estado_sii TEXT,
                glosa_sii TEXT,
                orden_id TEXT,
                canal TEXT,
                fecha_emision TIMESTAMP DEFAULT NOW(),
                fecha_envio_sii TIMESTAMP,
                fecha_aceptacion_sii TIMESTAMP,
                fecha_anulacion TIMESTAMP,
                motivo_anulacion TEXT,
                referencia_dte_id INTEGER,
                CONSTRAINT facturacion_dte_tenant_folio_tipo
                  UNIQUE (tenant_id, tipo_dte, folio)
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_facturacion_dte_tenant_fecha
            ON facturacion_dtes(tenant_id, fecha_emision DESC)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_facturacion_dte_orden
            ON facturacion_dtes(tenant_id, orden_id)
        """)

        # ─────────────────────────────────────────────────────────────────
        # 5. ACTIVACIONES de DTE: registro de cuándo se activó/desactivó cada tipo
        # Cobro: mes completo desde la activación, hasta fin del mes en que se desactiva
        # ─────────────────────────────────────────────────────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS facturacion_dte_activaciones (
                id SERIAL PRIMARY KEY,
                tenant_id INTEGER NOT NULL,
                tipo_dte INTEGER NOT NULL,
                fecha_activacion TIMESTAMP NOT NULL DEFAULT NOW(),
                fecha_desactivacion TIMESTAMP,
                precio_uf_al_activar NUMERIC(4, 2) NOT NULL,
                aceptado_por TEXT,
                ip_aceptacion TEXT,
                terminos_version TEXT DEFAULT 'v1',
                activo BOOLEAN DEFAULT TRUE
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_dte_activaciones_tenant
            ON facturacion_dte_activaciones(tenant_id, tipo_dte, activo)
        """)

        conn.commit()
        print("[Facturación] Tablas creadas/verificadas correctamente")

        # ─────────────────────────────────────────────────────────────────
        # 5. RLS (Row Level Security) por tenant
        # ─────────────────────────────────────────────────────────────────
        if enable_rls_func:
            tablas_facturacion = [
                "facturacion_config_tenant",
                "facturacion_certificados",
                "facturacion_cafs",
                "facturacion_dtes",
            ]
            for tabla in tablas_facturacion:
                try:
                    enable_rls_func(tabla)
                    print(f"[Facturación] RLS habilitado en {tabla}")
                except Exception as e:
                    print(f"[Facturación] No se pudo habilitar RLS en {tabla}: {e}")

    except Exception as e:
        conn.rollback()
        print(f"[Facturación] Error creando tablas: {e}")
        raise
    finally:
        cur.close()
        if release_conn_func:
            release_conn_func(conn)
        else:
            conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# CONFIG por tenant
# ─────────────────────────────────────────────────────────────────────────────
def obtener_config_facturacion(get_conn_func, release_conn_func, tenant_id):
    """Obtiene la config de facturación de un tenant. Devuelve dict o None."""
    conn = get_conn_func(); cur = conn.cursor()
    try:
        cur.execute("""
            SELECT rut_emisor, razon_social, giro, direccion, comuna, ciudad,
                   telefono, email, resolucion_sii_fecha, resolucion_sii_numero,
                   ambiente, emite_boleta, emite_factura, emite_nota_credito,
                   emite_nota_debito, emite_guia_despacho, activo,
                   fecha_creacion, fecha_actualizacion,
                   COALESCE(emite_boleta_exenta, FALSE),
                   COALESCE(emite_factura_exenta, FALSE),
                   COALESCE(emite_factura_compra, FALSE),
                   COALESCE(emite_liquidacion, FALSE),
                   COALESCE(emite_fact_exportacion, FALSE),
                   COALESCE(emite_nc_exportacion, FALSE),
                   COALESCE(emite_nd_exportacion, FALSE)
            FROM facturacion_config_tenant
            WHERE tenant_id = %s
        """, (tenant_id,))
        r = cur.fetchone()
        if not r:
            return None
        return {
            "rut_emisor": r[0],
            "razon_social": r[1],
            "giro": r[2],
            "direccion": r[3],
            "comuna": r[4],
            "ciudad": r[5],
            "telefono": r[6],
            "email": r[7],
            "resolucion_sii_fecha": r[8].isoformat() if r[8] else None,
            "resolucion_sii_numero": r[9],
            "ambiente": r[10],
            "emite_boleta": r[11],
            "emite_factura": r[12],
            "emite_nota_credito": r[13],
            "emite_nota_debito": r[14],
            "emite_guia_despacho": r[15],
            "activo": r[16],
            "fecha_creacion": r[17].isoformat() if r[17] else None,
            "fecha_actualizacion": r[18].isoformat() if r[18] else None,
            "emite_boleta_exenta": r[19],
            "emite_factura_exenta": r[20],
            "emite_factura_compra": r[21],
            "emite_liquidacion": r[22],
            "emite_fact_exportacion": r[23],
            "emite_nc_exportacion": r[24],
            "emite_nd_exportacion": r[25],
        }
    finally:
        cur.close()
        release_conn_func(conn)


def calcular_mrr_tributario(get_conn_func, release_conn_func, tenant_id, uf_clp=None):
    """Calcula el costo mensual en UF por los DTEs activados del tenant.

    Args:
        uf_clp: valor UF en CLP. Si None, lo obtiene dinámicamente del cache/API.

    Returns:
        dict con:
            total_uf: suma de precios UF de DTEs activos
            total_clp_neto: equivalente en pesos (sin IVA)
            total_clp_iva: con IVA 19%
            uf_valor: valor UF usado en el cálculo
            uf_fecha: fecha del valor UF
            desglose: lista de {tipo_dte, nombre, precio_uf, activo}
    """
    from .utils import TIPOS_DTE, CAMPO_BD_A_TIPO_DTE
    from .uf import obtener_uf_actual

    config = obtener_config_facturacion(get_conn_func, release_conn_func, tenant_id)
    if not config:
        return {"total_uf": 0.0, "total_clp": 0, "total_clp_neto": 0,
                "total_clp_iva": 0, "iva_porcentaje": 19, "desglose": []}

    # Obtener UF dinámica si no se pasó
    uf_info = None
    if uf_clp is None:
        uf_info = obtener_uf_actual(get_conn_func, release_conn_func)
        uf_clp = uf_info["valor"]

    desglose = []
    total_uf = 0.0

    for campo_bd, tipo_dte in CAMPO_BD_A_TIPO_DTE.items():
        info = TIPOS_DTE.get(tipo_dte, {})
        precio = float(info.get("precio_uf", 0))
        activo = bool(config.get(campo_bd, False))
        desglose.append({
            "tipo_dte": tipo_dte,
            "nombre": info.get("nombre", f"Tipo {tipo_dte}"),
            "precio_uf": precio,
            "activo": activo,
            "campo_bd": campo_bd,
        })
        if activo:
            total_uf += precio

    # Redondeo correcto (los floats acumulan ruido)
    total_uf = round(total_uf, 2)
    total_clp_neto = int(round(total_uf * uf_clp))
    total_clp_iva = int(round(total_clp_neto * 1.19))
    result = {
        "total_uf": total_uf,
        "total_clp": total_clp_neto,      # backward compat
        "total_clp_neto": total_clp_neto,
        "total_clp_iva": total_clp_iva,
        "iva_porcentaje": 19,
        "uf_valor": float(uf_clp),
        "desglose": desglose,
    }
    if uf_info:
        result["uf_fecha"] = uf_info.get("fecha")
        result["uf_fuente"] = uf_info.get("fuente")
    return result


def registrar_activacion_dte(get_conn_func, release_conn_func, tenant_id,
                              tipo_dte, precio_uf, aceptado_por, ip_aceptacion,
                              terminos_version="v1"):
    """Registra que un cliente aceptó los términos y activó un tipo de DTE.

    Reglas comerciales:
        - Si ya existe activación activa para este tenant+tipo, no duplica (es no-op)
        - Si hay desactivación previa, crea nueva fila (historia)
        - Se cobra mes completo independiente de cuándo se activó/desactivó
    """
    conn = get_conn_func(); cur = conn.cursor()
    try:
        # ¿Ya tiene una activa? (no duplicar)
        cur.execute("""
            SELECT id FROM facturacion_dte_activaciones
            WHERE tenant_id = %s AND tipo_dte = %s AND activo = TRUE
        """, (tenant_id, tipo_dte))
        if cur.fetchone():
            return {"ok": True, "ya_activo": True}

        cur.execute("""
            INSERT INTO facturacion_dte_activaciones
              (tenant_id, tipo_dte, precio_uf_al_activar, aceptado_por,
               ip_aceptacion, terminos_version, activo)
            VALUES (%s, %s, %s, %s, %s, %s, TRUE)
            RETURNING id, fecha_activacion
        """, (tenant_id, tipo_dte, precio_uf, aceptado_por, ip_aceptacion, terminos_version))
        r = cur.fetchone()
        conn.commit()
        return {
            "ok": True,
            "activacion_id": r[0],
            "fecha_activacion": r[1].isoformat() if r[1] else None,
        }
    except Exception as e:
        conn.rollback()
        return {"ok": False, "error": str(e)[:200]}
    finally:
        cur.close()
        release_conn_func(conn)


def registrar_desactivacion_dte(get_conn_func, release_conn_func, tenant_id, tipo_dte):
    """Marca la activación como desactivada. NO elimina, mantiene historia.

    IMPORTANTE: el cobro sigue hasta fin de mes. Esto solo registra la fecha
    para que el ciclo de cobro mensual sepa que no debe renovar el cargo.
    """
    conn = get_conn_func(); cur = conn.cursor()
    try:
        cur.execute("""
            UPDATE facturacion_dte_activaciones
            SET activo = FALSE, fecha_desactivacion = NOW()
            WHERE tenant_id = %s AND tipo_dte = %s AND activo = TRUE
        """, (tenant_id, tipo_dte))
        afectadas = cur.rowcount
        conn.commit()
        return {"ok": True, "desactivadas": afectadas}
    except Exception as e:
        conn.rollback()
        return {"ok": False, "error": str(e)[:200]}
    finally:
        cur.close()
        release_conn_func(conn)


def obtener_historial_activaciones(get_conn_func, release_conn_func, tenant_id):
    """Devuelve histórico de activaciones del tenant (para auditoría / mostrar al cliente)."""
    from .utils import TIPOS_DTE
    conn = get_conn_func(); cur = conn.cursor()
    try:
        cur.execute("""
            SELECT id, tipo_dte, fecha_activacion, fecha_desactivacion,
                   precio_uf_al_activar, aceptado_por, activo, terminos_version
            FROM facturacion_dte_activaciones
            WHERE tenant_id = %s
            ORDER BY fecha_activacion DESC
        """, (tenant_id,))
        rows = cur.fetchall()
        result = []
        for r in rows:
            info = TIPOS_DTE.get(r[1], {})
            result.append({
                "id": r[0],
                "tipo_dte": r[1],
                "nombre": info.get("nombre", f"Tipo {r[1]}"),
                "fecha_activacion": r[2].isoformat() if r[2] else None,
                "fecha_desactivacion": r[3].isoformat() if r[3] else None,
                "precio_uf_al_activar": float(r[4]) if r[4] else 0.0,
                "aceptado_por": r[5],
                "activo": r[6],
                "terminos_version": r[7],
            })
        return result
    finally:
        cur.close()
        release_conn_func(conn)


def guardar_config_facturacion(get_conn_func, release_conn_func, tenant_id, data):
    """Crea o actualiza config de facturación. UPSERT por tenant_id.

    Args:
        data: dict con campos opcionales: rut_emisor, razon_social, giro, direccion,
              comuna, ciudad, telefono, email, resolucion_sii_fecha, resolucion_sii_numero,
              ambiente, emite_boleta, emite_factura, emite_nota_credito,
              emite_nota_debito, emite_guia_despacho, activo
    """
    conn = get_conn_func(); cur = conn.cursor()
    try:
        # Verificar si ya existe
        cur.execute("SELECT 1 FROM facturacion_config_tenant WHERE tenant_id = %s", (tenant_id,))
        existe = cur.fetchone() is not None

        campos_actualizables = [
            "rut_emisor", "razon_social", "giro", "direccion", "comuna", "ciudad",
            "telefono", "email", "resolucion_sii_fecha", "resolucion_sii_numero",
            "ambiente", "emite_boleta", "emite_factura", "emite_nota_credito",
            "emite_nota_debito", "emite_guia_despacho", "activo",
            "emite_boleta_exenta", "emite_factura_exenta", "emite_factura_compra",
            "emite_liquidacion", "emite_fact_exportacion",
            "emite_nc_exportacion", "emite_nd_exportacion",
        ]

        if existe:
            # UPDATE solo de campos enviados en data
            sets, valores = [], []
            for c in campos_actualizables:
                if c in data:
                    sets.append(f"{c} = %s")
                    valores.append(data[c])
            if sets:
                sets.append("fecha_actualizacion = NOW()")
                valores.append(tenant_id)
                cur.execute(
                    f"UPDATE facturacion_config_tenant SET {', '.join(sets)} WHERE tenant_id = %s",
                    valores
                )
        else:
            # INSERT con valores por defecto
            cols, placeholders, valores = ["tenant_id"], ["%s"], [tenant_id]
            for c in campos_actualizables:
                if c in data:
                    cols.append(c)
                    placeholders.append("%s")
                    valores.append(data[c])
            cur.execute(
                f"INSERT INTO facturacion_config_tenant ({', '.join(cols)}) VALUES ({', '.join(placeholders)})",
                valores
            )

        conn.commit()
        return {"ok": True, "actualizado": existe}
    except Exception as e:
        conn.rollback()
        return {"ok": False, "error": str(e)}
    finally:
        cur.close()
        release_conn_func(conn)
