"""
Cintillo de precios para ordersblock.com.

Un hilo refresca en memoria los mercados principales y la ruta pública
/precios devuelve el JSON. El navegador del usuario lo consulta directo
(sin pasar por el Worker de Cloudflare), así que mil visitantes cuestan
lo mismo que uno: lo que pida este hilo.

Fuentes:
  - Cripto y oro: tickers públicos de Bybit (sin clave, sin límite práctico).
  - Plata y forex: Twelve Data /quote (1 crédito por símbolo). El plan
    gratuito da 800 créditos al día, así que se refresca cada
    ORDERSBLOCK_PRECIOS_FX_SEG (600 s por defecto) y solo de lunes a viernes:
    4 símbolos x 6/h x 24 h x 5 días = 2.880 a la semana, bien por debajo
    del tope semanal y sin estorbar al motor de análisis.

Si una fuente falla, se conserva el último precio bueno con su hora: el
cintillo nunca muestra ceros.
"""

import logging
import os
import threading
import time
from datetime import datetime, timezone

import requests

logger = logging.getLogger("ordersblock.precios")

BYBIT = "https://api.bybit.com/v5/market/tickers"
TWELVE_QUOTE = "https://api.twelvedata.com/quote"

# Orden en que se muestran. (clave, fuente, símbolo en la fuente, decimales)
MERCADOS = [
    ("BTCUSD", "bybit", "BTCUSDT", 0),
    ("ETHUSD", "bybit", "ETHUSDT", 1),
    ("SOLUSD", "bybit", "SOLUSDT", 2),
    ("XAUUSD", "bybit", "XAUUSDT", 2),
    ("XAGUSD", "twelve", "XAG/USD", 3),
    ("EURUSD", "twelve", "EUR/USD", 5),
    ("GBPUSD", "twelve", "GBP/USD", 5),
    ("USDJPY", "twelve", "USD/JPY", 3),
]

CRIPTO_SEG = int(os.getenv("ORDERSBLOCK_PRECIOS_CRIPTO_SEG", "30"))
FX_SEG = int(os.getenv("ORDERSBLOCK_PRECIOS_FX_SEG", "600"))
TWELVE_KEY = os.getenv("TWELVEDATA_API_KEY", "").strip()

_lock = threading.Lock()
_precios: dict = {}          # clave -> {"precio", "cambio_pct", "hora_utc", "fuente"}
_hilo = None
_parar = threading.Event()


def _ahora():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _guardar(clave, precio, cambio_pct, fuente, decimales):
    with _lock:
        _precios[clave] = {
            "precio": round(float(precio), decimales),
            "cambio_pct": None if cambio_pct is None else round(float(cambio_pct), 2),
            "hora_utc": _ahora(),
            "fuente": fuente,
        }


def refrescar_bybit(get=requests.get) -> int:
    """Tickers lineales de Bybit: precio y variación 24 h. Devuelve cuántos actualizó."""
    n = 0
    for clave, fuente, symbol, dec in MERCADOS:
        if fuente != "bybit":
            continue
        try:
            r = get(BYBIT, params={"category": "linear", "symbol": symbol}, timeout=8)
            d = r.json()
            lista = (d.get("result") or {}).get("list") or []
            if d.get("retCode") != 0 or not lista:
                continue
            t = lista[0]
            _guardar(clave, t["lastPrice"], float(t.get("price24hPcnt", 0)) * 100, "bybit", dec)
            n += 1
        except Exception as e:  # red, JSON, campo ausente
            logger.warning(f"precios bybit {symbol}: {type(e).__name__}: {e}")
    return n


def refrescar_twelve(get=requests.get, clave_api=None) -> int:
    """Una sola llamada /quote con todos los símbolos forex y metales (1 crédito por símbolo)."""
    clave_api = TWELVE_KEY if clave_api is None else clave_api
    if not clave_api:
        return 0
    pares = [(c, s, dec) for c, f, s, dec in MERCADOS if f == "twelve"]
    try:
        r = get(TWELVE_QUOTE, params={"symbol": ",".join(s for _, s, _ in pares), "apikey": clave_api}, timeout=10)
        d = r.json()
    except Exception as e:
        logger.warning(f"precios twelve: {type(e).__name__}: {e}")
        return 0
    n = 0
    for clave, symbol, dec in pares:
        q = d.get(symbol) if len(pares) > 1 else d   # con un solo símbolo Twelve no anida
        if not isinstance(q, dict) or q.get("status") == "error" or "close" not in q:
            continue
        try:
            _guardar(clave, q["close"], q.get("percent_change"), "twelvedata", dec)
            n += 1
        except (TypeError, ValueError):
            continue
    return n


def _es_horario_fx() -> bool:
    """Forex y plata cotizan de domingo 22:00 UTC a viernes 22:00 UTC."""
    t = datetime.now(timezone.utc)
    wd, h = t.weekday(), t.hour
    if wd == 5:
        return False
    if wd == 6:
        return h >= 22
    if wd == 4:
        return h < 22
    return True


def _bucle():
    ultimo_fx = 0.0
    while not _parar.is_set():
        try:
            refrescar_bybit()
            if time.time() - ultimo_fx >= FX_SEG and (_es_horario_fx() or not _precios.get("EURUSD")):
                refrescar_twelve()
                ultimo_fx = time.time()
        except Exception as e:
            logger.error(f"precios: {type(e).__name__}: {e}")
        _parar.wait(CRIPTO_SEG)


def arrancar():
    global _hilo
    if _hilo and _hilo.is_alive():
        return
    _parar.clear()
    _hilo = threading.Thread(target=_bucle, name="precios", daemon=True)
    _hilo.start()
    logger.info(f"cintillo de precios activo: cripto cada {CRIPTO_SEG}s, forex cada {FX_SEG}s"
                + ("" if TWELVE_KEY else " (sin TWELVEDATA_API_KEY: solo cripto y oro)"))


def detener():
    _parar.set()


def snapshot() -> dict:
    """Lo que devuelve /precios: lista en el orden de MERCADOS, solo los que tienen dato."""
    with _lock:
        items = [{"mercado": c, **_precios[c]} for c, _, _, _ in MERCADOS if c in _precios]
    return {"actualizado_utc": _ahora(), "mercados": items,
            "nota": "Precios de referencia; pueden diferir de los de tu bróker."}
