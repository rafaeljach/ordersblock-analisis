"""Pruebas de seguridad de la API (sin red): filtro de secretos en registros, clave obligatoria,
limite que no se puede falsear en rutas publicas y /velas solo para mercados conocidos."""
import importlib, io, logging, os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["ORDERSBLOCK_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["ORDERSBLOCK_LIMITE_MINUTO"] = "3"
os.environ.pop("ORDERSBLOCK_API_KEY", None)
from starlette.testclient import TestClient

ok = True
def chk(nombre, cond, extra=""):
    global ok
    ok = ok and bool(cond)
    print(("OK    " if cond else "FALLA ") + nombre + (("  " + str(extra)) if extra else ""))

def cargar(clave):
    if clave is None:
        os.environ.pop("ORDERSBLOCK_API_KEY", None)
    else:
        os.environ["ORDERSBLOCK_API_KEY"] = clave
    import api
    return importlib.reload(api)

# 1. Filtro de secretos
api = cargar("k" * 32)
buf = io.StringIO(); h = logging.StreamHandler(buf); h.addFilter(api.FiltroSecretos())
lg = logging.getLogger("prueba.secretos"); lg.addHandler(h); lg.setLevel(logging.INFO); lg.propagate = False
lg.warning("precios twelve: ConnectionError: HTTPSConnectionPool(host='api.twelvedata.com', port=443): Max retries exceeded with url: /quote?symbol=EUR/USD&apikey=SECRETO123abc (Caused by X)")
lg.warning("cabecera X-API-Key: OTROSECRETO y token=abc.def y password = hunter2")
lg.warning("%s", "parametro apikey%3DCODIFICADO999")
try:
    raise ConnectionError("HTTPSConnectionPool(host='api.twelvedata.com'): Max retries exceeded with url: /time_series?symbol=EUR%2FUSD&apikey=ENTRAZA456 (Caused by X)")
except ConnectionError:
    lg.exception("Fallo en /analisis/EURUSD")
salida = buf.getvalue()
chk("1. Los registros no guardan claves (apikey=, X-API-Key:, token=, password =, %3D, y dentro de trazas de error)",
    "ENTRAZA456" not in salida and "Traceback" in salida and "SECRETO123abc" not in salida and "OTROSECRETO" not in salida and "abc.def" not in salida and "hunter2" not in salida and "CODIFICADO999" not in salida and salida.count("***") >= 6, salida.replace("\n", " | ")[:300])
chk("2. El resto del mensaje se conserva (simbolo y causa siguen legibles)", "symbol=EUR/USD" in salida and "Max retries" in salida)

# 2. Sin clave configurada: falla cerrado
api = cargar(None)
with TestClient(api.app) as c:
    r = c.get("/oportunidades"); r2 = c.get("/salud")
    chk("3. Sin ORDERSBLOCK_API_KEY las rutas privadas responden 503 (antes quedaban abiertas)", r.status_code == 503 and "sin clave" in r.json()["error"].lower(), r.status_code)
    chk("4. /salud sigue respondiendo sin clave", r2.status_code == 200)

# 3. Con clave: 401 sin clave o con clave mala, 200 con la buena
api = cargar("clave_buena_" + "x" * 20)
with TestClient(api.app) as c:
    chk("5. Con clave configurada: sin cabecera 401, clave mala 401, clave buena pasa",
        c.get("/memoria").status_code == 401 and c.get("/memoria", headers={"X-API-Key": "mala"}).status_code == 401 and c.get("/memoria", headers={"X-API-Key": "clave_buena_" + "x" * 20}).status_code == 200)

    # 4. Limite en rutas publicas: X-Usuario inventado ya no lo salta
    import precios as _p
    codigos = [c.get("/precios", headers={"X-Usuario": "falso%d" % i, "CF-Connecting-IP": "9.9.9.9"}).status_code for i in range(5)]
    chk("6. /precios: cambiar X-Usuario en cada peticion ya no salta el limite (3/min por IP)", codigos[:3] == [200, 200, 200] and codigos[3:] == [429, 429], codigos)
    chk("7. Otra IP tiene su propio cupo", c.get("/precios", headers={"CF-Connecting-IP": "8.8.8.8"}).status_code == 200)
    # El Worker (con clave) si puede repartir el cupo por usuario
    api._ventanas.clear()
    cw = [c.get("/memoria", headers={"X-API-Key": "clave_buena_" + "x" * 20, "X-Usuario": "web:%d" % i, "CF-Connecting-IP": "1.1.1.1"}).status_code for i in range(5)]
    chk("8. Con la clave, X-Usuario sigue repartiendo el cupo (el Worker no se bloquea a si mismo)", cw == [200] * 5, cw)

    # 5. /velas solo para mercados conocidos y tamaños fijos
    api._ventanas.clear()
    llamadas = []
    original = api.servicio.engine.obtener_datos_mercado
    api.servicio.engine.obtener_datos_mercado = lambda m, i, n, p=None: llamadas.append((m, n)) or {"velas": [], "precio_actual": 1, "fuente_datos": "prueba"}
    r_malo = c.get("/velas/ZZZINVENTADO?tf=60", headers={"CF-Connecting-IP": "7.7.7.1"})
    chk("9. /velas con un simbolo inventado: 404 y no se descarga nada", r_malo.status_code == 404 and not llamadas, (r_malo.status_code, llamadas))
    r_ok = c.get("/velas/BTC?tf=60&n=173", headers={"CF-Connecting-IP": "7.7.7.2"})
    chk("10. /velas BTC funciona y n=173 se ajusta a 160 (tamaños fijos)", r_ok.status_code == 200 and llamadas and llamadas[-1][1] == 160, (r_ok.status_code, llamadas))
    r_x = c.get("/velas/EURUSD?tf=60&n=99999", headers={"CF-Connecting-IP": "7.7.7.3"})
    chk("11. n absurdo se recorta a 300", r_x.status_code == 200 and llamadas[-1][1] == 300, llamadas[-1])
    for sim, esperado in (("SOLUSD", "SOL"), ("XRPUSD", "XRP"), ("DOGEUSD", "DOGE"), ("ADAUSD", "ADA"), ("BNBUSD", "BNB")):
        rr = c.get("/velas/%s?tf=60" % sim, headers={"CF-Connecting-IP": "7.7.8." + str(len(llamadas))})
        chk("13. /velas/%s (pestaña de Mercado) sigue funcionando y se pide como %s (Bybit, sin cuota)" % (sim, esperado), rr.status_code == 200 and llamadas[-1][0] == esperado, (rr.status_code, llamadas[-1]))
    for sim in ("BTCUSD", "XAUUSD", "XAGUSD", "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF", "NZDUSD", "DXY"):
        rr = c.get("/velas/%s?tf=60" % sim, headers={"CF-Connecting-IP": "7.7.9." + sim})
        chk("14. /velas/%s sigue respondiendo" % sim, rr.status_code in (200, 503), rr.status_code)
    api.servicio.engine.obtener_datos_mercado = original

    # 6. CORS solo para los dominios propios
    ra = c.get("/salud", headers={"Origin": "https://evil.example"}); rb = c.get("/salud", headers={"Origin": "https://ordersblock.com"})
    chk("12. CORS: origen ajeno sin permiso, ordersblock.com con permiso", "access-control-allow-origin" not in ra.headers and rb.headers.get("access-control-allow-origin") == "https://ordersblock.com")

print("\nTODO OK" if ok else "\nHAY FALLAS"); sys.exit(0 if ok else 1)
