"""
Gestión de certificados digitales (.pfx / PKCS#12) para firma DTE.

Flujo:
    1. Cliente sube .pfx + password vía form
    2. validar_pfx() lee metadata (RUT, expiración, titular) — NO altera el binario
    3. subir_certificado() encripta pfx + password con Fernet → guarda en BD
    4. obtener_certificado() desencripta cuando se va a firmar un DTE (Fase 2)

Seguridad:
    - El .pfx se guarda como BYTEA encriptado con Fernet
    - El password va encriptado en TEXT (no hasheado, necesitamos recuperarlo)
    - La key Fernet está en env: LUSYNC_FERNET_KEY
    - Cuando se firma un DTE, se desencripta a memoria, NUNCA a disco
    - Auditoría: cada uso queda en audit_log
"""

import os
import base64
from datetime import datetime
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.hazmat.primitives import hashes
from cryptography.x509.oid import NameOID


# ─────────────────────────────────────────────────────────────────────────────
# FERNET — encriptación simétrica
# ─────────────────────────────────────────────────────────────────────────────
def _get_fernet():
    """Obtiene una instancia de Fernet con la KEY del env.
    Si no hay key, genera una temporal en memoria (modo dev) y avisa.
    """
    key = os.environ.get("LUSYNC_FERNET_KEY")
    if not key:
        # Modo dev: generar temporal en memoria (NO recomendado en producción)
        if not hasattr(_get_fernet, "_temp_key"):
            _get_fernet._temp_key = Fernet.generate_key().decode()
            print(f"⚠ [Facturación] LUSYNC_FERNET_KEY no configurada — usando temporal: {_get_fernet._temp_key}")
            print("⚠ [Facturación] Configura la variable en Render para que sobreviva reinicios.")
        key = _get_fernet._temp_key
    if isinstance(key, str):
        key = key.encode()
    return Fernet(key)


def _encriptar_bytes(data_bytes):
    """Encripta bytes con Fernet, devuelve bytes encriptados."""
    return _get_fernet().encrypt(data_bytes)


def _desencriptar_bytes(data_encriptada):
    """Desencripta bytes Fernet, devuelve bytes originales."""
    if isinstance(data_encriptada, memoryview):
        data_encriptada = bytes(data_encriptada)
    return _get_fernet().decrypt(data_encriptada)


def _encriptar_texto(texto):
    """Encripta string, devuelve string base64 (apto para TEXT en PG)."""
    return _get_fernet().encrypt(texto.encode("utf-8")).decode("utf-8")


def _desencriptar_texto(texto_encriptado):
    """Desencripta string base64, devuelve string original."""
    return _get_fernet().decrypt(texto_encriptado.encode("utf-8")).decode("utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# VALIDACIÓN .pfx — leer metadata sin almacenar
# ─────────────────────────────────────────────────────────────────────────────
def validar_pfx(pfx_bytes, password):
    """Valida y extrae metadata de un certificado .pfx (PKCS#12).

    Args:
        pfx_bytes: contenido binario del archivo .pfx
        password: password del .pfx (str)

    Returns:
        dict con: {ok, rut, titular, emisor, fecha_emision, fecha_expiracion, error}
    """
    try:
        if isinstance(password, str):
            password_bytes = password.encode("utf-8")
        else:
            password_bytes = password

        # Cargar PKCS#12: devuelve (private_key, cert, additional_certs)
        try:
            private_key, cert, _ = pkcs12.load_key_and_certificates(
                pfx_bytes, password_bytes
            )
        except ValueError as e:
            # Error típico: password incorrecto o archivo corrupto
            return {"ok": False, "error": f"No se pudo leer el .pfx. Verifica el password. ({str(e)[:100]})"}

        if cert is None:
            return {"ok": False, "error": "El archivo .pfx no contiene un certificado válido"}

        # Extraer metadata del cert
        subject = cert.subject
        issuer = cert.issuer

        # Buscar el RUT en el subject (puede venir como serialNumber o en CN)
        rut = None
        titular = None
        for attr in subject:
            oid_name = attr.oid._name if hasattr(attr.oid, "_name") else str(attr.oid)
            value = attr.value
            if "serialNumber" in oid_name.lower() or attr.oid.dotted_string == "2.5.4.5":
                rut = value
            elif attr.oid == NameOID.COMMON_NAME:
                titular = value

        # Fallback: buscar el RUT en email del subject
        if not rut:
            try:
                ext = cert.extensions.get_extension_for_class(
                    __import__("cryptography.x509", fromlist=["SubjectAlternativeName"]).SubjectAlternativeName
                )
                for nm in ext.value:
                    nm_str = str(nm.value)
                    # Heurística: RUT chileno en mail puede ser "12345678-K@dominio"
                    if "-" in nm_str and "@" in nm_str:
                        rut = nm_str.split("@")[0]
                        break
            except Exception:
                pass

        # Issuer (la entidad certificadora: E-Sign, E-Cert, etc)
        emisor_cert = None
        for attr in issuer:
            if attr.oid == NameOID.COMMON_NAME:
                emisor_cert = attr.value
                break
        if not emisor_cert:
            emisor_cert = "Desconocido"

        # Fechas — usar UTC para evitar confusión de timezones
        fecha_emision = cert.not_valid_before_utc.date() if hasattr(cert, 'not_valid_before_utc') else cert.not_valid_before.date()
        fecha_expiracion = cert.not_valid_after_utc.date() if hasattr(cert, 'not_valid_after_utc') else cert.not_valid_after.date()

        # Validaciones críticas
        hoy = datetime.utcnow().date()
        if fecha_expiracion < hoy:
            return {
                "ok": False,
                "error": f"Certificado VENCIDO (expiró el {fecha_expiracion.isoformat()}). Renuévalo antes de continuar.",
                "rut": rut, "titular": titular, "emisor": emisor_cert,
                "fecha_emision": fecha_emision.isoformat(),
                "fecha_expiracion": fecha_expiracion.isoformat(),
            }

        dias_restantes = (fecha_expiracion - hoy).days
        warning = None
        if dias_restantes < 30:
            warning = f"⚠ El certificado expira en {dias_restantes} días. Considera renovar pronto."

        return {
            "ok": True,
            "rut": rut,
            "titular": titular,
            "emisor": emisor_cert,
            "fecha_emision": fecha_emision.isoformat(),
            "fecha_expiracion": fecha_expiracion.isoformat(),
            "dias_restantes": dias_restantes,
            "warning": warning,
        }

    except Exception as e:
        return {"ok": False, "error": f"Error procesando certificado: {str(e)[:200]}"}


# ─────────────────────────────────────────────────────────────────────────────
# OPERACIONES DE BASE DE DATOS
# ─────────────────────────────────────────────────────────────────────────────
def subir_certificado(get_conn_func, release_conn_func, tenant_id, pfx_bytes,
                      password, nombre_archivo, activar=True):
    """Sube un certificado .pfx para un tenant. Encripta antes de guardar.

    Args:
        tenant_id: ID del tenant dueño
        pfx_bytes: binario del archivo .pfx
        password: password en claro (se encripta antes de guardar)
        nombre_archivo: nombre original del archivo (para mostrar en UI)
        activar: si True, desactiva los otros certificados del tenant

    Returns:
        dict: {ok, certificado_id, metadata, error}
    """
    # Validar primero
    metadata = validar_pfx(pfx_bytes, password)
    if not metadata.get("ok"):
        return {"ok": False, "error": metadata.get("error")}

    # Encriptar
    try:
        pfx_encriptado = _encriptar_bytes(pfx_bytes)
        password_encriptado = _encriptar_texto(password)
    except Exception as e:
        return {"ok": False, "error": f"Error encriptando: {str(e)[:200]}"}

    # Guardar
    conn = get_conn_func(); cur = conn.cursor()
    try:
        # Si vamos a activar, desactivar los demás
        if activar:
            cur.execute("""
                UPDATE facturacion_certificados
                SET activo = FALSE, fecha_desactivacion = NOW()
                WHERE tenant_id = %s AND activo = TRUE
            """, (tenant_id,))

        cur.execute("""
            INSERT INTO facturacion_certificados
              (tenant_id, nombre_archivo, pfx_encriptado, password_encriptado,
               rut_certificado, titular, emisor_cert, fecha_emision_cert,
               fecha_expiracion_cert, activo)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (
            tenant_id, nombre_archivo, pfx_encriptado, password_encriptado,
            metadata.get("rut"), metadata.get("titular"), metadata.get("emisor"),
            metadata.get("fecha_emision"), metadata.get("fecha_expiracion"),
            activar
        ))
        cert_id = cur.fetchone()[0]
        conn.commit()

        return {
            "ok": True,
            "certificado_id": cert_id,
            "metadata": {
                "rut": metadata.get("rut"),
                "titular": metadata.get("titular"),
                "emisor": metadata.get("emisor"),
                "fecha_emision": metadata.get("fecha_emision"),
                "fecha_expiracion": metadata.get("fecha_expiracion"),
                "dias_restantes": metadata.get("dias_restantes"),
                "warning": metadata.get("warning"),
            }
        }
    except Exception as e:
        conn.rollback()
        return {"ok": False, "error": f"Error BD: {str(e)[:200]}"}
    finally:
        cur.close()
        release_conn_func(conn)


def obtener_certificado(get_conn_func, release_conn_func, tenant_id, certificado_id=None):
    """Obtiene el certificado ACTIVO de un tenant (o uno específico si se pasa ID).
    DESENCRIPTA el .pfx y password — usar SOLO cuando se va a firmar.

    Returns:
        dict: {ok, pfx_bytes, password, metadata, error}
        IMPORTANTE: el .pfx desencriptado solo debe vivir en memoria, jamás escribir a disco.
    """
    conn = get_conn_func(); cur = conn.cursor()
    try:
        if certificado_id:
            cur.execute("""
                SELECT id, pfx_encriptado, password_encriptado, rut_certificado,
                       titular, fecha_expiracion_cert, nombre_archivo
                FROM facturacion_certificados
                WHERE tenant_id = %s AND id = %s
            """, (tenant_id, certificado_id))
        else:
            cur.execute("""
                SELECT id, pfx_encriptado, password_encriptado, rut_certificado,
                       titular, fecha_expiracion_cert, nombre_archivo
                FROM facturacion_certificados
                WHERE tenant_id = %s AND activo = TRUE
                ORDER BY fecha_subida DESC LIMIT 1
            """, (tenant_id,))

        r = cur.fetchone()
        if not r:
            return {"ok": False, "error": "No hay certificado activo para este tenant"}

        # Verificar no expirado
        if r[5] and r[5] < datetime.utcnow().date():
            return {"ok": False, "error": f"Certificado expirado el {r[5].isoformat()}"}

        # Desencriptar
        try:
            pfx_bytes = _desencriptar_bytes(r[1])
            password = _desencriptar_texto(r[2])
        except InvalidToken:
            return {"ok": False, "error": "No se pudo desencriptar — la LUSYNC_FERNET_KEY cambió o es inválida"}
        except Exception as e:
            return {"ok": False, "error": f"Error desencriptando: {str(e)[:200]}"}

        return {
            "ok": True,
            "certificado_id": r[0],
            "pfx_bytes": pfx_bytes,
            "password": password,
            "metadata": {
                "rut": r[3],
                "titular": r[4],
                "fecha_expiracion": r[5].isoformat() if r[5] else None,
                "nombre_archivo": r[6],
            }
        }
    finally:
        cur.close()
        release_conn_func(conn)


def listar_certificados_tenant(get_conn_func, release_conn_func, tenant_id):
    """Lista los certificados de un tenant SIN exponer el binario ni password.
    Para mostrar en UI: nombre, RUT, fechas, estado.
    Resiliente a tabla faltante.
    """
    conn = get_conn_func(); cur = conn.cursor()
    try:
        # Verificar que la tabla exista
        cur.execute("""
            SELECT 1 FROM information_schema.tables
            WHERE table_name = 'facturacion_certificados' LIMIT 1
        """)
        if not cur.fetchone():
            return []

        cur.execute("""
            SELECT id, nombre_archivo, rut_certificado, titular, emisor_cert,
                   fecha_emision_cert, fecha_expiracion_cert, activo,
                   fecha_subida, fecha_desactivacion
            FROM facturacion_certificados
            WHERE tenant_id = %s
            ORDER BY activo DESC, fecha_subida DESC
        """, (tenant_id,))

        certs = []
        for r in cur.fetchall():
            dias_para_expirar = None
            if r[6]:
                try:
                    dias_para_expirar = (r[6] - datetime.utcnow().date()).days
                except Exception:
                    dias_para_expirar = None
            certs.append({
                "id": r[0],
                "nombre_archivo": r[1],
                "rut": r[2],
                "titular": r[3],
                "emisor": r[4],
                "fecha_emision": r[5].isoformat() if r[5] else None,
                "fecha_expiracion": r[6].isoformat() if r[6] else None,
                "dias_para_expirar": dias_para_expirar,
                "activo": bool(r[7]),
                "fecha_subida": r[8].isoformat() if r[8] else None,
                "fecha_desactivacion": r[9].isoformat() if r[9] else None,
            })
        return certs
    except Exception as e:
        print(f"[Facturación] Error listar_certificados_tenant: {e}")
        return []
    finally:
        cur.close()
        release_conn_func(conn)


def eliminar_certificado(get_conn_func, release_conn_func, tenant_id, certificado_id):
    """Elimina un certificado del tenant. Si era el activo, no queda ninguno activo."""
    conn = get_conn_func(); cur = conn.cursor()
    try:
        cur.execute("""
            DELETE FROM facturacion_certificados
            WHERE tenant_id = %s AND id = %s
        """, (tenant_id, certificado_id))
        eliminado = cur.rowcount > 0
        conn.commit()
        return {"ok": eliminado, "mensaje": "Certificado eliminado" if eliminado else "No encontrado"}
    except Exception as e:
        conn.rollback()
        return {"ok": False, "error": str(e)}
    finally:
        cur.close()
        release_conn_func(conn)
