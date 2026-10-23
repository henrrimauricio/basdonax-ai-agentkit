"""Reproduce, con piezas de mentira, los hallazgos del análisis del repo.

No sale a internet ni gasta tokens: el modelo y Chatwoot son simulados, como
en los tests del propio repo. Cada hallazgo se mide y se informa como
CONFIRMADO o NO CONFIRMADO, con el número que lo respalda. El script no
falla si un hallazgo se confirma: es un informe, no un test.

    python verificacion/hallazgos.py > hallazgos.md
"""

from __future__ import annotations

import logging
import math
import os
import sys
import threading
import time
import traceback
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "src"))
sys.path.insert(0, str(RAIZ / "tests"))

logging.disable(logging.CRITICAL)  # el informe va por stdout, sin ruido

from fastapi.testclient import TestClient  # noqa: E402

from agente.agente import Respuesta  # noqa: E402
from agente.canales.adjuntos import Lector  # noqa: E402
from agente.config import Config  # noqa: E402
from agente.frenos import TopeDeMensajes  # noqa: E402
from agente.web.webhook import crear_app  # noqa: E402
from test_chatwoot import TOKEN, AvisosFalsos, ChatwootFalso, evento  # noqa: E402

URL = f"/chatwoot/{TOKEN}"


# -- Piezas de mentira ---------------------------------------------------------


class AgenteFalso:
    """Responde siempre lo mismo, tardando lo que se le pida (como un modelo)."""

    def __init__(self, segundos: float = 0.0) -> None:
        self.segundos = segundos

    def responder(self, texto: str, conversacion: str = "local") -> Respuesta:
        if self.segundos:
            time.sleep(self.segundos)
        return Respuesta(texto="Listo, te ayudo con eso.", modelo="falso")

    def historial(self, conversacion: str = "local") -> list:
        return []

    def olvidar(self, conversacion: str = "local") -> None:
        pass


class CanalConEtiquetasLentas(ChatwootFalso):
    """Chatwoot tarda en contestar la consulta de etiquetas."""

    def __init__(self, segundos: float) -> None:
        super().__init__()
        self.segundos = segundos

    def _api(self, metodo, camino, datos=None):
        if metodo == "GET" and camino.endswith("/labels"):
            time.sleep(self.segundos)
        return super()._api(metodo, camino, datos)


class CanalConEnvioLento(ChatwootFalso):
    """Mandar la respuesta tarda (como las pausas entre globos)."""

    def __init__(self, segundos: float) -> None:
        super().__init__()
        self.segundos = segundos
        self._candado = threading.Lock()
        self.enviados = 0

    def enviar(self, conversacion, mensajes, antes_de_cada=None, pausa=None):
        time.sleep(self.segundos)
        super().enviar(conversacion, mensajes, antes_de_cada, None)
        with self._candado:
            self.enviados += 1


def configuracion(**ajustes) -> Config:
    config = Config(
        proveedor="claude",
        modelo="modelo-de-prueba",
        api_key="no-hace-falta",
        max_tokens=1024,
        memoria_mensajes=20,
        prompt_sistema=RAIZ / "prompts/sistema.md",
    )
    base = {
        "chatwoot_webhook_token": TOKEN,
        "buffer_segundos": 0.1,
        "pausa_entre_globos": False,
        "espera_segundos": 0,
        "respetar_resueltas": 0,
        "solo_el_primer_mensaje": 0,
        "tope_mensajes_por_dia": 0,
    }
    base.update(ajustes)
    for nombre, valor in base.items():
        setattr(config, nombre, valor)
    return config


def armar(canal, agente, **ajustes):
    config = configuracion(**ajustes)
    app = crear_app(
        config,
        agente=agente,
        canal=canal,
        avisos=AvisosFalsos(),
        tope=TopeDeMensajes(0),
        lector=Lector(canal),
    )
    return app, TestClient(app)


def esperar(condicion, segundos: float) -> bool:
    limite = time.monotonic() + segundos
    while time.monotonic() < limite:
        if condicion():
            return True
        time.sleep(0.02)
    return condicion()


def variables_de(funcion) -> dict:
    """Las variables que una función interna de crear_app tiene guardadas."""
    return {
        nombre: celda.cell_contents
        for nombre, celda in zip(funcion.__code__.co_freevars, funcion.__closure__ or ())
    }


# -- Los hallazgos ---------------------------------------------------------------


def h1_bloqueo_del_event_loop() -> tuple[bool, str]:
    """Un evento sin `labels` hace una llamada HTTP síncrona dentro del endpoint async."""
    demora = 3.0
    canal = CanalConEtiquetasLentas(demora)
    _, cliente = armar(canal, AgenteFalso())
    with cliente as web:
        e = evento("hola", conversacion=101, id_mensaje=5001)
        del e["conversation"]["labels"]
        hilo = threading.Thread(target=lambda: web.post(URL, json=e))
        hilo.start()
        time.sleep(0.5)
        inicio = time.monotonic()
        salud = web.get("/salud")
        latencia = time.monotonic() - inicio
        hilo.join()
    confirmado = latencia > 1.5
    return confirmado, (
        f"Con Chatwoot tardando {demora:.0f}s en devolver etiquetas, `GET /salud` "
        f"(que no hace nada) tardó **{latencia:.2f}s** (status {salud.status_code}). "
        "Mientras dura esa consulta, el servidor no atiende ningún otro pedido."
    )


def h2_concurrencia_del_pool() -> tuple[bool, str]:
    """Todo lo bloqueante comparte el pool por defecto de asyncio.to_thread."""
    n = 40
    modelo_s, envio_s = 1.0, 1.0
    canal = CanalConEnvioLento(envio_s)
    _, cliente = armar(canal, AgenteFalso(modelo_s))
    hilos = min(32, (os.cpu_count() or 1) + 4)
    with cliente as web:
        inicio = time.monotonic()
        for i in range(n):
            web.post(URL, json=evento(f"consulta {i}", conversacion=1000 + i, id_mensaje=20000 + i))
        listo = esperar(lambda: canal.enviados >= n, 180)
        total = time.monotonic() - inicio
    ideal = modelo_s + envio_s
    teorico = math.ceil(n / hilos) * ideal
    confirmado = listo and total > ideal * 2
    return confirmado, (
        f"{n} personas escriben a la vez; cada respuesta tarda {modelo_s:.0f}s de modelo + "
        f"{envio_s:.0f}s de envío. Con hilos ilimitados terminarían en ~{ideal:.0f}s; "
        f"el pool tiene **{hilos} hilos** ({os.cpu_count()} CPU) y tardaron **{total:.1f}s** "
        f"(previsto por el pool: ~{teorico:.0f}s). La última persona esperó ~{total / ideal:.0f}× más."
        + ("" if listo else " ⚠️ No terminaron todas dentro del límite de 180s.")
    )


def h3_una_resuelta_silencia_al_contacto() -> tuple[bool, str]:
    """Con RESPETAR_RESUELTAS=1, cualquier conversación resuelta del contacto lo silencia."""
    canal = ChatwootFalso()
    canal.resueltas_del_contacto = True  # p. ej. una auto-resuelta por inactividad hace meses
    _, cliente = armar(canal, AgenteFalso(), respetar_resueltas=1)
    with cliente as web:
        web.post(URL, json=evento("Hola, quiero hacer un pedido", conversacion=301, id_mensaje=7001))
        esperar(lambda: canal.envios() or canal.etiquetas_mandadas(), 3)
        time.sleep(0.3)
    sin_respuesta = not canal.envios()
    etiquetada = any("humano" in e for e in canal.etiquetas_mandadas())
    confirmado = sin_respuesta and etiquetada
    return confirmado, (
        "Un cliente con una conversación vieja resuelta abre una conversación **nueva**: "
        f"respuestas enviadas = {len(canal.envios())}, etiqueta `humano` puesta = {etiquetada}. "
        "Si la bandeja auto-resuelve por inactividad, todo cliente que vuelve queda sin bot."
    )


def h4_estado_que_no_se_libera() -> tuple[bool, str]:
    """revisadas y candados guardan una entrada por conversación, para siempre."""
    n = 300
    canal = ChatwootFalso()
    app, cliente = armar(canal, AgenteFalso(), respetar_resueltas=1, buffer_segundos=0.05)
    with cliente as web:
        for i in range(n):
            web.post(URL, json=evento("hola", conversacion=50000 + i, id_mensaje=90000 + i))
        esperar(lambda: len(canal.envios()) >= n, 60)
        entrante = next(r.endpoint for r in app.routes if getattr(r, "path", "") == "/chatwoot/{token}")
        vars_entrante = variables_de(entrante)
        revisadas = vars_entrante["revisadas"]
        candados = variables_de(vars_entrante["buffer"].al_completar)["candados"]
        tam_revisadas, tam_candados = len(revisadas), len(candados)
    confirmado = tam_revisadas >= n and tam_candados >= n
    return confirmado, (
        f"Después de atender {n} conversaciones ya terminadas: `revisadas` = {tam_revisadas}, "
        f"`candados` = {tam_candados}. Crecen una por conversación y solo se vacían al reiniciar."
    )


def h5_buffer_cero_retiene_el_200() -> tuple[bool, str]:
    """Con BUFFER_SEGUNDOS=0 el webhook no contesta hasta que el agente terminó."""
    modelo_s = 3.0
    canal = ChatwootFalso()
    _, cliente = armar(canal, AgenteFalso(modelo_s), buffer_segundos=0)
    with cliente as web:
        inicio = time.monotonic()
        r = web.post(URL, json=evento("hola", conversacion=401, id_mensaje=8001))
        latencia = time.monotonic() - inicio
    confirmado = latencia > modelo_s * 0.8
    return confirmado, (
        f"Con `BUFFER_SEGUNDOS=0` y un modelo que tarda {modelo_s:.0f}s, el 200 a Chatwoot "
        f"tardó **{latencia:.2f}s** (status {r.status_code}). Con buffer > 0 sale enseguida. "
        "Un modelo lento con buffer 0 puede hacer que Chatwoot dé el webhook por fallado."
    )


HALLAZGOS = [
    ("1", "Llamada bloqueante en el event loop (etiquetas)", h1_bloqueo_del_event_loop),
    ("2", "Concurrencia limitada por el pool de hilos", h2_concurrencia_del_pool),
    ("3", "Una conversación resuelta silencia al contacto para siempre", h3_una_resuelta_silencia_al_contacto),
    ("4", "Estado en memoria que no se libera", h4_estado_que_no_se_libera),
    ("5", "BUFFER_SEGUNDOS=0 retiene el 200 del webhook", h5_buffer_cero_retiene_el_200),
]


def main() -> int:
    filas, detalles = [], []
    for numero, titulo, prueba in HALLAZGOS:
        try:
            confirmado, detalle = prueba()
            estado = "🔴 CONFIRMADO" if confirmado else "🟢 NO CONFIRMADO"
        except Exception:
            estado = "⚠️ ERROR AL MEDIR"
            detalle = "```\n" + traceback.format_exc()[-1500:] + "\n```"
        filas.append(f"| {numero} | {titulo} | {estado} |")
        detalles.append(f"**{numero}. {titulo}** — {estado}\n\n{detalle}\n")

    print("| # | Hallazgo | Resultado |\n|---|---|---|")
    print("\n".join(filas))
    print()
    print("\n".join(detalles))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
