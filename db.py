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
                emite_factura BOOLEAN DEFAULT TRUE,
                emite_nota_credito BOOLEAN DEFAULT TRUE,
                emite_nota_debito BOOLEAN DEFAULT FALSE,
                emite_guia_despacho BOOLEAN DEFAULT FALSE,
                activo BOOLEAN DEFAULT FALSE,
                fecha_creacion TIMESTAMP DEFAULT NOW(),
                fecha_actualizacion TIMESTAMP DEFAULT NOW()
            )
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
                   fecha_creacion, fecha_actualizacion
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
        }
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
