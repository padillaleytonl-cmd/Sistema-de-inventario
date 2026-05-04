"""
bodegas_logic.py — Lógica de inventario multi-bodega y fulfillment

Este módulo centraliza TODA la lógica relacionada con:
  - Bodegas (Central, MELI Full, París Fulfillment, Walmart WFS, etc.)
  - Detección automática de tipo de venta (Seller vs Fulfillment)
  - Descuento inteligente desde la bodega correcta
  - Sincronización entre productos.stock y stock_bodega

Las funciones de bajo nivel (CRUD de tablas) siguen en inventario.py.
Acá vive la INTELIGENCIA de negocio sobre cuándo descontar de qué bodega.

Estructura del módulo:
  1. Detectores de fulfillment por marketplace
  2. Función central descontar_venta_inteligente()
  3. Reintegración para cancelaciones
  4. Helpers de sincronización
"""

# ═══════════════════════════════════════════════════════════════════════════
# DETECTORES DE FULFILLMENT POR MARKETPLACE
# ═══════════════════════════════════════════════════════════════════════════
# Cada marketplace tiene su propia forma de indicar si la venta es:
#  - Seller-fulfilled: el seller despacha desde su bodega
#  - Marketplace-fulfilled: el marketplace despacha desde su bodega (FULL/FBM/CD)
#
# Estos detectores leen el payload de la orden y devuelven True/False.

def detectar_fulfillment_meli(orden_data):
    """
    MercadoLibre Full vs Seller envía.

    IMPORTANTE: La orden NO trae logistic_type directamente, solo shipping.id.
    Para saber si es Full, hay que consultar /shipments/{id}.

    Campos posibles donde puede venir 'fulfillment':
      1. orden.fulfilled = true → Indicador directo (raro)
      2. orden.shipping.logistic_type = 'fulfillment' (no aparece, viene null)
      3. orden.tags incluye 'fulfillment' / 'fbm'
      4. orden.shipping.id → consultar /shipments/{id}.logistic_type → AUTORITATIVO

    Valores logistic_type:
      'fulfillment'    → Full (MELI guarda y despacha)
      'self_service'   → Seller envía con etiqueta
      'cross_docking'  → Seller despacha a centro de MELI
      'drop_off'       → Seller deja en punto de retiro
      'xd_drop_off'    → variantes
    """
    try:
        # Path 1: campo fulfilled de la orden
        if orden_data.get("fulfilled") is True:
            return True

        # Path 2: shipping a nivel de orden (suele venir null pero por si acaso)
        shipping = orden_data.get("shipping", {}) or {}
        logistic_type = (shipping.get("logistic_type") or "").lower()
        if logistic_type == "fulfillment":
            return True

        # Path 3: tags
        tags = orden_data.get("tags", []) or []
        if "fulfillment" in tags or "fbm" in tags:
            return True

        # Path 4 (AUTORITATIVO): consultar /shipments/{id}
        shipping_id = shipping.get("id")
        if shipping_id:
            try:
                # Importar acá para evitar circular
                from mercadolibre import meli_headers, MELI_API_URL
                import requests as req
                res = req.get(f"{MELI_API_URL}/shipments/{shipping_id}",
                              headers=meli_headers(), timeout=10)
                if res.status_code == 200:
                    ship = res.json()
                    ship_logistic = (ship.get("logistic_type") or "").lower()
                    if ship_logistic == "fulfillment":
                        return True
            except Exception as e:
                print(f"[Bodegas] No se pudo consultar shipment {shipping_id}: {e}")

        return False
    except Exception as e:
        print(f"[Bodegas] detectar_fulfillment_meli error: {e}")
        return False


def detectar_fulfillment_paris(orden_data):
    """
    París CrossDocking (Fulfillment) vs Seller envía.

    Campos posibles:
      shipments[].flow      = 'CROSSDOCKING' → CD/Fulfillment
      shipments[].flowType
      shippingType o shipping_type
    """
    try:
        # Path 1: shipments[].flow
        shipments = orden_data.get("shipments", [])
        for ship in shipments:
            flow = (ship.get("flow") or ship.get("flowType") or "").upper()
            if "CROSS" in flow or flow == "CD":
                return True

        # Path 2: shippingType en la orden
        shipping_type = (orden_data.get("shippingType") or
                         orden_data.get("shipping_type") or "").upper()
        if "CROSS" in shipping_type or shipping_type == "CD":
            return True

        return False
    except Exception as e:
        print(f"[Bodegas] detectar_fulfillment_paris error: {e}")
        return False


def detectar_fulfillment_walmart(orden_data):
    """
    Walmart WFS (Walmart Fulfillment Services) vs Seller envía.

    Campos posibles según versión del API:
      fulfillmentInfo.fulfillmentMethod = 'WFS' o 'FULFILLED_BY_WALMART'
      orderType o purchaseOrderType
      shippingInfo.shipMethod
      orderLines[].fulfillment.fulfillmentOption
    """
    try:
        # Path 1: fulfillmentInfo
        fi = (orden_data.get("fulfillmentInfo") or
              orden_data.get("fulfillment_info") or {})
        method = (fi.get("fulfillmentMethod") or
                  fi.get("fulfillment_method") or "").upper()
        if "WFS" in method or "FULFILLED_BY_WALMART" in method:
            return True

        # Path 2: tipo de orden
        order_type = (orden_data.get("orderType") or
                      orden_data.get("purchaseOrderType") or "").upper()
        if "WFS" in order_type or "FULFILLED" in order_type:
            return True

        # Path 3: shipping info
        ship_info = (orden_data.get("shippingInfo") or
                     orden_data.get("shipping_info") or {})
        ship_method = (ship_info.get("shipMethod") or
                       ship_info.get("methodCode") or "").upper()
        if "WFS" in ship_method:
            return True

        # Path 4: en orderLines
        order_lines = orden_data.get("orderLines", {}).get("orderLine", [])
        if isinstance(order_lines, dict):
            order_lines = [order_lines]
        for line in order_lines:
            line_fi = line.get("fulfillment", {}) or {}
            opt = (line_fi.get("fulfillmentOption") or "").upper()
            if opt in ("WFS", "FULFILLED_BY_WALMART"):
                return True

        return False
    except Exception as e:
        print(f"[Bodegas] detectar_fulfillment_walmart error: {e}")
        return False


def detectar_fulfillment_falabella(orden_data):
    """Falabella Fulfillment vs Seller (cuando se habilite)."""
    try:
        # Falabella usa Mirakl. El campo es 'fulfillment_type' o 'logistic_class'
        f_type = (orden_data.get("fulfillment_type") or
                  orden_data.get("logistic_class") or "").upper()
        return "FULFILLED" in f_type or f_type == "FBF"
    except: return False


def detectar_fulfillment_ripley(orden_data):
    """Ripley Fulfillment vs Seller (cuando se habilite)."""
    try:
        # Ripley también usa Mirakl
        f_type = (orden_data.get("fulfillment_type") or
                  orden_data.get("shipping_type") or "").upper()
        return "FULFILLED" in f_type or f_type == "FBR"
    except: return False


def detectar_fulfillment_hites(orden_data):
    """Hites Fulfillment vs Seller (cuando se habilite)."""
    try:
        f_type = (orden_data.get("fulfillment_type") or "").upper()
        return "FULFILLED" in f_type
    except: return False


# Diccionario de detectores: canal → función
DETECTORES = {
    "mercadolibre":  detectar_fulfillment_meli,
    "paris":         detectar_fulfillment_paris,
    "walmart":       detectar_fulfillment_walmart,
    "falabella":     detectar_fulfillment_falabella,
    "ripley":        detectar_fulfillment_ripley,
    "hites":         detectar_fulfillment_hites,
}


def detectar_fulfillment(canal, orden_data):
    """Llamada genérica: dado un canal y el payload de la orden, devuelve True si es fulfillment."""
    canal_l = (canal or "").lower()
    detector = DETECTORES.get(canal_l)
    if not detector:
        return False
    return detector(orden_data)


# ═══════════════════════════════════════════════════════════════════════════
# DESCUENTO INTELIGENTE
# ═══════════════════════════════════════════════════════════════════════════
# Estas funciones son wrappers que llaman a inventario.py pero centralizan
# la decisión de qué bodega usar.

def descontar_venta(sku, cantidad, canal, orden_data=None, fulfillment=None,
                    orden_id=None, motivo=None, usuario="Sistema",
                    fecha_compra_marketplace=None,
                    origen_registro="sync_manual"):
    """
    Función central para descontar stock de una venta.

    Puede recibir:
      - fulfillment=True/False explícito
      - orden_data: el payload de la orden, y el detector decide automáticamente

    Args nuevos (trazabilidad):
      - fecha_compra_marketplace: datetime real de la compra en el marketplace
        (ej: orden.date_created en MELI, orden.createdAt en París, etc.). Si llega
        con tzinfo se convierte automáticamente a Chile.
      - origen_registro: 'sync_manual' (default), 'webhook', 'scheduler', 'manual',
                         'import_excel', 'devolucion', 'pos', 'sistema'.

    Returns:
        dict: {ok, sku, cantidad_solicitada, cantidad_descontada,
               bodega, stock_antes, stock_despues, advertencia}
    """
    from inventario import descontar_venta_inteligente

    # Si no se especifica fulfillment, intentar detectar automáticamente
    if fulfillment is None:
        if orden_data:
            fulfillment = detectar_fulfillment(canal, orden_data)
        else:
            fulfillment = False  # default: seller envía

    return descontar_venta_inteligente(
        sku=sku,
        cantidad=cantidad,
        canal=canal,
        fulfillment=fulfillment,
        orden_id=orden_id,
        motivo=motivo,
        usuario=usuario,
        fecha_compra_marketplace=fecha_compra_marketplace,
        origen_registro=origen_registro
    )


def reintegrar_venta(sku, cantidad, canal, orden_data=None, fulfillment=None,
                     orden_id=None, motivo=None, usuario="Sistema"):
    """
    Reintegra stock cuando una orden es cancelada.
    Detecta automáticamente la bodega que originalmente fue afectada.
    """
    from inventario import determinar_bodega_para_canal, reintegrar_stock_bodega

    if fulfillment is None and orden_data:
        fulfillment = detectar_fulfillment(canal, orden_data)
    fulfillment = bool(fulfillment)

    bodega = determinar_bodega_para_canal(canal, fulfillment=fulfillment)

    reintegrar_stock_bodega(
        sku=sku,
        cantidad=cantidad,
        bodega_codigo=bodega,
        motivo=motivo or f"Cancelación {canal}",
        canal=canal,
        orden_id=orden_id,
        usuario=usuario
    )
    return {"ok": True, "bodega": bodega}


# ═══════════════════════════════════════════════════════════════════════════
# SYNC CRUZADO ENTRE CANALES
# ═══════════════════════════════════════════════════════════════════════════
# Cuando hay venta Seller (de bodega Central), hay que avisar a los demás
# marketplaces que el stock disponible bajó.
# Cuando hay venta Fulfillment, NO hay que sincronizar (cada bodega es separada).

def sincronizar_stock_a_marketplaces(sku, excepto=None):
    """
    Envía el stock de bodega Central a todos los marketplaces que tengan
    el SKU mapeado. Útil después de una venta Seller o ajuste manual.

    Args:
        sku: SKU del producto
        excepto: lista de canales a excluir del sync (ej. ['mercadolibre'])
    """
    from inventario import get_stock_bodega
    excepto = [c.lower() for c in (excepto or [])]

    stock_central = get_stock_bodega(sku, "CENTRAL")
    resultados = {}

    if "woocommerce" not in excepto and "woo" not in excepto:
        try:
            from woo import actualizar_stock_woo
            actualizar_stock_woo(sku, stock_central)
            resultados["woocommerce"] = "ok"
        except Exception as e:
            resultados["woocommerce"] = f"error: {e}"

    if "walmart" not in excepto:
        try:
            from walmart import actualizar_stock_walmart
            actualizar_stock_walmart(sku, stock_central)
            resultados["walmart"] = "ok"
        except Exception as e:
            resultados["walmart"] = f"error: {e}"

    if "paris" not in excepto:
        try:
            from paris import actualizar_stock_paris
            actualizar_stock_paris(sku, stock_central)
            resultados["paris"] = "ok"
        except Exception as e:
            resultados["paris"] = f"error: {e}"

    if "mercadolibre" not in excepto and "meli" not in excepto:
        try:
            from mercadolibre import actualizar_stock_meli
            actualizar_stock_meli(sku, stock_central)
            resultados["mercadolibre"] = "ok"
        except Exception as e:
            resultados["mercadolibre"] = f"error: {e}"

    return {"sku": sku, "stock_central": stock_central, "resultados": resultados}
