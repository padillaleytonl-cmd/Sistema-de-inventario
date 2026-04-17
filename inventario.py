import json
import os

def cargar_productos():
    if os.path.exists(ARCHIVO_PRODUCTOS):
        try:
            with open(ARCHIVO_PRODUCTOS, "r") as f:
                contenido = f.read().strip()
                if not contenido:
                    return []
                return json.loads(contenido)
        except:
            return []
    return []