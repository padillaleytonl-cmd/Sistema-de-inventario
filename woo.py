import requests
from config import WC_KEY, WC_SECRET

# 🔥 ACTUALIZAR STOCK
def actualizar_stock_woo(sku, stock):

    try:
        res = requests.get(
            "https://www.babymine.cl/wp-json/wc/v3/products",
            params={
                "consumer_key": WC_KEY,
                "consumer_secret": WC_SECRET,
                "sku": sku
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