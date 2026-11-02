"""
Tasa USDT -> COP para los pagos con tarjeta (Bold cobra en pesos).

Fuente: anuncios publicos de venta de USDT en Binance P2P Colombia (sin clave), mediana de los 10 mejores
precios con los que un comprador normal paga en COP. Es el precio real al que tus usuarios comprarian
USDT; sobre el se aplica el recargo del panel. Cache de 10 minutos; si Binance no responde se conserva la
ultima tasa buena hasta 24 h y despues se avisa que no hay tasa (el Worker usa entonces la fija del panel).
"""
import logging
import statistics
import threading
import time

import requests

logger = logging.getLogger("ordersblock.tasa")
URL = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
_cache = {"t": 0.0, "tasa": None, "fuente": "", "muestras": 0}
_lock = threading.Lock()
CADA_SEG = 600
MAX_EDAD_SEG = 24 * 3600


def _consultar(post=requests.post) -> tuple:
    cuerpo = {"asset": "USDT", "fiat": "COP", "tradeType": "BUY", "page": 1, "rows": 10, "payTypes": [], "publisherType": None}
    r = post(URL, json=cuerpo, timeout=10, headers={"User-Agent": "ordersblock-tasa/1.0", "Accept": "application/json"})
    d = r.json()
    precios = []
    for it in (d.get("data") or []):
        adv = it.get("adv") or {}
        try:
            p = float(adv.get("price"))
        except (TypeError, ValueError):
            continue
        if 1000 < p < 100000:
            precios.append(p)
    if len(precios) < 3:
        raise ValueError("pocos anuncios P2P (%d)" % len(precios))
    return statistics.median(precios), len(precios)


def tasa_cop(post=requests.post) -> dict:
    ahora = time.time()
    with _lock:
        if _cache["tasa"] and ahora - _cache["t"] < CADA_SEG:
            return {"tasa": _cache["tasa"], "fuente": _cache["fuente"], "muestras": _cache["muestras"], "edad_seg": int(ahora - _cache["t"])}
    try:
        tasa, n = _consultar(post)
        with _lock:
            _cache.update(t=ahora, tasa=round(tasa, 2), fuente="Binance P2P Colombia (mediana de %d anuncios)" % n, muestras=n)
    except Exception as e:  # red, JSON, bloqueo: se usa la ultima buena
        logger.warning(f"tasa COP: {type(e).__name__}: {e}")
    with _lock:
        if _cache["tasa"] and ahora - _cache["t"] < MAX_EDAD_SEG:
            return {"tasa": _cache["tasa"], "fuente": _cache["fuente"] + (" (cache)" if ahora - _cache["t"] >= CADA_SEG else ""), "muestras": _cache["muestras"], "edad_seg": int(ahora - _cache["t"])}
    return {"tasa": None, "fuente": "sin dato", "muestras": 0, "edad_seg": None}
