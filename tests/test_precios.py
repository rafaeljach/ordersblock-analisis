"""
Pruebas del cintillo de precios (sin red): fuentes simuladas, formato de /precios,
tolerancia a fallos y, sobre todo, que no se coma la cuota diaria de Twelve Data.

    python -m tests.test_precios
"""

import os
import sys
import time

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)
os.environ.pop("TWELVEDATA_API_KEY", None)

import precios  # noqa: E402


class _Resp:
    def __init__(self, data):
        self._d = data

    def json(self):
        return self._d


def _get_bybit(url, params=None, timeout=0):
    sym = params["symbol"]
    if sym == "SOLUSDT":
        raise ConnectionError("caido")
    base = {"BTCUSDT": ("84012.5", "-0.0123"), "ETHUSDT": ("3011.42", "0.0211"), "XAUUSDT": ("4056.31", "-0.0040")}[sym]
    return _Resp({"retCode": 0, "result": {"list": [{"lastPrice": base[0], "price24hPcnt": base[1]}]}})


# Formato documentado de Kraken: result -> {nombre_interno: {"c": [ultimo, vol], "o": apertura_del_dia, ...}}
_KRAKEN = {"EURUSD": ("ZEURZUSD", "1.084215", "1.083000"), "GBPUSD": ("ZGBPZUSD", "1.271100", "1.272371"),
           "USDJPY": ("ZUSDZJPY", "151.2345", "151.2345")}
_llamadas_twelve = []


def _get_kraken(url, params=None, timeout=0):
    assert url == precios.KRAKEN
    k, c, o = _KRAKEN[params["pair"]]
    return _Resp({"error": [], "result": {k: {"a": [c, "1", "1"], "b": [c, "1", "1"], "c": [c, "1000"], "o": o}}})


def _get_kraken_sin_gbp(url, params=None, timeout=0):
    if params["pair"] == "GBPUSD":
        return _Resp({"error": ["EQuery:Unknown asset pair"], "result": {}})
    return _get_kraken(url, params, timeout)


def _get_twelve(url, params=None, timeout=0):
    syms = params["symbol"].split(",")
    _llamadas_twelve.append(syms)
    datos = {"XAG/USD": {"close": "48.1234", "percent_change": "-1.02"},
             "GBP/USD": {"close": "1.271234", "percent_change": "-0.10"},
             "EUR/USD": {"status": "error", "message": "symbol not available"}}
    if len(syms) == 1:
        return _Resp(datos[syms[0]])
    return _Resp({s: datos[s] for s in syms})


def _get_twelve_sin_creditos(url, params=None, timeout=0):
    _llamadas_twelve.append(params["symbol"].split(","))
    return _Resp({"code": 429, "status": "error",
                  "message": "You have run out of API credits for the day. 812 API credits were used, with the current limit being 800."})


def chk(n, c, x=""):
    print(("CORRECTO " if c else "FALLA    ") + n + ("  " + str(x) if x else ""))
    return bool(c)


def main():
    ok = True
    siempre = lambda: True
    n = precios.refrescar_bybit(get=_get_bybit)
    s = precios.snapshot()
    por = {m["mercado"]: m for m in s["mercados"]}
    ok &= chk("1. Bybit: 3 de 4 actualizados (SOL caido no rompe)", n == 3 and "SOLUSD" not in por and por["BTCUSD"]["precio"] == 84012 and por["ETHUSD"]["precio"] == 3011.4, s)
    ok &= chk("2. Variacion 24h en porcentaje con dos decimales y signo", por["BTCUSD"]["cambio_pct"] == -1.23 and por["ETHUSD"]["cambio_pct"] == 2.11)
    ok &= chk("3. Oro desde Bybit con 2 decimales", por["XAUUSD"]["precio"] == 4056.31 and por["XAUUSD"]["fuente"] == "bybit")

    fall = precios.refrescar_kraken(get=_get_kraken)
    por = {m["mercado"]: m for m in precios.snapshot()["mercados"]}
    ok &= chk("4. Forex desde Kraken (gratis, sin cuota): EURUSD, GBPUSD y USDJPY", fall == [] and all(por[k]["fuente"] == "kraken" for k in ("EURUSD", "GBPUSD", "USDJPY")), fall)
    ok &= chk("5. Precio con decimales del par y variacion contra la apertura del dia UTC",
              por["EURUSD"]["precio"] == round(1.084215, 5) and por["EURUSD"]["cambio_pct"] == 0.11 and por["GBPUSD"]["cambio_pct"] == -0.1 and por["USDJPY"]["cambio_pct"] == 0.0 and por["USDJPY"]["precio"] == round(151.2345, 3),
              {k: (por[k]["precio"], por[k]["cambio_pct"]) for k in ("EURUSD", "GBPUSD", "USDJPY")})

    ok &= chk("6. Sin clave de Twelve Data no se le pide nada", precios.refrescar_twelve(get=_get_twelve, clave_api="", permiso=siempre) == 0 and not _llamadas_twelve)

    fall = precios.refrescar_kraken(get=_get_kraken_sin_gbp)
    _llamadas_twelve.clear()
    n3 = precios.refrescar_twelve(get=_get_twelve, clave_api="k", claves=["XAGUSD"] + fall, permiso=siempre)
    por = {m["mercado"]: m for m in precios.snapshot()["mercados"]}
    ok &= chk("7. Si Kraken no da un par, Twelve Data lo cubre en la misma llamada que la plata", fall == ["GBPUSD"] and n3 == 2 and _llamadas_twelve == [["XAG/USD", "GBP/USD"]] and por["GBPUSD"]["fuente"] == "twelvedata" and por["XAGUSD"]["precio"] == 48.123, _llamadas_twelve)

    # El limitador del motor manda: si no da permiso, no se gasta credito
    _llamadas_twelve.clear()
    negar = lambda: False
    ok &= chk("8. Sin permiso del limitador del motor (reserva de usuarios), no se llama a Twelve Data", precios.refrescar_twelve(get=_get_twelve, clave_api="k", permiso=negar) == 0 and not _llamadas_twelve)
    cupo = iter([True, False])
    _llamadas_twelve.clear()
    precios.refrescar_twelve(get=_get_twelve, clave_api="k", claves=["XAGUSD", "GBPUSD"], permiso=lambda: next(cupo))
    ok &= chk("9. Con permiso para 1 credito, pide 1 simbolo (no 2)", _llamadas_twelve == [["XAG/USD"]], _llamadas_twelve)

    # Creditos del dia agotados: pausa hasta las 00:00 UTC
    _llamadas_twelve.clear()
    precios.refrescar_twelve(get=_get_twelve_sin_creditos, clave_api="k", permiso=siempre)
    hasta = precios._twelve_bloqueado_hasta["ts"]
    precios.refrescar_twelve(get=_get_twelve, clave_api="k", permiso=siempre)
    ok &= chk("10. Si Twelve Data dice que se acabaron los creditos del dia, no se le insiste hasta las 00:00 UTC",
              len(_llamadas_twelve) == 1 and hasta == precios._manana_utc() and hasta > time.time(), (_llamadas_twelve, hasta))
    precios._twelve_bloqueado_hasta["ts"] = 0.0

    # Presupuesto: un dia entero del hilo (cripto cada 30 s) gasta poquisimo de Twelve Data
    _llamadas_twelve.clear()
    reloj = [1_000_000.0]
    real_time = precios.time.time
    precios.time.time = lambda: reloj[0]
    try:
        ultimo_fx = 0.0
        for _ in range(int(86400 / precios.CRIPTO_SEG)):
            fallidos = precios.refrescar_kraken(get=_get_kraken)
            if reloj[0] - ultimo_fx >= precios.FX_SEG:
                precios.refrescar_twelve(get=_get_twelve, clave_api="k", claves=["XAGUSD"] + fallidos, permiso=siempre)
                ultimo_fx = reloj[0]
            reloj[0] += precios.CRIPTO_SEG
    finally:
        precios.time.time = real_time
    creditos = sum(len(x) for x in _llamadas_twelve)
    ok &= chk("11. Un dia completo: <= 48 creditos de Twelve Data (antes 576 de 800)", creditos <= 48, f"{creditos} creditos")

    s2 = precios.snapshot()
    ok &= chk("12. El orden del cintillo es el de MERCADOS y lleva nota legal", [m["mercado"] for m in s2["mercados"]] == ["BTCUSD", "ETHUSD", "XAUUSD", "XAGUSD", "EURUSD", "GBPUSD", "USDJPY"] and "bróker" in s2["nota"], [m["mercado"] for m in s2["mercados"]])

    def _caido(url, params=None, timeout=0):
        raise ConnectionError("sin red")
    precios.refrescar_bybit(get=_caido)
    fk = precios.refrescar_kraken(get=_caido)
    por3 = {m["mercado"]: m for m in precios.snapshot()["mercados"]}
    ok &= chk("13. Si las fuentes caen, se conserva el ultimo precio bueno (nunca ceros)", por3["BTCUSD"]["precio"] == 84012 and por3["EURUSD"]["precio"] == round(1.084215, 5) and sorted(fk) == ["EURUSD", "GBPUSD", "USDJPY"])

    # El permiso real sale del limitador del motor como proceso automatico
    try:
        import real_engine
        visto = {}
        orig = real_engine._pedir_permiso
        real_engine._pedir_permiso = lambda prioridad, esperar=True: visto.update(p=prioridad, e=esperar) or True
        try:
            precios._permiso_motor()()
        finally:
            real_engine._pedir_permiso = orig
        ok &= chk("14. Cada credito pide permiso al limitador del motor con prioridad de proceso automatico y sin esperar",
                  visto == {"p": real_engine.PRIORIDAD_WORKER, "e": False}, visto)
    except ImportError as e:
        ok &= chk("14. real_engine importable", False, e)
    print("\nTODO OK" if ok else "\nHAY FALLAS")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
