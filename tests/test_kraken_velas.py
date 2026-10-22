"""
Velas de forex desde Kraken (sin red): formato, prioridad sobre Twelve Data,
pares que Kraken no tiene, temporalidades no soportadas y caídas.

    python -m tests.test_kraken_velas
"""

import os
import sys
import time

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)

import real_engine as E  # noqa: E402


class _Resp:
    def __init__(self, data):
        self._d = data

    def json(self):
        return self._d


def _filas(n, t0=1_760_000_000, paso=3600, base=1.08):
    filas = []
    for i in range(n):
        o = base + i * 0.0001
        filas.append([t0 + i * paso, f"{o:.5f}", f"{o + 0.0006:.5f}", f"{o - 0.0005:.5f}", f"{o + 0.0002:.5f}", f"{o:.5f}", "12.5", 30])
    return filas


llamadas = []


def get_kraken(url, params=None, timeout=0):
    llamadas.append((url, dict(params or {})))
    if params["pair"] in ("USDCOP", "USDMXN"):
        return _Resp({"error": ["EQuery:Unknown asset pair"]})
    nombre = {"EURUSD": "ZEURZUSD", "GBPUSD": "ZGBPZUSD", "USDJPY": "ZUSDZJPY"}.get(params["pair"], params["pair"])
    return _Resp({"error": [], "result": {nombre: _filas(720, paso=params["interval"] * 60), "last": 1}})


def chk(n, c, x=""):
    print(("CORRECTO " if c else "FALLA    ") + n + ("  " + str(x) if x else ""))
    return bool(c)


def main():
    ok = True
    v = E._velas_desde_kraken("EURUSD", "1h", 160, get=get_kraken)
    ok &= chk("1. Kraken: 160 velas de EURUSD en el formato del motor (de la mas vieja a la mas nueva)",
              v and len(v) == 160 and set(v[0]) == {"datetime", "open", "high", "low", "close", "volume"} and v[0]["datetime"] < v[-1]["datetime"] and isinstance(v[-1]["close"], float),
              v and v[-1])
    ok &= chk("2. Pide el par e intervalo de Kraken (1h = 60 min) al endpoint OHLC publico",
              llamadas[-1] == (E.KRAKEN_OHLC, {"pair": "EURUSD", "interval": 60}), llamadas[-1])
    ok &= chk("3. H4 = 240 y diario = 1440", E._velas_desde_kraken("GBPUSD", "4h", 100, get=get_kraken) and llamadas[-1][1]["interval"] == 240
              and E._velas_desde_kraken("USDJPY", "1day", 100, get=get_kraken) and llamadas[-1][1]["interval"] == 1440)
    n0 = len(llamadas)
    ok &= chk("4. Temporalidad que Kraken no tiene (2h): no pregunta y deja paso a Twelve Data",
              E._velas_desde_kraken("EURUSD", "2h", 100, get=get_kraken) is None and len(llamadas) == n0)
    ok &= chk("5. Plata, oro y cripto NO pasan por Kraken",
              all(E._velas_desde_kraken(m, "1h", 100, get=get_kraken) is None for m in ("XAGUSD", "XAUUSD", "BTC", "ETH")) and len(llamadas) == n0)
    ok &= chk("6. Par que Kraken no tiene (USDCOP): None y se recuerda 6 h sin volver a preguntar",
              E._velas_desde_kraken("USDCOP", "1h", 100, get=get_kraken) is None and E._KRAKEN_SIN_PAR.get("USDCOP", 0) > time.time() + 5 * 3600)
    n1 = len(llamadas)
    E._velas_desde_kraken("USDCOP", "1h", 100, get=get_kraken)
    ok &= chk("7. ...y la segunda vez ni siquiera llama a Kraken", len(llamadas) == n1)

    def caido(url, params=None, timeout=0):
        raise ConnectionError("sin red")
    ok &= chk("8. Kraken caido: None (el motor sigue con Twelve Data), sin excepcion", E._velas_desde_kraken("EURUSD", "1h", 100, get=caido) is None)
    ok &= chk("9. Respuesta corta (<30 velas): None", E._velas_desde_kraken("EURUSD", "1h", 100, get=lambda u, params=None, timeout=0: _Resp({"error": [], "result": {"ZEURZUSD": _filas(10)}})) is None)

    # Integracion con obtener_datos_mercado: forex sale de Kraken y no gasta creditos de Twelve Data
    twelve = []
    orig = (E._velas_desde_bybit, E._pedir_a_twelvedata, E.requests.get)
    E._velas_desde_bybit = lambda *a, **k: None
    E._pedir_a_twelvedata = lambda mercado, *a, **k: twelve.append(mercado) or {"fuente_datos": "twelvedata", "mercado": mercado}
    E.requests.get = get_kraken
    E._cache.clear()
    try:
        d = E.obtener_datos_mercado("EURUSD", "1h", 160, E.PRIORIDAD_WORKER)
        ok &= chk("10. obtener_datos_mercado(EURUSD): fuente kraken, 0 creditos de Twelve Data, motor completo calculado",
                  d["fuente_datos"] == "kraken" and not twelve and len(d["velas"]) == 160 and d["precio_actual"] > 0 and "volatilidad" in str(d.keys()), (d["fuente_datos"], twelve))
        d2 = E.obtener_datos_mercado("EURUSD", "1h", 160, E.PRIORIDAD_WORKER)
        ok &= chk("11. La segunda consulta sale de la cache del motor (ni Kraken ni Twelve)", d2 is d and not twelve)
        E.obtener_datos_mercado("XAGUSD", "1h", 160, E.PRIORIDAD_WORKER)
        E.obtener_datos_mercado("USDCOP", "1h", 160, E.PRIORIDAD_WORKER)
        ok &= chk("12. Plata y pares que Kraken no tiene siguen yendo a Twelve Data como antes", twelve == ["XAGUSD", "USDCOP"], twelve)
    finally:
        E._velas_desde_bybit, E._pedir_a_twelvedata, E.requests.get = orig
        E._cache.clear()

    print("\nTODO OK" if ok else "\nHAY FALLAS")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
