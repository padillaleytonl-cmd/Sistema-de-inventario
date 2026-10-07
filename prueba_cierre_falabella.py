#!/usr/bin/env python3
"""
prueba_cierre_falabella.py — Prueba si la API de Falabella deja cerrar un pedido
de Falabella Directo (falaflex) como entregado, sin pasar por Enviatrack.

POR QUE ES UNA PRUEBA APARTE
    SetStatusToDelivered y SetStatusToFailedDelivery figuran como "Deprecado" en la
    documentacion, y no dicen si aplican a falaflex. Antes de construir nada encima,
    hay que ver que contesta Falabella con UN pedido real ya entregado.
    No modifica falabella.py: solo usa su llamar_api_falabella().

USO
    # 1. Solo lectura: estado actual del pedido y de cada item
    python prueba_cierre_falabella.py 1168359274

    # 2. Intentar cerrarlo como entregado (pide confirmacion antes)
    python prueba_cierre_falabella.py 1168359274 --entregado

    El numero es el "Orden id" interno (empieza con 11), no el N° de orden (3...):
    es el que pide la API. Sale en la columna "Orden id" del export del Seller Center.
"""

import argparse
import json
import sys

from falabella import llamar_api_falabella


def items_de(order_id: str) -> list[dict]:
    res = llamar_api_falabella("GetOrderItems", params_extra={"OrderId": order_id})
    if not res["ok"]:
        sys.exit(f"GetOrderItems fallo: {res.get('error')}")
    body = ((res.get("data") or {}).get("SuccessResponse") or {}).get("Body") or {}
    items = (body.get("OrderItems") or {}).get("OrderItem") or []
    return [items] if isinstance(items, dict) else items


def mostrar(order_id: str):
    res = llamar_api_falabella("GetOrder", params_extra={"OrderId": order_id})
    if not res["ok"]:
        sys.exit(f"GetOrder fallo: {res.get('error')}")
    body = ((res.get("data") or {}).get("SuccessResponse") or {}).get("Body") or {}
    orden = (body.get("Orders") or {}).get("Order") or {}
    print(f"Orden {orden.get('OrderNumber')} (id {order_id})")
    print(f"  Estados: {orden.get('Statuses')}")
    print(f"  Actualizada: {orden.get('UpdatedAt')}")
    for it in items_de(order_id):
        print(
            f"  Item {it.get('OrderItemId')}: {it.get('Status')} · {it.get('ShippingType')} · "
            f"{it.get('ShipmentProvider')} · tracking {it.get('TrackingCode')} · {it.get('Name')}"
        )


def cerrar(order_id: str):
    items = items_de(order_id)
    ids = [str(it["OrderItemId"]) for it in items if it.get("OrderItemId")]
    print(f"Se va a llamar SetStatusToDelivered para {len(ids)} item(s): {', '.join(ids)}")
    if input("Escribe SI para confirmar: ").strip() != "SI":
        print("Cancelado, no se llamo nada.")
        return
    for item_id in ids:
        res = llamar_api_falabella("SetStatusToDelivered", params_extra={"OrderItemId": item_id}, method="POST")
        print(f"  Item {item_id}: {'OK' if res['ok'] else 'RECHAZADO'}")
        print("   ", res.get("error") or json.dumps(res.get("data"), ensure_ascii=False)[:500])
    print("\nEstado despues del intento:")
    mostrar(order_id)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("order_id", help="Orden id interno de Falabella (empieza con 11)")
    ap.add_argument("--entregado", action="store_true", help="intentar cerrarlo como entregado")
    args = ap.parse_args()
    mostrar(args.order_id)
    if args.entregado:
        print()
        cerrar(args.order_id)
