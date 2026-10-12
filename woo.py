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

# Errores temporales de la tienda: el hosting responde 503 seguido (11/10/2026)
# y una publicacion que fallaba ahi dejaba la tienda con el stock viejo.
_REINTENTABLES = (429, 502, 503, 504)


def _pedir(metodo, url, **kw):
    """requests.get/put con hasta 3 intentos ante un error temporal de la tienda."""
    import time
    r = None
    for espera in (0, 3, 8):
        if espera:
            time.sleep(espera)
        try:
            r = requests.request(metodo, url, timeout=_TIMEOUT, **kw)
        except requests.exceptions.RequestException:
            if espera == 8:
                raise
            continue
        if r.status_code not in _REINTENTABLES:
            return r
    return r


def _skus_web(sku):
    """SKU(s) con que el producto esta en la tienda. Igual que los demas
    canales: primero el mapeo por canal (sku_mapeo_canal), despues el mapeo
    viejo (sku_mapeo.sku_web) y al final el mismo SKU de Lusync.

    Hasta el 09/10/2026 la Web miraba solo el mapeo viejo. Al fusionar
    CTSECNSB001 en CBTSECN001 el mapeo por canal quedo bien (en la tienda es
    CTSECNSB001), pero el viejo no tenia SKU web: se buscaba "CBTSECN001", la
    tienda no lo encontraba y el coche E-Crib siguio a la venta con stock en
    la Web aunque Lusync tenia 0."""
    skus = []
    try:
        from inventario import obtener_publicaciones_canal
        skus = [str(p.get("sku_canal")).strip() for p in (obtener_publicaciones_canal(sku, "web") or [])
                if p.get("sku_canal")]
    except Exception:
        skus = []
    if not skus:
        try:
            from inventario import get_sku_canal
            legado = get_sku_canal(sku, "web")
            if legado:
                skus = [legado]
        except Exception:
            pass
    return list(dict.fromkeys(skus)), bool(skus)


def actualizar_stock_woo(sku, stock):
    """Publica el stock de un SKU en la tienda Web (WooCommerce), en todas sus
    publicaciones (ver _skus_web).

    Devuelve el mismo dict que los otros canales:
      {"ok", "exitosas", "fallidas", "total_publicaciones", "log"}
    para que sincronizar_stock_marketplaces diga la verdad.
    """
    stock = max(0, int(stock or 0))
    total = {"ok": True, "exitosas": 0, "fallidas": 0, "total_publicaciones": 0, "log": []}
    skus_web, mapeado = _skus_web(sku)
    for sku_web in (skus_web or [sku]):
        r = _publicar_sku_web(sku, sku_web, stock)
        if mapeado and r["total_publicaciones"] == 0:
            # El mapeo apunta a un SKU que la tienda no tiene. Si la tienda si
            # tiene el SKU de Lusync, se publica ahi: dejar la tienda sin el
            # cambio es peor (el coche E-Crib siguio vendiendose en la Web con
            # Lusync en 0 porque el mapeo decia CTSECNSB001 y en la tienda es
            # CBTSECN001). Igual queda como falla, para que se corrija el mapeo.
            r2 = _publicar_sku_web(sku, sku, stock) if sku_web != sku else None
            aviso = (f"el mapeo Web dice {sku_web} pero la tienda no tiene ese SKU"
                     + (f"; se publico en {sku}, que si existe: corrige el mapeo" if r2 and r2["exitosas"] else ""))
            print(f"[Woo Stock] MAPEO {sku}: {aviso}")
            r = {"exitosas": (r2 or {}).get("exitosas", 0), "fallidas": 1,
                 "total_publicaciones": 1 + (r2 or {}).get("total_publicaciones", 0),
                 "log": [aviso] + (r2 or {}).get("log", [])}
        for k in ("exitosas", "fallidas", "total_publicaciones"):
            total[k] += r[k]
        total["log"] += r["log"]
    total["ok"] = total["fallidas"] == 0
    return total


def _publicar_sku_web(sku, sku_web, stock):
    """Publica `stock` en el producto o variacion con SKU `sku_web`.

    Antes (hasta el 06/10/2026) no devolvia nada y se tragaba cualquier error
    con un except vacio: el resumen decia "woo: ok" aunque la tienda no se
    hubiera enterado. Ademas solo mandaba stock_quantity, que WooCommerce
    ignora si el producto o la variacion no tienen "gestionar inventario"
    activado, y no hacia nada si el SKU era el de un producto variable.
    """
    log = []
    try:
        res = _pedir("GET", _woo_api() + "/products", params={**_woo_auth(), "sku": sku_web})
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
            r = _pedir("PUT", url, params=_woo_auth(),
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
