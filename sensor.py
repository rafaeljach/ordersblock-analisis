"""
Sensor de rumores y noticias (v119, Rafael 25 nov 2026).

Descubre eventos minuto a minuto (no hay lista fija) en fuentes gratis y publicas, calcula su mapa de calor por evento y activo
y lo deja listo para que el Worker lo lea con UNA sola llamada (GET /sensor). Corre en su propio hilo, con su propio SQLite
(sensor.db), sin tocar la base del motor ni el escaner. Arranca solo con ORDERSBLOCK_SENSOR=1 en el .env.

Fuentes (cada una con su tope de llamadas por minuto, tope de tiempo de 8 s y pausa de 15 min tras 5 fallos seguidos):
  - Polymarket (Gamma, publica): mercados activos con mas volumen en 24 h; probabilidad y su cambio en 1 h y 24 h.
  - Kalshi (datos publicos de mercado): series de Fed, IPC, empleo y cierre del gobierno.
  - Titulares de alto impacto del servicio de noticias que ya corre en el droplet (sin llamadas nuevas).
  - Derivados de Bybit (publicos): interes abierto y financiacion de BTC y ETH; proporcion de largos y cortos cada 5 min.
  - Bluesky (contrasena de app en el .env, nunca en el repo): SOLO el conteo de publicaciones por tema; nada se muestra.
  - GDELT (prensa de 65 idiomas, cada 15 min, 1 llamada cada 5 s): volumen por tema y volumen por idioma (ventaja de origen).
  - Wikipedia (una vez al dia): vistas diarias de los articulos de cada tema en varias ediciones (confirmacion lenta).

Algoritmo (especificacion v119): A = suma w_s * sigmoid(z_s - 2) entre las fuentes con dato; S por tipo de causa y activo
(0.6 direccion clara, 0.3 mixto, 0 no toca; aprende con S <- 0.9 S + 0.1 min(1, m/2) a las 4 h); T = 100 A S.
Fria < 30, tibia 30-60, caliente > 60; como mucho 25 tibios o calientes; se cierra tras 6 h por debajo de 15.
Todos los umbrales se pueden cambiar en el .env sin desplegar.
"""

import calendar
import contextlib
import json
import logging
import math
import os
import re
import sqlite3
import threading
import time
import unicodedata

import requests

logger = logging.getLogger("ordersblock.sensor")

ACTIVO_ENV = "ORDERSBLOCK_SENSOR"
CARPETA = os.path.dirname(os.path.abspath(__file__))
RUTA_DB = os.getenv("ORDERSBLOCK_SENSOR_DB", os.path.join(os.path.dirname(os.getenv("ORDERSBLOCK_DB", "memoria_ordersblock.db")) or ".", "sensor.db"))
UA = "ordersblock-sensor/1.0 (+https://ordersblock.com)"
TIMEOUT = 8


def _f(nombre, defecto):
    try:
        return float(os.getenv(nombre, str(defecto)))
    except ValueError:
        return float(defecto)


U = {
    "prob_salto": _f("ORDERSBLOCK_SENSOR_PROB_SALTO", 0.05),      # 5 puntos en 1 h
    "vol_min": _f("ORDERSBLOCK_SENSOR_VOL_MIN", 50000),            # USD en 24 h para que una sola apuesta no cree un evento
    "vol_veces": _f("ORDERSBLOCK_SENSOR_VOL_VECES", 3),            # volumen del dia frente a su promedio diario de 7 dias
    "z_salto": _f("ORDERSBLOCK_SENSOR_Z_SALTO", 3),                # GDELT y Bluesky: 3 desviaciones
    "max_eventos": int(_f("ORDERSBLOCK_SENSOR_MAX_EVENTOS", 25)),
    "tibio": _f("ORDERSBLOCK_SENSOR_TIBIO", 30),
    "caliente": _f("ORDERSBLOCK_SENSOR_CALIENTE", 60),
    "cierre_T": _f("ORDERSBLOCK_SENSOR_CIERRE_T", 15),
    "cierre_h": _f("ORDERSBLOCK_SENSOR_CIERRE_H", 6),
    "funding_extremo": _f("ORDERSBLOCK_SENSOR_FUNDING_EXTREMO", 0.0005),
}
ACTIVOS = ["XAUUSD", "EURUSD", "GBPUSD", "USDJPY", "BTCUSD", "ETHUSD"]

# ---------------------------------------------------------------- mapa de causas
# efecto: direccion de cada activo cuando el tema va en el sentido "pos" (polaridad +1). corr: lo mismo para el DXY y el Brent,
# que son los correlacionados que prueban la causa en el Worker. Sin corr, la causa no se puede probar (queda sin causa clara).
MAPA = {
    "fed": {"claves": ["fed", "federal reserve", "fomc", "powell", "reserva federal", "fed funds", "美联储", "federal reserve system"],
            "pos": ["cut", "cuts", "recorte", "recorta", "baja de tasas", "rebaja", "dovish", "easing", "corte de tasas"],
            "neg": ["hike", "hikes", "subida de tasas", "sube las tasas", "alza de tasas", "hawkish", "tightening"],
            "efecto": {"XAUUSD": 1, "EURUSD": 1, "GBPUSD": 1, "USDJPY": -1, "BTCUSD": 1, "ETHUSD": 1}, "corr": {"DXY": -1}},
    "bce": {"claves": ["ecb", "bce", "lagarde", "banco central europeo", "european central bank", "ezb", "europaische zentralbank"],
            "pos": ["cut", "recorte", "rebaja", "dovish", "senkung"], "neg": ["hike", "subida", "alza", "hawkish", "erhohung"],
            "efecto": {"EURUSD": -1}, "corr": {"DXY": 1}},
    "boj": {"claves": ["boj", "bank of japan", "banco de japon", "ueda", "日本銀行", "日銀"],
            "pos": ["hike", "subida", "alza", "hawkish", "利上げ"], "neg": ["cut", "recorte", "dovish", "easing"],
            "efecto": {"USDJPY": -1}, "corr": {"DXY": -1}},
    "boe": {"claves": ["boe", "bank of england", "banco de inglaterra", "bailey"],
            "pos": ["cut", "recorte", "rebaja", "dovish"], "neg": ["hike", "subida", "alza", "hawkish"],
            "efecto": {"GBPUSD": -1}, "corr": {"DXY": 1}},
    "inflacion": {"claves": ["cpi", "inflation", "inflacion", "ipc", "pce", "inflacao", "inflationsrate", "通胀"],
                  "pos": ["hot", "hotter", "higher", "rises", "accelerat", "above", "sube", "acelera", "mayor de lo esperado", "por encima"],
                  "neg": ["cool", "cooler", "lower", "falls", "slows", "below", "baja", "se modera", "menor de lo esperado", "por debajo"],
                  "efecto": {"XAUUSD": -1, "EURUSD": -1, "GBPUSD": -1, "USDJPY": 1, "BTCUSD": -1, "ETHUSD": -1}, "corr": {"DXY": 1}},
    "empleo": {"claves": ["nonfarm", "payrolls", "nfp", "jobs report", "empleo", "desempleo", "unemployment", "nominas"],
               "pos": ["beat", "beats", "strong", "stronger", "above", "mas de lo esperado", "supera", "fuerte"],
               "neg": ["miss", "misses", "weak", "weaker", "below", "menos de lo esperado", "debil"],
               "efecto": {"XAUUSD": -1, "EURUSD": -1, "GBPUSD": -1, "USDJPY": 1, "BTCUSD": -1, "ETHUSD": -1}, "corr": {"DXY": 1}},
    "aranceles": {"claves": ["tariff", "tariffs", "arancel", "aranceles", "trade war", "guerra comercial", "zolle", "关税"],
                  "pos": ["impose", "new", "raise", "hike", "impone", "sube", "nuevos"], "neg": ["pause", "lift", "cut", "deal", "acuerdo", "suspende", "elimina"],
                  "efecto": {"XAUUSD": 1, "BTCUSD": -1, "ETHUSD": -1}, "corr": {}},
    "petroleo": {"claves": ["oil", "crude", "opec", "petroleo", "opep", "brent", "wti", "нефть"],
                 "pos": ["cut", "output cut", "recorte de produccion", "surge", "sube", "dispara", "supply disruption"],
                 "neg": ["increase output", "aumenta la produccion", "plunge", "cae", "se desploma", "glut"],
                 "efecto": {"XAUUSD": 1}, "corr": {"BRENT": 1}},
    "ormuz": {"claves": ["hormuz", "ormuz", "هرمز", "strait of hormuz", "estrecho de ormuz"],
              "pos": ["close", "closure", "cierre", "attack", "ataque", "blockade", "bloqueo", "seize", "tension"],
              "neg": ["deal", "acuerdo", "agreement", "reopen", "reabre", "ceasefire", "alto el fuego"],
              "efecto": {"XAUUSD": 1, "USDJPY": -1, "BTCUSD": -1, "ETHUSD": -1}, "corr": {"BRENT": 1}},
    "iran": {"claves": ["iran", "tehran", "teheran", "ایران", "irgc"],
             "pos": ["strike", "attack", "ataque", "war", "guerra", "missile", "misil", "sanction", "sancion"],
             "neg": ["deal", "acuerdo", "talks", "negociacion", "ceasefire", "alto el fuego"],
             "efecto": {"XAUUSD": 1, "BTCUSD": -1, "ETHUSD": -1}, "corr": {"BRENT": 1}},
    "ucrania": {"claves": ["ukraine", "ucrania", "україна", "украина", "kyiv", "kiev", "zelensky", "zelenski"],
                "pos": ["attack", "offensive", "ofensiva", "escalat", "escala", "missile", "misil"],
                "neg": ["ceasefire", "alto el fuego", "peace", "paz", "deal", "acuerdo", "truce", "tregua"],
                "efecto": {"XAUUSD": 1, "EURUSD": -1}, "corr": {"DXY": 1}},
    "regulacion_cripto": {"claves": ["crypto regulation", "regulacion cripto", "stablecoin", "bitcoin etf", "etf de bitcoin", "sec crypto", "crypto bill", "ley cripto"],
                          "pos": ["approve", "approves", "aprueba", "approval", "aprobacion", "passes", "legal"],
                          "neg": ["ban", "prohibe", "lawsuit", "demanda", "reject", "rechaza", "crackdown"],
                          "efecto": {"BTCUSD": 1, "ETHUSD": 1}, "corr": {}},
}
# Consultas de GDELT por tema (sintaxis de su API: OR entre comillas, entre parentesis)
GDELT_CONSULTA = {
    "fed": '("federal reserve" OR fomc OR powell)', "bce": '("european central bank" OR lagarde)', "boj": '("bank of japan" OR "kazuo ueda")',
    "boe": '("bank of england" OR "andrew bailey")', "inflacion": '(inflation OR "consumer price index")', "empleo": '(payrolls OR "jobs report" OR unemployment)',
    "aranceles": '(tariff OR tariffs OR "trade war")', "petroleo": '(opec OR "crude oil" OR brent)', "ormuz": '(hormuz)', "iran": '(iran OR tehran)',
    "ucrania": '(ukraine OR kyiv)', "regulacion_cripto": '("crypto regulation" OR stablecoin OR "bitcoin etf")',
}
# Articulos de Wikipedia por tema (titulos por edicion; los de cada idioma salen de Wikidata y se fijan aqui)
WIKI = {
    "fed": {"en": "Federal_Reserve", "es": "Reserva_Federal_de_los_Estados_Unidos", "de": "Federal_Reserve_System"},
    "ormuz": {"en": "Strait_of_Hormuz", "es": "Estrecho_de_Ormuz", "fa": "تنگه_هرمز"},
    "iran": {"en": "Iran", "es": "Irán", "fa": "ایران"},
    "ucrania": {"en": "Russian_invasion_of_Ukraine", "es": "Invasión_rusa_de_Ucrania_de_2022", "uk": "Російське_вторгнення_в_Україну_(2022)"},
    "aranceles": {"en": "Tariff", "es": "Arancel"},
    "inflacion": {"en": "Inflation", "es": "Inflación"},
}
# Series de Kalshi seguidas (configurables: si una no existe, la fuente lo anota y sigue)
KALSHI_SERIES = [s.strip() for s in os.getenv("ORDERSBLOCK_SENSOR_KALSHI_SERIES", "KXFED,KXFEDDECISION,KXCPI,KXCPIYOY,KXPAYROLLS,KXU3,KXGOVSHUT").split(",") if s.strip()]

POLY = "https://gamma-api.polymarket.com/markets"
KALSHI = "https://api.elections.kalshi.com/trade-api/v2/markets"
BYBIT_TICKER = "https://api.bybit.com/v5/market/tickers"
BYBIT_RATIO = "https://api.bybit.com/v5/market/account-ratio"
GDELT = "https://api.gdeltproject.org/api/v2/doc/doc"
WIKI_API = "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/{edicion}.wikipedia/all-access/user/{titulo}/daily/{desde}/{hasta}"
BSKY_LOGIN = "https://bsky.social/xrpc/com.atproto.server.createSession"
BSKY_BUSCAR = "https://bsky.social/xrpc/app.bsky.feed.searchPosts"
YAHOO_BRENT = "https://query1.finance.yahoo.com/v8/finance/chart/BZ=F"
# El Brent sale de Yahoo, cuyas condiciones no permiten uso comercial sin permiso: apagado hasta tener una fuente con licencia.
BRENT_ACTIVO = os.getenv("ORDERSBLOCK_SENSOR_BRENT", "0") == "1"

# fuente -> (cada cuantos segundos, tope de llamadas por minuto)
FUENTES = {"polymarket": (60, 60), "kalshi": (60, 30), "noticias": (60, 0), "precios": (60, 0), "bybit": (60, 20),
           "bluesky": (300, 30), "gdelt": (900, 12), "wikipedia": (86400, 60), "correlados": (300, 12)}

_lock_init = threading.Lock()      # solo para crear las tablas (rapido)
_lock_ciclo = threading.Lock()     # una vuelta a la vez; /sensor NUNCA lo espera
_listas = set()                    # bases ya inicializadas
_hilo = None
_parar = threading.Event()
_ultima = {}            # fuente -> ultima vez que corrio
_llamadas = {}          # fuente -> [tiempos de las llamadas del ultimo minuto]
_bsky = {"jwt": None, "t": 0.0}
_cola_gdelt = []        # temas pendientes de GDELT en esta vuelta de 15 min


def activo() -> bool:
    return os.getenv(ACTIVO_ENV, "0") == "1"


# ---------------------------------------------------------------- base propia

@contextlib.contextmanager
def conectar(ruta: str = None):
    """Autocommit (nunca una transaccion larga mientras se espera a la red) y la conexion se cierra al salir."""
    c = sqlite3.connect(ruta or RUTA_DB, timeout=5, check_same_thread=False, isolation_level=None)
    try:
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA busy_timeout = 5000")
        c.execute("PRAGMA synchronous = NORMAL")      # seguro en WAL: un fsync por transaccion, no por fila
        yield c
    finally:
        c.close()


def inicializar(ruta: str = None, forzar: bool = False) -> None:
    r = ruta or RUTA_DB
    if r in _listas and not forzar:
        return
    with _lock_init, conectar(ruta) as c:
        c.execute("PRAGMA journal_mode = WAL")      # quien lee (la ruta /sensor) no espera a quien escribe (el hilo)
        c.execute("PRAGMA journal_size_limit = 67108864")   # el -wal no se queda gigante despues de la poda
        c.executescript("""
        CREATE TABLE IF NOT EXISTS lecturas (id INTEGER PRIMARY KEY AUTOINCREMENT, fuente TEXT NOT NULL, clave TEXT NOT NULL, ts INTEGER NOT NULL,
            valor REAL, volumen REAL, extra TEXT);
        CREATE INDEX IF NOT EXISTS ix_lec ON lecturas(fuente, clave, ts);
        CREATE TABLE IF NOT EXISTS eventos (id TEXT PRIMARY KEY, titulo TEXT NOT NULL, causa TEXT NOT NULL, palabras TEXT NOT NULL DEFAULT '',
            activos TEXT NOT NULL DEFAULT '', polaridad INTEGER NOT NULL DEFAULT 0, primera INTEGER NOT NULL, ultima INTEGER NOT NULL,
            salto_ts INTEGER, estado TEXT NOT NULL DEFAULT 'frio', origen TEXT NOT NULL, fuentes TEXT NOT NULL DEFAULT '', bajo_desde INTEGER,
            aprendido INTEGER NOT NULL DEFAULT 0, extra TEXT);
        CREATE TABLE IF NOT EXISTS temperaturas (evento TEXT NOT NULL, activo TEXT NOT NULL, ts INTEGER NOT NULL, A REAL, S REAL, T REAL,
            idioma_origen TEXT, ventaja_min REAL);
        CREATE INDEX IF NOT EXISTS ix_tem ON temperaturas(evento, activo, ts);
        CREATE TABLE IF NOT EXISTS sensibilidad (causa TEXT NOT NULL, activo TEXT NOT NULL, S REAL NOT NULL, n INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (causa, activo));
        CREATE TABLE IF NOT EXISTS salud (fuente TEXT PRIMARY KEY, ultimo_ok INTEGER, ultimo_error TEXT, error_ts INTEGER, seguidos INTEGER NOT NULL DEFAULT 0,
            pausado_hasta INTEGER, llamadas TEXT NOT NULL DEFAULT '{}', errores TEXT NOT NULL DEFAULT '{}');
        """)
    _listas.add(r)


def _num(x):
    """Numero finito o None (un 1e999, NaN o texto no entra a la base ni a la respuesta)."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def limpio(o):
    """Copia sin numeros no finitos (JSON valido siempre)."""
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {k: limpio(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [limpio(v) for v in o]
    return o


_buffer = threading.local()   # lecturas de la fuente en curso: se escriben juntas al final, en una transaccion corta y sin red


def _guardar_lectura(c, fuente, clave, ts, valor, volumen=None, extra=None):
    fila = (fuente, clave, int(ts), _num(valor), _num(volumen), json.dumps(extra, ensure_ascii=False)[:2000] if extra is not None else None)
    pend = getattr(_buffer, "filas", None)
    if pend is not None:
        pend.append(fila)
        return
    c.execute("INSERT INTO lecturas (fuente, clave, ts, valor, volumen, extra) VALUES (?, ?, ?, ?, ?, ?)", fila)


def _volcar(c):
    filas, _buffer.filas = getattr(_buffer, "filas", None) or [], None
    if filas:
        c.execute("BEGIN")
        try:
            c.executemany("INSERT INTO lecturas (fuente, clave, ts, valor, volumen, extra) VALUES (?, ?, ?, ?, ?, ?)", filas)
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
    return len(filas)


def _serie(c, fuente, clave, desde_ts):
    return [(r["ts"], r["valor"]) for r in c.execute("SELECT ts, valor FROM lecturas WHERE fuente = ? AND clave = ? AND ts >= ? AND valor IS NOT NULL ORDER BY ts",
                                                     (fuente, clave, int(desde_ts)))]


def _valor_en(c, fuente, clave, ts, tolerancia_s=3600):
    """La lectura mas cercana a ts (dentro de la tolerancia)."""
    r = c.execute("SELECT valor, ts FROM lecturas WHERE fuente = ? AND clave = ? AND ts BETWEEN ? AND ? AND valor IS NOT NULL ORDER BY ABS(ts - ?) LIMIT 1",
                  (fuente, clave, int(ts - tolerancia_s), int(ts + tolerancia_s), int(ts))).fetchone()
    return r["valor"] if r else None


# ---------------------------------------------------------------- salud y topes

def _permiso(fuente: str, ahora: float = None) -> bool:
    """Tope de llamadas por minuto de cada fuente (por debajo del que publica)."""
    ahora = ahora or time.time()
    tope = FUENTES.get(fuente, (0, 30))[1]
    lista = [t for t in _llamadas.get(fuente, []) if ahora - t < 60]
    if tope and len(lista) >= tope:
        _llamadas[fuente] = lista
        return False
    lista.append(ahora)
    _llamadas[fuente] = lista
    return True


def _pausada(c, fuente, ahora):
    r = c.execute("SELECT pausado_hasta FROM salud WHERE fuente = ?", (fuente,)).fetchone()
    return bool(r and r["pausado_hasta"] and r["pausado_hasta"] > ahora)


def _cubetas(texto, ahora):
    """{hora: cuantas} de las ultimas 24 h (antes era una lista de tiempos que se cortaba en 5000)."""
    try:
        d = json.loads(texto or "{}")
        d = d if isinstance(d, dict) else {}
    except ValueError:
        d = {}
    h0 = int(ahora // 3600)
    return {k: int(v) for k, v in d.items() if str(k).lstrip("-").isdigit() and h0 - int(k) < 24}


def _anotar(c, fuente, ok: bool, error: str = None, ahora: float = None, llamadas: int = 0, parcial: str = None):
    """ok: la fuente respondio; parcial: respondio pero algo fallo (se anota el error sin contarlo como fallo seguido)."""
    ahora = int(ahora or time.time())
    h = str(ahora // 3600)
    r = c.execute("SELECT seguidos, llamadas, errores FROM salud WHERE fuente = ?", (fuente,)).fetchone()
    seguidos = (r["seguidos"] if r else 0)
    ll, er = _cubetas(r["llamadas"] if r else None, ahora), _cubetas(r["errores"] if r else None, ahora)
    ll[h] = ll.get(h, 0) + max(0, int(llamadas))
    if ok:
        if parcial:
            er[h] = er.get(h, 0) + 1
        c.execute("""INSERT INTO salud (fuente, ultimo_ok, seguidos, llamadas, errores, ultimo_error, error_ts) VALUES (?, ?, 0, ?, ?, ?, ?)
            ON CONFLICT(fuente) DO UPDATE SET ultimo_ok = excluded.ultimo_ok, seguidos = 0, llamadas = excluded.llamadas, errores = excluded.errores,
              ultimo_error = COALESCE(excluded.ultimo_error, salud.ultimo_error), error_ts = COALESCE(excluded.error_ts, salud.error_ts)""",
                  (fuente, ahora, json.dumps(ll), json.dumps(er), ("parcial: " + parcial)[:300] if parcial else None, ahora if parcial else None))
        return
    seguidos += 1
    er[h] = er.get(h, 0) + 1
    pausa = ahora + 900 if seguidos >= 5 else None   # 5 fallos seguidos: 15 minutos de pausa
    c.execute("""INSERT INTO salud (fuente, ultimo_error, error_ts, seguidos, pausado_hasta, llamadas, errores) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(fuente) DO UPDATE SET ultimo_error = excluded.ultimo_error, error_ts = excluded.error_ts, seguidos = excluded.seguidos,
          pausado_hasta = COALESCE(excluded.pausado_hasta, salud.pausado_hasta), llamadas = excluded.llamadas, errores = excluded.errores""",
              (fuente, str(error or "error")[:300], ahora, 0 if pausa else seguidos, pausa, json.dumps(ll), json.dumps(er)))
    if pausa:
        logger.warning(f"sensor: {fuente} fallo 5 veces seguidas, pausa de 15 min ({error})")


def salud(ruta: str = None) -> dict:
    out = {}
    if not os.path.exists(ruta or RUTA_DB):
        return out                                   # apagado y sin base: no se crea una vacia
    try:
        ahora = time.time()
        with conectar(ruta) as c:
            for r in c.execute("SELECT fuente, ultimo_ok, ultimo_error, error_ts, pausado_hasta, llamadas, errores FROM salud"):
                out[r["fuente"]] = {"ultimo_ok": r["ultimo_ok"], "ultimo_error": r["ultimo_error"], "error_ts": r["error_ts"], "pausado_hasta": r["pausado_hasta"],
                                    "llamadas_24h": sum(_cubetas(r["llamadas"], ahora).values()), "errores_24h": sum(_cubetas(r["errores"], ahora).values())}
    except Exception as e:  # noqa: BLE001
        out["_error"] = f"{type(e).__name__}: {e}"
    return out


def _json(resp):
    """Respuesta -> JSON o excepcion con el codigo (429, 500, JSON roto)."""
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}")
    try:
        return resp.json()
    except ValueError as e:
        raise RuntimeError("JSON roto") from e


def _pedir(fuente, get, url, params=None, headers=None):
    if not _permiso(fuente):
        raise RuntimeError("tope de llamadas por minuto")
    h = {"User-Agent": UA, "Accept": "application/json"}
    h.update(headers or {})
    return _json(get(url, params=params, headers=h, timeout=TIMEOUT))


# ---------------------------------------------------------------- texto y clasificacion

_ESP = re.compile(r"\s+")
_PAL = re.compile(r"[a-z0-9Ѐ-ӿ؀-ۿ぀-ヿ一-鿿]+")
_VACIAS = {"will", "the", "and", "for", "with", "from", "that", "this", "before", "after", "into", "over", "under", "than", "what", "when",
           "del", "los", "las", "una", "por", "para", "con", "que", "sobre", "antes", "despues", "como", "mas", "este", "esta"}


def normalizar(t: str) -> str:
    """Minusculas y sin tildes; las escrituras no latinas (arabe, cirilico, chino, japones) se conservan."""
    t = "".join(ch for ch in unicodedata.normalize("NFKD", str(t or "")) if not unicodedata.combining(ch))
    return _ESP.sub(" ", t.lower()).strip()


def palabras(t: str) -> set:
    return {p for p in _PAL.findall(normalizar(t)) if len(p) > 3 and p not in _VACIAS}


def _contiene(texto_norm: str, clave: str) -> bool:
    k = normalizar(clave)
    if not k:
        return False
    if k.isascii() and re.fullmatch(r"[a-z0-9 ]+", k):
        return re.search(r"(?<![a-z0-9])" + re.escape(k), texto_norm) is not None
    return k in texto_norm


def tema_de(texto: str):
    """El tema del mapa de causas al que pertenece un texto (el de mas coincidencias), o None si no tiene cadena a nuestros activos."""
    t = normalizar(texto)
    mejor, n = None, 0
    for tema, m in MAPA.items():
        k = sum(1 for cl in m["claves"] if _contiene(t, cl))
        if k > n:
            mejor, n = tema, k
    return mejor


def polaridad_texto(texto: str, tema: str) -> int:
    t, m = normalizar(texto), MAPA.get(tema) or {}
    pos = sum(1 for k in m.get("pos", []) if _contiene(t, k))
    neg = sum(1 for k in m.get("neg", []) if _contiene(t, k))
    return 1 if pos > neg else -1 if neg > pos else 0


def direccion(tema: str, activo: str, polaridad: int) -> int:
    e = (MAPA.get(tema) or {}).get("efecto", {}).get(activo, 0)
    return int(e * polaridad) if polaridad else 0


def sigmoide(x: float) -> float:
    try:
        return 1.0 / (1.0 + math.exp(-x))
    except OverflowError:
        return 0.0 if x < 0 else 1.0


def zscore(x, serie, minimo=10):
    """Cuanto se sale x del promedio de la serie (sin x). None si no hay historial suficiente."""
    v = [float(s) for s in serie if s is not None]
    if len(v) < minimo:
        return None
    m = sum(v) / len(v)
    sd = math.sqrt(sum((a - m) ** 2 for a in v) / max(1, len(v) - 1))
    if sd <= 1e-12:
        return 0.0 if abs(x - m) <= 1e-12 else (10.0 if x > m else -10.0)
    return (x - m) / sd


# ---------------------------------------------------------------- lectura de fuentes
# Cada lectora devuelve una lista de "senales": {fuente, clave, titulo, tema, polaridad, z, ts, salto, extra}. Una senal con salto
# crea o refuerza un evento; sin salto solo aporta atencion a un evento que ya existe.

def leer_polymarket(c, get=requests.get, ahora=None):
    ahora = ahora or time.time()
    d = _pedir("polymarket", get, POLY, params={"active": "true", "closed": "false", "order": "volume24hr", "ascending": "false", "limit": 100})
    if not isinstance(d, list):
        raise RuntimeError("Polymarket no devolvio una lista")
    out, basura = [], 0
    for m in d[:100]:
        try:
            if not isinstance(m, dict):
                basura += 1
                continue
            q = str(m.get("question") or "")[:200]
            tema = tema_de(q)
            if not tema:
                continue
            precios = m.get("outcomePrices")
            precios = json.loads(precios) if isinstance(precios, str) else precios
            p = _num(precios[0]) if precios else _num(m.get("lastTradePrice"))
            if p is None or not (0 <= p <= 1):
                continue
            vol24 = _num(m.get("volume24hr"))
            if vol24 is None:
                basura += 1                                # un volumen que no es numero es un formato desconocido
                continue
            vol1w = _num(m.get("volume1wk")) or 0.0
            if vol24 < U["vol_min"] / 10:
                continue                                   # sin volumen no se guarda (la base no crece con mercados muertos)
            clave = "pm:" + str(m.get("id") or m.get("slug") or q)[:60]
            _guardar_lectura(c, "polymarket", clave, ahora, p, vol24, {"q": q})
            hace1h = _valor_en(c, "polymarket", clave, ahora - 3600, 900)
            d1h = (p - hace1h) if hace1h is not None else (_num(m.get("oneHourPriceChange")) or 0.0)
            d24 = _num(m.get("oneDayPriceChange")) or 0.0
            z = abs(d1h) / 0.025                           # punto de partida: 5 puntos en 1 h valen z = 2 (a = 0.5)
            vol_salto = vol1w > 0 and vol24 >= U["vol_veces"] * vol1w / 7
            salto = vol24 >= U["vol_min"] and (abs(d1h) >= U["prob_salto"] or vol_salto)
            pol = polaridad_texto(q, tema) * (1 if d1h > 0 else -1 if d1h < 0 else 0)
            out.append({"fuente": "polymarket", "clave": clave, "titulo": q, "tema": tema, "polaridad": pol, "z": z, "ts": ahora,
                        "salto": salto, "extra": {"prob": p, "d1h": round(d1h, 4), "d24h": round(d24, 4), "vol24": vol24}})
        except (TypeError, ValueError, IndexError, KeyError):
            basura += 1
            continue
    if d and basura >= len(d[:100]):
        out.append({"_parcial": f"los {basura} mercados llegaron con un formato desconocido (cambio la API?)"})
    return out


def _kalshi_precio(m, campo):
    """Kalshi migro a campos en dolares (*_dollars, 0-1); los viejos venian en centavos."""
    v = _num(m.get(campo + "_dollars"))
    if v is not None:
        return v
    v = _num(m.get(campo))
    return v / 100.0 if v is not None else None


KALSHI_TEMA = {"KXFED": "fed", "KXFEDDECISION": "fed", "KXCPI": "inflacion", "KXCPIYOY": "inflacion", "KXPAYROLLS": "empleo", "KXU3": "empleo"}


def leer_kalshi(c, get=requests.get, ahora=None):
    ahora = ahora or time.time()
    out, errores, series = [], [], KALSHI_SERIES[:10]
    for serie in series:
        try:
            d = _pedir("kalshi", get, KALSHI, params={"series_ticker": serie, "status": "open", "limit": 50})
        except RuntimeError as e:
            errores.append(f"{serie}: {e}")
            continue
        if not (isinstance(d, dict) and isinstance(d.get("markets"), list)):
            errores.append(f"{serie}: respuesta sin lista de mercados (cambio la API?)")
            continue
        for m in d["markets"][:50]:
            try:
                if not isinstance(m, dict):
                    continue
                titulo = str(m.get("title") or m.get("subtitle") or m.get("ticker") or "")[:200]
                tema = tema_de(titulo + " " + serie.replace("KX", "")) or KALSHI_TEMA.get(serie)
                if not tema:
                    continue
                p = _kalshi_precio(m, "last_price")
                if p is None:
                    p = _kalshi_precio(m, "yes_bid")
                if p is None or not (0 <= p <= 1):
                    continue
                prev = _kalshi_precio(m, "previous_price")
                vol24 = _num(m.get("volume_24h_fp")) or _num(m.get("volume_24h")) or 0.0
                if vol24 < U["vol_min"] / 10:
                    continue
                clave = "ks:" + str(m.get("ticker") or titulo)[:60]
                _guardar_lectura(c, "kalshi", clave, ahora, p, vol24, {"t": titulo})
                hace1h = _valor_en(c, "kalshi", clave, ahora - 3600, 900)
                d1h = (p - hace1h) if hace1h is not None else 0.0
                d24 = (p - prev) if prev is not None else 0.0
                salto = vol24 >= U["vol_min"] and abs(d1h) >= U["prob_salto"]
                pol = polaridad_texto(titulo, tema) * (1 if d1h > 0 else -1 if d1h < 0 else 0)
                out.append({"fuente": "kalshi", "clave": clave, "titulo": titulo, "tema": tema, "polaridad": pol, "z": abs(d1h) / 0.025, "ts": ahora, "salto": salto,
                            "extra": {"prob": p, "d1h": round(d1h, 4), "d24h": round(d24, 4), "vol24": vol24}})
            except Exception:  # noqa: BLE001  (un item basura no tumba la fuente)
                continue
    if errores and len(errores) == len(series):
        raise RuntimeError("ninguna serie de Kalshi respondio: " + errores[0])
    if errores:
        out.append({"_parcial": f"{len(errores)} de {len(series)} series fallaron ({errores[0]})"})
    return out


def leer_noticias(c, ahora=None, ruta_noticias=None):
    """Titulares de alto impacto (impacto 2) de las ultimas 2 h del servicio de noticias. Sin llamadas nuevas."""
    ahora = ahora or time.time()
    ruta = ruta_noticias or os.getenv("ORDERSBLOCK_NOTICIAS_DB", os.path.join(CARPETA, "noticias.db"))
    if not os.path.exists(ruta):
        raise RuntimeError("no hay base de noticias (servicio ordersblock-noticias)")
    out = []
    with contextlib.closing(sqlite3.connect("file:" + ruta + "?mode=ro", uri=True, timeout=5)) as cn:
        cn.row_factory = sqlite3.Row
        for r in cn.execute("SELECT clave, titulo, publicado, recibido FROM noticias WHERE impacto = 2 AND recibido >= ? ORDER BY recibido DESC LIMIT 100",
                            (int(ahora - 7200),)):
            tema = tema_de(r["titulo"])
            if not tema:
                continue
            edad_h = max(0.0, (ahora - float(r["recibido"])) / 3600.0)
            out.append({"fuente": "noticias", "clave": "nt:" + str(r["clave"])[:60], "titulo": str(r["titulo"])[:200], "tema": tema,
                        "polaridad": polaridad_texto(r["titulo"], tema), "z": 3.0 * math.exp(-edad_h / 2.0), "ts": min(ahora, float(r["publicado"] or r["recibido"])),
                        "salto": edad_h <= 1.0, "extra": {}})
    return out


def leer_precios(c, ahora=None):
    """Precios del cintillo (sin llamadas nuevas): sirven para medir cuanto se movio cada activo tras un evento."""
    ahora = ahora or time.time()
    import precios
    n = 0
    for it in (precios.snapshot().get("mercados") or []):
        if it.get("mercado") in ACTIVOS and it.get("precio"):
            _guardar_lectura(c, "precio", it["mercado"], ahora, float(it["precio"]))
            n += 1
    return n


def leer_bybit(c, get=requests.get, ahora=None, con_ratio=False):
    ahora = ahora or time.time()
    n = 0
    for sim, activo_ in (("BTCUSDT", "BTCUSD"), ("ETHUSDT", "ETHUSD")):
        d = _pedir("bybit", get, BYBIT_TICKER, params={"category": "linear", "symbol": sim})
        lst = ((d.get("result") or {}).get("list") or []) if isinstance(d, dict) else []
        if not lst:
            raise RuntimeError("Bybit sin datos de " + sim)
        t = lst[0]
        _guardar_lectura(c, "bybit_oi", activo_, ahora, _num(t.get("openInterest")) or None)
        fr = _num(t.get("fundingRate"))
        _guardar_lectura(c, "bybit_funding", activo_, ahora, fr if fr is not None and abs(fr) <= 0.05 else None)   # mas de 5 % por 8 h no es real
        n += 1
        if con_ratio:
            r = _pedir("bybit", get, BYBIT_RATIO, params={"category": "linear", "symbol": sim, "period": "5min", "limit": 1})
            rl = ((r.get("result") or {}).get("list") or []) if isinstance(r, dict) else []
            if rl:
                _guardar_lectura(c, "bybit_ratio", activo_, ahora, _num(rl[0].get("buyRatio")))
    return n


def _sesion_bluesky(get_post):
    usuario, clave = os.getenv("BLUESKY_HANDLE", "").strip(), os.getenv("BLUESKY_APP_PASSWORD", "").strip()
    if not usuario or not clave:
        return None
    if _bsky["jwt"] and time.time() - _bsky["t"] < 3600:
        return _bsky["jwt"]
    if not _permiso("bluesky"):
        raise RuntimeError("tope de llamadas por minuto")
    r = get_post(BSKY_LOGIN, json={"identifier": usuario, "password": clave}, headers={"User-Agent": UA}, timeout=TIMEOUT)
    d = _json(r)
    _bsky.update(jwt=d.get("accessJwt"), t=time.time())
    return _bsky["jwt"]


def leer_bluesky(c, consultas, get=requests.get, post=requests.post, ahora=None):
    """Solo el conteo de publicaciones de los ultimos 5 min por tema seguido (hasta 10). Nada se guarda de las publicaciones."""
    ahora = ahora or time.time()
    jwt = _sesion_bluesky(post)
    if not jwt:
        raise RuntimeError("sin credenciales de Bluesky en el .env")
    out = []
    desde = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ahora - 300))
    for tema, q in list(consultas.items())[:10]:
        try:
            d = _pedir("bluesky", get, BSKY_BUSCAR, params={"q": q, "since": desde, "limit": 100}, headers={"Authorization": "Bearer " + jwt})
        except RuntimeError as e:
            if "401" in str(e):
                _bsky.update(jwt=None, t=0.0)             # sesion vencida: la proxima vuelta entra de nuevo
            raise
        n = len(d.get("posts") or []) if isinstance(d, dict) else 0
        clave = "bs:" + tema
        hist = [v for _, v in _serie(c, "bluesky", clave, ahora - 30 * 86400)]
        _guardar_lectura(c, "bluesky", clave, ahora, n)
        z = zscore(n, hist)
        if z is None:
            continue                                   # sin historial no se opina (una fuente sin dato no cuenta)
        out.append({"fuente": "bluesky", "clave": clave, "titulo": tema, "tema": tema, "polaridad": 0, "z": z, "ts": ahora,
                    "salto": z >= U["z_salto"], "extra": {"n": n}})
    return out


def _gdelt_fecha(s):
    try:
        return int(calendar.timegm(time.strptime(str(s)[:15], "%Y%m%dT%H%M%S")))
    except (ValueError, OverflowError):
        return None


def leer_gdelt_tema(c, tema, get=requests.get, ahora=None):
    """Volumen de prensa del tema en bloques de 15 min (24 h): z del ultimo bloque frente a los anteriores."""
    ahora = ahora or time.time()
    d = _pedir("gdelt", get, GDELT, params={"query": GDELT_CONSULTA[tema], "mode": "timelinevolraw", "timespan": "24h", "format": "json"})
    serie = (((d or {}).get("timeline") or [{}])[0].get("data") or []) if isinstance(d, dict) else []
    vals = [(_gdelt_fecha(p.get("date")), _num(p.get("value"))) for p in serie[-200:] if isinstance(p, dict)]
    vals = [(t, v) for t, v in vals if t and v is not None and t <= ahora + 900]   # una fecha del futuro no cuenta
    if len(vals) < 12:
        raise RuntimeError("GDELT con serie corta")
    t_ult, v_ult = vals[-1]
    z = zscore(v_ult, [v for _, v in vals[:-1]], minimo=10) or 0.0
    _guardar_lectura(c, "gdelt", "gd:" + tema, t_ult, v_ult)
    return {"fuente": "gdelt", "clave": "gd:" + tema, "titulo": "Prensa: " + tema, "tema": tema, "polaridad": 0, "z": z, "ts": t_ult,
            "salto": z >= U["z_salto"] and v_ult >= 5, "extra": {"articulos": v_ult}}


def ventaja_origen(d_lang: dict, ahora: float = None):
    """Del modo timelinelang de GDELT: por idioma, el primer bloque de 15 min con z >= 3 (frente a los anteriores; en una pasada).
    Devuelve (idiomas en el orden en que empezaron, minutos de ventaja del primero sobre el ingles)."""
    ahora = ahora or time.time()
    inicio = {}
    for s_ in ((d_lang or {}).get("timeline") or [])[:80]:
        try:
            nombre = str(s_.get("series") or "")[:30]
            vals = [(_gdelt_fecha(p.get("date")), _num(p.get("value"))) for p in (s_.get("data") or [])[-200:] if isinstance(p, dict)]
            vals = [(t, v) for t, v in vals if t and v is not None and t <= ahora + 900]
            n, media, m2 = 0, 0.0, 0.0
            for t, v in vals:
                if n >= 10:
                    sd = math.sqrt(m2 / (n - 1)) if n > 1 else 0.0
                    z = (v - media) / sd if sd > 1e-12 else (10.0 if v > media + 1e-12 else 0.0)
                    if z >= U["z_salto"]:
                        inicio[nombre] = t
                        break
                n += 1
                d = v - media
                media += d / n
                m2 += d * (v - media)
        except (TypeError, ValueError, AttributeError):
            continue
    orden = sorted(inicio, key=lambda k: inicio[k])
    if not orden:
        return [], None
    ventaja = (inicio["English"] - inicio[orden[0]]) / 60.0 if "English" in inicio else None
    return orden[:8], ventaja


def leer_gdelt_idiomas(c, tema, get=requests.get):
    d = _pedir("gdelt", get, GDELT, params={"query": GDELT_CONSULTA[tema], "mode": "timelinelang", "timespan": "24h", "format": "json"})
    return ventaja_origen(d if isinstance(d, dict) else {})


def leer_wikipedia(c, get=requests.get, ahora=None):
    """Vistas diarias de ayer frente a los 30 dias anteriores, por tema (todas sus ediciones sumadas)."""
    ahora = ahora or time.time()
    hasta = time.strftime("%Y%m%d", time.gmtime(ahora - 86400))
    desde = time.strftime("%Y%m%d", time.gmtime(ahora - 31 * 86400))
    out, fallos, total_llamadas = [], [], 0
    for tema, ediciones in WIKI.items():
        total = {}
        for ed, titulo in ediciones.items():
            total_llamadas += 1
            try:
                d = _pedir("wikipedia", get, WIKI_API.format(edicion=ed, titulo=requests.utils.quote(titulo, safe=""), desde=desde, hasta=hasta))
            except RuntimeError as e:
                fallos.append(f"{ed}:{tema} {e}")
                continue
            for it in (d.get("items") or []) if isinstance(d, dict) else []:
                if not isinstance(it, dict):
                    continue
                k = str(it.get("timestamp") or "")[:8]
                total[k] = total.get(k, 0) + int(_num(it.get("views")) or 0)
            time.sleep(0 if os.getenv("ORDERSBLOCK_SENSOR_PRUEBA") == "1" else 1)
        if len(total) < 11:
            continue
        dias = [total[k] for k in sorted(total)]
        z = zscore(dias[-1], dias[:-1]) or 0.0
        _guardar_lectura(c, "wikipedia", "wk:" + tema, ahora, dias[-1])
        out.append({"fuente": "wikipedia", "clave": "wk:" + tema, "titulo": "Wikipedia: " + tema, "tema": tema, "polaridad": 0, "z": z, "ts": ahora,
                    "salto": False, "extra": {"vistas": dias[-1]}})
    if fallos and len(fallos) == total_llamadas:
        raise RuntimeError("ninguna edicion de Wikipedia respondio: " + fallos[0])
    if fallos:
        out.append({"_parcial": f"{len(fallos)} de {total_llamadas} consultas fallaron ({fallos[0]})"})
    return out


# ---------------------------------------------------------------- eventos: descubrimiento, union y temperatura

def _evento_para(c, s):
    """El evento abierto con el que se une una senal: mismo tema y la mitad o mas de sus palabras en comun (o senal de tema entero)."""
    pal = palabras(s["titulo"])
    por_tema = s["fuente"] in ("gdelt", "bluesky", "wikipedia")
    for e in c.execute("SELECT id, palabras FROM eventos WHERE causa = ? AND estado != 'cerrado' ORDER BY ultima DESC", (s["tema"],)):
        if por_tema or e["id"] == s["clave"] or e["id"].startswith(s["clave"] + "@"):
            return e["id"]
        pe = set(filter(None, e["palabras"].split(",")))
        if pal and pe and len(pal & pe) >= 0.5 * min(len(pal), len(pe)):
            return e["id"]
    return None


def integrar(c, senales, ahora=None):
    """Crea o refuerza eventos con las senales de esta vuelta. Devuelve los ids tocados."""
    ahora = ahora or time.time()
    tocados = set()
    for s in senales:
        if not isinstance(s, dict) or not s.get("tema") or s["tema"] not in MAPA:
            continue                                         # sin fila en el mapa de causas: se descarta
        eid = _evento_para(c, s)
        if eid is None:
            if not s.get("salto"):
                continue                                     # sin salto no nace un evento
            eid = s["clave"] if s["fuente"] in ("polymarket", "kalshi", "noticias") else "tema:" + s["tema"] + ":" + str(int(ahora))
            if c.execute("SELECT 1 FROM eventos WHERE id = ?", (eid,)).fetchone():
                eid = eid + "@" + str(int(ahora))          # el mismo mercado vuelve a saltar despues de cerrado: evento nuevo
            activos = [a for a in MAPA[s["tema"]]["efecto"]]
            c.execute("""INSERT OR IGNORE INTO eventos (id, titulo, causa, palabras, activos, polaridad, primera, ultima, salto_ts, estado, origen, fuentes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'frio', ?, ?)""",
                      (eid, s["titulo"][:200], s["tema"], ",".join(sorted(palabras(s["titulo"])))[:500], ",".join(activos), int(s.get("polaridad") or 0),
                       int(s["ts"]), int(ahora), int(s["ts"]), s["fuente"], s["fuente"]))
        e = c.execute("SELECT fuentes, polaridad, extra FROM eventos WHERE id = ?", (eid,)).fetchone()
        fuentes = set(filter(None, (e["fuentes"] or "").split(","))) | {s["fuente"]}
        extra = json.loads(e["extra"] or "{}")
        extra.setdefault("aportes", {})[s["fuente"]] = {"z": round(float(s.get("z") or 0), 3), "ts": int(s["ts"])}
        if s["fuente"] in ("polymarket", "kalshi"):
            extra["prob"] = s["extra"]
        pol = int(e["polaridad"] or 0) or int(s.get("polaridad") or 0)
        c.execute("UPDATE eventos SET ultima = ?, fuentes = ?, polaridad = ?, extra = ?, salto_ts = CASE WHEN ? THEN ? ELSE salto_ts END WHERE id = ?",
                  (int(ahora), ",".join(sorted(fuentes)), pol, json.dumps(extra, ensure_ascii=False)[:4000], 1 if s.get("salto") else 0, int(s["ts"]), eid))
        tocados.add(eid)
    return tocados


def _S(c, causa, activo_, polaridad):
    e = (MAPA.get(causa) or {}).get("efecto", {})
    if activo_ not in e:
        return 0.0
    r = c.execute("SELECT S FROM sensibilidad WHERE causa = ? AND activo = ?", (causa, activo_)).fetchone()
    base = r["S"] if r else 0.6
    return round(min(base, 0.3) if not polaridad or not e[activo_] else base, 4)


_ult_temp = {}     # (evento, activo) -> (ts, T) de la ultima fila escrita


def temperaturas(c, ahora=None):
    """A, S y T de cada evento abierto; estado, tope de 25 y cierre tras 6 h por debajo de 15."""
    ahora = ahora or time.time()
    calientes = []
    for e in list(c.execute("SELECT id, causa, polaridad, extra, bajo_desde, activos FROM eventos WHERE estado != 'cerrado'")):
        extra = json.loads(e["extra"] or "{}")
        aportes = [v for v in (extra.get("aportes") or {}).values() if isinstance(v, dict) and 0 <= ahora - (_num(v.get("ts")) or 0) <= 6 * 3600 and _num(v.get("z")) is not None]
        A = sum(sigmoide(float(v["z"]) - 2) for v in aportes) / len(aportes) if aportes else 0.0
        Tmax = 0.0
        for a in filter(None, (e["activos"] or "").split(",")):
            S = _S(c, e["causa"], a, e["polaridad"])
            T = round(100 * A * S, 2)
            Tmax = max(Tmax, T)
            prev = _ult_temp.get((e["id"], a))
            if prev is None or abs(T - prev[1]) >= 1 or ahora - prev[0] >= 900:   # solo si cambia o cada 15 min: la base no crece de mas
                c.execute("INSERT INTO temperaturas (evento, activo, ts, A, S, T, idioma_origen, ventaja_min) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                          (e["id"], a, int(ahora), round(A, 4), S, T, (extra.get("idiomas") or [None])[0], extra.get("ventaja_min")))
                _ult_temp[(e["id"], a)] = (ahora, T)
        estado = "caliente" if Tmax > U["caliente"] else "tibio" if Tmax >= U["tibio"] else "frio"
        bajo = e["bajo_desde"] if Tmax < U["cierre_T"] else None
        if Tmax < U["cierre_T"] and not bajo:
            bajo = int(ahora)
        if bajo and ahora - bajo >= U["cierre_h"] * 3600:
            estado = "cerrado"
            for k in [k for k in _ult_temp if k[0] == e["id"]]:
                _ult_temp.pop(k, None)
        c.execute("UPDATE eventos SET estado = ?, bajo_desde = ? WHERE id = ?", (estado, bajo, e["id"]))
        if estado in ("tibio", "caliente"):
            calientes.append((Tmax, e["id"]))
    calientes.sort(reverse=True)
    for _, eid in calientes[U["max_eventos"]:]:             # se siguen como mucho 25: los demas se enfrian y salen
        c.execute("UPDATE eventos SET estado = 'frio' WHERE id = ?", (eid,))
    return len(calientes[:U["max_eventos"]])


def aprender(c, ahora=None):
    """4 h despues de cada evento: cuanto se movio cada activo (en ATR de 1 h) -> S <- 0.9 S + 0.1 min(1, m/2)."""
    ahora = ahora or time.time()
    n = 0
    for e in list(c.execute("SELECT id, causa, activos, salto_ts, primera FROM eventos WHERE aprendido = 0 AND COALESCE(salto_ts, primera) <= ?", (int(ahora - 4 * 3600),))):
        t0 = e["salto_ts"] or e["primera"]
        for a in filter(None, (e["activos"] or "").split(",")):
            p0, p4 = _valor_en(c, "precio", a, t0, 900), _valor_en(c, "precio", a, t0 + 4 * 3600, 900)
            serie = _serie(c, "precio", a, t0 - 24 * 3600)
            horas = [v for ts, v in serie if ts <= t0][::60]
            atr = sum(abs(b - x) for x, b in zip(horas[:-1], horas[1:])) / max(1, len(horas) - 1) if len(horas) >= 6 else None
            if not (p0 and p4 and atr and atr > 0):
                continue
            m = abs(p4 - p0) / atr
            r = c.execute("SELECT S, n FROM sensibilidad WHERE causa = ? AND activo = ?", (e["causa"], a)).fetchone()
            S0 = r["S"] if r else 0.6
            c.execute("INSERT INTO sensibilidad (causa, activo, S, n) VALUES (?, ?, ?, 1) ON CONFLICT(causa, activo) DO UPDATE SET S = excluded.S, n = sensibilidad.n + 1",
                      (e["causa"], a, round(0.9 * S0 + 0.1 * min(1.0, m / 2.0), 4)))
            n += 1
        c.execute("UPDATE eventos SET aprendido = 1 WHERE id = ?", (e["id"],))
    return n


def podar(c, ahora=None):
    ahora = ahora or time.time()
    c.execute("DELETE FROM lecturas WHERE ts < ?", (int(ahora - 35 * 86400),))
    c.execute("DELETE FROM temperaturas WHERE ts < ?", (int(ahora - 180 * 86400),))
    c.execute("DELETE FROM eventos WHERE estado = 'cerrado' AND ultima < ?", (int(ahora - 180 * 86400),))


# ---------------------------------------------------------------- el ciclo

def leer_correlados(c, get=requests.get, ahora=None):
    """DXY (calculado con las cotizaciones de Kraken, de uso permitido) y, solo si se enciende, el Brent; velas de 15 min a la base."""
    ahora = ahora or time.time()
    import dxy
    n = 0
    def get_con_tope(*a, **k):
        if not _permiso("correlados"):
            raise RuntimeError("tope de llamadas por minuto")
        return get(*a, **k)
    velas, _ = dxy.desde_kraken("15", get_con_tope)
    ult = c.execute("SELECT MAX(ts) t FROM lecturas WHERE fuente = 'corr' AND clave = 'DXY'").fetchone()["t"] or 0
    for v in velas[-400:]:
        t = int(v["t"] // 1000) + 900          # el cierre de la vela es a su hora de apertura + 15 min
        if t > ult and t <= ahora and _num(v.get("c")):   # la vela que sigue abierta no se guarda (su cierre aun no existe)
            _guardar_lectura(c, "corr", "DXY", t, v["c"])
            n += 1
    if BRENT_ACTIVO:
        d = _pedir("correlados", get, YAHOO_BRENT, params={"interval": "15m", "range": "5d"}, headers={"User-Agent": "Mozilla/5.0 ordersblock-sensor"})
        res = ((d.get("chart") or {}).get("result") or [None])[0] or {} if isinstance(d, dict) else {}
        q = ((res.get("indicators") or {}).get("quote") or [{}])[0]
        ult = c.execute("SELECT MAX(ts) t FROM lecturas WHERE fuente = 'corr' AND clave = 'BRENT'").fetchone()["t"] or 0
        for ts, cc in zip(res.get("timestamp") or [], q.get("close") or []):
            if _num(ts) and _num(cc) and int(ts) + 900 > ult and int(ts) + 900 <= ahora:
                _guardar_lectura(c, "corr", "BRENT", int(ts) + 900, cc)
                n += 1
    return n


def ciclo(get=requests.get, post=requests.post, ahora=None, ruta=None, dormir=time.sleep):
    """Una vuelta (cada minuto): las fuentes que tocan, union, temperaturas, aprendizaje. Nunca lanza: lo que falle queda en salud
    (o en el resultado, si ni siquiera la base responde). Cada escritura se confirma al momento: nadie espera a la red."""
    ahora = ahora or time.time()
    hecho = {}
    if not _lock_ciclo.acquire(blocking=False):
        return {"saltada": "otra vuelta en curso"}
    try:
        inicializar(ruta)
        with conectar(ruta) as c:
            senales = []

            def correr(fuente, fn, llamadas=0):
                try:
                    cada = FUENTES[fuente][0]
                    if ahora - _ultima.get(fuente, 0) < cada - 1 or _pausada(c, fuente, ahora):
                        return
                    _ultima[fuente] = ahora
                    try:
                        _buffer.filas = []
                        try:
                            r = fn()
                        finally:
                            _volcar(c)
                        parcial = None
                        if isinstance(r, list):
                            parcial = "; ".join(x["_parcial"] for x in r if isinstance(x, dict) and "_parcial" in x) or None
                            senales.extend(x for x in r if isinstance(x, dict) and "_parcial" not in x)
                        _anotar(c, fuente, True, ahora=ahora, llamadas=llamadas() if callable(llamadas) else llamadas, parcial=parcial)
                        hecho[fuente] = "ok" if not parcial else "parcial"
                    except Exception as e:  # noqa: BLE001
                        _anotar(c, fuente, False, f"{type(e).__name__}: {e}", ahora=ahora, llamadas=llamadas() if callable(llamadas) else llamadas)
                        hecho[fuente] = f"error: {e}"
                except Exception as e:  # noqa: BLE001  (la base misma fallo: se anota en el resultado y sigue la siguiente fuente)
                    hecho[fuente] = f"error de base: {type(e).__name__}: {e}"
                    if "no such table" in str(e):
                        _listas.discard(ruta or RUTA_DB)      # la proxima vuelta vuelve a crear las tablas

            con_ratio = int(ahora // 60) % 5 == 0
            correr("precios", lambda: leer_precios(c, ahora))
            correr("polymarket", lambda: leer_polymarket(c, get, ahora), 1)
            correr("kalshi", lambda: leer_kalshi(c, get, ahora), len(KALSHI_SERIES[:10]))
            correr("noticias", lambda: leer_noticias(c, ahora))
            correr("bybit", lambda: leer_bybit(c, get, ahora, con_ratio=con_ratio), 4 if con_ratio else 2)
            correr("correlados", lambda: leer_correlados(c, get, ahora), 5 + (1 if BRENT_ACTIVO else 0))
            try:
                temas_abiertos = {r["causa"] for r in c.execute("SELECT causa FROM eventos WHERE estado IN ('tibio', 'caliente')")}
            except Exception:  # noqa: BLE001
                temas_abiertos = set()
            if os.getenv("BLUESKY_HANDLE") and os.getenv("BLUESKY_APP_PASSWORD"):
                correr("bluesky", lambda: leer_bluesky(c, {t: GDELT_CONSULTA[t].strip("()").replace('"', "").split(" OR ")[0] for t in (list(temas_abiertos) or list(MAPA))[:10]}, get, post, ahora), 10)
            # GDELT: cada 15 min se encolan los 12 temas; cada minuto salen como mucho 2 (1 llamada cada 5 s)
            if ahora - _ultima.get("gdelt_cola", 0) >= FUENTES["gdelt"][0] - 1:
                _ultima["gdelt_cola"] = ahora
                _cola_gdelt[:] = list(MAPA)
            for _ in range(2):
                try:
                    if not _cola_gdelt or _pausada(c, "gdelt", ahora):
                        break
                    tema = _cola_gdelt.pop(0)
                    try:
                        senales.append(leer_gdelt_tema(c, tema, get, ahora))
                        if tema in temas_abiertos:
                            dormir(5)
                            idiomas, ventaja = leer_gdelt_idiomas(c, tema, get)
                            for e in list(c.execute("SELECT id, extra FROM eventos WHERE causa = ? AND estado != 'cerrado'", (tema,))):
                                x = json.loads(e["extra"] or "{}")
                                x.update(idiomas=idiomas, ventaja_min=ventaja)
                                c.execute("UPDATE eventos SET extra = ? WHERE id = ?", (json.dumps(limpio(x), ensure_ascii=False)[:4000], e["id"]))
                        _anotar(c, "gdelt", True, ahora=ahora, llamadas=1)
                        hecho["gdelt"] = "ok"
                    except Exception as e:  # noqa: BLE001
                        _anotar(c, "gdelt", False, f"{type(e).__name__}: {e}", ahora=ahora, llamadas=1)
                        hecho["gdelt"] = f"error: {e}"
                    dormir(5)
                except Exception as e:  # noqa: BLE001
                    hecho["gdelt"] = f"error de base: {type(e).__name__}: {e}"
                    break
            if time.gmtime(ahora).tm_hour == 3:       # las vistas de ayer ya estan publicadas a esta hora
                correr("wikipedia", lambda: leer_wikipedia(c, get, ahora), sum(len(v) for v in WIKI.values()))
            try:
                integrar(c, senales, ahora)
                hecho["calientes"] = temperaturas(c, ahora)
                hecho["aprendidos"] = aprender(c, ahora)
                if ahora - _ultima.get("podar", 0) >= 86400:
                    _ultima["podar"] = ahora
                    podar(c, ahora)
                    hecho["podado"] = True
            except Exception as e:  # noqa: BLE001
                logger.error(f"sensor: integracion {type(e).__name__}: {e}")
                hecho["integracion"] = f"error: {e}"
                if "no such table" in str(e):
                    _listas.discard(ruta or RUTA_DB)
    except Exception as e:  # noqa: BLE001
        logger.error(f"sensor: vuelta sin base ({type(e).__name__}: {e})")
        _listas.discard(ruta or RUTA_DB)              # la proxima vuelta vuelve a crear las tablas si hace falta
        hecho["base"] = f"error: {type(e).__name__}: {e}"
    finally:
        _lock_ciclo.release()
    return hecho


# ---------------------------------------------------------------- lo que lee el Worker

def _cambio_desde(c, clave, desde_s):
    base = c.execute("SELECT valor FROM lecturas WHERE fuente = 'corr' AND clave = ? AND ts <= ? ORDER BY ts DESC LIMIT 1", (clave, int(desde_s))).fetchone()
    ult = c.execute("SELECT valor, ts FROM lecturas WHERE fuente = 'corr' AND clave = ? ORDER BY ts DESC LIMIT 1", (clave,)).fetchone()
    if not base or not ult or not base["valor"] or ult["ts"] <= desde_s - 900:
        return None
    return round((ult["valor"] - base["valor"]) / base["valor"] * 100.0, 3)


_ALIAS = {"BTC": "BTCUSD", "BTCUSDT": "BTCUSD", "ETH": "ETHUSD", "ETHUSDT": "ETHUSD", "XAU": "XAUUSD", "GOLD": "XAUUSD", "EUR": "EURUSD", "GBP": "GBPUSD", "JPY": "USDJPY"}


def normalizar_activo(a: str):
    a = re.sub(r"[^A-Z]", "", str(a or "").upper())
    a = _ALIAS.get(a, a)
    return a if a in ACTIVOS else None


def consultar(activo_: str, desde_s: int = None, ruta: str = None, ahora=None) -> dict:
    """Respuesta de GET /sensor (sin red y sin esperar al hilo): eventos tibios y calientes del activo, correlacionados desde t0
    (de lo que el hilo ya guardo), derivados (cripto) y salud."""
    ahora = ahora or time.time()
    base = {"salud": salud(ruta)}
    if not activo():
        return limpio(dict(base, activo=False))
    a = normalizar_activo(activo_)
    if not a:
        return limpio(dict(base, activo=True, hilo=bool(_hilo and _hilo.is_alive()), mercado=None, eventos=[], correlacionados={}, derivados=None, error="activo no seguido"))
    desde_s = int(desde_s or ahora - 3600)
    inicializar(ruta)
    base = {"salud": salud(ruta)}
    eventos, corr, der = [], {}, None
    with conectar(ruta) as c:
        for e in c.execute("SELECT id, titulo, causa, polaridad, primera, salto_ts, fuentes, extra FROM eventos WHERE estado IN ('tibio', 'caliente') AND (',' || activos || ',') LIKE ?",
                           ("%," + a + ",%",)):
            t = c.execute("SELECT A, S, T FROM temperaturas WHERE evento = ? AND activo = ? ORDER BY ts DESC LIMIT 1", (e["id"], a)).fetchone()
            if not t or (t["T"] or 0) < U["tibio"] or e["causa"] not in MAPA:
                continue
            x = json.loads(e["extra"] or "{}")
            pol = int(e["polaridad"] or 0)
            cmap = MAPA[e["causa"]].get("corr") or {}
            corr_e = {k: int(v * pol) for k, v in cmap.items() if k != "BRENT" or BRENT_ACTIVO} if pol else {}
            prob = x.get("prob") or {}
            eventos.append({"id": e["id"], "titulo": e["titulo"], "causa": e["causa"], "T": t["T"], "A": t["A"], "S": t["S"],
                            "dir_esperada": direccion(e["causa"], a, pol), "corr_esperada": corr_e,
                            "cambio_prob_1h": prob.get("d1h"), "cambio_prob_24h": prob.get("d24h"),
                            "salto_prensa": ((x.get("aportes") or {}).get("gdelt") or {}).get("z"),
                            "idiomas": x.get("idiomas") or [], "ventaja_min": x.get("ventaja_min"),
                            "inicio_ts": int(e["primera"]) * 1000, "salto_ts": int(e["salto_ts"]) * 1000 if e["salto_ts"] else None,
                            "fuentes": [f for f in (e["fuentes"] or "").split(",") if f]})
        eventos.sort(key=lambda z: z["T"] or 0, reverse=True)
        for k in (["DXY", "BRENT"] if BRENT_ACTIVO else ["DXY"]):
            cp = _cambio_desde(c, k, desde_s)
            if cp is not None:
                corr[k] = {"cambio_pct": cp}
        if a in ("BTCUSD", "ETHUSD"):
            oi0 = _valor_en(c, "bybit_oi", a, desde_s - 1800, 900)
            oi1 = _valor_en(c, "bybit_oi", a, min(desde_s + 1800, ahora), 900)   # la hora alrededor de t0 (o hasta ahora si t0 es reciente)
            fr = c.execute("SELECT valor FROM lecturas WHERE fuente = 'bybit_funding' AND clave = ? ORDER BY ts DESC LIMIT 1", (a,)).fetchone()
            rt = c.execute("SELECT valor FROM lecturas WHERE fuente = 'bybit_ratio' AND clave = ? ORDER BY ts DESC LIMIT 1", (a,)).fetchone()
            funding = fr["valor"] if fr else None
            der = {"oi_cambio_pct": round((oi1 - oi0) / oi0 * 100.0, 3) if oi0 and oi1 else None, "funding": funding,
                   "funding_extremo": bool(funding is not None and abs(funding) >= U["funding_extremo"]), "proporcion_largos": rt["valor"] if rt else None}
    return limpio(dict(base, activo=True, hilo=bool(_hilo and _hilo.is_alive()), mercado=a, desde=desde_s, eventos=eventos[:25], correlacionados=corr, derivados=der))


# ---------------------------------------------------------------- hilo

def _bucle(parar):
    parar.wait(60)
    while not parar.is_set():
        try:
            ciclo()
        except Exception as e:  # noqa: BLE001
            logger.error(f"sensor: {type(e).__name__}: {e}")
        parar.wait(60)


def arrancar():
    """Nunca lanza. Si la base no abre al arrancar, el hilo arranca igual y la crea en su primera vuelta."""
    global _hilo, _parar
    try:
        if (_hilo and _hilo.is_alive()) or not activo():
            return
        try:
            inicializar()
        except Exception as e:  # noqa: BLE001  (un bloqueo pasajero: el hilo lo reintenta en cada vuelta)
            logger.error(f"sensor: la base no abrio al arrancar ({type(e).__name__}: {e}); el hilo lo reintenta")
        _parar = threading.Event()               # uno por hilo: detener y volver a arrancar nunca revive un bucle viejo
        _hilo = threading.Thread(target=_bucle, args=(_parar,), name="sensor", daemon=True)
        _hilo.start()
        logger.info(f"sensor activo: {len(MAPA)} temas, base {RUTA_DB}")
    except Exception as e:  # noqa: BLE001
        _hilo = None
        logger.error(f"sensor: no arranco ({type(e).__name__}: {e}); la API sigue sin el")


def detener():
    global _hilo
    _parar.set()
    h, _hilo = _hilo, None
    if h and h.is_alive():
        h.join(timeout=10)
