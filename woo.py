import requests
from config import WC_KEY, WC_SECRET

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
            "https://www.babymine.cl/wp-json/wc/v3/products",
            params={
                "consumer_key": WC_KEY,
                "consumer_secret": WC_SECRET,
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
                f"https://www.babymine.cl/wp-json/wc/v3/products/{producto['id']}",
                params={
                    "consumer_key": WC_KEY,
                    "consumer_secret": WC_SECRET
                },
                json={"stock_quantity": stock}
            )

        # variación
        if producto["type"] == "variation":
            parent_id = producto["parent_id"]

            requests.put(
                f"https://www.babymine.cl/wp-json/wc/v3/products/{parent_id}/variations/{producto['id']}",
                params={
                    "consumer_key": WC_KEY,
                    "consumer_secret": WC_SECRET
                },
                json={"stock_quantity": stock}
            )

    except:
        pass