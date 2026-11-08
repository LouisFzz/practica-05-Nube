"""Mide cuánto tarda la cocina en terminar una ráfaga de pedidos.

Repone el inventario, lanza los pedidos a la vez (un tinto cada uno), espera a
que todos queden listos o rechazados y calcula el tiempo desde el primer pedido
creado hasta el último terminado con las marcas de tiempo que guarda Firestore.

Uso, desde Cloud Shell:
    python3 pruebas/medir.py "$(cd infra && terraform output -raw url_backend)" 60
"""
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.request import Request, urlopen

BACKEND = sys.argv[1].rstrip("/")
CANTIDAD = int(sys.argv[2]) if len(sys.argv) > 2 else 60
TERMINADOS = ("listo", "rechazado")


def llamar(ruta, datos=None):
    cuerpo = json.dumps(datos).encode() if datos is not None else None
    peticion = Request(BACKEND + ruta, data=cuerpo, headers={"Content-Type": "application/json"})
    with urlopen(peticion, timeout=60) as respuesta:
        return json.load(respuesta)


def pedir(i):
    return llamar("/pedidos", {"cliente": f"Medida {i}", "items": [{"producto": "tinto", "cantidad": 1}]})


llamar("/inventario/reiniciar", {})

inicio = time.time()
with ThreadPoolExecutor(20) as hilos:
    ids = [pedido["id"] for pedido in hilos.map(pedir, range(1, CANTIDAD + 1))]
print(f"{CANTIDAD} pedidos creados en {time.time() - inicio:.1f} s")

while True:
    time.sleep(3)
    with ThreadPoolExecutor(20) as hilos:
        pedidos = list(hilos.map(lambda pedido_id: llamar(f"/pedidos/{pedido_id}"), ids))
    pendientes = sum(pedido["estado"] not in TERMINADOS for pedido in pedidos)
    print(f"  {pendientes} pendientes")
    if not pendientes:
        break

primero = min(datetime.fromisoformat(pedido["creado"]) for pedido in pedidos)
ultimo = max(datetime.fromisoformat(pedido["actualizado"]) for pedido in pedidos)
listos = sum(pedido["estado"] == "listo" for pedido in pedidos)
print(f"Listos: {listos}, rechazados: {CANTIDAD - listos}")
print(f"Del primer pedido creado al último terminado: {(ultimo - primero).total_seconds():.1f} s")
