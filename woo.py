import requests
from credenciales_canal import credencial


def _woo_api():
    # El sitio y las claves son del cliente que opera (credenciales_canal).
    return (credencial("web", "site_url") or "").rstrip("/") + "/wp-json/wc/v3"

# 🔥 ACTUALIZAR STOCK
def actualizar_stock_woo(sku, stock):
    """Actualiza stock en WooCommerce. Si hay mapeo de SKU, lo usa (Opción A)."""
    try:
        # Intentar usar SKU mapeado de la web
        try:
            from inventario import get_sku_canal
            sku_web = get_sku_canal(sku, "web")
        except Exception:
            sku_web = sku
        res = requests.get(
            _woo_api() + "/products",
            params={
                "consumer_key": credencial("web", "consumer_key"),
                "consumer_secret": credencial("web", "consumer_secret"),
                "sku": sku_web
            }
        )

        if res.status_code != 200:
            return

        data = res.json()
        if not data:
            return

        producto = data[0]

        # simple
        if producto["type"] == "simple":
            requests.put(
                f"{_woo_api()}/products/{producto['id']}",
                params={
                    "consumer_key": credencial("web", "consumer_key"),
                    "consumer_secret": credencial("web", "consumer_secret")
                },
                json={"stock_quantity": stock}
            )

        # variación
        if producto["type"] == "variation":
            parent_id = producto["parent_id"]

            requests.put(
                f"{_woo_api()}/products/{parent_id}/variations/{producto['id']}",
                params={
                    "consumer_key": credencial("web", "consumer_key"),
                    "consumer_secret": credencial("web", "consumer_secret")
                },
                json={"stock_quantity": stock}
            )

    except:
        pass