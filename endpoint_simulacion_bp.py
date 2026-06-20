"""
facturacion/endpoint_simulacion_bp.py  (va en la RAÍZ del repo, al lado de app.py)
─────────────────────────────────────────────────────────────
Blueprint con el endpoint del SET DE SIMULACIÓN SII.

La etapa de Simulación exige UN solo envío con 20–100 documentos de TODOS los
tipos que el contribuyente certifica, con datos de su operación real y CERO
reparos/rechazos. Reutiliza los generadores ya certificados.

Tipos incluidos (10) — los certificados por GRUPO PH (sin boletas 39/41, que
no se certificaron, y sin libros, que no son DTE):
    33  Factura Afecta          × 7
    34  Factura Exenta          × 3
    52  Guía de Despacho        × 3
    61  Nota de Crédito         × 3   (referencian facturas del mismo lote)
    56  Nota de Débito          × 2   (referencian NC del mismo lote)
    43  Liquidación Factura     × 1
    46  Factura de Compra       × 1   (retención total IVA)
    110 Factura Exportación     × 1
    111 ND Exportación          × 1
    112 NC Exportación          × 1
    ───────────────────────────────
    TOTAL                        23 documentos

INTEGRACIÓN en app.py — agregar estas 2 líneas (junto a los otros blueprints):

    from endpoint_simulacion_bp import simulacion_bp
    app.register_blueprint(simulacion_bp)

Ruta que expone:  GET /admin/lusync/sii/test-simulacion
  ?descargar=si      → descarga el XML firmado (para subir manual a maullin)
  ?confirmar=si      → envía por SOAP y devuelve Track ID
  ?debug=sin-firma   → descarga el sobre sin firmar (inspección)
  &f33=&f34=&f52=&f61=&f56=&f43=&f46=&f110=&f111=&f112=  → override de folios
"""
from flask import Blueprint, session, redirect, request, jsonify, Response
from datetime import datetime
import html as _html

simulacion_bp = Blueprint("simulacion_bp", __name__)


# ─────────────────────────────────────────────────────────────
# Datos realistas del giro (programación informática / marketplace)
# Receptores: RUT chilenos con dígito verificador VÁLIDO.
# ─────────────────────────────────────────────────────────────
RECEPTORES = [
    {"rut": "76264343-9", "razon_social": "COMERCIAL ECOMMERCE SUR SPA",
     "giro": "Venta al por menor por internet", "direccion": "Av. Apoquindo 4501 of 1203", "comuna": "Las Condes"},
    {"rut": "77598531-0", "razon_social": "DISTRIBUIDORA ANDINA DIGITAL LTDA",
     "giro": "Venta al por mayor de equipos informáticos", "direccion": "Av. Vicuña Mackenna 1865", "comuna": "Ñuñoa"},
    {"rut": "78091624-5", "razon_social": "SERVICIOS GASTRONOMICOS BELLAVISTA SPA",
     "giro": "Restaurantes y servicios de comida", "direccion": "Pío Nono 145", "comuna": "Recoleta"},
    {"rut": "76555011-K", "razon_social": "LOGISTICA Y BODEGAJE CENTRAL SA",
     "giro": "Almacenamiento y depósito", "direccion": "Camino a Melipilla 9871", "comuna": "Maipú"},
    {"rut": "77123980-6", "razon_social": "BOUTIQUE MODA URBANA LIMITADA",
     "giro": "Venta al por menor de prendas de vestir", "direccion": "Av. Providencia 2134 local 7", "comuna": "Providencia"},
]

# Proveedor para la Factura de Compra (a quien se le retiene IVA)
PROVEEDOR_FC = {
    "rut": "13871547-8", "razon_social": "JUAN CARLOS MORALES PEREZ",
    "giro": "Servicios de desarrollo de software", "direccion": "Los Alerces 234", "comuna": "La Florida",
}

# Mandante de la liquidación (consignación marketplace)
MANDANTE_LIQ = {
    "rut": "76264343-9", "razon_social": "COMERCIAL ECOMMERCE SUR SPA",
    "giro": "Venta al por menor por internet", "direccion": "Av. Apoquindo 4501 of 1203", "comuna": "Las Condes",
}

# Cliente extranjero para exportación de servicios
RECEPTOR_EXPORT = {
    "rut": "55555555-5", "razon_social": "GLOBAL SOFTWARE PARTNERS LLC",
    "giro": "Software services", "direccion": "1209 Orange Street, Wilmington DE",
    "comuna": "Wilmington", "nacionalidad": 502,  # 502 = Estados Unidos (cód. aduana SII)
}


def _fecha_santiago():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/Santiago")).strftime("%Y-%m-%d")
    except Exception:
        from datetime import timezone as _tz, timedelta as _td
        return (datetime.now(_tz.utc) - _td(hours=4)).strftime("%Y-%m-%d")


@simulacion_bp.route("/admin/lusync/sii/test-simulacion", methods=["GET"])
def admin_lusync_sii_test_simulacion():
    """Emite los 23 DTE del Set de Simulación en UN sobre EnvioDTE."""
    if not session.get("logged"):
        return redirect("/login")
    if session.get("rol") != "admin" and not session.get("is_lusync_admin"):
        return jsonify({"ok": False, "error": "solo admin del tenant"}), 403
    from inventario import get_conn, release_conn
    from facturacion.certificados import obtener_certificado
    from facturacion.db import obtener_config_facturacion

    tenant_id = request.args.get("tenant_id", default=3, type=int)
    ambiente = request.args.get("ambiente", default="certificacion")
    confirmar = request.args.get("confirmar", default="")
    descargar = request.args.get("descargar", default="")
    debug = request.args.get("debug", default="")

    pasos = []
    def paso(nombre, ok, detalle=""):
        pasos.append({"nombre": nombre, "ok": ok, "detalle": detalle})

    error_fatal = False
    track_id = None
    sobre = None
    sobre_firmado = None
    detalles_casos = []

    # Página de confirmación (igual patrón que los otros endpoints)
    if confirmar != "si" and descargar != "si" and debug == "":
        return """<!DOCTYPE html><html><head><meta charset="utf-8">
        <title>Set de Simulación SII</title>
        <style>body{font-family:-apple-system,sans-serif;background:#f6f5f1;padding:40px;}
        .card{max-width:720px;margin:0 auto;background:white;border-radius:14px;padding:28px;
        box-shadow:0 4px 20px rgba(0,0,0,0.06);}
        .warn{background:#fef3c7;border:1px solid #f59e0b;border-radius:8px;padding:14px;color:#92400e;font-size:13px;margin-bottom:14px;}
        a.btn{display:inline-block;background:#1f4e79;color:white;text-decoration:none;padding:11px 20px;border-radius:8px;font-size:14px;margin-right:8px;}
        h2{color:#1f4e79;} li{margin:3px 0;font-size:14px;}</style></head>
        <body><div class="card">
        <h2>📦 Set de Simulación SII</h2>
        <div class="warn">Genera <b>23 DTE</b> de todos los tipos certificados en <b>un solo envío</b>,
        con datos de operación real. Requisito SII: 20–100 docs, sin reparos, mes actual o anterior.</div>
        <p>Documentos: 7×Factura(33), 3×Exenta(34), 3×Guía(52), 3×NC(61), 2×ND(56),
        1×Liquidación(43), 1×Factura Compra(46), 1×Export(110), 1×ND Export(111), 1×NC Export(112).</p>
        <p><a class="btn" href="?tenant_id=""" + str(tenant_id) + """&descargar=si">⬇️ Descargar XML firmado</a>
        <a class="btn" href="?tenant_id=""" + str(tenant_id) + """&debug=sin-firma">🔍 Ver sin firma</a></p>
        <p style="font-size:12px;color:#888;">Para enviar por SOAP: agregar <code>&confirmar=si</code></p>
        </div></body></html>"""

    try:
        # ─── 1. Certificado ───
        cert = obtener_certificado(get_conn, release_conn, tenant_id)
        if not cert.get("ok"):
            paso("Leer certificado", False, cert.get("error", "?"))
            error_fatal = True
        else:
            paso("Leer certificado", True, cert["metadata"].get("titular", "?"))

        # ─── 2. Emisor + todos los CAF ───
        emisor = None
        cafs = {}
        if not error_fatal:
            config = obtener_config_facturacion(get_conn, release_conn, tenant_id)
            emisor = {
                "rut": config["rut_emisor"], "razon_social": config["razon_social"],
                "giro": config.get("giro", "Actividades de programación informática"),
                "dir_origen": config.get("direccion", "Ahumada 254 of 806"),
                "cmna_origen": config.get("comuna", "Santiago"),
            }
            if config.get("acteco"):
                emisor["acteco"] = config["acteco"]
            paso("Datos del emisor", True, f"{emisor['razon_social']} · {emisor['rut']}")

            from facturacion.dtes.caf_parser import parsear_caf_xml
            tipos_necesarios = [33, 34, 52, 61, 56, 43, 46, 110, 111, 112]
            conn = get_conn()
            try:
                with conn.cursor() as cur:
                    for td in tipos_necesarios:
                        cur.execute("""
                            SELECT xml_caf FROM facturacion_cafs
                            WHERE tenant_id = %s AND tipo_dte = %s
                            ORDER BY id DESC LIMIT 1
                        """, (tenant_id, td))
                        row = cur.fetchone()
                        if not row:
                            paso(f"CAF tipo {td}", False, f"No hay CAF tipo {td} cargado")
                            error_fatal = True
                        else:
                            cafs[td] = parsear_caf_xml(row[0])
            finally:
                release_conn(conn)
            if not error_fatal:
                rangos = " · ".join(f"{td}:{cafs[td].rango_desde}-{cafs[td].rango_hasta}" for td in tipos_necesarios)
                paso("CAF de los 10 tipos", True, rangos)

        # ─── 3. Generar los 23 documentos ───
        documentos_sin_firma = []
        documento_ids = []
        if not error_fatal:
            fecha = _fecha_santiago()

            # Folio inicial de cada tipo (override por query param ?f33=… etc.)
            def folio0(td):
                p = (request.args.get(f"f{td}") or "").strip()
                return int(p) if p.isdigit() else cafs[td].rango_desde
            fol = {td: folio0(td) for td in [33, 34, 52, 61, 56, 43, 46, 110, 111, 112]}

            from facturacion.dtes.factura import generar_factura_xml
            from facturacion.dtes.guia_despacho import generar_guia_despacho_xml
            from facturacion.dtes.nota_credito import generar_nota_credito_xml
            from facturacion.dtes.nota_debito import generar_nota_debito_xml
            from facturacion.dtes.liquidacion import generar_liquidacion_xml
            from facturacion.dtes.factura_compra import generar_factura_compra_xml
            from facturacion.dtes.exportacion import generar_exportacion_xml

            def add(r, etiqueta):
                documentos_sin_firma.append(r["xml"])
                documento_ids.append(r["documento_id"])
                t = r["totales"]["mnt_total"]
                detalles_casos.append(f"{etiqueta} f{r['folio']}: ${t:,}".replace(",", "."))
                return r

            facturas_emitidas = []  # (folio, total) para referenciar en NC

            # ── 7 Facturas Afectas (33) — servicios del giro ──
            CATALOGO_33 = [
                [{'nombre': 'Desarrollo módulo integración API marketplace', 'cantidad': 1, 'precio_unitario': 1850000, 'exento': False},
                 {'nombre': 'Horas soporte técnico especializado', 'cantidad': 12, 'precio_unitario': 35000, 'exento': False}],
                [{'nombre': 'Licencia mensual plataforma Lusync Pro', 'cantidad': 3, 'precio_unitario': 89000, 'exento': False}],
                [{'nombre': 'Implementación centralizador de marketplace', 'cantidad': 1, 'precio_unitario': 2400000, 'exento': False},
                 {'nombre': 'Capacitación equipo (sesiones)', 'cantidad': 4, 'precio_unitario': 65000, 'exento': False}],
                [{'nombre': 'Comisión por gestión de ventas marketplace', 'cantidad': 1, 'precio_unitario': 540000, 'exento': False},
                 {'nombre': 'Hosting dedicado mensual', 'cantidad': 1, 'precio_unitario': 120000, 'exento': False}],
                [{'nombre': 'Desarrollo conector ERP a medida', 'cantidad': 1, 'precio_unitario': 1650000, 'exento': False, 'descuento_pct': 10}],
                [{'nombre': 'Mantención y actualización mensual sistema', 'cantidad': 6, 'precio_unitario': 145000, 'exento': False}],
                [{'nombre': 'Plan anual licencia enterprise', 'cantidad': 1, 'precio_unitario': 3200000, 'exento': False},
                 {'nombre': 'Migración de datos histórica', 'cantidad': 1, 'precio_unitario': 480000, 'exento': False}],
            ]
            for i, items in enumerate(CATALOGO_33):
                recep = RECEPTORES[i % len(RECEPTORES)]
                r = add(generar_factura_xml(
                    caf=cafs[33], folio=fol[33], fecha_emision=fecha,
                    emisor=emisor, receptor=recep, items=items), f"FAC33")
                facturas_emitidas.append((r["folio"], r["totales"]["mnt_total"], recep))
                fol[33] += 1

            # ── 3 Facturas Exentas (34) — servicios exentos de IVA ──
            CATALOGO_34 = [
                [{'nombre': 'Asesoría en transformación digital', 'cantidad': 1, 'precio_unitario': 950000, 'exento': True}],
                [{'nombre': 'Servicio de consultoría estratégica TI', 'cantidad': 8, 'precio_unitario': 85000, 'exento': True}],
                [{'nombre': 'Curso certificado e-commerce (exento)', 'cantidad': 15, 'precio_unitario': 42000, 'exento': True}],
            ]
            exentas_emitidas = []
            for i, items in enumerate(CATALOGO_34):
                recep = RECEPTORES[i % len(RECEPTORES)]
                r = add(generar_factura_xml(
                    caf=cafs[34], folio=fol[34], fecha_emision=fecha,
                    emisor=emisor, receptor=recep, items=items, es_exenta=True), f"EXE34")
                exentas_emitidas.append((r["folio"], r["totales"]["mnt_total"], recep))
                fol[34] += 1

            # ── 3 Guías de Despacho (52) — traslado por venta a clientes ──
            CATALOGO_52 = [
                (1, 2, [{'nombre': 'Servidor físico marca Dell PowerEdge', 'cantidad': 2, 'precio_unitario': 1450000, 'exento': False}]),
                (1, 2, [{'nombre': 'Notebook corporativo (unidades)', 'cantidad': 8, 'precio_unitario': 720000, 'exento': False}]),
                (1, 2, [{'nombre': 'Equipos de red y switches', 'cantidad': 6, 'precio_unitario': 340000, 'exento': False}]),
            ]
            for i, (ind_tras, tipo_desp, items) in enumerate(CATALOGO_52):
                recep = RECEPTORES[i % len(RECEPTORES)]
                add(generar_guia_despacho_xml(
                    caf=cafs[52], folio=fol[52], fecha_emision=fecha,
                    emisor=emisor, receptor=recep,
                    ind_traslado=ind_tras, tipo_despacho=tipo_desp, items=items), f"GUI52")
                fol[52] += 1

            # ── 3 Notas de Crédito (61) — referencian facturas del lote ──
            #   NC1: anula factura 1 completa (CORRIGE GIRO)
            #   NC2: devolución parcial factura 2
            #   NC3: corrige monto factura exenta 1
            f1, t1, rec1 = facturas_emitidas[0]
            add(generar_nota_credito_xml(
                caf=cafs[61], folio=fol[61], fecha_emision=fecha,
                emisor=emisor, receptor=rec1,
                referencia={"folio_ref": f1, "tipo_doc_ref": 33, "fecha_ref": fecha,
                            "cod_ref": 1, "razon_ref": "ANULA FACTURA POR ERROR EN EMISION"},
                items=None, monto_anulacion=t1), "NC61")
            fol[61] += 1

            f2, t2, rec2 = facturas_emitidas[1]
            add(generar_nota_credito_xml(
                caf=cafs[61], folio=fol[61], fecha_emision=fecha,
                emisor=emisor, receptor=rec2,
                referencia={"folio_ref": f2, "tipo_doc_ref": 33, "fecha_ref": fecha,
                            "cod_ref": 3, "razon_ref": "DEVOLUCION PARCIAL LICENCIAS"},
                items=[{'nombre': 'Licencia mensual plataforma Lusync Pro', 'cantidad': 1, 'precio_unitario': 89000, 'exento': False}]),
                "NC61")
            fol[61] += 1

            fe1, te1, rece1 = exentas_emitidas[0]
            add(generar_nota_credito_xml(
                caf=cafs[61], folio=fol[61], fecha_emision=fecha,
                emisor=emisor, receptor=rece1,
                referencia={"folio_ref": fe1, "tipo_doc_ref": 34, "fecha_ref": fecha,
                            "cod_ref": 1, "razon_ref": "ANULA FACTURA EXENTA POR ERROR"},
                items=None, monto_anulacion=te1, es_exenta=True), "NC61")
            fol[61] += 1

            # ── 2 Notas de Débito (56) — referencian las NC anteriores ──
            #   ND1: anula la NC1 (revierte la anulación)
            #   ND2: aumenta monto por intereses sobre factura 3
            nc1_folio = fol[61] - 3  # primera NC emitida
            add(generar_nota_debito_xml(
                caf=cafs[56], folio=fol[56], fecha_emision=fecha,
                emisor=emisor, receptor=rec1,
                referencia={"folio_ref": nc1_folio, "tipo_doc_ref": 61, "fecha_ref": fecha,
                            "cod_ref": 1, "razon_ref": "ANULA NOTA DE CREDITO ELECTRONICA"},
                items=[{'nombre': 'Desarrollo módulo integración API marketplace', 'cantidad': 1, 'precio_unitario': 1850000, 'exento': False},
                       {'nombre': 'Horas soporte técnico especializado', 'cantidad': 12, 'precio_unitario': 35000, 'exento': False}]),
                "ND56")
            fol[56] += 1

            f3, t3, rec3 = facturas_emitidas[2]
            add(generar_nota_debito_xml(
                caf=cafs[56], folio=fol[56], fecha_emision=fecha,
                emisor=emisor, receptor=rec3,
                referencia={"folio_ref": f3, "tipo_doc_ref": 33, "fecha_ref": fecha,
                            "cod_ref": 3, "razon_ref": "INTERES POR PAGO FUERA DE PLAZO"},
                items=[{'nombre': 'Interés por mora (1.5%)', 'cantidad': 1, 'precio_unitario': 44475, 'exento': False}]),
                "ND56")
            fol[56] += 1

            # ── 1 Liquidación Factura (43) — comisión marketplace real ──
            add(generar_liquidacion_xml(
                caf=cafs[43], folio=fol[43], fecha_emision=fecha,
                emisor=emisor, receptor=MANDANTE_LIQ,
                items=[
                    {'nombre': 'NETO VENTAS MARKETPLACE PERIODO', 'cantidad': 1, 'monto': 4250000, 'exento': False, 'tpo_doc_liq': 33},
                    {'nombre': 'EXENTO VENTAS MARKETPLACE', 'cantidad': 1, 'monto': 380000, 'exento': True, 'tpo_doc_liq': 33},
                ],
                comisiones=[
                    {'tipo_movim': 'C', 'glosa': 'COMISION POR INTERMEDIACION 8%', 'neto': 370400},
                ],
                referencias=[{"tpo_doc_ref": "SET", "folio_ref": "SIM", "fecha_ref": fecha,
                              "razon_ref": "LIQUIDACION COMISION MARKETPLACE"}]),
                "LIQ43")
            fol[43] += 1

            # ── 1 Factura de Compra (46) — retención total IVA a proveedor ──
            add(generar_factura_compra_xml(
                caf=cafs[46], folio=fol[46], fecha_emision=fecha,
                emisor=emisor, receptor=PROVEEDOR_FC,
                items=[
                    {'nombre': 'Servicio desarrollo freelance (honorarios)', 'cantidad': 1, 'precio_unitario': 980000},
                    {'nombre': 'Horas consultoría externa', 'cantidad': 20, 'precio_unitario': 28000},
                ],
                referencias=[{"tpo_doc_ref": "SET", "folio_ref": "SIM", "fecha_ref": fecha,
                              "razon_ref": "COMPRA SERVICIOS CON RETENCION"}]),
                "FC46")
            fol[46] += 1

            # ── Exportación: 110 (factura), luego 111 (ND) y 112 (NC) que la referencian ──
            ADUANA = {
                "cod_mod_venta": 1, "cod_clau_venta": 1, "tot_clau_venta": 1,
                "cod_via_transp": 1, "cod_pto_embarque": 901, "cod_pto_desemb": 999,
                "cod_pais_recep": 563, "cod_pais_destin": 563,
            }
            r110 = add(generar_exportacion_xml(
                caf=cafs[110], folio=fol[110], fecha_emision=fecha,
                emisor=emisor, receptor=RECEPTOR_EXPORT,
                items=[{'nombre': 'Software development services - SaaS platform', 'cantidad': 1, 'precio_unitario': 12000}],
                moneda="DOLAR USA", tipo_cambio=945.0, aduana=ADUANA,
                ind_servicio=3, tipo_dte=110), "EXP110")
            export_folio = r110["folio"]
            fol[110] += 1

            # 111 ND Export — aumenta monto de la factura export
            add(generar_exportacion_xml(
                caf=cafs[111], folio=fol[111], fecha_emision=fecha,
                emisor=emisor, receptor=RECEPTOR_EXPORT,
                items=[{'nombre': 'Additional development hours', 'cantidad': 1, 'precio_unitario': 1500}],
                moneda="DOLAR USA", tipo_cambio=945.0, aduana=ADUANA,
                ind_servicio=3, tipo_dte=111,
                referencias=[{"tpo_doc_ref": 110, "folio_ref": export_folio, "fecha_ref": fecha,
                              "cod_ref": 3, "razon_ref": "ADDITIONAL SERVICES"}]), "NDEXP111")
            fol[111] += 1

            # 112 NC Export — anula parte de la factura export
            add(generar_exportacion_xml(
                caf=cafs[112], folio=fol[112], fecha_emision=fecha,
                emisor=emisor, receptor=RECEPTOR_EXPORT,
                items=[{'nombre': 'Discount on software services', 'cantidad': 1, 'precio_unitario': 800}],
                moneda="DOLAR USA", tipo_cambio=945.0, aduana=ADUANA,
                ind_servicio=3, tipo_dte=112,
                referencias=[{"tpo_doc_ref": 110, "folio_ref": export_folio, "fecha_ref": fecha,
                              "cod_ref": 1, "razon_ref": "SERVICE ADJUSTMENT"}]), "NCEXP112")
            fol[112] += 1

            paso(f"Generar 23 documentos", True,
                 f"{len(documentos_sin_firma)} DTE · " + " · ".join(detalles_casos[:6]) + " …")

        # ─── 4. Armar sobre EnvioDTE ───
        if not error_fatal:
            from facturacion.dtes.envio_dte import armar_envio_dte
            set_id = "SetSimulacion"
            subtotales = {33: 7, 34: 3, 52: 3, 61: 3, 56: 2, 43: 1, 46: 1, 110: 1, 111: 1, 112: 1}
            sobre = armar_envio_dte(
                dtes_firmados=documentos_sin_firma,
                rut_emisor=emisor["rut"],
                rut_envia=cert["metadata"].get("rut", "18849272-K"),
                fch_resol="2026-05-15", nro_resol=0,
                subtotales=subtotales, set_dte_id=set_id,
            )
            paso("Armar sobre EnvioDTE (23 docs)", True, f"{len(sobre)} bytes")

        # ─── debug sin-firma ───
        if not error_fatal and debug == "sin-firma":
            return Response(sobre, mimetype="application/xml",
                headers={"Content-Disposition": 'attachment; filename="EnvioDTE_Simulacion_SinFirma.xml"'})

        # ─── 5. Firmar el sobre completo ───
        if not error_fatal:
            from facturacion.dtes.firma import firmar_envio_completo
            try:
                sobre_firmado = firmar_envio_completo(
                    sobre, cert["pfx_bytes"], cert["password"],
                    set_dte_id=set_id, documento_ids=documento_ids)
                paso("Firmar sobre completo", True, f"{len(sobre_firmado)} bytes")
            except Exception as e_firma:
                import traceback
                paso("Firmar sobre completo", False, f"{str(e_firma)[:400]}\n{traceback.format_exc()[:300]}")
                error_fatal = True

        if not error_fatal and descargar == "si":
            return Response(sobre_firmado, mimetype="application/xml",
                headers={"Content-Disposition": 'attachment; filename="EnvioDTE_Simulacion.xml"'})

        # ─── 6. Envío SOAP ───
        if not error_fatal and confirmar == "si":
            from facturacion.dtes.sii_client import obtener_token_dte, enviar_dte
            try:
                tok = obtener_token_dte(cert["pfx_bytes"], cert["password"], ambiente)
                paso("Obtener token SOAP", True, f"Token: {tok[:18]}…")
            except Exception as e:
                paso("Obtener token SOAP", False, str(e)[:300])
                error_fatal = True
            if not error_fatal:
                resultado = enviar_dte(
                    envio_xml=sobre_firmado, token=tok,
                    rut_emisor=emisor["rut"],
                    rut_envia=cert["metadata"].get("rut", "18849272-K"),
                    ambiente=ambiente)
                if resultado.get("ok"):
                    track_id = resultado["track_id"]
                    paso("Enviar al SII (DTEUpload)", True, f"✓ Track ID: {track_id}")
                else:
                    detalle = resultado.get("error") or _html.escape(str(resultado.get("respuesta_cruda", ""))[:400])
                    paso("Enviar al SII (DTEUpload)", False, detalle)
                    error_fatal = True

    except Exception as e:
        import traceback
        paso("Error", False, _html.escape(traceback.format_exc()[:600]))
        error_fatal = True

    todo_ok = all(p["ok"] for p in pasos) and (track_id is not None or confirmar != "si")
    color = "#10b981" if todo_ok else "#dc2626"
    emoji = "🎉" if todo_ok else "❌"
    filas = "".join(
        f'<tr><td>{"✅" if p["ok"] else "❌"}</td><td><b>{_html.escape(p["nombre"])}</b><br>'
        f'<span style="color:#666;font-size:12px;">{_html.escape(str(p["detalle"]))}</span></td></tr>'
        for p in pasos)
    track_html = f'<div style="background:#d1fae5;border-radius:8px;padding:16px;margin-top:14px;"><b>Track ID: {track_id}</b><br>Consultá el estado del envío y declará este N° en el portal de Simulación del SII.</div>' if track_id else ""
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>Set Simulación</title>
    <style>body{{font-family:-apple-system,sans-serif;background:#f6f5f1;padding:30px;}}
    .card{{max-width:760px;margin:0 auto;background:white;border-radius:14px;padding:26px;box-shadow:0 4px 20px rgba(0,0,0,0.06);}}
    table{{width:100%;border-collapse:collapse;}} td{{padding:8px;border-bottom:1px solid #eee;vertical-align:top;}}
    td:first-child{{width:30px;}} h2{{color:{color};}}</style></head>
    <body><div class="card"><h2>{emoji} Set de Simulación</h2>
    <table>{filas}</table>{track_html}</div></body></html>"""
