# Hoja de ruta — Lusync

Lo que quedó pendiente a propósito, con el motivo. No es una lista de deseos:
cada punto salió de algo que se encontró trabajando, y está acá para que no se
pierda ni se rehaga desde cero.

Última actualización: 21-09-2026

---

## 1. Precios de Walmart — hay que decidir antes de codificar

**Estado hoy:** el precio solo se sincroniza si alguien aprieta el botón
(`POST /walmart/sync_precios`). No hay job automático, a diferencia del stock,
que sí se publica solo cuando cambia.

**La pregunta es de negocio, no técnica:** ¿un cambio de precio en Lusync debe
publicarse solo, o seguir siendo una acción deliberada?

| Opción | A favor | En contra |
|---|---|---|
| **Automático, por evento** | El precio de Lusync es siempre el que ve el cliente. Igual que el stock. | Un error de tipeo llega al marketplace en segundos. Un cero de más es una venta a pérdida. |
| **Automático, con tope de variación** | Publica solo, pero frena y avisa si el cambio supera un %. | Hay que elegir el umbral, y un cambio legítimo grande queda trabado esperando aprobación. |
| **Manual, como hoy** | Nada se publica sin que alguien lo mire. | Los precios quedan desfasados si nadie aprieta el botón. |

**Recomendación:** la segunda, con el tope como configuración del tenant. Es la
única que evita los dos fracasos — publicar un error y no publicar nada.

**Antes de implementar hay que definir:** el umbral por defecto, si el tope se
mide en porcentaje o en pesos, y qué pasa con los DTE ya emitidos a un precio
distinto (probablemente nada, pero hay que decirlo).

**Ojo:** esto aplica a los seis canales, no solo a Walmart. Conviene resolverlo
una vez y aplicarlo parejo.

---

## 2. Migrar los colores a tokens

**Estado hoy:** más de 2.200 colores escritos a mano.

| Archivo | Hex hardcodeados | Usos de `var(--)` |
|---|---|---|
| `templates/panel.html` | 1.323 | 2.046 |
| `app.py` (HTML incrustado) | 889 | 9 |

Un `#6b7280` dentro de un `style=` no responde a ningún tema. Mientras esos
2.200 sigan ahí, no se puede cambiar el aspecto ni ofrecer temas por tenant, y
el HTML de `app.py` es el caso duro: 889 colores y 9 tokens.

**Esto habilita el punto 3.** Es el trabajo aburrido que hay que hacer primero.

---

## 3. Rediseño del panel

Propuesta visual hecha y aprobada para revisión, **pendiente de decisión**:
cromo desaturado con la saturación reservada para los estados, filas regladas en
vez de tarjetas, DM Mono para los datos, íconos en vez de emoji.

Hallazgos concretos que salieron al armarla y que valen aunque el rediseño no se
haga:

- El POS corre con un acento distinto (`#6366f1`) al del panel (`#2563eb`): son
  dos sistemas de color conviviendo.
- El tablero dibuja **las mismas ventas por canal dos veces** (barras y dona).
  Consolidar el gráfico y deduplicar la petición son el mismo arreglo.
- La dona de stock por bodega tiene ocho porciones: en barras se compara, en
  dona no.

---

## 4. Deuda de facturación que se dejó a propósito

Se encontró auditando el módulo DTE y no se tocó para no mezclar cambios con la
investigación de las boletas:

- **5 `INSERT INTO facturacion_dtes` duplicados** en `app.py`. Cada camino de
  emisión escribe su propia versión de la misma fila. Un cambio de columna hay
  que hacerlo cinco veces y es cuestión de tiempo que uno quede atrás.
- **`_fact_actualizar_estado_dte` definida dos veces.** La segunda gana; la
  primera es código muerto que se lee como si estuviera vivo.
- **`compare_digest` en los guards de token.** Son 78 comparaciones de token con
  `==`, vulnerables a timing. Riesgo bajo pero gratis de arreglar.
- **`facturacion/dtes/emitir_boleta_backend.py`** es un huérfano con columnas
  que ya no existen. No lo importa nadie. Borrarlo.
- **`SII_URLS["rce"]`** en `facturacion/utils.py` apunta a `palena` en los dos
  ambientes, y no coincide con ningún endpoint real de boletas. Es código
  muerto, pero es una mina: el día que alguien lo use, manda a producción algo
  de certificación. Borrarlo.

---

## 5. Operativos, no de código

- **`LUSYNC_FERNET_KEY` en Render.** Sin ella los certificados `.pfx` no se
  pueden desencriptar. Guardar copia en el gestor de contraseñas.
- **CAF nuevo de Nota de Crédito.** El rango 275-279 está agotado.
- **Folios de boleta:** quedan 25212-25237. Los folios 25208, 25209 y 25210 se
  consumieron en documentos que el SII rechazó y no se reutilizan.
- **`www.lusync.cl` no responde.** El dominio sin `www` sí. Hay que agregarlo
  como dominio personalizado en Render y apuntarle un CNAME.
- **RUT de relleno en `tenants`.** La semilla dejó `'76.XXX.XXX-X'`
  (`tenancy.py:255`). La pantalla de facturación ya no lo usa —toma el del
  emisor— pero la fila sigue mal y cualquier otra vista que lo lea lo va a
  mostrar así.
- **`SDCEM001`** está publicado en el fulfillment de Walmart y Lusync no lo
  reconoce. Mientras no esté mapeado, ese producto es invisible: no se le
  sincroniza stock ni se le descuenta una venta.

---

## 6. Probado a medias

Cambios que se hicieron y no se pudieron verificar corriendo el sistema, porque
esta máquina no tiene las dependencias instaladas. Conviene mirarlos en el
primer uso real:

- Interactividad del tablero: clic en las barras del top de productos, en la
  dona de canales, en la distribución de stock y en las filas de stock crítico.
- `/walmart/sync_stock` y `/walmart/forzar_sync_todos` después del arreglo del
  conteo y del mapeo.
