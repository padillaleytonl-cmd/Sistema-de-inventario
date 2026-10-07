import requests
from credenciales_canal import credencial


def _woo_api():
    # El sitio y las claves son del cliente que opera (credenciales_canal).
    return (credencial("web", "site_url") or "").rstrip("/") + "/wp-json/wc/v3"


def _woo_auth():
    return {"consumer_key": credencial("web", "consumer_key"),
            "consumer_secret": credencial("web", "consumer_secret")}


# Tope de espera por llamada: sin el, una tienda que no responde dejaba colgada
# la publicacion de stock (y el hilo que la hacia).
_TIMEOUT = 15


def actualizar_stock_woo(sku, stock):
    """Publica el stock de un SKU en la tienda Web (WooCommerce).

    Devuelve el mismo dict que los otros canales:
      {"ok", "exitosas", "fallidas", "total_publicaciones", "log"}
    para que sincronizar_stock_marketplaces diga la verdad.

    Antes (hasta el 06/10/2026) no devolvia nada y se tragaba cualquier error
    con un except vacio: el resumen decia "woo: ok" aunque la tienda no se
    hubiera enterado. Ademas solo mandaba stock_quantity, que WooCommerce
    ignora si el producto o la variacion no tienen "gestionar inventario"
    activado, y no hacia nada si el SKU era el de un producto variable.
    """
    log = []
    try:
        try:
            from inventario import get_sku_canal
            sku_web = get_sku_canal(sku, "web") or sku
        except Exception:
            sku_web = sku
        stock = max(0, int(stock or 0))

        res = requests.get(_woo_api() + "/products", params={**_woo_auth(), "sku": sku_web},
                           timeout=_TIMEOUT)
        if res.status_code != 200:
            msg = f"buscar SKU {sku_web}: HTTP {res.status_code} {res.text[:150]}"
            print(f"[Woo Stock] {msg}")
            return {"ok": False, "exitosas": 0, "fallidas": 1, "total_publicaciones": 1, "log": [msg]}

        productos = res.json() or []
        if not productos:
            print(f"[Woo Stock] SKU {sku_web}: no existe en la tienda, no se actualiza")
            return {"ok": True, "exitosas": 0, "fallidas": 0, "total_publicaciones": 0,
                    "log": [f"SKU {sku_web} no existe en la tienda"]}

        exitosas, fallidas = 0, 0
        for producto in productos:
            tipo = producto.get("type")
            if tipo == "variation":
                url = f"{_woo_api()}/products/{producto.get('parent_id')}/variations/{producto['id']}"
            elif tipo in ("simple", "variable"):
                url = f"{_woo_api()}/products/{producto['id']}"
                if tipo == "variable":
                    log.append(f"{sku_web} es un producto variable: se publica en el producto principal")
            else:
                log.append(f"{sku_web}: tipo {tipo} no maneja stock")
                continue

            # manage_stock=True: sin esto WooCommerce ignora la cantidad. En una
            # variacion que heredaba el stock del padre ("parent"), la deja con
            # stock propio: Lusync lleva el stock por SKU, o sea por variacion.
            r = requests.put(url, params=_woo_auth(), timeout=_TIMEOUT,
                             json={"manage_stock": True, "stock_quantity": stock})
            quedo = None
            try:
                quedo = (r.json() or {}).get("stock_quantity")
            except ValueError:
                pass
            if r.status_code in (200, 201) and quedo is not None and int(quedo) == stock:
                exitosas += 1
                print(f"[Woo Stock] SKU:{sku_web} ({tipo} {producto['id']}) Qty:{stock} OK")
            else:
                fallidas += 1
                msg = (f"SKU {sku_web} ({tipo} {producto['id']}): HTTP {r.status_code}, "
                       f"quedo en {quedo!r} en vez de {stock} — {r.text[:150]}")
                log.append(msg)
                print(f"[Woo Stock] FALLO {msg}")

        total = exitosas + fallidas
        return {"ok": fallidas == 0, "exitosas": exitosas, "fallidas": fallidas,
                "total_publicaciones": total, "log": log}

    except Exception as e:
        msg = f"SKU {sku}: {type(e).__name__}: {str(e)[:150]}"
        print(f"[Woo Stock] ERROR {msg}")
        return {"ok": False, "exitosas": 0, "fallidas": 1, "total_publicaciones": 1, "log": [msg]}
