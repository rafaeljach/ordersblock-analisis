"""
Pruebas del cintillo de precios (sin red): fuentes simuladas, formato de /precios,
tolerancia a fallos y refresco de forex solo cuando hay clave.

    python -m tests.test_precios
"""

import os
import sys

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


def _get_twelve(url, params=None, timeout=0):
    assert "XAG/USD" in params["symbol"] and "EUR/USD" in params["symbol"]
    return _Resp({
        "XAG/USD": {"close": "48.1234", "percent_change": "-1.02"},
        "EUR/USD": {"close": "1.123456", "percent_change": "0.12"},
        "GBP/USD": {"status": "error", "message": "symbol not available"},
        "USD/JPY": {"close": "151.2345", "percent_change": None},
    })


def chk(n, c, x=""):
    print(("CORRECTO " if c else "FALLA    ") + n + ("  " + str(x) if x else ""))
    return bool(c)


def main():
    ok = True
    n = precios.refrescar_bybit(get=_get_bybit)
    s = precios.snapshot()
    por = {m["mercado"]: m for m in s["mercados"]}
    ok &= chk("1. Bybit: 3 de 4 actualizados (SOL caido no rompe)", n == 3 and "SOLUSD" not in por and por["BTCUSD"]["precio"] == 84012 and por["ETHUSD"]["precio"] == 3011.4, s)
    ok &= chk("2. Variacion 24h en porcentaje con dos decimales y signo", por["BTCUSD"]["cambio_pct"] == -1.23 and por["ETHUSD"]["cambio_pct"] == 2.11)
    ok &= chk("3. Oro desde Bybit con 2 decimales", por["XAUUSD"]["precio"] == 4056.31 and por["XAUUSD"]["fuente"] == "bybit")
    n2 = precios.refrescar_twelve(get=_get_twelve, clave_api="")
    ok &= chk("4. Sin clave de Twelve Data no se llama a forex (0 actualizados)", n2 == 0)
    n3 = precios.refrescar_twelve(get=_get_twelve, clave_api="k")
    s2 = precios.snapshot(); por2 = {m["mercado"]: m for m in s2["mercados"]}
    ok &= chk("5. Twelve Data: plata, EURUSD y USDJPY entran; GBPUSD con error se omite", n3 == 3 and por2["XAGUSD"]["precio"] == 48.123 and por2["EURUSD"]["precio"] == 1.12346 and por2["USDJPY"]["cambio_pct"] is None and "GBPUSD" not in por2)
    ok &= chk("6. El orden del cintillo es el de MERCADOS y lleva nota legal", [m["mercado"] for m in s2["mercados"]] == ["BTCUSD", "ETHUSD", "XAUUSD", "XAGUSD", "EURUSD", "USDJPY"] and "bróker" in s2["nota"])
    # Un fallo total de Bybit conserva los ultimos precios buenos
    def _caido(url, params=None, timeout=0):
        raise ConnectionError("sin red")
    precios.refrescar_bybit(get=_caido)
    s3 = precios.snapshot(); por3 = {m["mercado"]: m for m in s3["mercados"]}
    ok &= chk("7. Si la fuente cae, se conserva el ultimo precio bueno (nunca ceros)", por3["BTCUSD"]["precio"] == 84012 and por3["BTCUSD"]["hora_utc"])
    print("\nTODO OK" if ok else "\nHAY FALLAS")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
