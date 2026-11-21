"""
Prueba del perpetuo de XMR en /velas (sin red): Binance USDT-M primero, Bybit si Binance no responde.
    python -m tests.test_perpetuo
"""
import os, sys, tempfile
RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, RAIZ)
os.environ["ORDERSBLOCK_DB"] = os.path.join(tempfile.mkdtemp(), "p.db"); os.environ["ORDERSBLOCK_SIN_WORKER"] = "1"; os.environ["ORDERSBLOCK_SIN_ESCANER"] = "1"; os.environ["ORDERSBLOCK_SIN_PRECIOS"] = "1"
os.environ.pop("ORDERSBLOCK_API_KEY", None)
from starlette.testclient import TestClient
import api, servicio, perpetuos

def chk(n, c, x=""):
    print(("CORRECTO " if c else "FALLA    ") + n + ("  " + str(x) if x else "")); return bool(c)

class R:
    def __init__(self, st, js): self.status_code, self._js = st, js
    def json(self): return self._js

T0 = 1795291200000
BIN = [[T0, "485.4", "492.2", "484.8", "491.8", "10"], [T0 + 3600000, "491.4", "491.4", "488.7", "488.9", "8"]]
BYB = {"retCode": 0, "result": {"list": [[str(T0 + 3600000), "491.0", "491.5", "488.5", "489.0", "7", "0"], [str(T0), "485.0", "492.0", "484.5", "491.0", "9", "0"]]}}
modo = {"binance": "ok", "bybit": "ok"}; pedidos = []
def falso_get(url, params=None, timeout=None):
    pedidos.append((url, dict(params or {})))
    if "binance" in url:
        return R(200, BIN) if modo["binance"] == "ok" else R(451, {"code": 0, "msg": "Service unavailable from a restricted location"})
    return R(200, BYB) if modo["bybit"] == "ok" else R(403, {})
perpetuos.requests.get = falso_get
motor = []
servicio.engine.obtener_datos_mercado = lambda *a, **k: motor.append(a) or {"precio_actual": 1, "fuente_datos": "bybit", "velas": []}

ok = True
ok &= chk("0. XMRUSD, XMRUSDT y XMR se reconocen; BTCUSD y LTCUSD no son de este modulo",
          [perpetuos.base_de(x) for x in ("XMRUSD", "xmrusdt", "XMR", "BTCUSD", "LTCUSD", "")] == ["XMR", "XMR", "XMR", "", "", ""])
with TestClient(api.app) as cl:
    r = cl.get("/velas/XMRUSD?tf=60&n=100"); d = r.json(); x = d.get("datos") or {}
    ok &= chk("1. Binance responde: velas del perpetuo de Binance, en orden, con t en ms y precio actual = ultimo cierre",
              r.status_code == 200 and x.get("fuente") == "binance-perp" and x.get("simbolo") == "XMRUSDT" and [v["t"] for v in x["velas"]] == [T0, T0 + 3600000]
              and x["velas"][0] == {"t": T0, "o": 485.4, "h": 492.2, "l": 484.8, "c": 491.8, "v": 10.0} and x["precio_actual"] == 488.9 and x["intentos"] == [], d)
    ok &= chk("2. Pide a Binance USDT-M el XMRUSDT de 1h y 100 velas; el motor no se usa",
              pedidos[-1] == (perpetuos.BINANCE_FAPI, {"symbol": "XMRUSDT", "interval": "1h", "limit": 100}) and motor == [], pedidos[-1])
    n = len(pedidos); cl.get("/velas/XMRUSD?tf=60&n=100")
    ok &= chk("3. Segunda peticion en 60 s: sale de cache", len(pedidos) == n)
    modo["binance"] = "451"
    r = cl.get("/velas/XMRUSDT?tf=240"); x = r.json().get("datos") or {}
    ok &= chk("4. Binance bloquea (451): sale del perpetuo de Bybit, en orden cronologico, y dice por que no fue Binance",
              r.status_code == 200 and x.get("fuente") == "bybit-perp" and [v["t"] for v in x["velas"]] == [T0, T0 + 3600000] and "binance: RuntimeError: HTTP 451" in x["intentos"][0], x)
    ok &= chk("5. A Bybit se le pide el linear XMRUSDT de 240", pedidos[-1] == (perpetuos.BYBIT_KLINE, {"category": "linear", "symbol": "XMRUSDT", "interval": "240", "limit": 200}), pedidos[-1])
    modo["bybit"] = "403"
    r = cl.get("/velas/XMR?tf=15")
    ok &= chk("6. Ninguna responde: 503 con los dos motivos", r.status_code == 503 and "HTTP 451" in r.json()["error"] and "HTTP 403" in r.json()["error"], r.json())
    r = cl.get("/velas/ZZZ?tf=60"); ok &= chk("7. Un mercado inventado sigue en 404", r.status_code == 404)
    r = cl.get("/velas/XMRUSD?tf=7"); ok &= chk("8. tf invalido: 400", r.status_code == 400)
    r = cl.get("/velas/LTCUSD?tf=60"); ok &= chk("9. LTC sigue yendo por el motor como antes", len(motor) == 1 and motor[0][0] == "LTC", motor)
print("\nTODO OK" if ok else "\nHAY FALLAS"); sys.exit(0 if ok else 1)
