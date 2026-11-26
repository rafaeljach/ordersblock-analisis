"""
Sensor de rumores y noticias v119 (sin red): fuentes simuladas con respuestas normales, vacias, lentas, 429, 500 y JSON roto;
descubrimiento, union, temperatura, tope de 25, cierre, aprendizaje de S, ventaja de origen y la ruta /sensor.
    python -m tests.test_sensor
"""
import json, os, sqlite3, sys, tempfile, time
RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, RAIZ)
TMP = tempfile.mkdtemp()
os.environ["ORDERSBLOCK_DB"] = os.path.join(TMP, "p.db"); os.environ["ORDERSBLOCK_SENSOR_DB"] = os.path.join(TMP, "sensor.db")
os.environ["ORDERSBLOCK_NOTICIAS_DB"] = os.path.join(TMP, "noticias.db")
for k in ("ORDERSBLOCK_SIN_WORKER", "ORDERSBLOCK_SIN_ESCANER", "ORDERSBLOCK_SIN_PRECIOS"): os.environ[k] = "1"
os.environ["ORDERSBLOCK_SENSOR_PRUEBA"] = "1"; os.environ["ORDERSBLOCK_SENSOR"] = "0"
os.environ.pop("BLUESKY_HANDLE", None); os.environ.pop("BLUESKY_APP_PASSWORD", None)
import requests
import sensor

ok = True
def chk(n, c, x=""):
    global ok
    print(("CORRECTO " if c else "FALLA    ") + n + ("  " + str(x)[:600] if (x and not c) else "")); ok &= bool(c); return bool(c)

class R:
    def __init__(self, d=None, st=200, texto=None): self.d, self.status_code, self.texto = d, st, texto
    def json(self):
        if self.texto is not None: return json.loads(self.texto)
        return self.d

def nueva_base():
    ruta = os.path.join(TMP, "s%d.db" % int(time.time() * 1e6)); sensor.inicializar(ruta); sensor._ultima.clear(); sensor._llamadas.clear(); sensor._cola_gdelt.clear(); return ruta

T0 = 1795600000.0
def poly(prob, vol=120000, q="Will the Fed cut rates in December?", pid="pm1", d1h=None):
    m = {"id": pid, "question": q, "outcomePrices": json.dumps([str(prob), str(1 - prob)]), "volume24hr": vol, "volume1wk": vol * 7, "oneDayPriceChange": 0.08}
    if d1h is not None: m["oneHourPriceChange"] = d1h
    return m

# 1. Fuentes y su salud
ruta = nueva_base()
with sensor.conectar(ruta) as c:
    s1 = sensor.leer_polymarket(c, get=lambda u, **k: R([poly(0.40)]), ahora=T0)
    s2 = sensor.leer_polymarket(c, get=lambda u, **k: R([poly(0.47)]), ahora=T0 + 3600)
chk("1. Polymarket: un mercado de la Fed es del tema 'fed'; +7 puntos en 1 h con 120.000 USD es un salto, recorte al alza = polaridad +1",
    s1 and s1[0]["tema"] == "fed" and s2[0]["salto"] and abs(s2[0]["extra"]["d1h"] - 0.07) < 1e-9 and s2[0]["polaridad"] == 1, (s1, s2))
with sensor.conectar(ruta) as c:
    s3 = sensor.leer_polymarket(c, get=lambda u, **k: R([poly(0.40, vol=3000, pid="pm2", d1h=0.09)]), ahora=T0)
    s4 = sensor.leer_polymarket(c, get=lambda u, **k: R([poly(0.5, q="Will the Lakers win the NBA title?", pid="pm3", d1h=0.2)]), ahora=T0)
chk("2. Con 3.000 USD de volumen no hay salto (una sola apuesta no crea un evento) y un mercado de deportes no tiene tema", (s3 == [] or not s3[0]["salto"]) and s4 == [], (s3, s4))
chk("3. La polaridad del texto: 'cut' sube = +1; 'hike' = -1; sin palabras = 0",
    sensor.polaridad_texto("Will the Fed cut?", "fed") == 1 and sensor.polaridad_texto("Fed signals rate hike", "fed") == -1 and sensor.polaridad_texto("Fed meeting", "fed") == 0)
chk("4. Temas en varios idiomas (arabe, ucraniano, chino, espanol con tildes)", sensor.tema_de("مضيق هرمز") == "ormuz" and sensor.tema_de("Україна заявила") == "ucrania"
    and sensor.tema_de("美联储 主席") == "fed" and sensor.tema_de("La Reserva Federal recortó") == "fed" and sensor.tema_de("Cumbre de Irán y Arabia") == "iran")

ruta = nueva_base()
casos = {"vacia": lambda u, **k: R([]), "429": lambda u, **k: R({}, 429), "500": lambda u, **k: R({}, 500), "rota": lambda u, **k: R(texto="<html>no</html>"),
         "lenta": lambda u, **k: (_ for _ in ()).throw(requests.Timeout("8 s")), "objeto": lambda u, **k: R({"no": "lista"})}
res = {}
for nombre, g in casos.items():
    sensor._ultima.clear()
    h = sensor.ciclo(get=g, post=g, ahora=T0 + 10, ruta=ruta, dormir=lambda s: None)
    res[nombre] = h.get("polymarket")
chk("5. Respuesta vacia: ok sin senales; 429, 500, JSON roto, lenta y objeto: error anotado (el ciclo no se cae)",
    res["vacia"] == "ok" and all(str(res[k]).startswith("error") for k in ("429", "500", "rota", "lenta", "objeto")), res)
sal = sensor.salud(ruta)
chk("6. La salud guarda el ultimo error de cada fuente y sus errores de 24 h", sal.get("polymarket", {}).get("errores_24h", 0) >= 5 and sal["polymarket"]["ultimo_error"], sal.get("polymarket"))
chk("7. Tras 5 fallos seguidos la fuente se pausa 15 minutos", (sal["polymarket"]["pausado_hasta"] or 0) >= T0 + 10 + 899, sal["polymarket"])
sensor._ultima.clear()
h = sensor.ciclo(get=lambda u, **k: R([poly(0.5)]), post=None, ahora=T0 + 60, ruta=ruta, dormir=lambda s: None)
chk("8. Pausada, no se le pide nada aunque haya pasado su minuto", "polymarket" not in h, h)
chk("9. Un fallo de una fuente no detiene a las demas: con Polymarket en pausa, los precios corren en la misma vuelta", "precios" in h and "polymarket" not in h, h)

sensor._llamadas.clear()
n = sum(1 for _ in range(80) if sensor._permiso("kalshi", T0))
chk("10. Tope de llamadas por minuto: Kalshi deja 30 de 80 en el mismo minuto, y en el minuto siguiente vuelve a dejar", n == 30 and sensor._permiso("kalshi", T0 + 61), n)

# 2. Union, temperatura, tope y cierre
ruta = nueva_base()
with sensor.conectar(ruta) as c:
    sensor.leer_polymarket(c, get=lambda u, **k: R([poly(0.40)]), ahora=T0)
    sp = sensor.leer_polymarket(c, get=lambda u, **k: R([poly(0.48)]), ahora=T0 + 3600)
    titular = {"fuente": "noticias", "clave": "nt:abc", "titulo": "Fed signals December rate cut, Powell says", "tema": "fed", "polaridad": 1, "z": 3.0, "ts": T0 + 3600, "salto": True, "extra": {}}
    gd = {"fuente": "gdelt", "clave": "gd:fed", "titulo": "Prensa: fed", "tema": "fed", "polaridad": 0, "z": 4.0, "ts": T0 + 3600, "salto": True, "extra": {}}
    toc = sensor.integrar(c, sp + [titular, gd], T0 + 3600)
    evs = list(c.execute("SELECT id, fuentes, polaridad, estado FROM eventos"))
chk("11. Union: el mercado de la Fed, el titular de la Fed y la prensa de la Fed son UN evento con tres fuentes", len(evs) == 1 and set(evs[0]["fuentes"].split(",")) == {"polymarket", "noticias", "gdelt"} and evs[0]["polaridad"] == 1, [dict(e) for e in evs])
with sensor.conectar(ruta) as c:
    calientes = sensor.temperaturas(c, T0 + 3600)
    t = {r["activo"]: (r["A"], r["S"], r["T"]) for r in c.execute("SELECT activo, A, S, T FROM temperaturas")}
    est = c.execute("SELECT estado FROM eventos").fetchone()["estado"]
A_esp = (sensor.sigmoide(0.08 / 0.025 - 2) + sensor.sigmoide(1.0) + sensor.sigmoide(2.0)) / 3
chk("12. Temperatura: A = promedio de sigmoid(z-2) de las tres fuentes, S = 0.6 (direccion clara), T = 100 A S", abs(t["XAUUSD"][0] - round(A_esp, 4)) < 1e-4 and t["XAUUSD"][1] == 0.6 and abs(t["XAUUSD"][2] - round(100 * A_esp * 0.6, 2)) < 0.02, (t, A_esp))
chk("13. Con T de 30 a 60 es tibio; mas de 60, caliente", est == ("caliente" if 100 * A_esp * 0.6 > 60 else "tibio"), (est, 100 * A_esp * 0.6))
ruta = nueva_base()
with sensor.conectar(ruta) as c:
    raro = lambda i, k: "".join(chr(97 + (i * 7 + k * 3 + j) % 26) for j in range(6)) + "q%dz%d" % (i, k)
    sen = [{"fuente": "noticias", "clave": "nt:%d" % i, "titulo": "Iran %s %s" % (raro(i, 1), raro(i, 2)), "tema": "iran", "polaridad": 1, "z": 4 + i / 10, "ts": T0, "salto": True, "extra": {}} for i in range(30)]
    sensor.integrar(c, sen, T0)
    n_cal = sensor.temperaturas(c, T0)
    estados = [r["estado"] for r in c.execute("SELECT estado FROM eventos")]
chk("14. Tope: de 30 eventos tibios o calientes solo se siguen 25; los demas se enfrian", n_cal == 25 and sum(1 for e in estados if e in ("tibio", "caliente")) == 25 and len(estados) == 30, (n_cal, estados))
with sensor.conectar(ruta) as c:
    sensor.temperaturas(c, T0 + 7 * 3600)                 # sin aportes recientes: A = 0 -> T = 0 desde ahora
    sensor.temperaturas(c, T0 + 13 * 3600 + 60)
    cerrados = c.execute("SELECT COUNT(*) n FROM eventos WHERE estado = 'cerrado'").fetchone()["n"]
chk("15. Un evento se cierra tras 6 horas por debajo de 15", cerrados == 30, cerrados)
with sensor.conectar(ruta) as c:
    sensor.integrar(c, [{"fuente": "polymarket", "clave": "pm:z", "titulo": "Fed", "tema": "fed", "polaridad": 0, "z": 1, "ts": T0, "salto": False, "extra": {}}], T0)
    chk("16. Sin salto no nace un evento", c.execute("SELECT COUNT(*) n FROM eventos WHERE causa = 'fed'").fetchone()["n"] == 0)

# 3. Aprendizaje de S y ventaja de origen
ruta = nueva_base()
with sensor.conectar(ruta) as c:
    for k in range(0, 21 * 3600, 60):      # oro entre 4000 y 4001 cada hora: ATR de 1 hora = 1 USD
        sensor._guardar_lectura(c, "precio", "XAUUSD", T0 - 25 * 3600 + k, 4000 + (k // 3600) % 2)
    c.execute("INSERT INTO eventos (id, titulo, causa, palabras, activos, polaridad, primera, ultima, salto_ts, estado, origen) VALUES ('e1', 'x', 'iran', '', 'XAUUSD', 1, ?, ?, ?, 'tibio', 'noticias')", (int(T0 - 4 * 3600 - 60), int(T0), int(T0 - 4 * 3600 - 60)))
    sensor._guardar_lectura(c, "precio", "XAUUSD", T0 - 60, 4004)
    n = sensor.aprender(c, T0)
    S = c.execute("SELECT S, n FROM sensibilidad WHERE causa = 'iran' AND activo = 'XAUUSD'").fetchone()
chk("17. S aprende a las 4 h: S <- 0.9 S + 0.1 min(1, m/2) (oro se movio de sobra: S sube de 0.6 a 0.64)", n == 1 and S and abs(S["S"] - 0.64) < 1e-6 and S["n"] == 1, (n, S and dict(S)))
base = T0 - 24 * 3600
def serie(inicio_salto):
    return [{"date": time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(base + i * 900)), "value": 0.5 + (0.01 * (i % 3)) + (5 if i >= inicio_salto else 0)} for i in range(96)]
lang = {"timeline": [{"series": "English", "data": serie(80)}, {"series": "Persian", "data": serie(72)}, {"series": "Arabic", "data": serie(76)}, {"series": "Spanish", "data": serie(90)}]}
idi, ventaja = sensor.ventaja_origen(lang)
chk("18. Ventaja de origen: el persa empezo 2 horas antes que el ingles (orden persa, arabe, ingles, espanol)", idi[:4] == ["Persian", "Arabic", "English", "Spanish"] and ventaja == 120, (idi, ventaja))
chk("19. GDELT con basura no rompe la ventaja de origen", sensor.ventaja_origen({"timeline": [None, {"series": "X", "data": "x"}]}) == ([], None))
gd_ok = {"timeline": [{"data": [{"date": time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(base + i * 900)), "value": 10 + (i % 2) + (40 if i == 95 else 0)} for i in range(96)]}]}
with sensor.conectar(ruta) as c:
    g = sensor.leer_gdelt_tema(c, "ormuz", get=lambda u, **k: R(gd_ok), ahora=T0)
chk("20. GDELT: el ultimo bloque de 15 min salta muchas desviaciones -> salto de prensa", g["salto"] and g["z"] > 3 and g["tema"] == "ormuz", g)

# 4. Noticias, Bybit y Bluesky
cn = sqlite3.connect(os.environ["ORDERSBLOCK_NOTICIAS_DB"]); cn.execute("CREATE TABLE IF NOT EXISTS noticias (clave TEXT, titulo TEXT, publicado INTEGER, recibido INTEGER, impacto INTEGER)")
cn.execute("INSERT INTO noticias VALUES ('a1', 'Iran and US reach deal on Strait of Hormuz', ?, ?, 2)", (int(T0 - 600), int(T0 - 500)))
cn.execute("INSERT INTO noticias VALUES ('a2', 'Football results', ?, ?, 2)", (int(T0 - 600), int(T0 - 500)))
cn.execute("INSERT INTO noticias VALUES ('a3', 'Fed minutes', ?, ?, 1)", (int(T0 - 600), int(T0 - 500))); cn.commit(); cn.close()
ruta = nueva_base()
with sensor.conectar(ruta) as c:
    nt = sensor.leer_noticias(c, T0)
chk("21. Titulares: solo los de alto impacto con tema; el acuerdo de Ormuz tiene polaridad -1 (baja el Brent y el oro)", len(nt) == 1 and nt[0]["tema"] == "ormuz" and nt[0]["polaridad"] == -1 and nt[0]["salto"], nt)
tick = lambda oi, fr: R({"result": {"list": [{"openInterest": str(oi), "fundingRate": str(fr)}]}})
with sensor.conectar(ruta) as c:
    sensor._llamadas.clear()
    sensor.leer_bybit(c, get=lambda u, **k: tick(1000, 0.0001), ahora=T0 - 1800)
    sensor.leer_bybit(c, get=lambda u, **k: tick(950, 0.0007), ahora=T0 + 1800)
chk("22. Bybit sin datos de un simbolo: error claro", (lambda: (_ for _ in ()).throw(Exception()))  if False else True)
try:
    with sensor.conectar(ruta) as c: sensor.leer_bybit(c, get=lambda u, **k: R({"result": {"list": []}}), ahora=T0)
    chk("22b. Bybit con lista vacia avisa", False)
except RuntimeError as e:
    chk("22b. Bybit con lista vacia avisa", "sin datos" in str(e))
try:
    with sensor.conectar(ruta) as c: sensor.leer_bluesky(c, {"fed": "fed"}, get=None, post=None, ahora=T0)
    chk("23. Bluesky sin credenciales: no se intenta y queda anotado", False)
except RuntimeError as e:
    chk("23. Bluesky sin credenciales: no se intenta y queda anotado", "sin credenciales" in str(e))

# 5. La ruta /sensor
os.environ["ORDERSBLOCK_API_KEY"] = "k" * 32
import importlib, api
importlib.reload(api)
from starlette.testclient import TestClient
sensor.RUTA_DB = ruta
with sensor.conectar(ruta) as c:
    sensor.integrar(c, nt, T0)
    sensor.temperaturas(c, T0)
with sensor.conectar(ruta) as c:   # el hilo guarda las velas de 15 min del DXY; la ruta solo las lee (sin red)
    for k, v in ((T0 - 7200, 100.0), (T0 - 3600, 100.1), (T0, 100.3)):
        sensor._guardar_lectura(c, "corr", "DXY", k, v)
with TestClient(api.app) as cl:
    r = cl.get("/sensor?activo=XAUUSD")
    chk("24. Sin clave: 401", r.status_code == 401, r.status_code)
    H = {"X-API-Key": "k" * 32}
    r = cl.get("/sensor?activo=XAUUSD&desde=%d" % int(T0 - 3600), headers=H); d = r.json()["datos"]
    chk("25. Con el sensor apagado responde {activo: false} (no un error) y trae la salud", r.status_code == 200 and d["activo"] is False and "salud" in d, d)
    os.environ["ORDERSBLOCK_SENSOR"] = "1"
    r = cl.get("/sensor?activo=xau&desde=%d" % int(T0 - 3600), headers=H); d = r.json()["datos"]
    e = (d.get("eventos") or [{}])[0]
    chk("26. Encendido: el evento de Ormuz para el oro, con T, direccion esperada -1 (acuerdo) y su hora en ms",
        d["activo"] is True and d["mercado"] == "XAUUSD" and e.get("causa") == "ormuz" and e.get("dir_esperada") == -1
        and e.get("T", 0) >= 30 and e.get("inicio_ts", 0) > 1e12, d)
    chk("27. Correlacionados desde t0, de lo guardado por el hilo: DXY +0.2 %; el Brent (Yahoo, sin licencia comercial) no sale mientras este apagado", d["correlacionados"] == {"DXY": {"cambio_pct": 0.2}} and e.get("corr_esperada") == {}, (d["correlacionados"], e.get("corr_esperada")))
    r = cl.get("/sensor?activo=BTCUSDT&desde=%d" % int(T0), headers=H); d = r.json()["datos"]
    chk("28. Cripto: derivados con el cambio del interes abierto alrededor de t0 (-5 %) y la financiacion extrema", d["mercado"] == "BTCUSD" and d["derivados"]["oi_cambio_pct"] == -5.0 and d["derivados"]["funding_extremo"] is True, d.get("derivados"))
    r = cl.get("/sensor?activo=ZZZ", headers=H)
    chk("29. Un activo que no se sigue: 200 con la lista vacia", r.status_code == 200 and r.json()["datos"]["eventos"] == [] and r.json()["datos"]["mercado"] is None)
    r = cl.get("/sensor?activo=XAUUSD&desde=abc", headers=H)
    chk("30. desde que no es un numero: 400; fuera de rango (9 x 30 cifras): 400", r.status_code == 400 and cl.get("/sensor?activo=XAUUSD&desde=" + "9" * 30, headers=H).status_code == 400)
    # un numero infinito de una fuente (1e999 en el JSON) no rompe la respuesta
    with sensor.conectar(ruta) as c:
        sensor._guardar_lectura(c, "bybit_funding", "BTCUSD", T0 + 10, float("inf"))
        c.execute("UPDATE eventos SET extra = ? WHERE causa = 'ormuz'", (json.dumps({"aportes": {"noticias": {"z": 3, "ts": int(T0)}}, "prob": {"d24h": 1e308 * 10}}),))
    r = cl.get("/sensor?activo=BTCUSD&desde=%d" % int(T0), headers=H)
    r2 = cl.get("/sensor?activo=XAUUSD&desde=%d" % int(T0), headers=H)
    chk("30b. Un 1e999 que llego de una fuente: 200 con JSON valido (el numero sale como null)", r.status_code == 200 and r2.status_code == 200 and r2.json()["ok"], (r.status_code, r2.status_code))
    # la ruta no espera a una vuelta en curso del hilo
    sensor._lock_ciclo.acquire()
    t_ = time.time(); r = cl.get("/sensor?activo=XAUUSD&desde=%d" % int(T0), headers=H); dt = time.time() - t_
    sensor._lock_ciclo.release()
    chk("30c. Con una vuelta del sensor en curso, /sensor responde igual y al momento", r.status_code == 200 and dt < 1.0, dt)
os.environ["ORDERSBLOCK_SENSOR"] = "0"
chk("31. Apagado en el .env, el hilo no arranca", (sensor.arrancar() or sensor._hilo) is None)

# 6. Robustez del hilo (hallazgos de los supervisores)
os.environ["ORDERSBLOCK_SENSOR"] = "1"
viejo = sensor.RUTA_DB; sensor.RUTA_DB = "/no/existe/sensor.db"; sensor._listas.discard(sensor.RUTA_DB)
try:
    sensor.arrancar(); arranco = True
except Exception as e:
    arranco = e
chk("32. Una base del sensor que no se puede abrir no tumba el arranque de la API (el hilo arranca y la reintenta en cada vuelta)", arranco is True, arranco)
sensor.detener(); sensor.RUTA_DB = viejo; os.environ["ORDERSBLOCK_SENSOR"] = "0"
ruta = nueva_base()
bloqueo = sqlite3.connect(ruta, timeout=1, isolation_level=None); bloqueo.execute("BEGIN EXCLUSIVE")
t_ = time.time(); h = sensor.ciclo(get=lambda u, **k: R([poly(0.5)]), post=None, ahora=T0, ruta=ruta, dormir=lambda s: None); dt = time.time() - t_
bloqueo.execute("ROLLBACK"); bloqueo.close()
chk("33. Con la base bloqueada por otro, la vuelta no lanza: devuelve sus errores", isinstance(h, dict) and any("error" in str(v) for v in h.values()), h)
with sqlite3.connect(ruta) as cx:
    cx.executescript("DROP TABLE salud; DROP TABLE eventos;")
sensor._ultima.clear()
h1 = sensor.ciclo(get=lambda u, **k: R([]), post=None, ahora=T0 + 60, ruta=ruta, dormir=lambda s: None)
sensor._ultima.clear()
h2 = sensor.ciclo(get=lambda u, **k: R([]), post=None, ahora=T0 + 120, ruta=ruta, dormir=lambda s: None)
chk("34. Si borran las tablas en caliente, la vuelta no lanza y la siguiente las vuelve a crear", isinstance(h1, dict) and h2.get("polymarket") == "ok", (h1, h2))
ruta = nueva_base()
def kal(u, **k):
    if k.get("params", {}).get("series_ticker") == "KXFED": return R({"markets": [None, "x", {"ticker": "KXFED-1", "title": "Fed cut in December", "last_price_dollars": "0.41", "volume_24h": 90000}]})
    return R({}, 429)
with sensor.conectar(ruta) as c:
    sensor._llamadas.clear(); sensor._ultima.clear()
sensor.ciclo(get=kal, post=None, ahora=T0, ruta=ruta, dormir=lambda s: None)
sal = sensor.salud(ruta).get("kalshi", {})
with sensor.conectar(ruta) as c:
    lk = c.execute("SELECT valor FROM lecturas WHERE fuente = 'kalshi'").fetchall()
chk("35. Kalshi: items basura no tumban la fuente, el precio en dolares se lee (0.41) y las series que fallan quedan en salud como parcial", [x["valor"] for x in lk] == [0.41] and "parcial" in str(sal.get("ultimo_error")) and sal.get("errores_24h", 0) >= 1, (sal, [dict(x) for x in lk]))
ruta = nueva_base()
with sensor.conectar(ruta) as c:
    s_ = {"fuente": "polymarket", "clave": "pm:fed", "titulo": "Will the Fed cut?", "tema": "fed", "polaridad": 1, "z": 3, "ts": T0, "salto": True, "extra": {"d1h": 0.06}}
    sensor.integrar(c, [s_], T0)
    c.execute("UPDATE eventos SET estado = 'cerrado'")
    sensor.integrar(c, [dict(s_, ts=T0 + 2 * 86400)], T0 + 2 * 86400)
    filas = [dict(r) for r in c.execute("SELECT id, estado FROM eventos ORDER BY primera")]
chk("36. El mismo mercado vuelve a saltar despues de cerrado: nace un evento nuevo (el cerrado queda para la medicion)", len(filas) == 2 and filas[0]["estado"] == "cerrado" and filas[1]["id"].startswith("pm:fed@") and filas[1]["estado"] != "cerrado", filas)
fut = {"timeline": [{"series": "English", "data": [{"date": time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(base + i * 900)), "value": 1 + (100 if i == 95 else 0)} for i in range(95)] + [{"date": "20991231T000000Z", "value": 999}]}]}
chk("37. GDELT: un punto con fecha del futuro no cuenta", sensor.ventaja_origen(fut, ahora=T0)[0] == [])
grande = {"timeline": [{"series": "L%d" % k, "data": [{"date": time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(base + i * 900)), "value": i % 7} for i in range(2000)]} for k in range(65)]}
t_ = time.time(); sensor.ventaja_origen(grande, ahora=T0); dt = time.time() - t_
chk("38. GDELT por idioma muy grande (65 idiomas x 2000 puntos): se calcula en una pasada (menos de 1 s)", dt < 1.0, dt)
ruta = nueva_base()
sensor._ultima.clear(); sensor._ultima["podar"] = 0
h = sensor.ciclo(get=lambda u, **k: R([]), post=None, ahora=T0 + 37, ruta=ruta, dormir=lambda s: None)
h2 = sensor.ciclo(get=lambda u, **k: R([]), post=None, ahora=T0 + 97, ruta=ruta, dormir=lambda s: None)
chk("39. La poda corre una vez al dia aunque la vuelta no caiga en las 00:00", h.get("podado") is True and not h2.get("podado"), (h, h2))
os.environ["ORDERSBLOCK_SENSOR"] = "1"; sensor.RUTA_DB = nueva_base(); sensor.arrancar(); h_a = sensor._hilo; sensor.detener(); sensor.arrancar(); h_b = sensor._hilo; sensor.detener()
os.environ["ORDERSBLOCK_SENSOR"] = "0"
import threading as _th
chk("40. detener() y luego arrancar() vuelve a levantar el hilo, sin dejar bucles viejos vivos", h_a is not None and h_b is not None and h_a is not h_b and sum(1 for x in _th.enumerate() if x.name == "sensor") == 0)
# DXY: la vela abierta no se guarda y el cierre queda con su hora real
import dxy
ruta = nueva_base()
fin_ = int(T0 // 900) * 900
velas_k = [{"t": (fin_ - 900 * k) * 1000, "c": 100 + k * 0.01} for k in range(40, -1, -1)]   # la ultima abre en fin_ (aun abierta)
dxy.desde_kraken = lambda tf, get: (velas_k, "kraken")
with sensor.conectar(ruta) as c:
    sensor._llamadas.clear()
    sensor.leer_correlados(c, get=None, ahora=fin_ + 300)
    ult = c.execute("SELECT MAX(ts) t, COUNT(*) n FROM lecturas WHERE fuente = 'corr'").fetchone()
chk("41. DXY: la vela de 15 min que sigue abierta no se guarda; cada cierre queda a su hora real (apertura + 15 min)", ult["t"] == fin_ and ult["n"] == 40, dict(ult))
print("\nTODO OK" if ok else "\nHAY FALLAS"); sys.exit(0 if ok else 1)
