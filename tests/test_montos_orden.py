# -*- coding: utf-8 -*-
"""Montos por canal con ordenes reales revisadas el 18/10/2026."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from facturacion.montos_orden import (montos_falabella, montos_meli, montos_meli_pack,  # noqa: E402
                                      montos_paris, montos_ripley, montos_walmart, montos_web)


def _ch(tipo, nombre, monto, iva=0):
    return {"chargeType": tipo, "chargeName": nombre, "chargeAmount": {"amount": monto},
            "tax": {"taxAmount": {"amount": iva}}}


def _linea_wm(prod, iva_prod, envio, iva_envio, tax, ship_disc=0):
    cargos = [_ch("PRODUCT", "ItemPrice", prod, iva_prod), _ch("COMMISSION", "Commission", 0),
              _ch("SHIPPING", "Shipping", envio, iva_envio), _ch("TAX", "TAX", tax),
              _ch("DISCOUNT", "DISCOUNT", 0)]
    if ship_disc:
        cargos.append(_ch("DISCOUNT", "SHIP_DISC", ship_disc))
    return {"orderLineQuantity": {"amount": "1"}, "charges": {"charge": cargos}}


def test_walmart_envio_con_descuento_total():
    # 8572652000134: 4 lineas, envio 3.990 descontado entero (SHIP_DISC)
    orden = {"orderLines": {"orderLine": [
        _linea_wm(5454, 1036, 838, 159, 1195, 997), _linea_wm(4614, 876, 839, 159, 1035, 998),
        _linea_wm(5454, 1036, 838, 159, 1195, 997), _linea_wm(4614, 876, 839, 159, 1035, 998)]},
        "orderSummary": {"totalAmount": {"amount": 23960}}}
    r = montos_walmart(orden)
    assert r["productos"] == 23960
    assert r["envio_cobrado"] == 3990 and r["descuento_envio"] == 3990 and r["envio"] == 0
    assert r["total"] == 23960 and r["cuadra"] is True


def test_walmart_no_suma_el_cargo_tax():
    # 4912640001485: el cargo TAX (16.585) repite el IVA de producto y envio
    orden = {"orderLines": {"orderLine": _linea_wm(82261, 15629, 5034, 956, 16585)},
             "orderSummary": {"totalAmount": {"amount": 103880}}}
    r = montos_walmart(orden)
    assert r["productos"] == 97890 and r["envio"] == 5990
    assert r["total"] == 103880 and r["cuadra"] is True


def test_falabella_cabecera():
    r = montos_falabella({"ProductTotal": "54,990.00", "ShippingFeeTotal": "8,990.00",
                          "Voucher": "0.00", "GrandTotal": "63,980.00"})
    assert r["total"] == 63980 and r["envio"] == 8990 and r["cuadra"] is True


def test_falabella_voucher_se_resta():
    r = montos_falabella({"ProductTotal": "20,000.00", "ShippingFeeTotal": "3,990.00",
                          "Voucher": "2,000.00", "GrandTotal": "21,990.00"})
    assert r["productos"] == 18000 and r["total"] == 21990 and r["cuadra"] is True


def _orden_ml(unit, gross, pagado, envio=0, cupon=0, oid=1, seller=1):
    return {"id": oid, "seller": {"id": seller},
            "order_items": [{"quantity": 1, "unit_price": unit, "gross_price": gross, "item": {}}],
            "payments": [{"status": "approved", "shipping_cost": envio}],
            "coupon": {"amount": cupon}, "paid_amount": pagado}


def test_meli_promocion_del_vendedor_y_envio_que_paga_el_vendedor():
    # 2000019044570458: lista 74.990, vendido a 59.990, envio gratis para el comprador
    r = montos_meli(_orden_ml(59990, 74990, 59990))
    assert r["total"] == 59990 and r["envio"] == 0 and r["cuadra"] is True


def test_meli_cupon_queda_para_revisar():
    r = montos_meli(_orden_ml(10000, 10000, 9000, cupon=1000))
    assert r["cuadra"] is False and "cupon" in r["nota"]


def test_meli_pack_solo_ordenes_nuestras():
    pack = [_orden_ml(19980, 19980, 19980, oid=1, seller=7),
            _orden_ml(5690, 5690, 5690, oid=2, seller=7),
            _orden_ml(9990, 9990, 9990, oid=3, seller=99)]  # otro vendedor
    r = montos_meli_pack(pack, seller_id=7)
    assert r["total"] == 25670 and r["ordenes"] == ["1", "2"] and r["cuadra"] is True


def test_paris_sin_total_para_comparar():
    orden = {"shipments": [{"cost": "4990.00", "items": [
        {"priceAfterDiscounts": "6990.00", "grossPrice": "6990.00"},
        {"priceAfterDiscounts": "6990.00", "grossPrice": "6990.00"},
        {"priceAfterDiscounts": "6490.00", "grossPrice": "6490.00"}]}]}
    r = montos_paris(orden)
    assert r["total"] == 25460 and r["cuadra"] is None


def test_ripley():
    r = montos_ripley({"order_lines": [{"price": 9990, "shipping_price": 8990, "quantity": 1}],
                       "total_price": 18980})
    assert r["total"] == 18980 and r["cuadra"] is True


def test_web():
    r = montos_web({"line_items": [{"total": "5490", "total_tax": "0", "subtotal": "5490"},
                                   {"total": "5490", "total_tax": "0", "subtotal": "5490"},
                                   {"total": "6490", "total_tax": "0", "subtotal": "6490"}],
                    "shipping_total": "4000", "shipping_tax": "0", "total": "21470"})
    assert r["total"] == 21470 and r["cuadra"] is True
