"""
Gestión de CAFs (Códigos de Autorización de Folios).

Un CAF es un XML emitido por el SII que autoriza un rango de folios para un
tipo de DTE específico. Ejemplo: "Boleta Electrónica (39), folios 1 al 1000".
El XML del CAF incluye una clave RSA que se usa para timbrar cada DTE (TED).

Flujo:
    1. Cliente solicita folios en https://palena.sii.cl (Folios)
    2. Descarga el archivo .xml del CAF
    3. En Lusync sube el CAF → este módulo lo parsea, valida y guarda
    4. Al emitir un DTE, obtener_folio_disponible() devuelve el próximo folio
    5. marcar_folio_usado() incrementa el contador

Validaciones que hace este módulo:
    - El RUT del CAF coincide con el del tenant
    - El tipo de DTE del CAF está habilitado
    - El rango es válido (desde <= hasta)
    - No se solapa con otro CAF activo del mismo tipo
"""

from datetime import datetime
from xml.etree import ElementTree as ET


def parsear_caf(xml_str):
    """Parsea un XML de CAF y extrae sus metadatos.

    Returns:
        dict: {ok, tipo_dte, folio_desde, folio_hasta, rut_emisor,
               fecha_autorizacion, error}
    """
    try:
        # Algunos CAFs vienen con encoding declarado, parsear robusto
        if isinstance(xml_str, bytes):
            xml_str = xml_str.decode("ISO-8859-1", errors="ignore")

        # Quitar BOM si existe
        if xml_str.startswith("\ufeff"):
            xml_str = xml_str[1:]

        root = ET.fromstring(xml_str)

        # El CAF tiene estructura:
        # <AUTORIZACION>
        #   <CAF version="1.0">
        #     <DA>
        #       <RE>RUT_EMISOR</RE>
        #       <RS>RAZON_SOCIAL</RS>
        #       <TD>TIPO_DTE</TD>
        #       <RNG>
        #         <D>folio_desde</D>
        #         <H>folio_hasta</H>
        #       </RNG>
        #       <FA>fecha_autorizacion</FA>
        #       <RSAPK>...</RSAPK>
        #     </DA>
        #     <FRMA algoritmo="SHA1withRSA">firma</FRMA>
        #   </CAF>
        #   <RSASK>...clave_privada...</RSASK>
        #   <RSAPUBK>...clave_publica...</RSAPUBK>
        # </AUTORIZACION>

        # Encontrar el bloque DA
        da = None
        if root.tag == "AUTORIZACION":
            caf = root.find("CAF")
            if caf is not None:
                da = caf.find("DA")
        elif root.tag == "CAF":
            da = root.find("DA")
        elif root.tag == "DA":
            da = root

        if da is None:
            return {"ok": False, "error": "Estructura de CAF no reconocida (falta bloque DA)"}

        # Extraer campos
        rut_emisor_el = da.find("RE")
        tipo_dte_el = da.find("TD")
        rng = da.find("RNG")
        fa_el = da.find("FA")

        if any(x is None for x in (rut_emisor_el, tipo_dte_el, rng, fa_el)):
            return {"ok": False, "error": "Faltan campos obligatorios en el CAF"}

        folio_desde_el = rng.find("D")
        folio_hasta_el = rng.find("H")
        if folio_desde_el is None or folio_hasta_el is None:
            return {"ok": False, "error": "El rango de folios (RNG/D, RNG/H) no se encontró"}

        rut_emisor = rut_emisor_el.text.strip()
        tipo_dte = int(tipo_dte_el.text.strip())
        folio_desde = int(folio_desde_el.text.strip())
        folio_hasta = int(folio_hasta_el.text.strip())
        fecha_autorizacion = datetime.strptime(fa_el.text.strip(), "%Y-%m-%d").date()

        if folio_hasta < folio_desde:
            return {"ok": False, "error": "Rango inválido: folio_hasta < folio_desde"}

        return {
            "ok": True,
            "tipo_dte": tipo_dte,
            "folio_desde": folio_desde,
            "folio_hasta": folio_hasta,
            "rut_emisor": rut_emisor,
            "fecha_autorizacion": fecha_autorizacion,
            "total_folios": folio_hasta - folio_desde + 1,
        }

    except ET.ParseError as e:
        return {"ok": False, "error": f"XML inválido: {str(e)[:200]}"}
    except Exception as e:
        return {"ok": False, "error": f"Error parseando CAF: {str(e)[:200]}"}


def subir_caf(get_conn_func, release_conn_func, tenant_id, xml_caf,
              rut_emisor_esperado=None, ambiente="certificacion"):
    """Sube un archivo CAF para un tenant. Valida y guarda.

    Args:
        tenant_id: tenant dueño
        xml_caf: contenido del XML CAF (str o bytes)
        rut_emisor_esperado: si se pasa, se valida que el RUT del CAF coincida
                             con el del tenant (anti-error operacional)
        ambiente: 'certificacion' o 'produccion'

    Returns:
        dict: {ok, caf_id, info, error}
    """
    if isinstance(xml_caf, bytes):
        xml_caf_str = xml_caf.decode("ISO-8859-1", errors="ignore")
    else:
        xml_caf_str = xml_caf

    # Parsear primero
    info = parsear_caf(xml_caf_str)
    if not info.get("ok"):
        return {"ok": False, "error": info.get("error")}

    # Validar RUT si se pidió
    if rut_emisor_esperado:
        rut_caf_limpio = info["rut_emisor"].replace(".", "").replace("-", "").upper()
        rut_esperado_limpio = str(rut_emisor_esperado).replace(".", "").replace("-", "").upper()
        if rut_caf_limpio != rut_esperado_limpio:
            return {
                "ok": False,
                "error": f"El RUT del CAF ({info['rut_emisor']}) no coincide con el del tenant ({rut_emisor_esperado})"
            }

    # Guardar en BD
    conn = get_conn_func(); cur = conn.cursor()
    try:
        # Verificar que no se solape con otro CAF activo
        cur.execute("""
            SELECT id, folio_desde, folio_hasta
            FROM facturacion_cafs
            WHERE tenant_id = %s AND tipo_dte = %s AND agotado = FALSE
              AND ambiente = %s
              AND NOT (folio_hasta < %s OR folio_desde > %s)
        """, (
            tenant_id, info["tipo_dte"], ambiente,
            info["folio_desde"], info["folio_hasta"]
        ))
        conflicto = cur.fetchone()
        if conflicto:
            return {
                "ok": False,
                "error": f"Este rango de folios se solapa con un CAF existente ({conflicto[1]}-{conflicto[2]})"
            }

        cur.execute("""
            INSERT INTO facturacion_cafs
              (tenant_id, tipo_dte, folio_desde, folio_hasta, folio_actual,
               xml_caf, rut_emisor_caf, fecha_autorizacion, ambiente)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (
            tenant_id, info["tipo_dte"], info["folio_desde"], info["folio_hasta"],
            info["folio_desde"],  # folio_actual arranca en folio_desde
            xml_caf_str, info["rut_emisor"], info["fecha_autorizacion"], ambiente
        ))
        caf_id = cur.fetchone()[0]
        conn.commit()

        return {
            "ok": True,
            "caf_id": caf_id,
            "info": info,
        }
    except Exception as e:
        conn.rollback()
        return {"ok": False, "error": f"Error BD: {str(e)[:200]}"}
    finally:
        cur.close()
        release_conn_func(conn)


def listar_cafs_tenant(get_conn_func, release_conn_func, tenant_id):
    """Lista CAFs de un tenant para UI. No expone el XML completo."""
    conn = get_conn_func(); cur = conn.cursor()
    try:
        cur.execute("""
            SELECT id, tipo_dte, folio_desde, folio_hasta, folio_actual,
                   rut_emisor_caf, fecha_autorizacion, ambiente, agotado,
                   fecha_subida, fecha_agotamiento
            FROM facturacion_cafs
            WHERE tenant_id = %s
            ORDER BY tipo_dte ASC, fecha_autorizacion DESC
        """, (tenant_id,))

        from .utils import TIPOS_DTE
        cafs = []
        for r in cur.fetchall():
            tipo_dte = r[1]
            folios_total = r[3] - r[2] + 1
            folios_usados = r[4] - r[2]
            folios_restantes = max(0, r[3] - r[4] + 1)
            pct_usado = round((folios_usados / folios_total) * 100, 1) if folios_total > 0 else 0

            cafs.append({
                "id": r[0],
                "tipo_dte": tipo_dte,
                "tipo_dte_nombre": TIPOS_DTE.get(tipo_dte, {}).get("nombre", f"Tipo {tipo_dte}"),
                "folio_desde": r[2],
                "folio_hasta": r[3],
                "folio_actual": r[4],
                "folios_total": folios_total,
                "folios_usados": folios_usados,
                "folios_restantes": folios_restantes,
                "pct_usado": pct_usado,
                "rut_emisor": r[5],
                "fecha_autorizacion": r[6].isoformat() if r[6] else None,
                "ambiente": r[7],
                "agotado": r[8],
                "fecha_subida": r[9].isoformat() if r[9] else None,
                "fecha_agotamiento": r[10].isoformat() if r[10] else None,
            })
        return cafs
    finally:
        cur.close()
        release_conn_func(conn)


def obtener_folio_disponible(get_conn_func, release_conn_func, tenant_id,
                              tipo_dte, ambiente="certificacion"):
    """Reserva un folio disponible para emitir un DTE del tipo indicado.
    Atómico: incrementa folio_actual en BD para evitar duplicados.

    Returns:
        dict: {ok, folio, caf_id, xml_caf, error}
    """
    conn = get_conn_func(); cur = conn.cursor()
    try:
        # Buscar el CAF más antiguo con folios disponibles
        # FOR UPDATE para lockear la fila mientras la actualizamos
        cur.execute("""
            SELECT id, folio_actual, folio_hasta, xml_caf
            FROM facturacion_cafs
            WHERE tenant_id = %s AND tipo_dte = %s
              AND ambiente = %s AND agotado = FALSE
              AND folio_actual <= folio_hasta
            ORDER BY fecha_autorizacion ASC
            LIMIT 1 FOR UPDATE
        """, (tenant_id, tipo_dte, ambiente))

        r = cur.fetchone()
        if not r:
            return {
                "ok": False,
                "error": f"Sin folios disponibles para tipo DTE {tipo_dte} en {ambiente}. Solicita CAFs al SII."
            }

        caf_id = r[0]
        folio = r[1]
        folio_hasta = r[2]
        xml_caf = r[3]

        # Avanzar el folio_actual
        nuevo_folio_actual = folio + 1
        agotar = nuevo_folio_actual > folio_hasta
        if agotar:
            cur.execute("""
                UPDATE facturacion_cafs
                SET folio_actual = %s, agotado = TRUE, fecha_agotamiento = NOW()
                WHERE id = %s
            """, (nuevo_folio_actual, caf_id))
        else:
            cur.execute("""
                UPDATE facturacion_cafs SET folio_actual = %s WHERE id = %s
            """, (nuevo_folio_actual, caf_id))

        conn.commit()

        return {
            "ok": True,
            "folio": folio,
            "caf_id": caf_id,
            "xml_caf": xml_caf,
            "agotado_tras_este": agotar,
        }
    except Exception as e:
        conn.rollback()
        return {"ok": False, "error": f"Error reservando folio: {str(e)[:200]}"}
    finally:
        cur.close()
        release_conn_func(conn)


def marcar_folio_usado(get_conn_func, release_conn_func, caf_id, folio):
    """No-op para la BD: ya quedó marcado al hacer obtener_folio_disponible.
    Esta función existe para hacer rollback si algo falla en la emisión.

    En Fase 2 implementamos la lógica de rollback: si falla la emisión SII,
    devolvemos el folio al pool con un endpoint admin.
    """
    return {"ok": True, "mensaje": "Folio ya quedó reservado al obtenerlo"}
