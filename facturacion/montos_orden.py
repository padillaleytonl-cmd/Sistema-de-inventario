# -*- coding: utf-8 -*-
"""
Cuanto pago el cliente en una orden de marketplace, canal por canal.

Una sola fuente para el reporte de ventas y para las boletas: si cada parte
calcula a su manera, una de las dos termina declarando de mas. Todo en pesos
con IVA incluido (lo que va en una boleta).

Cada funcion recibe la orden TAL COMO la entrega la API y devuelve:

    {
      "lineas": [{"sku", "nombre", "cantidad", "total"}],  # producto, ya con descuentos
      "productos":  suma de las lineas,
      "envio_cobrado":   lo que el canal informa como envio,
      "descuento_envio": lo que el canal descuenta del envio,
      "envio":      envio_cobrado - descuento_envio (lo que pago el cliente),
      "descuentos_producto": descuentos que ya se restaron de las lineas,
      "total":      productos + envio,
      "total_canal": el total que informa el propio marketplace (o None),
      "cuadra":     True/False si se pudo comparar, None si el canal no trae total,
      "nota":       texto si hay algo que mirar,
    }

Revisado con ordenes reales el 18/10/2026 (5 por canal). Si una orden no
cuadra al peso con el total del canal, NO se emite boleta: queda para revisar.
"""


def _num(v):
    if v is None or v == "":
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return 0.0


def _lista(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def _resultado(lineas, envio_cobrado, descuento_envio, descuentos_producto, total_canal, nota=""):
    productos = round(sum(l["total"] for l in lineas))
    envio = round(max(envio_cobrado - descuento_envio, 0))
    total = productos + envio
    cuadra = None if total_canal is None else abs(total - round(total_canal)) < 1
    return {"lineas": lineas, "productos": productos,
            "envio_cobrado": round(envio_cobrado), "descuento_envio": round(descuento_envio),
            "envio": envio, "descuentos_producto": round(descuentos_producto),
            "total": total, "total_canal": None if total_canal is None else round(total_canal),
            "cuadra": cuadra, "nota": nota}


# ── Walmart ─────────────────────────────────────────────────────────────
#
# Cada linea trae cargos (charges.charge[]), cada uno con su IVA en tax:
#   PRODUCT   ItemPrice      producto (neto + IVA)
#   SHIPPING  Shipping       envio (neto + IVA)
#   DISCOUNT  DISCOUNT       descuento del producto (en la linea viene positivo)
#   DISCOUNT  SHIP_DISC      descuento del envio
#   TAX       TAX            el IVA de los cargos anteriores SUMADO DE NUEVO:
#                            se ignora, si no el IVA se cuenta dos veces.
#   COMMISSION               lo que cobra Walmart al vendedor: no es del cliente.
# orderSummary.totalAmount es lo que pago el cliente.

def montos_walmart(orden):
    lineas, envio, desc_envio, desc_prod = [], 0.0, 0.0, 0.0
    for line in _lista((orden.get("orderLines") or {}).get("orderLine")):
        item = line.get("item") or {}
        qty = int(_num((line.get("orderLineQuantity") or {}).get("amount")) or 1)
        prod = prod_desc = 0.0
        for ch in _lista((line.get("charges") or {}).get("charge")):
            tipo = str(ch.get("chargeType") or "").upper()
            nombre = str(ch.get("chargeName") or "").upper()
            monto = (_num((ch.get("chargeAmount") or {}).get("amount")) +
                     _num(((ch.get("tax") or {}).get("taxAmount") or {}).get("amount")))
            if tipo == "PRODUCT":
                prod += monto
            elif tipo == "SHIPPING":
                envio += monto
            elif tipo == "DISCOUNT":
                if "SHIP" in nombre:
                    desc_envio += abs(monto)
                else:
                    prod_desc += abs(monto)
            # TAX y COMMISSION no son plata del cliente aparte
        desc_prod += prod_desc
        lineas.append({"sku": item.get("sku") or "", "nombre": item.get("productName") or "",
                       "cantidad": qty, "total": prod - prod_desc})
    total_canal = (orden.get("orderSummary") or {}).get("totalAmount")
    total_canal = _num(total_canal.get("amount")) if isinstance(total_canal, dict) else None
    return _resultado(lineas, envio, desc_envio, desc_prod, total_canal)


# ── Falabella ───────────────────────────────────────────────────────────
#
# Cabecera: ProductTotal + ShippingFeeTotal - Voucher = GrandTotal.
# Voucher trae el detalle en VoucherDetails (CmrDiscount, OtherDiscount).
# Los items (GetOrderItems) traen PaidPrice por unidad (cada OrderItem es 1).

def montos_falabella(orden, items_crudos=None):
    voucher = _num(orden.get("Voucher"))
    nota = ""
    lineas = []
    if items_crudos:
        for it in items_crudos:
            precio = _num(it.get("PaidPrice") or it.get("ItemPrice") or it.get("Price"))
            lineas.append({"sku": it.get("Sku") or it.get("SellerSku") or "",
                           "nombre": it.get("Name") or "", "cantidad": 1, "total": precio})
        # PaidPrice ya viene con el voucher del item: no se resta otra vez
        desc_prod = sum(_num(it.get("VoucherAmount")) for it in items_crudos)
    else:
        lineas = [{"sku": "", "nombre": "Productos", "cantidad": 1,
                   "total": _num(orden.get("ProductTotal")) - voucher}]
        desc_prod = voucher
        if voucher:
            nota = "voucher restado de los productos (sin detalle por item)"
    return _resultado(lineas, _num(orden.get("ShippingFeeTotal") or orden.get("ShippingFee")),
                      0.0, desc_prod, _num(orden.get("GrandTotal") or orden.get("Price")), nota)


# ── MercadoLibre ────────────────────────────────────────────────────────
#
# unit_price ya es el precio con la promocion del vendedor (gross_price es el
# de lista). El envio que paga el COMPRADOR va en payments[].shipping_cost; lo
# que paga el vendedor por el envio (senders.cost) es un costo nuestro, no va
# en la boleta. paid_amount = productos + envio del comprador - cupon.

def montos_meli(orden):
    lineas, desc = [], 0.0
    for oi in _lista(orden.get("order_items")):
        item = oi.get("item") or {}
        qty = int(_num(oi.get("quantity")) or 1)
        # Promocion del vendedor: ya esta restada en unit_price (informativo)
        desc += max(_num(oi.get("gross_price") or oi.get("full_unit_price")) -
                    _num(oi.get("unit_price")), 0) * qty
        lineas.append({"sku": item.get("seller_sku") or item.get("seller_custom_field") or "",
                       "nombre": item.get("title") or "", "cantidad": qty,
                       "total": _num(oi.get("unit_price")) * qty})
    pagos = [p for p in _lista(orden.get("payments"))
             if str(p.get("status") or "approved") in ("approved", "accredited")]
    envio = sum(_num(p.get("shipping_cost")) for p in pagos)
    cupon = max(_num((orden.get("coupon") or {}).get("amount")),
                sum(_num(p.get("coupon_amount")) for p in pagos))
    nota = ""
    if cupon:
        # Quien financia el cupon cambia lo que se declara: se deja para revisar
        nota = "orden con cupon de %s: revisar quien lo financia" % round(cupon)
    total_canal = orden.get("paid_amount")
    res = _resultado(lineas, envio, 0.0, desc,
                     None if total_canal is None else _num(total_canal), nota)
    if cupon:
        res["cuadra"] = False
    return res


def montos_meli_pack(ordenes, seller_id=None):
    """Una boleta por pack: suma SOLO ordenes nuestras (seller.id) que no sean
    Full. Nunca el order_cost del envio, que puede incluir otros vendedores."""
    propias = [o for o in ordenes
               if seller_id is None or str((o.get("seller") or {}).get("id")) == str(seller_id)]
    partes = [montos_meli(o) for o in propias]
    lineas = [l for p in partes for l in p["lineas"]]
    total_canal = sum(p["total_canal"] or 0 for p in partes)
    res = _resultado(lineas, sum(p["envio_cobrado"] for p in partes), 0.0, 0.0, total_canal,
                     "; ".join(p["nota"] for p in partes if p["nota"]))
    if any(p["cuadra"] is False for p in partes):
        res["cuadra"] = False
    res["ordenes"] = [str(o.get("id")) for o in propias]
    return res


# ── Paris ───────────────────────────────────────────────────────────────
#
# Cada item es 1 unidad; priceAfterDiscounts es lo que pago el cliente.
# El envio es shipments[].cost. Paris no trae total de la sub-orden para
# comparar: cuadra queda en None.

def montos_paris(orden):
    items = list(_lista(orden.get("items")))
    if not items:
        for ship in _lista(orden.get("shipments")):
            items.extend(_lista(ship.get("items")))
    lineas, desc = [], 0.0
    for it in items:
        pagado = _num(it.get("priceAfterDiscounts") or it.get("price_after_discounts") or
                      it.get("grossPrice") or it.get("basePrice") or it.get("price"))
        bruto = _num(it.get("grossPrice") or it.get("basePrice")) or pagado
        desc += max(bruto - pagado, 0)
        lineas.append({"sku": it.get("sku") or it.get("sellerSku") or "",
                       "nombre": it.get("name") or it.get("productName") or "",
                       "cantidad": int(_num(it.get("quantity")) or 1), "total": pagado})
    envio = sum(_num(s.get("cost")) for s in _lista(orden.get("shipments")))
    return _resultado(lineas, envio, 0.0, desc, None)


# ── Ripley (Mirakl) ─────────────────────────────────────────────────────
#
# order_lines[].price ya trae las promociones aplicadas; shipping_price es el
# envio de la linea. total_price de la orden = price + shipping_price.

def montos_ripley(orden):
    lineas, envio, desc = [], 0.0, 0.0
    for ln in _lista(orden.get("order_lines")):
        lineas.append({"sku": ln.get("offer_sku") or "", "nombre": ln.get("product_title") or "",
                       "cantidad": int(_num(ln.get("quantity")) or 1), "total": _num(ln.get("price"))})
        envio += _num(ln.get("shipping_price"))
        for pr in _lista(ln.get("promotions")):
            desc += _num(pr.get("deduced_amount"))
    if not lineas:
        envio = _num(orden.get("shipping_price"))
    tc = orden.get("total_price")
    return _resultado(lineas, envio, 0.0, desc, None if tc is None else _num(tc))


# ── Web (WooCommerce) ───────────────────────────────────────────────────
#
# line_items[].total ya viene con los cupones aplicados (subtotal es antes).
# total del pedido = lineas + impuestos + envio + cargos extra (fee_lines).

def montos_web(orden):
    lineas, desc = [], 0.0
    for li in _lista(orden.get("line_items")):
        total = _num(li.get("total")) + _num(li.get("total_tax"))
        sub = _num(li.get("subtotal")) + _num(li.get("subtotal_tax"))
        desc += max(sub - total, 0)
        lineas.append({"sku": li.get("sku") or "", "nombre": li.get("name") or "",
                       "cantidad": int(_num(li.get("quantity")) or 1), "total": total})
    for fee in _lista(orden.get("fee_lines")):
        lineas.append({"sku": "", "nombre": fee.get("name") or "Cargo", "cantidad": 1,
                       "total": _num(fee.get("total")) + _num(fee.get("total_tax"))})
    envio = _num(orden.get("shipping_total")) + _num(orden.get("shipping_tax"))
    tc = orden.get("total")
    return _resultado(lineas, envio, 0.0, desc, None if tc is None else _num(tc))


POR_CANAL = {"walmart": montos_walmart, "falabella": montos_falabella,
             "mercadolibre": montos_meli, "paris": montos_paris,
             "ripley": montos_ripley, "web": montos_web}
