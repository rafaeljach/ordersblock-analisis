"""
API HTTP del análisis sin IA para ordersblock.com.

Endpoints (todos GET, respuesta JSON):
  /salud                                  estado del servicio
  /mercados                               catálogo disponible
  /analisis/{mercado}?tipo=mercado|precision&fundamental=1
  /prediccion/{mercado}?minutos=5|15|30|60&strike=&cuota=
  /bloques/{mercado}                      los 4 vencimientos de reloj de ahora
  /memoria?mercado=BTC                    fiabilidad verificada (lo que lleva aprendido)
  /oportunidades?excluir=EURUSD&n=5       dónde SÍ hay ventaja ahora (escáner en segundo plano)
  /precios                                cintillo: mercados principales (público, sin clave, caché 30 s)

Seguridad: si ORDERSBLOCK_API_KEY está definida, toda ruta salvo /salud
exige la cabecera  X-API-Key: <clave>. Sin ella cualquiera podría usar
el droplet como servicio de señales gratis y tumbar a ICC FUTUROS, que
comparte la máquina.

Arranque:  uvicorn api:app --host 127.0.0.1 --port 8091 --workers 1
(UN solo worker: la memoria SQLite y el limitador de datos son por proceso.)
"""

import asyncio
import contextlib
import hmac
import logging
import os
import time

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

import escaner
import memoria
import precios
import predicciones
import servicio

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("ordersblock.api")

API_KEY = os.getenv("ORDERSBLOCK_API_KEY", "").strip()
ORIGENES = [o.strip() for o in os.getenv(
    "ORDERSBLOCK_CORS", "https://ordersblock.com,https://www.ordersblock.com").split(",") if o.strip()]
INTERVALO_VERIFICACION = int(os.getenv("ORDERSBLOCK_VERIFICAR_CADA_SEG", "120"))

# Un análisis completo descarga velas y corre Montecarlo: limitar la
# concurrencia evita que diez consultas simultáneas se coman las dos
# vCPU que también usa ICC FUTUROS.
_semaforo = asyncio.Semaphore(int(os.getenv("ORDERSBLOCK_CONCURRENCIA", "2")))

# Límite simple por IP (o por usuario si la app lo manda): N consultas/min.
LIMITE_POR_MINUTO = int(os.getenv("ORDERSBLOCK_LIMITE_MINUTO", "20"))
_ventanas: dict = {}


def _cliente(request: Request) -> str:
    return (request.headers.get("X-Usuario")
            or request.headers.get("CF-Connecting-IP")
            or (request.client.host if request.client else "anon"))


def _limite_ok(cliente: str) -> bool:
    ahora = time.time()
    v = [t for t in _ventanas.get(cliente, []) if ahora - t < 60]
    if len(v) >= LIMITE_POR_MINUTO:
        _ventanas[cliente] = v
        return False
    v.append(ahora)
    _ventanas[cliente] = v
    if len(_ventanas) > 5000:   # limpieza para que no crezca sin fin
        for k in [k for k, t in _ventanas.items() if not t or ahora - t[-1] > 120]:
            _ventanas.pop(k, None)
    return True


def _autorizado(request: Request) -> bool:
    if not API_KEY:
        return True
    return hmac.compare_digest(request.headers.get("X-API-Key", ""), API_KEY)


def _error(codigo: int, mensaje: str) -> JSONResponse:
    return JSONResponse({"ok": False, "error": mensaje}, status_code=codigo)


async def _ejecutar(request: Request, funcion, *args, **kwargs) -> JSONResponse:
    if not _autorizado(request):
        return _error(401, "Falta o es inválida la cabecera X-API-Key.")
    cliente = _cliente(request)
    if not _limite_ok(cliente):
        return _error(429, f"Máximo {LIMITE_POR_MINUTO} consultas por minuto.")
    async with _semaforo:
        try:
            datos = await asyncio.wait_for(asyncio.to_thread(funcion, *args, **kwargs), timeout=60)
            return JSONResponse({"ok": True, "datos": datos})
        except servicio.ErrorServicio as e:
            return _error(e.codigo, e.mensaje)
        except asyncio.TimeoutError:
            return _error(504, "El cálculo tardó demasiado. Intenta de nuevo.")
        except Exception as e:
            logger.exception(f"Fallo en {request.url.path}")
            return _error(500, f"Error interno ({type(e).__name__}).")


def _float(v):
    if v in (None, ""):
        return None
    try:
        return float(str(v).replace(",", "."))
    except ValueError:
        raise servicio.ErrorServicio(400, f"Número inválido: {v}")


# ---------------------------------------------------------------------------
# Rutas
# ---------------------------------------------------------------------------

async def salud(request: Request):
    return JSONResponse({"ok": True, "servicio": "ordersblock-analisis",
                         "ia": False, "hora_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})


async def mercados(request: Request):
    if not _autorizado(request):
        return _error(401, "Falta o es inválida la cabecera X-API-Key.")
    return JSONResponse({"ok": True, "datos": servicio.mercados_disponibles()})


async def analisis(request: Request):
    mercado = request.path_params["mercado"]
    tipo = request.query_params.get("tipo", "mercado")
    con_fund = request.query_params.get("fundamental", "1") not in ("0", "false", "no")
    return await _ejecutar(request, servicio.analizar, mercado, tipo, con_fund,
                           request.headers.get("X-Usuario"))


async def prediccion(request: Request):
    mercado = request.path_params["mercado"]
    try:
        minutos = int(request.query_params.get("minutos", "15"))
        strike = _float(request.query_params.get("strike"))
        cuota = _float(request.query_params.get("cuota"))
    except (ValueError, servicio.ErrorServicio) as e:
        return _error(400, getattr(e, "mensaje", "Parámetros inválidos."))
    return await _ejecutar(request, servicio.predecir, mercado, minutos, strike, cuota,
                           request.headers.get("X-Usuario"))


async def bloques(request: Request):
    """Los vencimientos de reloj vigentes, para pintar los botones en la app."""
    if not _autorizado(request):
        return _error(401, "Falta o es inválida la cabecera X-API-Key.")
    out = []
    for h in predicciones.HORIZONTES:
        v = predicciones.proximo_vencimiento(h)
        out.append({"minutos": h, "vence_utc": v["etiqueta"],
                    "segundos_restantes": round(v["segundos_restantes"])})
    return JSONResponse({"ok": True, "datos": out})


async def oportunidades(request: Request):
    """Ranking del escáner: mercados con esperanza positiva y consenso suficiente, ya calculados."""
    if not _autorizado(request):
        return _error(401, "Falta o es inválida la cabecera X-API-Key.")
    try:
        n = int(request.query_params.get("n", "5"))
    except ValueError:
        n = 5
    return JSONResponse({"ok": True, "datos": escaner.oportunidades(request.query_params.get("excluir"), n)})


async def precios_ruta(request: Request):
    """Público: lo lee el navegador del usuario para el cintillo. Sin clave (no expone nada privado)."""
    if not _limite_ok(_cliente(request)):
        return _error(429, "Demasiadas consultas; espera un minuto.")
    return JSONResponse({"ok": True, "datos": precios.snapshot()},
                        headers={"Cache-Control": "public, max-age=30"})


async def memoria_ruta(request: Request):
    if not _autorizado(request):
        return _error(401, "Falta o es inválida la cabecera X-API-Key.")
    m = request.query_params.get("mercado")
    return JSONResponse({"ok": True, "datos": memoria.resumen(m.upper() if m else None)})


# ---------------------------------------------------------------------------
# Worker de verificación (cierra el ciclo de aprendizaje)
# ---------------------------------------------------------------------------

async def _worker_verificacion():
    await asyncio.sleep(30)
    while True:
        try:
            r = await asyncio.to_thread(servicio.resolver_pendientes)
            if r["predicciones"] or r["senales"]:
                logger.info(f"Verificadas: {r}")
        except Exception as e:
            logger.error(f"Worker de verificación: {type(e).__name__}: {e}")
        await asyncio.sleep(INTERVALO_VERIFICACION)


@contextlib.asynccontextmanager
async def _ciclo_de_vida(app):
    memoria.inicializar()
    tarea = None
    if os.getenv("ORDERSBLOCK_SIN_WORKER") != "1":
        tarea = asyncio.get_running_loop().create_task(_worker_verificacion())
    if os.getenv("ORDERSBLOCK_SIN_ESCANER") != "1":
        escaner.arrancar()
    if os.getenv("ORDERSBLOCK_SIN_PRECIOS") != "1":
        precios.arrancar()
    logger.info(f"ordersblock-analisis listo (API key {'activa' if API_KEY else 'DESACTIVADA'})")
    try:
        yield
    finally:
        if tarea:
            tarea.cancel()
        escaner.detener()
        precios.detener()


app = Starlette(
    routes=[
        Route("/salud", salud),
        Route("/mercados", mercados),
        Route("/analisis/{mercado}", analisis),
        Route("/prediccion/{mercado}", prediccion),
        Route("/bloques/{mercado}", bloques),
        Route("/bloques", bloques),
        Route("/memoria", memoria_ruta),
        Route("/oportunidades", oportunidades),
        Route("/precios", precios_ruta),
    ],
    middleware=[Middleware(CORSMiddleware, allow_origins=ORIGENES, allow_methods=["GET"],
                           allow_headers=["X-API-Key", "X-Usuario"])],
    lifespan=_ciclo_de_vida,
)
