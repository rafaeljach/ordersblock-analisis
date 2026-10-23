"""
Índice del dólar (DXY) del panel del oro (sin red): fórmula del ICE, Yahoo, agrupación H4,
respaldo calculado con Kraken, caché y la ruta /velas/DXY.

    python -m tests.test_dxy
"""
import os
import sys
import tempfile

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)
os.environ["ORDERSBLOCK_DB"] = os.path.join(tempfile.mkdtemp(), "p.db")
for k in ("ORDERSBLOCK_SIN_WORKER", "ORDERSBLOCK_SIN_ESCANER", "ORDERSBLOCK_SIN_PRECIOS"):
    os.environ[k] = "1"
os.environ.pop("ORDERSBLOCK_API_KEY", None)

import dxy  # noqa: E402


class _R:
    def __init__(self, d):
        self._d = d

    def json(self):
        return self._d


T0 = 1_792_627_200            # 2026-10-21 00:00:00 UTC (múltiplo de 4 h)
llamadas = []


def yahoo_ok(url, params=None, headers=None, timeout=0):
    llamadas.append(("yahoo", params))
    n = 120
    ts = [T0 + i * 3600 for i in range(n)]
    c = [104.0 + (i % 10) * 0.01 for i in range(n)]
    c[5] = None                                    # Yahoo a veces deja huecos
    return _R({"chart": {"result": [{"timestamp": ts, "indicators": {"quote": [
        {"open": c, "high": [x and x + 0.02 for x in c], "low": [x and x - 0.02 for x in c], "close": c}]}}]}})


def yahoo_caido(url, params=None, headers=None, timeout=0):
    llamadas.append(("yahoo", params))
    raise ConnectionError("bloqueado")


BASE = {"EURUSD": 1.0798, "USDJPY": 151.06, "GBPUSD": 1.2973, "USDCAD": 1.3833, "USDCHF": 0.8666}


def kraken(sin=()):
    def get(url, params=None, headers=None, timeout=0):
        if "yahoo" in url:
            return yahoo_caido(url, params, headers, timeout)
        par = params["pair"]
        llamadas.append(("kraken", par))
        if par in sin:
            return _R({"error": ["EQuery:Unknown asset pair"]})
        filas = [[T0 + i * 3600, str(BASE[par]), "0", "0", str(BASE[par] * (1 + 0.0001 * i)), "0", "1", 3] for i in range(100)]
        return _R({"error": [], "result": {"X" + par: filas, "last": 1}})
    return get


def chk(n, c, x=""):
    print(("CORRECTO " if c else "FALLA    ") + n + ("  " + str(x) if x else ""))
    return bool(c)


def main():
    ok = True
    # Valores reales de cierre del 22-oct-2024 (DXY ~104.07)
    real = dxy.formula(1.0798, 151.06, 1.2973, 1.3833, 0.8666, usdsek=10.57)
    est = dxy.formula(1.0798, 151.06, 1.2973, 1.3833, 0.8666)
    ok &= chk("1. Formula del ICE reproduce el DXY real (104.07) con la corona real", abs(real - 104.07) < 0.3, round(real, 3))
    ok &= chk("2. Con la corona estimada (EURSEK 11.2) el error es de decimas", abs(est - 104.07) < 0.6, round(est, 3))

    dxy._cache.clear(); llamadas.clear()
    d = dxy.velas_dxy("60", 100, get=yahoo_ok)
    ok &= chk("3. Yahoo: velas con t en ms, sin el hueco, ultimas n, fuente real",
              d["fuente"] == "yahoo:DX-Y.NYB" and len(d["velas"]) == 100 and d["velas"][0]["t"] % 1000 == 0 and all(v["c"] for v in d["velas"]) and d["precio_actual"] == d["velas"][-1]["c"], d["fuente"])
    ok &= chk("4. Pide a Yahoo DX-Y.NYB en 1h y 60 dias", llamadas[-1] == ("yahoo", {"interval": "1h", "range": "60d"}), llamadas[-1])
    dxy.velas_dxy("60", 100, get=yahoo_ok)
    ok &= chk("5. Segunda consulta en 2 minutos: sale de la cache (sin red)", len(llamadas) == 1)
    d4 = dxy.velas_dxy("240", 300, get=yahoo_ok)
    ok &= chk("6. H4: se agrupa desde 1h en bloques de 4 h alineados (00, 04, 08 UTC)",
              len(d4["velas"]) == 30 and all(v["t"] % (4 * 3600000) == 0 for v in d4["velas"]) and d4["velas"][1]["t"] - d4["velas"][0]["t"] == 4 * 3600000, len(d4["velas"]))

    dxy._cache.clear(); llamadas.clear()
    d = dxy.velas_dxy("60", 100, get=kraken())
    esperado = dxy.formula(*(BASE[p] * (1 + 0.0001 * 99) for p in ("EURUSD", "USDJPY", "GBPUSD", "USDCAD", "USDCHF")))
    ok &= chk("7. Yahoo caido: se calcula con Kraken (5 pares) y la fuente dice que es calculado",
              d["fuente"].startswith("calculado") and len(d["velas"]) == 100 and abs(d["velas"][-1]["c"] - esperado) < 0.002 and sorted(x[1] for x in llamadas if x[0] == "kraken") == sorted(dxy.PESOS), (d["fuente"], d["velas"][-1]["c"], round(esperado, 3)))
    dxy._cache.clear()
    d = dxy.velas_dxy("60", 100, get=kraken(sin=("USDCAD", "USDCHF")))
    ok &= chk("8. Si Kraken no tiene USDCAD/USDCHF, usa su valor tipico y sigue", len(d["velas"]) == 100 and d["fuente"].startswith("calculado"))
    dxy._cache.clear()
    try:
        dxy.velas_dxy("60", 100, get=kraken(sin=("EURUSD",)))
        ok &= chk("9. Sin EURUSD (el 57 % del indice) no se inventa: error", False)
    except Exception as e:
        ok &= chk("9. Sin EURUSD (el 57 % del indice) no se inventa: error", "EURUSD" in str(e), e)
    d = dxy.velas_dxy("60", 100, get=yahoo_ok)
    dxy._cache["60"] = (0, dxy._cache["60"][1])      # cache vencida
    d2 = dxy.velas_dxy("60", 100, get=kraken(sin=("EURUSD",)))
    ok &= chk("10. Todo caido pero habia un dato bueno: se devuelve el ultimo bueno", d2["velas"] == d["velas"])

    # Ruta /velas/DXY: no toca el motor ni la cuota de Twelve Data
    from starlette.testclient import TestClient
    import api
    import servicio
    motor = []
    servicio.engine.obtener_datos_mercado = lambda *a, **k: motor.append(a) or {}
    dxy._cache.clear()
    orig = dxy.desde_yahoo
    dxy.desde_yahoo = lambda tf, get=None: orig(tf, get=yahoo_ok)
    try:
        with TestClient(api.app) as cl:
            r = cl.get("/velas/DXY?tf=60&n=160", headers={"Origin": "https://ordersblock.com"})
            dj = r.json()
            ok &= chk("11. /velas/DXY: 200, mismo formato que las demas velas, CORS para ordersblock.com, sin motor",
                      r.status_code == 200 and dj["ok"] and dj["datos"]["mercado"] == "DXY" and len(dj["datos"]["velas"]) == 119
                      and r.headers.get("access-control-allow-origin") == "https://ordersblock.com" and not motor, (r.status_code, len(dj.get("datos", {}).get("velas", [])), motor))
            r = cl.get("/velas/usdx?tf=240")
            ok &= chk("12. Alias USDX y H4 funcionan", r.status_code == 200 and r.json()["datos"]["tf"] == 240)
            r = cl.get("/velas/DXY?tf=7")
            ok &= chk("13. tf invalido: 400", r.status_code == 400)
            dxy._cache.clear()
            dxy.desde_yahoo = lambda tf, get=None: (_ for _ in ()).throw(ValueError("x"))
            dxy.desde_kraken = lambda tf, get=None: (_ for _ in ()).throw(ValueError("y"))
            r = cl.get("/velas/DXY?tf=60")
            ok &= chk("14. Sin ninguna fuente: 503 con mensaje (no 500)", r.status_code == 503 and "DXY" in r.json()["error"])
    finally:
        dxy.desde_yahoo = orig
    print("\nTODO OK" if ok else "\nHAY FALLAS")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
