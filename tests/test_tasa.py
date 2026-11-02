"""Tasa USDT->COP: mediana de anuncios P2P, cache, respaldo si Binance falla, y la ruta /tasa/COP con clave."""
import os, sys, tempfile, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["ORDERSBLOCK_DB"] = os.path.join(tempfile.mkdtemp(), "t.db"); os.environ["ORDERSBLOCK_API_KEY"] = "k" * 32
import tasa_cop
ok = True
def chk(n, c, x=""):
    global ok; ok = ok and bool(c); print(("OK    " if c else "FALLA ") + n + (("  " + str(x)) if x else ""))
class R:
    def __init__(self, d): self.d = d
    def json(self): return self.d
llamadas = []
def post_ok(url, json=None, timeout=None, headers=None):
    llamadas.append(url); return R({"data": [{"adv": {"price": str(p)}} for p in (4210, 4190, 4250, 4205, 99, 4215)]})
def post_falla(url, **k): llamadas.append(url); raise ConnectionError("sin red")
d = tasa_cop.tasa_cop(post_ok)
chk("1. Mediana de los anuncios validos (el de 99 COP se descarta)", d["tasa"] == 4210 and d["muestras"] == 5, d)
d2 = tasa_cop.tasa_cop(post_ok)
chk("2. Segunda consulta inmediata: cache, sin llamar a Binance", len(llamadas) == 1 and d2["tasa"] == 4210)
tasa_cop._cache["t"] -= 700
d3 = tasa_cop.tasa_cop(post_falla)
chk("3. Si Binance falla se conserva la ultima tasa buena (marcada cache)", d3["tasa"] == 4210 and "cache" in d3["fuente"], d3)
tasa_cop._cache["t"] -= 25 * 3600
d4 = tasa_cop.tasa_cop(post_falla)
chk("4. Tasa de mas de 24 h sin refrescar: se informa que no hay dato", d4["tasa"] is None)
tasa_cop._cache.update(t=time.time(), tasa=4200.0, fuente="prueba", muestras=5)
import api
from starlette.testclient import TestClient
with TestClient(api.app) as c:
    chk("5. /tasa/COP exige clave", c.get("/tasa/COP").status_code == 401)
    r = c.get("/tasa/COP", headers={"X-API-Key": "k" * 32})
    chk("6. /tasa/COP con clave devuelve la tasa", r.status_code == 200 and r.json()["datos"]["tasa"] == 4200.0, r.text[:120])
    chk("7. Otra moneda: 404", c.get("/tasa/EUR", headers={"X-API-Key": "k" * 32}).status_code == 404)
print("\nTODO OK" if ok else "\nHAY FALLAS"); sys.exit(0 if ok else 1)
