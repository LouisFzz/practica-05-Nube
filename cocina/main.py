import base64
import binascii
import logging
import os
import random
import time
from collections import Counter
from datetime import datetime, timezone
from functools import cache

from flask import Flask, request
from google.cloud import firestore

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("cocina")

BASE_DATOS = os.environ.get("BASE_DATOS", "(default)")
TIEMPO_PREPARACION = float(os.environ.get("TIEMPO_PREPARACION", "6"))
PROBABILIDAD_FALLO = float(os.environ.get("PROBABILIDAD_FALLO", "0"))
PLATO_MISTERIOSO = "plato-misterioso"
ESTADOS_FINALES = ("listo", "rechazado")

app = Flask(__name__)


@cache
def db():
    return firestore.Client(database=BASE_DATOS)


def ahora():
    return datetime.now(timezone.utc)


@firestore.transactional
def marcar_preparando(transaccion, pedido_ref):
    """Pasa el pedido a preparando, salvo que otra entrega ya lo haya terminado.

    Devuelve el pedido tal como estaba antes de marcarlo.
    """
    pedido = pedido_ref.get(transaction=transaccion)
    if pedido.exists and pedido.get("estado") not in ESTADOS_FINALES:
        transaccion.update(pedido_ref, {"estado": "preparando", "actualizado": ahora()})
    return pedido


@firestore.transactional
def terminar(transaccion, pedido_ref, cantidades):
    """Revisa el estado del pedido y las existencias, y descuenta, en una sola transacción.

    Si dos entregas del mismo pedido llegan a la vez, Firestore no deja que las
    dos transacciones terminen sobre los mismos datos: una se repite, vuelve a
    leer el pedido, lo encuentra terminado y no descuenta nada. Por la misma
    razón, dos pedidos distintos no pueden gastar las mismas existencias.
    Devuelve ("listo" | "rechazado" | "repetido", productos que faltan).
    """
    pedido = pedido_ref.get(transaction=transaccion)
    if pedido.get("estado") in ESTADOS_FINALES:
        return "repetido", []

    inventario = db().collection("inventario")
    # En una transacción todas las lecturas van antes de cualquier escritura.
    existencias = {
        producto: inventario.document(producto).get(transaction=transaccion).get("existencias") or 0
        for producto in cantidades
    }
    faltan = [producto for producto, cantidad in cantidades.items() if existencias[producto] < cantidad]
    if faltan:
        transaccion.update(pedido_ref, {
            "estado": "rechazado",
            "motivo": "Se acabó: " + ", ".join(faltan),
            "actualizado": ahora(),
        })
        return "rechazado", faltan

    for producto, cantidad in cantidades.items():
        transaccion.update(inventario.document(producto), {"existencias": existencias[producto] - cantidad})
    transaccion.update(pedido_ref, {"estado": "listo", "actualizado": ahora()})
    return "listo", []


@app.post("/")
def recibir():
    # Pub/Sub entrega un sobre JSON: {"message": {"data": <base64>, ...},
    # "subscription": ..., "deliveryAttempt": n}. El último campo solo viene
    # cuando la suscripción tiene política de mensajes muertos.
    sobre = request.get_json(silent=True)
    try:
        pedido_id = base64.b64decode(sobre["message"]["data"], validate=True).decode()
    except (TypeError, KeyError, binascii.Error, UnicodeDecodeError):
        log.error("Llegó un sobre que no es de Pub/Sub o no trae data: %r", sobre)
        return "sobre inválido", 400

    intento = sobre.get("deliveryAttempt")
    log.info("Pedido %s, intento de entrega %s", pedido_id, intento or "desconocido")

    pedido_ref = db().collection("pedidos").document(pedido_id)
    pedido = marcar_preparando(db().transaction(), pedido_ref)
    if not pedido.exists:
        # Se confirma igual: reintentar un pedido que no existe no lo va a crear.
        log.warning("El pedido %s no existe; se descarta el mensaje", pedido_id)
        return "", 204

    if pedido.get("estado") in ESTADOS_FINALES:
        # Entrega repetida de un pedido terminado: se confirma sin volver a prepararlo.
        log.info("Pedido %s ya estaba %s; se confirma sin prepararlo otra vez", pedido_id, pedido.get("estado"))
        return "", 204

    cantidades = Counter()
    for item in pedido.get("items"):
        cantidades[item["producto"]] += item["cantidad"]

    if PLATO_MISTERIOSO in cantidades:
        raise RuntimeError(f"La cocina no tiene receta para el plato misterioso (pedido {pedido_id})")

    time.sleep(TIEMPO_PREPARACION)

    # En una ráfaga muchos pedidos compiten por el mismo producto y Firestore
    # repite las transacciones que chocan; 5 intentos (el valor por defecto) se quedan cortos.
    resultado, faltan = terminar(db().transaction(max_attempts=15), pedido_ref, cantidades)
    if resultado == "repetido":
        log.info("Pedido %s ya lo terminó otra entrega; no se descuenta otra vez", pedido_id)
    elif resultado == "rechazado":
        log.info("Pedido %s rechazado, no alcanza: %s", pedido_id, faltan)
    else:
        log.info("Pedido %s listo", pedido_id)

    # Simula que la cocina se cae justo después de terminar y antes de confirmar.
    # Pub/Sub no recibe el 2xx y vuelve a entregar el mismo pedido.
    if random.random() < PROBABILIDAD_FALLO:
        log.warning("Falla simulada en el pedido %s: el trabajo quedó hecho pero no se confirma", pedido_id)
        return "falla simulada", 500

    return "", 204
