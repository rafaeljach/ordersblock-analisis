"""
Cintillo de precios para ordersblock.com.

Un hilo refresca en memoria los mercados principales y la ruta pública
/precios devuelve el JSON. El navegador del usuario lo consulta directo
(sin pasar por el Worker de Cloudflare), así que mil visitantes cuestan
lo mismo que uno: lo que pida este hilo.

Fuentes (todas gratis):
  - Cripto y oro: tickers públicos de Bybit (sin clave, sin límite práctico).
  - Forex (EURUSD, GBPUSD, USDJPY): ticker público de Kraken (sin clave, sin
    cuota diaria), al mismo ritmo que la cripto.
  - Plata, y forex solo si Kraken falla: Twelve Data /quote (1 crédito por
    símbolo). OJO: el plan gratuito da 800 créditos AL DÍA compartidos con el
    motor de análisis, el escáner y las velas de la vista Mercado. Antes este
    hilo pedía 4 símbolos cada 10 min (576 créditos diarios, sin pasar por el
    contador del motor) y dejaba a los análisis sin cuota. Ahora:
      * cada crédito pide permiso al limitador del motor con prioridad de
        proceso automático: nunca toca la reserva de los usuarios;
      * como mucho cada ORDERSBLOCK_PRECIOS_FX_SEG (1800 s por defecto);
      * si Twelve Data dice que se acabaron los créditos del día, no se le
        vuelve a pedir nada hasta las 00:00 UTC.

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
KRAKEN = "https://api.kraken.com/0/public/Ticker"

# Orden en que se muestran. (clave, fuente, símbolo en la fuente, decimales)
MERCADOS = [
    ("BTCUSD", "bybit", "BTCUSDT", 0),
    ("ETHUSD", "bybit", "ETHUSDT", 1),
    ("SOLUSD", "bybit", "SOLUSDT", 2),
    ("XAUUSD", "bybit", "XAUUSDT", 2),
    ("XAGUSD", "twelve", "XAG/USD", 3),
    ("EURUSD", "kraken", "EURUSD", 5),
    ("GBPUSD", "kraken", "GBPUSD", 5),
    ("USDJPY", "kraken", "USDJPY", 3),
]
# Símbolo en Twelve Data para usarlo de respaldo si Kraken no responde
RESPALDO_TWELVE = {"EURUSD": "EUR/USD", "GBPUSD": "GBP/USD", "USDJPY": "USD/JPY"}

CRIPTO_SEG = int(os.getenv("ORDERSBLOCK_PRECIOS_CRIPTO_SEG", "30"))
FX_SEG = int(os.getenv("ORDERSBLOCK_PRECIOS_FX_SEG", "1800"))
TWELVE_KEY = os.getenv("TWELVEDATA_API_KEY", "").strip()

_lock = threading.Lock()
_precios: dict = {}          # clave -> {"precio", "cambio_pct", "hora_utc", "fuente"}
_hilo = None
_parar = threading.Event()
_twelve_bloqueado_hasta = {"ts": 0.0}   # créditos del día agotados: no insistir hasta las 00:00 UTC


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


def refrescar_kraken(get=requests.get) -> list:
    """Forex desde Kraken, un par por petición (no depende del nombre interno del par).
    Variación = contra la apertura del día UTC. Devuelve las claves que NO se pudieron actualizar."""
    fallidos = []
    for clave, fuente, symbol, dec in MERCADOS:
        if fuente != "kraken":
            continue
        try:
            d = get(KRAKEN, params={"pair": symbol}, timeout=8).json()
            res = d.get("result") or {}
            if d.get("error") or len(res) != 1:
                fallidos.append(clave)
                continue
            t = next(iter(res.values()))
            ultimo, apertura = float(t["c"][0]), float(t.get("o") or 0)
            if ultimo <= 0:
                fallidos.append(clave)
                continue
            pct = (ultimo - apertura) / apertura * 100 if apertura > 0 else None
            _guardar(clave, ultimo, pct, "kraken", dec)
        except Exception as e:  # red, JSON, campo ausente
            fallidos.append(clave)
            logger.warning(f"precios kraken {symbol}: {type(e).__name__}: {e}")
    return fallidos


def _permiso_motor():
    """Cada crédito de Twelve Data pasa por el limitador del motor, como proceso automático:
    cede ante los análisis de los usuarios y queda contado en el total del día."""
    try:
        import real_engine
        return lambda: real_engine._pedir_permiso(real_engine.PRIORIDAD_WORKER, esperar=False)
    except Exception:
        return lambda: True


def _manana_utc() -> float:
    t = datetime.now(timezone.utc)
    return datetime(t.year, t.month, t.day, tzinfo=timezone.utc).timestamp() + 86400


def refrescar_twelve(get=requests.get, clave_api=None, claves=None, permiso=None) -> int:
    """Una llamada /quote solo con los símbolos que tengan permiso de cuota (1 crédito por símbolo).
    claves: qué mercados pedir (por defecto, los que son de Twelve Data en MERCADOS)."""
    clave_api = TWELVE_KEY if clave_api is None else clave_api
    if not clave_api or time.time() < _twelve_bloqueado_hasta["ts"]:
        return 0
    if claves is None:
        claves = [c for c, f, _, _ in MERCADOS if f == "twelve"]
    dec_de = {c: d for c, _, _, d in MERCADOS}
    sym_de = {c: s for c, f, s, _ in MERCADOS if f == "twelve"}
    sym_de.update(RESPALDO_TWELVE)
    permiso = permiso or _permiso_motor()
    pares = []
    for c in claves:
        if c not in sym_de:
            continue
        if not permiso():          # sin margen de cuota: se deja para la próxima vuelta
            break
        pares.append((c, sym_de[c], dec_de.get(c, 5)))
    if not pares:
        return 0
    try:
        r = get(TWELVE_QUOTE, params={"symbol": ",".join(s for _, s, _ in pares), "apikey": clave_api}, timeout=10)
        d = r.json()
    except Exception as e:
        logger.warning(f"precios twelve: {type(e).__name__}: {e}")
        return 0
    if isinstance(d, dict) and d.get("status") == "error":
        msg = str(d.get("message", "")).lower()
        if "credits" in msg or "run out" in msg or str(d.get("code")) == "429":
            # Se acabaron los créditos del día: no insistir (cada intento fallido también molesta al motor)
            _twelve_bloqueado_hasta["ts"] = _manana_utc() if ("day" in msg or "daily" in msg or "run out" in msg) else time.time() + 120
            logger.warning(f"precios twelve: cuota agotada, en pausa ({d.get('message')})")
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
            fallidos = refrescar_kraken()
            # Twelve Data solo para la plata y, si Kraken no respondió, para el forex que falte; como mucho cada FX_SEG
            if time.time() - ultimo_fx >= FX_SEG and (_es_horario_fx() or not _precios.get("XAGUSD")):
                refrescar_twelve(claves=[c for c, f, _, _ in MERCADOS if f == "twelve"] + fallidos)
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
    logger.info(f"cintillo de precios activo: cripto y forex (Kraken) cada {CRIPTO_SEG}s, plata (Twelve Data) cada {FX_SEG}s"
                + ("" if TWELVE_KEY else " (sin TWELVEDATA_API_KEY: sin plata)"))


def detener():
    _parar.set()


def snapshot() -> dict:
    """Lo que devuelve /precios: lista en el orden de MERCADOS, solo los que tienen dato."""
    with _lock:
        items = [{"mercado": c, **_precios[c]} for c, _, _, _ in MERCADOS if c in _precios]
    return {"actualizado_utc": _ahora(), "mercados": items,
            "nota": "Precios de referencia; pueden diferir de los de tu bróker."}
