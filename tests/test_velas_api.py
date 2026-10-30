"""
Prueba de la ruta /velas (sin red): el motor se simula y se comprueba formato, cache y errores.
    python -m tests.test_velas_api
"""
import asyncio, os, sys, tempfile
RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, RAIZ)
os.environ["ORDERSBLOCK_DB"] = os.path.join(tempfile.mkdtemp(), "p.db"); os.environ["ORDERSBLOCK_SIN_WORKER"] = "1"; os.environ["ORDERSBLOCK_SIN_ESCANER"] = "1"; os.environ["ORDERSBLOCK_SIN_PRECIOS"] = "1"
os.environ.pop("ORDERSBLOCK_API_KEY", None)
from starlette.testclient import TestClient
import api, servicio

llamadas = []
def falso(mercado, interval, n, prioridad=None):
    llamadas.append((mercado, interval, n))
    if mercado in ("ZZZ", "SOL"): raise ValueError("no existe")
    return {"precio_actual": 4056.3, "fuente_datos": "bybit", "velas": [
        {"datetime": "2026-10-22 10:00:00", "open": 4050.0, "high": 4060.0, "low": 4045.0, "close": 4056.3},
        {"datetime": "2026-10-22 11:00:00", "open": 4056.3, "high": 4061.0, "low": 4050.0, "close": 4058.0}]}
servicio.engine.obtener_datos_mercado = falso

def chk(n, c, x=""):
    print(("CORRECTO " if c else "FALLA    ") + n + ("  " + str(x) if x else "")); return bool(c)

with TestClient(api.app) as cl:
    ok = True
    r = cl.get("/velas/xauusd?tf=60&n=100"); d = r.json()
    ok &= chk("1. /velas publica sin clave: ok, mercado normalizado, 2 velas con t en ms, precio actual y cabecera de cache",
              r.status_code == 200 and d["ok"] and d["datos"]["mercado"] == "XAUUSD" and len(d["datos"]["velas"]) == 2 and d["datos"]["velas"][0]["t"] == 1792663200000 and d["datos"]["precio_actual"] == 4056.3 and "max-age=30" in r.headers.get("cache-control", ""), d)
    ok &= chk("2. Pide al motor con intervalo 1h y n=100 (tf 60)", llamadas[-1] == ("XAUUSD", "1h", 100), llamadas)
    cl.get("/velas/xauusd?tf=60&n=100")
    ok &= chk("3. Segunda peticion en 60 s: sale de cache (el motor no se vuelve a llamar)", len(llamadas) == 1)
    r = cl.get("/velas/BTC?tf=240"); ok &= chk("4. tf=240 -> 4h, n por defecto 200", llamadas[-1] == ("BTC", "4h", 200))
    r = cl.get("/velas/BTC?tf=7"); ok &= chk("5. tf invalido: 400", r.status_code == 400)
    antes = len(llamadas)
    r = cl.get("/velas/ZZZ?tf=60"); ok &= chk("6. Mercado inventado: 404 sin llamar al motor (seguridad 2026-10-30)", r.status_code == 404 and len(llamadas) == antes, (r.status_code, len(llamadas) - antes))
    r = cl.get("/velas/SOL?tf=60"); ok &= chk("6b. Mercado real cuyo motor falla: 503 con mensaje", r.status_code == 503 and "SOL" in r.json()["error"], r.status_code)
    r = cl.get("/velas/EURUSD?tf=60&n=9999"); ok &= chk("7. n se acota a 300", llamadas[-1][2] == 300)
    print("\nTODO OK" if ok else "\nHAY FALLAS"); sys.exit(0 if ok else 1)
