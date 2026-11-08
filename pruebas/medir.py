"""Mide cuánto tarda la cocina en terminar una ráfaga de pedidos.

Repone el inventario, lanza los pedidos a la vez (un tinto cada uno), espera a
que todos queden listos o rechazados y calcula el tiempo desde el primer pedido
creado hasta el último terminado con las marcas de tiempo que guarda Firestore.

Uso, desde Cloud Shell:
    python3 pruebas/medir.py "$(cd infra && terraform output -raw url_backend)" 60
"""
import http.client
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.parse import urlsplit

HOST = urlsplit(sys.argv[1]).hostname
CANTIDAD = int(sys.argv[2]) if len(sys.argv) > 2 else 60
HILOS = 20
TERMINADOS = ("listo", "rechazado")

# Cada hilo abre una conexión y la reutiliza. Abrir una por petición obliga a
# resolver el nombre del backend cientos de veces, el DNS de Cloud Shell se
# satura y la ráfaga llega tan despacio que la cocina nunca hace fila.
locales = threading.local()


def llamar(ruta, datos=None):
    cuerpo = json.dumps(datos) if datos is not None else None
    for intento in range(5):
        if not hasattr(locales, "conexion"):
            locales.conexion = http.client.HTTPSConnection(HOST, timeout=60)
        try:
            locales.conexion.request(
                "POST" if datos is not None else "GET", ruta,
                body=cuerpo, headers={"Content-Type": "application/json"},
            )
            respuesta = locales.conexion.getresponse()
            contenido = respuesta.read()
        except (OSError, http.client.HTTPException):
            # La conexión se cayó antes de recibir respuesta: se abre otra y se repite.
            locales.conexion.close()
            del locales.conexion
            if intento == 4:
                raise
            time.sleep(1 + intento)
            continue
        if respuesta.status >= 400:
            raise RuntimeError(f"{ruta} respondió {respuesta.status}: {contenido[:200]!r}")
        return json.loads(contenido)


def pedir(i):
    return llamar("/pedidos", {"cliente": f"Medida {i}", "items": [{"producto": "tinto", "cantidad": 1}]})


llamar("/inventario/reiniciar", {})

inicio = time.time()
with ThreadPoolExecutor(HILOS) as hilos:
    ids = [pedido["id"] for pedido in hilos.map(pedir, range(1, CANTIDAD + 1))]
print(f"{CANTIDAD} pedidos creados en {time.time() - inicio:.1f} s")

while True:
    time.sleep(3)
    with ThreadPoolExecutor(HILOS) as hilos:
        pedidos = list(hilos.map(lambda pedido_id: llamar(f"/pedidos/{pedido_id}"), ids))
    pendientes = sum(pedido["estado"] not in TERMINADOS for pedido in pedidos)
    print(f"  {pendientes} pendientes")
    if not pendientes:
        break

primero = min(datetime.fromisoformat(pedido["creado"]) for pedido in pedidos)
ultimo_creado = max(datetime.fromisoformat(pedido["creado"]) for pedido in pedidos)
ultimo = max(datetime.fromisoformat(pedido["actualizado"]) for pedido in pedidos)
listos = sum(pedido["estado"] == "listo" for pedido in pedidos)
print(f"Listos: {listos}, rechazados: {CANTIDAD - listos}")
print(f"Del primer pedido creado al último creado: {(ultimo_creado - primero).total_seconds():.1f} s")
print(f"Del primer pedido creado al último terminado: {(ultimo - primero).total_seconds():.1f} s")
