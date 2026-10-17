"""
CBS Quantum Predictor - Motor de Análisis Fundamental SIN IA
=============================================================
Filosofía del proyecto aplicada aquí: nada decorativo, todo real,
todo verificable, costo de operación CERO (ninguna fuente requiere
API key ni pago).

Cómo funciona (100% determinista, sin ninguna llamada a IA):

  1. FUENTES GRATUITAS DE NOTICIAS (RSS, parseado con la librería
     estándar de Python — sin dependencias nuevas):
       - CoinDesk, Cointelegraph (cripto)
       - FXStreet (forex / oro / macro)
       - Yahoo Finance RSS por ticker (oro, EURUSD, BTC...)
  2. INDICADORES FUNDAMENTALES GRATUITOS:
       - Fear & Greed Index de alternative.me (cripto, JSON público)
       - Funding rate de Bybit (endpoint público v5, sin auth) para
         BTC/ETH/PAXG — funding muy positivo = largos pagando caro
         (mercado sobrecomprado), muy negativo = cortos pagando caro.
  3. SENTIMIENTO LÉXICO (sin IA): cada titular se puntúa con un
     diccionario financiero ES/EN curado (inspirado en las listas
     Loughran-McDonald usadas en investigación financiera), con
     manejo de negaciones ("no sube" invierte el signo) e
     intensificadores ("se dispara" pesa más que "sube").
     Es matemática transparente: cualquiera puede auditar por qué
     un titular dio el score que dio.
  4. RELEVANCIA POR MERCADO: un titular solo cuenta para XAUUSD si
     habla de oro/Fed/inflación/DXY; solo cuenta para BTC si habla
     de bitcoin/ETF/SEC/halving; etc. Nada de ruido genérico.
  5. AGREGACIÓN: promedio ponderado por recencia (pesos lineales:
     el titular más nuevo pesa más que el más viejo — sin EMA).
  6. MEMORIA REAL: este módulo NO toca la base de datos directamente
     (misma separación limpia que real_engine.py). Devuelve los
     titulares puntuados y quien llama (bot.py) los persiste vía
     database.py. La efectividad histórica verificada del fundamental
     (¿cuántas veces el sentimiento coincidió con lo que el precio
     hizo de verdad?) llega como parámetro y modula la magnitud del
     score — misma filosofía conservadora del sesgo direccional
     aprendido: sin muestra suficiente, no se amplifica nada.

FAIL-SOFT ABSOLUTO: cualquier fuente caída, lenta o bloqueada
devuelve neutral (score 0, "sin datos"). El fundamental JAMÁS
bloquea, retrasa en exceso ni degrada la señal técnica.
"""

import re
import math
import time
import hashlib
import logging
import unicodedata
import xml.etree.ElementTree as ET
from typing import Optional

import requests

logger = logging.getLogger("fundamental")

# ---------------------------------------------------------------------------
# CONFIGURACIÓN DE FUENTES (todas gratuitas, sin API key)
# ---------------------------------------------------------------------------

TIMEOUT_FUENTE_SEGUNDOS = 6      # por fuente — corto a propósito (fail-soft)
PRESUPUESTO_TOTAL_SEGUNDOS = 14  # tope duro para TODO el fetch fundamental
_CACHE_TTL_SEGUNDOS = 900        # 15 min: las noticias no cambian por segundo
MAX_TITULARES_POR_FUENTE = 15
HORAS_VENTANA_NOTICIAS = 48      # solo cuentan titulares de las últimas 48h

_HEADERS = {
    # User-Agent de navegador: varios servidores RSS rechazan el UA
    # por defecto de python-requests con 403.
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0 Safari/537.36"),
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}

# Feeds RSS: (nombre_fuente, url, categorias que cubre)
# categorias posibles: "cripto", "forex", "oro", "macro"
# ---------------------------------------------------------------------------
# FUENTES GEOPOLÍTICAS E INSTITUCIONALES (todas gratuitas, sin API key)
# ---------------------------------------------------------------------------
# El análisis técnico no ve venir un anuncio de la Fed, un arancel nuevo o
# una escalada militar. Estas fuentes son las que dicen si el contexto que
# sostenía la señal SIGUE EN PIE:
#   · Bancos centrales: Fed (comunicados y discursos), BCE, Banco de
#     Inglaterra. Son la fuente primaria, no la interpretación de un medio.
#   · Casa Blanca: declaraciones y órdenes ejecutivas del presidente de
#     EE.UU. — mueven oro, dólar y cripto en minutos.
#   · Geopolítica: Reuters y agencias para Rusia, China, UE, Oriente Medio.
#   · Redes sociales: sentimiento de la comunidad cripto vía Reddit
#     (r/cryptocurrency, r/wallstreetbets) — su API pública .json no
#     requiere clave y refleja el ánimo minorista antes que los medios.
FEEDS_RSS = [
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/", {"cripto"}),
    ("Cointelegraph", "https://cointelegraph.com/rss", {"cripto"}),
    ("FXStreet", "https://www.fxstreet.com/rss/news", {"forex", "oro", "macro"}),
    ("Yahoo Finance Oro", "https://feeds.finance.yahoo.com/rss/2.0/headline?s=GC=F&region=US&lang=en-US", {"oro", "macro"}),
    ("Yahoo Finance BTC", "https://feeds.finance.yahoo.com/rss/2.0/headline?s=BTC-USD&region=US&lang=en-US", {"cripto"}),
    ("Yahoo Finance EURUSD", "https://feeds.finance.yahoo.com/rss/2.0/headline?s=EURUSD=X&region=US&lang=en-US", {"forex", "macro"}),
    # --- Bancos centrales (fuente primaria) ---
    ("Reserva Federal", "https://www.federalreserve.gov/feeds/press_all.xml", {"macro", "forex", "oro", "cripto"}),
    ("Fed – discursos", "https://www.federalreserve.gov/feeds/speeches.xml", {"macro", "forex", "oro"}),
    ("BCE", "https://www.ecb.europa.eu/rss/press.html", {"macro", "forex"}),
    ("Banco de Inglaterra", "https://www.bankofengland.co.uk/boeapps/rss/feeds.aspx?feed=News", {"macro", "forex"}),
    # --- Casa Blanca / presidencia de EE.UU. ---
    ("Casa Blanca", "https://www.whitehouse.gov/feed/", {"macro", "forex", "oro", "cripto"}),
    # --- Geopolítica: Rusia, China, UE, conflictos ---
    ("Reuters Mundo", "https://news.google.com/rss/search?q=when:24h+(Russia+OR+China+OR+%22European+Union%22)+(sanctions+OR+tariffs+OR+conflict+OR+trade)&hl=en-US&gl=US&ceid=US:en", {"macro", "forex", "oro"}),
    ("Geopolítica – oro", "https://news.google.com/rss/search?q=when:24h+gold+(war+OR+sanctions+OR+central+bank+OR+inflation)&hl=en-US&gl=US&ceid=US:en", {"oro", "macro"}),
    ("Regulación cripto", "https://news.google.com/rss/search?q=when:24h+crypto+(SEC+OR+regulation+OR+ban+OR+ETF)&hl=en-US&gl=US&ceid=US:en", {"cripto"}),
]

# Redes sociales: API pública JSON de Reddit (sin clave, sin coste).
# Se leen los títulos más votados de las últimas horas: es el ánimo
# minorista en crudo, que suele moverse ANTES que los titulares de prensa.
FEEDS_REDDIT = [
    ("Reddit r/CryptoCurrency", "https://www.reddit.com/r/CryptoCurrency/hot.json?limit=25", {"cripto"}),
    ("Reddit r/Bitcoin", "https://www.reddit.com/r/Bitcoin/hot.json?limit=25", {"cripto"}),
    ("Reddit r/wallstreetbets", "https://www.reddit.com/r/wallstreetbets/hot.json?limit=25", {"macro", "cripto"}),
    ("Reddit r/Forex", "https://www.reddit.com/r/Forex/hot.json?limit=25", {"forex", "oro", "macro"}),
]

URL_FEAR_GREED = "https://api.alternative.me/fng/?limit=1"
URL_BYBIT_TICKER = "https://api.bybit.com/v5/market/tickers"

# Qué categorías y símbolos aplican a cada mercado del bot
MERCADO_A_CATEGORIAS = {
    "XAUUSD": {"oro", "macro"},
    "EURUSD": {"forex", "macro"},
    "GBPUSD": {"forex", "macro"},
    "USDCOP": {"forex", "macro"},
    "BTC": {"cripto"},
    "BTCUSD": {"cripto"},
    "ETH": {"cripto"},
    "ETHUSD": {"cripto"},
}

# Símbolo de futuros lineales en Bybit para funding rate (solo los que existen)
MERCADO_A_SYMBOL_FUNDING = {
    "BTC": "BTCUSDT", "BTCUSD": "BTCUSDT",
    "ETH": "ETHUSDT", "ETHUSD": "ETHUSDT",
    "XAUUSD": "PAXGUSDT",
}

# Mercados donde aplica el Fear & Greed de alternative.me (es un índice cripto)
MERCADOS_FEAR_GREED = {"BTC", "BTCUSD", "ETH", "ETHUSD"}

# ---------------------------------------------------------------------------
# RELEVANCIA: keywords por mercado (un titular sin keyword relevante NO cuenta)
# ---------------------------------------------------------------------------

KEYWORDS_MERCADO = {
    "XAUUSD": [
        "gold", "xau", "bullion", "oro", "fed", "fomc", "powell", "inflation",
        "inflacion", "cpi", "treasury", "yields", "dxy", "dollar index",
        "rate cut", "rate hike", "tasas", "reserva federal", "safe haven",
        "precious metal", "metales preciosos", "nonfarm", "payrolls",
    ],
    "EURUSD": [
        "euro", "eur/usd", "eurusd", "ecb", "bce", "lagarde", "eurozone",
        "eurozona", "fed", "fomc", "powell", "dollar", "dolar", "dxy",
        "inflation", "inflacion", "cpi", "rate cut", "rate hike", "tasas",
        "nonfarm", "payrolls", "pmi", "gdp", "pib",
    ],
    "GBPUSD": [
        "pound", "sterling", "gbp", "cable", "boe", "bank of england",
        "libra", "uk economy", "fed", "dollar", "dxy", "inflation", "cpi",
        "rate cut", "rate hike",
    ],
    "USDCOP": [
        "peso colombiano", "colombia", "banrep", "banco de la republica",
        "emerging market", "mercados emergentes", "oil", "petroleo", "crude",
        "brent", "wti", "dollar", "dolar", "fed", "latam",
    ],
    "BTC": [
        "bitcoin", "btc", "crypto", "cripto", "etf", "sec", "halving",
        "blackrock", "grayscale", "microstrategy", "mining", "mineria",
        "stablecoin", "binance", "coinbase", "whale", "ballena",
        "on-chain", "satoshi", "digital asset", "activos digitales",
    ],
    "ETH": [
        "ethereum", "eth", "ether", "vitalik", "staking", "defi",
        "layer 2", "l2", "rollup", "crypto", "cripto", "etf", "sec",
        "smart contract", "gas fees",
    ],
}
KEYWORDS_MERCADO["BTCUSD"] = KEYWORDS_MERCADO["BTC"]
KEYWORDS_MERCADO["ETHUSD"] = KEYWORDS_MERCADO["ETH"]

# ---------------------------------------------------------------------------
# LÉXICO FINANCIERO DETERMINISTA (ES/EN) — el "análisis sin IA"
# ---------------------------------------------------------------------------
# Peso positivo = titular alcista para el activo del que habla.
# Peso 2.0 = término fuerte ("se dispara"), 1.0 = término normal ("sube").
# Todo en minúsculas y sin tildes (se normaliza antes de comparar).

LEXICO_POSITIVO = {
    # EN
    "surge": 2.0, "surges": 2.0, "soar": 2.0, "soars": 2.0, "skyrocket": 2.0,
    "rally": 1.5, "rallies": 1.5, "jump": 1.5, "jumps": 1.5, "spike": 1.5,
    "rebound": 1.5, "rebounds": 1.5, "breakout": 1.5, "boom": 1.5,
    "rise": 1.0, "rises": 1.0, "gain": 1.0, "gains": 1.0, "climb": 1.0,
    "climbs": 1.0, "advance": 1.0, "advances": 1.0, "up": 0.5, "higher": 1.0,
    "bullish": 1.5, "bull": 1.0, "record high": 2.0, "all-time high": 2.0,
    "outperform": 1.0, "beat": 1.0, "beats": 1.0, "strong": 1.0,
    "strengthens": 1.0, "recovery": 1.0, "recovers": 1.0, "optimism": 1.0,
    "upgrade": 1.0, "approval": 1.5, "approved": 1.5, "adoption": 1.0,
    "inflow": 1.0, "inflows": 1.0, "buying": 0.5, "accumulation": 1.0,
    "support": 0.5, "dovish": 1.0, "rate cut": 1.0, "stimulus": 1.0,
    # ES
    "se dispara": 2.0, "disparo": 2.0, "sube": 1.0, "suben": 1.0,
    "avanza": 1.0, "avanzan": 1.0, "repunta": 1.5, "repunte": 1.5,
    "rebota": 1.5, "rebote": 1.5, "gana": 1.0, "ganancias": 1.0,
    "alcista": 1.5, "maximo historico": 2.0, "maximos": 1.0,
    "fortalece": 1.0, "recuperacion": 1.0, "optimismo": 1.0,
    "aprobacion": 1.5, "aprobado": 1.5, "adopcion": 1.0, "compras": 0.5,
    "recorte de tasas": 1.0, "estimulo": 1.0, "acumulacion": 1.0,
}

LEXICO_NEGATIVO = {
    # EN
    "crash": 2.0, "crashes": 2.0, "plunge": 2.0, "plunges": 2.0,
    "collapse": 2.0, "collapses": 2.0, "tumble": 1.5, "tumbles": 1.5,
    "plummet": 2.0, "plummets": 2.0, "slump": 1.5, "slumps": 1.5,
    "sink": 1.5, "sinks": 1.5, "drop": 1.0, "drops": 1.0, "fall": 1.0,
    "falls": 1.0, "decline": 1.0, "declines": 1.0, "slide": 1.0,
    "slides": 1.0, "down": 0.5, "lower": 1.0, "bearish": 1.5, "bear": 1.0,
    "selloff": 1.5, "sell-off": 1.5, "dump": 1.5, "liquidation": 1.5,
    "liquidations": 1.5, "fear": 1.0, "panic": 1.5, "warning": 1.0,
    "warns": 1.0, "risk": 0.5, "recession": 1.5, "crisis": 1.5,
    "hack": 1.5, "hacked": 1.5, "exploit": 1.5, "fraud": 1.5,
    "lawsuit": 1.0, "ban": 1.5, "banned": 1.5, "crackdown": 1.5,
    "outflow": 1.0, "outflows": 1.0, "weak": 1.0, "weakens": 1.0,
    "miss": 1.0, "misses": 1.0, "downgrade": 1.0, "hawkish": 1.0,
    "rate hike": 1.0, "default": 1.5, "rejection": 1.0, "rejected": 1.0,
    "record low": 2.0, "losses": 1.0, "loss": 1.0,
    # ES
    "se desploma": 2.0, "desplome": 2.0, "colapsa": 2.0, "colapso": 2.0,
    "cae": 1.0, "caen": 1.0, "caida": 1.0, "hunde": 1.5, "hundimiento": 1.5,
    "retrocede": 1.0, "retroceso": 1.0, "bajista": 1.5, "perdidas": 1.0,
    "pierde": 1.0, "panico": 1.5, "miedo": 1.0, "advertencia": 1.0,
    "advierte": 1.0, "riesgo": 0.5, "recesion": 1.5, "crisis": 1.5,
    "hackeo": 1.5, "fraude": 1.5, "demanda judicial": 1.0, "prohibicion": 1.5,
    "prohibe": 1.5, "debilita": 1.0, "debil": 1.0, "minimos": 1.0,
    "subida de tasas": 1.0, "alza de tasas": 1.0, "rechazo": 1.0,
    "liquidaciones": 1.5, "ventas masivas": 1.5,
}

# --- Léxico GEOPOLÍTICO E INSTITUCIONAL ---
# Términos que mueven mercados por decisión política, no por precio.
# Se suman a los léxicos generales; el signo es el efecto sobre el ACTIVO
# del que habla el titular (para el oro, "war" es alcista; para el euro,
# "sanctions" suele ser bajista — la relevancia por mercado ya filtra a
# cuál se aplica cada titular).
LEXICO_GEOPOLITICO_NEGATIVO = {
    "war": 1.5, "guerra": 1.5, "invasion": 1.5, "invasión": 1.5,
    "sanctions": 1.5, "sanciones": 1.5, "tariff": 1.5, "tariffs": 1.5,
    "arancel": 1.5, "aranceles": 1.5, "trade war": 2.0, "guerra comercial": 2.0,
    "escalation": 1.5, "escalada": 1.5, "airstrike": 2.0, "missile": 1.5,
    "embargo": 1.5, "blockade": 1.5, "bloqueo": 1.5, "conflict": 1.0,
    "conflicto": 1.0, "shutdown": 1.5, "cierre de gobierno": 1.5,
    "default": 2.0, "impago": 2.0, "downgrade": 1.5, "hawkish": 1.0,
    "rate hike": 1.0, "subida de tipos": 1.0, "tightening": 1.0,
    "restrictiva": 1.0, "quantitative tightening": 1.5,
    "geopolitical tension": 1.5, "tension geopolitica": 1.5,
}
LEXICO_GEOPOLITICO_POSITIVO = {
    "ceasefire": 1.5, "alto el fuego": 1.5, "peace deal": 1.5,
    "acuerdo de paz": 1.5, "trade deal": 1.5, "acuerdo comercial": 1.5,
    "sanctions lifted": 1.5, "levanta sanciones": 1.5,
    "stimulus": 1.5, "estimulo": 1.5, "rate cut": 1.0, "recorte de tipos": 1.0,
    "dovish": 1.0, "easing": 1.0, "expansiva": 1.0, "acuerdo": 0.5,
    "quantitative easing": 1.5, "de-escalation": 1.5, "desescalada": 1.5,
    "resolution": 0.5, "cooperation": 0.5, "cooperacion": 0.5,
}

# Términos que marcan un titular como ALTO IMPACTO: cuando aparecen, la
# noticia pesa más y se marca para que el usuario la vea explícitamente.
TERMINOS_ALTO_IMPACTO = {
    "fomc", "fed", "reserva federal", "powell", "ecb", "bce", "lagarde",
    "interest rate decision", "decision de tipos", "cpi", "inflation report",
    "nonfarm payrolls", "war", "guerra", "invasion", "sanctions", "sanciones",
    "tariff", "arancel", "trade war", "guerra comercial", "ceasefire",
    "alto el fuego", "default", "impago", "emergency", "emergencia",
    "executive order", "orden ejecutiva", "shutdown", "sec", "etf approval",
    # Escalada militar y crisis: mueven oro y refugio en minutos
    "escalates", "escalada", "missile", "misil", "airstrike", "strikes",
    "attack", "ataque", "nuclear", "embargo", "blockade", "bloqueo",
    "state of emergency", "coup", "golpe de estado", "evacuation",
    "opec", "output cut", "recorte de produccion", "downgrade",
}

NEGADORES = {"no", "not", "n't", "sin", "without", "never", "nunca",
             "ni", "neither", "nor", "tampoco"}

# ---------------------------------------------------------------------------
# NORMALIZACIÓN Y SENTIMIENTO POR TITULAR
# ---------------------------------------------------------------------------


def _normalizar(texto: str) -> str:
    """minúsculas + sin tildes + espacios simples — para comparar contra el léxico."""
    texto = unicodedata.normalize("NFD", texto or "")
    texto = "".join(c for c in texto if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", texto.lower()).strip()


def puntuar_titular(titular: str) -> float:
    """
    Score determinista en [-1, 1] para UN titular.
    Reglas (auditables a mano):
      - Se buscan primero las FRASES del léxico (ej. "rate cut",
        "se dispara") y luego palabras sueltas.
      - Si en las 2 palabras anteriores hay un negador, se invierte el
        signo de ese término ("gold does not rise" cuenta negativo).
      - El total se normaliza con tanh suave para acotarlo a [-1, 1]
        sin que un titular con 4 términos fuertes sature a lo bruto.
    """
    texto = _normalizar(titular)
    if not texto:
        return 0.0

    total = 0.0
    consumido = [False] * len(texto)

    def _aplicar(lexico: dict, signo: int):
        nonlocal total
        # frases primero (más largas primero para no pisarlas con subpalabras)
        for termino in sorted(lexico, key=len, reverse=True):
            peso = lexico[termino]
            for m in re.finditer(r"(?<![a-z])" + re.escape(termino) + r"(?![a-z])", texto):
                ini, fin = m.span()
                if any(consumido[ini:fin]):
                    continue  # ya lo contó una frase más larga
                for i in range(ini, fin):
                    consumido[i] = True
                # negación en las ~2 palabras previas
                prefijo = texto[:ini].split()[-2:]
                negado = any(p in NEGADORES for p in prefijo)
                total += (-signo if negado else signo) * peso

    _aplicar(LEXICO_POSITIVO, +1)
    _aplicar(LEXICO_NEGATIVO, -1)
    _aplicar(LEXICO_GEOPOLITICO_POSITIVO, +1)
    _aplicar(LEXICO_GEOPOLITICO_NEGATIVO, -1)

    if total == 0.0:
        return 0.0
    # tanh(total/2): 1 término normal ~ ±0.46, 2 fuertes ~ ±0.96, acotado.
    import math
    return round(math.tanh(total / 2.0), 3)


def es_alto_impacto(titular: str) -> bool:
    """
    ¿El titular es de los que mueven el mercado por sí solos (decisión de
    tipos, guerra, aranceles, orden ejecutiva)? Estos pesan el doble en
    la agregación y se muestran destacados: un usuario debe saber que
    opera contra un anuncio de la Fed, no enterarse después.
    """
    texto = _normalizar(titular)
    return any(t in texto for t in TERMINOS_ALTO_IMPACTO)


def es_relevante_para(titular: str, mercado: str) -> bool:
    texto = _normalizar(titular)
    for kw in KEYWORDS_MERCADO.get(mercado.upper(), []):
        if kw in texto:
            return True
    return False


def hash_titular(titular: str, fuente: str) -> str:
    """Hash estable para deduplicar titulares en la memoria (Postgres)."""
    return hashlib.sha256(f"{fuente}|{_normalizar(titular)}".encode()).hexdigest()[:32]


# ---------------------------------------------------------------------------
# FETCH DE FUENTES (con cache y presupuesto de tiempo)
# ---------------------------------------------------------------------------

_cache_feeds: dict = {}       # nombre_fuente -> (items, timestamp)
_cache_indicadores: dict = {} # clave -> (valor, timestamp)


def _parsear_fecha_rss(texto: str) -> Optional[float]:
    """Convierte pubDate RSS (RFC 822) a epoch. None si no se puede."""
    if not texto:
        return None
    from email.utils import parsedate_to_datetime
    try:
        return parsedate_to_datetime(texto.strip()).timestamp()
    except Exception:
        return None


def _fetch_feed(nombre: str, url: str) -> list:
    """Descarga y parsea un RSS con la librería estándar. [] si falla."""
    ahora = time.time()
    if nombre in _cache_feeds:
        items, ts = _cache_feeds[nombre]
        if ahora - ts < _CACHE_TTL_SEGUNDOS:
            return items
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=TIMEOUT_FUENTE_SEGUNDOS)
        resp.raise_for_status()
        raiz = ET.fromstring(resp.content)
        items = []
        # RSS 2.0: channel/item ; Atom: entry
        for item in raiz.iter():
            tag = item.tag.lower().split("}")[-1]
            if tag not in ("item", "entry"):
                continue
            titulo, fecha = None, None
            for hijo in item:
                htag = hijo.tag.lower().split("}")[-1]
                if htag == "title":
                    titulo = (hijo.text or "").strip()
                elif htag in ("pubdate", "published", "updated"):
                    fecha = _parsear_fecha_rss(hijo.text) or fecha
            if titulo:
                items.append({"titular": titulo, "epoch": fecha or ahora})
            if len(items) >= MAX_TITULARES_POR_FUENTE:
                break
        _cache_feeds[nombre] = (items, ahora)
        return items
    except Exception as e:
        logger.warning(f"Feed '{nombre}' no disponible ({e}) — se ignora (fail-soft)")
        _cache_feeds[nombre] = ([], ahora)  # cachear el fallo evita reintentos en loop
        return []


def _fetch_reddit(nombre: str, url: str) -> list:
    """
    Lee la API pública JSON de Reddit (sin clave). Filtra por votos para
    quedarse con lo que la comunidad realmente respaldó, no con cada
    mensaje suelto: un post con 500 votos es señal, uno con 3 es ruido.
    """
    ahora = time.time()
    if nombre in _cache_feeds:
        items, ts = _cache_feeds[nombre]
        if ahora - ts < _CACHE_TTL_SEGUNDOS:
            return items
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=TIMEOUT_FUENTE_SEGUNDOS)
        resp.raise_for_status()
        hijos = resp.json().get("data", {}).get("children", [])
        items = []
        for h in hijos:
            d = h.get("data", {})
            titulo = (d.get("title") or "").strip()
            votos = int(d.get("score", 0) or 0)
            creado = float(d.get("created_utc", ahora) or ahora)
            if not titulo or votos < 50:
                continue  # umbral de respaldo comunitario
            items.append({"titular": titulo, "epoch": creado, "votos": votos})
            if len(items) >= MAX_TITULARES_POR_FUENTE:
                break
        _cache_feeds[nombre] = (items, ahora)
        return items
    except Exception as e:
        logger.warning(f"Reddit '{nombre}' no disponible ({e}) — se ignora")
        _cache_feeds[nombre] = ([], ahora)
        return []


def obtener_fear_greed() -> Optional[dict]:
    """Fear & Greed Index cripto (alternative.me). None si falla."""
    ahora = time.time()
    if "fng" in _cache_indicadores:
        valor, ts = _cache_indicadores["fng"]
        if ahora - ts < _CACHE_TTL_SEGUNDOS:
            return valor
    try:
        resp = requests.get(URL_FEAR_GREED, headers=_HEADERS, timeout=TIMEOUT_FUENTE_SEGUNDOS)
        resp.raise_for_status()
        d = resp.json()["data"][0]
        valor = {"valor": int(d["value"]), "clasificacion": d.get("value_classification", "")}
        _cache_indicadores["fng"] = (valor, ahora)
        return valor
    except Exception as e:
        logger.warning(f"Fear&Greed no disponible ({e})")
        _cache_indicadores["fng"] = (None, ahora)
        return None


def obtener_funding_rate(mercado: str) -> Optional[float]:
    """Funding rate actual del futuro lineal en Bybit (público). None si falla."""
    symbol = MERCADO_A_SYMBOL_FUNDING.get(mercado.upper())
    if not symbol:
        return None
    clave = f"funding:{symbol}"
    ahora = time.time()
    if clave in _cache_indicadores:
        valor, ts = _cache_indicadores[clave]
        if ahora - ts < _CACHE_TTL_SEGUNDOS:
            return valor
    try:
        resp = requests.get(
            URL_BYBIT_TICKER,
            params={"category": "linear", "symbol": symbol},
            headers=_HEADERS, timeout=TIMEOUT_FUENTE_SEGUNDOS,
        )
        resp.raise_for_status()
        lista = resp.json()["result"]["list"]
        valor = float(lista[0]["fundingRate"]) if lista else None
        _cache_indicadores[clave] = (valor, ahora)
        return valor
    except Exception as e:
        logger.warning(f"Funding rate {symbol} no disponible ({e})")
        _cache_indicadores[clave] = (None, ahora)
        return None


# ---------------------------------------------------------------------------
# AGREGACIÓN: score fundamental por mercado
# ---------------------------------------------------------------------------

# Igual filosofía conservadora del sesgo direccional aprendido: la
# efectividad histórica verificada solo modula el score con muestra seria.
MINIMO_MUESTRAS_EFECTIVIDAD = 20
# Tope de aporte del funding y del F&G dentro del score (las noticias mandan)
PESO_NOTICIAS = 0.60
PESO_SOCIAL = 0.12        # redes: emoción minorista, peso deliberadamente bajo
PESO_FUNDING = 0.14
PESO_FEAR_GREED = 0.14


def _score_funding(funding: Optional[float]) -> float:
    """
    Funding rate como señal CONTRARIA (lectura estándar de mercado):
    funding muy positivo = largos apalancados pagando caro = mercado
    sobrecalentado = presión bajista potencial, y viceversa. Se escala
    contra ±0.05% por período (nivel ya considerado extremo) y se acota.
    """
    if funding is None:
        return 0.0
    return max(-1.0, min(1.0, -funding / 0.0005))


def _score_fear_greed(fng: Optional[dict]) -> float:
    """
    F&G como señal contraria clásica: miedo extremo (<=25) históricamente
    zona de acumulación (positivo), codicia extrema (>=75) zona de
    distribución (negativo). Zona media = neutral (0).
    """
    if not fng:
        return 0.0
    v = fng["valor"]
    if v <= 25:
        return (25 - v) / 25.0          # 0..1
    if v >= 75:
        return -((v - 75) / 25.0)       # 0..-1
    return 0.0


def analizar_fundamental(mercado: str, efectividad_historica: Optional[dict] = None) -> dict:
    """
    Punto de entrada. Devuelve:
      {
        "score": float [-1,1],        # señal 10 para el algoritmo colaborativo
        "score_bruto": float,         # antes de modular por efectividad
        "etiqueta": "ALCISTA/BAJISTA/NEUTRAL",
        "n_titulares": int,           # titulares relevantes usados
        "titulares": [ {titular, fuente, sentimiento, epoch, hash} ],  # para memoria
        "fear_greed": dict|None,
        "funding_rate": float|None,
        "detalle": str,               # explicación corta determinista
        "efectividad": dict|None,     # lo que llegó de la BD, eco para el reporte
      }
    efectividad_historica: dict opcional de database.efectividad_fundamental():
      {"total_verificadas": int, "tasa_acierto": float(0-100)}
    """
    mercado = mercado.upper()
    inicio = time.time()
    categorias = MERCADO_A_CATEGORIAS.get(mercado, {"macro"})

    # 1) Noticias de feeds cuyas categorías tocan este mercado
    titulares_relevantes = []
    ahora = time.time()
    limite_antiguedad = ahora - HORAS_VENTANA_NOTICIAS * 3600
    for nombre, url, cats in FEEDS_RSS:
        if time.time() - inicio > PRESUPUESTO_TOTAL_SEGUNDOS:
            logger.warning("Presupuesto de tiempo fundamental agotado — se corta el fetch")
            break
        if not (cats & categorias):
            continue
        for item in _fetch_feed(nombre, url):
            if item["epoch"] < limite_antiguedad:
                continue
            if not es_relevante_para(item["titular"], mercado):
                continue
            s = puntuar_titular(item["titular"])
            titulares_relevantes.append({
                "titular": item["titular"][:300],
                "fuente": nombre,
                "sentimiento": s,
                "epoch": item["epoch"],
                "alto_impacto": es_alto_impacto(item["titular"]),
                "hash": hash_titular(item["titular"], nombre),
            })

    # --- Redes sociales (Reddit): ánimo minorista en crudo ---
    # Se pondera aparte y con menos peso que la prensa: refleja emoción,
    # no información verificada. Sirve para detectar euforia o pánico
    # antes de que lleguen a los titulares, no para decidir por sí solo.
    social_scores = []
    for nombre, url, cats in FEEDS_REDDIT:
        if time.time() - inicio > PRESUPUESTO_TOTAL_SEGUNDOS:
            break
        if not (cats & categorias):
            continue
        for item in _fetch_reddit(nombre, url):
            if item["epoch"] < limite_antiguedad:
                continue
            if not es_relevante_para(item["titular"], mercado):
                continue
            s = puntuar_titular(item["titular"])
            if s == 0.0:
                continue
            # Ponderado por votos: 500+ votos pesa el doble que 50
            peso_votos = min(2.0, 0.5 + item["votos"] / 500.0)
            social_scores.append(s * peso_votos)
            titulares_relevantes.append({
                "titular": item["titular"][:300],
                "fuente": nombre,
                "sentimiento": s,
                "epoch": item["epoch"],
                "alto_impacto": False,
                "social": True,
                "hash": hash_titular(item["titular"], nombre),
            })

    # dedupe por hash y orden por recencia
    vistos = set()
    unicos = []
    for t in sorted(titulares_relevantes, key=lambda x: x["epoch"], reverse=True):
        if t["hash"] in vistos:
            continue
        vistos.add(t["hash"])
        unicos.append(t)
    unicos = unicos[:30]

    # 2) Score de noticias: promedio ponderado por recencia (pesos
    #    LINEALES 1.0 -> 0.4 del más nuevo al más viejo — sin EMA).
    prensa = [t for t in unicos if not t.get("social")]
    social = [t for t in unicos if t.get("social")]

    if prensa:
        n = len(prensa)
        # Peso por recencia (lineal, sin EMA) x2 si es de alto impacto:
        # un anuncio de la Fed no puede pesar igual que una nota de color.
        # DECAIMIENTO POR TIEMPO REAL, no por posición en la lista (bug
        # corregido 8-ago). La fórmula anterior usaba el índice: si salían
        # diez noticias en cinco minutos, la décima pesaba la mitad que la
        # primera solo por llegar después en el orden, aunque fuera igual
        # de reciente. Ahora el peso depende de las horas transcurridas,
        # que es lo que de verdad importa: una noticia de hace 30 minutos
        # pesa igual esté en la posición 1 o en la 10.
        ahora_ts = time.time()
        pesos = []
        for t in prensa:
            horas = max(0.0, (ahora_ts - float(t.get("epoch", ahora_ts))) / 3600.0)
            # Semivida de ~8 horas: a las 8h una noticia pesa la mitad
            peso_recencia = math.exp(-0.0866 * horas)
            pesos.append(peso_recencia * (2.0 if t.get("alto_impacto") else 1.0))
        score_noticias = sum(w * t["sentimiento"] for w, t in zip(pesos, prensa)) / sum(pesos)
    else:
        score_noticias = 0.0

    score_social = (sum(social_scores) / len(social_scores)) if social_scores else 0.0
    score_social = max(-1.0, min(1.0, score_social))

    # 3) Indicadores gratuitos
    fng = obtener_fear_greed() if mercado in MERCADOS_FEAR_GREED else None
    funding = obtener_funding_rate(mercado)

    score_bruto = (
        PESO_NOTICIAS * score_noticias
        + PESO_SOCIAL * score_social
        + PESO_FUNDING * _score_funding(funding)
        + PESO_FEAR_GREED * _score_fear_greed(fng)
    )
    score_bruto = max(-1.0, min(1.0, score_bruto))

    # 4) Modulación por efectividad histórica VERIFICADA (la "memoria real"):
    #    con >=20 snapshots verificados de este mercado, si el fundamental
    #    históricamente coincidió con lo que el precio hizo (tasa >50%),
    #    se le mantiene o refuerza la voz; si históricamente falló
    #    (tasa <50%), se le baja el volumen proporcionalmente. Sin muestra
    #    suficiente: factor 0.7 conservador (opina, pero bajito).
    factor = 0.7
    ef = efectividad_historica or {}
    n_ver = ef.get("total_verificadas", 0)
    if n_ver >= MINIMO_MUESTRAS_EFECTIVIDAD and ef.get("tasa_acierto") is not None:
        # Función SIGMOIDE, no escalón. Con un tramo lineal recortado, dos
        # mercados con 49.9% y 50.1% de acierto recibían factores muy
        # distintos, y una sola operación podía saltar el factor de golpe:
        # el sistema oscilaba (whipsaw) alrededor del umbral.
        # La sigmoide hace que la penalización sea gradual: cada punto de
        # acierto mueve el factor un poco, nunca de golpe.
        #   40% -> 0.27 · 50% -> 0.50 · 60% -> 0.73 · 70% -> 0.88
        factor = 1.0 / (1.0 + math.exp(-(ef["tasa_acierto"] - 50.0) / 10.0))
        # Además se pondera por CONFIANZA en la muestra: con 20 datos la
        # estimación es débil; con 100, sólida. Sin esto, veinte
        # operaciones mandaban tanto como doscientas.
        confianza_muestra = min(1.0, n_ver / 100.0)
        factor = 0.7 * (1 - confianza_muestra) + factor * confianza_muestra
    score = round(score_bruto * factor, 3)

    if score > 0.08:
        etiqueta = "ALCISTA"
    elif score < -0.08:
        etiqueta = "BAJISTA"
    else:
        etiqueta = "NEUTRAL"

    eventos_alto_impacto = [t for t in prensa if t.get("alto_impacto")]

    partes = []
    if unicos:
        pos = sum(1 for t in unicos if t["sentimiento"] > 0.05)
        neg = sum(1 for t in unicos if t["sentimiento"] < -0.05)
        partes.append(f"{len(unicos)} titulares relevantes ({pos} positivos, {neg} negativos)")
    else:
        partes.append("sin titulares relevantes recientes")
    if eventos_alto_impacto:
        partes.append(f"⚡ {len(eventos_alto_impacto)} evento(s) de alto impacto")
    if social:
        partes.append(f"redes: {len(social)} posts (score {score_social:+.2f})")
    if fng:
        partes.append(f"Fear&Greed {fng['valor']} ({fng['clasificacion']})")
    if funding is not None:
        partes.append(f"funding {funding * 100:.4f}%")

    return {
        "score": score,
        "score_bruto": round(score_bruto, 3),
        "score_social": round(score_social, 3),
        "n_social": len(social),
        "alto_impacto": [t["titular"] for t in eventos_alto_impacto[:3]],
        "n_alto_impacto": len(eventos_alto_impacto),
        "etiqueta": etiqueta,
        "n_titulares": len(unicos),
        "titulares": unicos,
        "fear_greed": fng,
        "funding_rate": funding,
        "detalle": " · ".join(partes),
        "efectividad": ef or None,
    }


# ---------------------------------------------------------------------------
# VIGILANCIA DEL CONTEXTO (¿sigue en pie lo que sostenía la señal?)
# ---------------------------------------------------------------------------

# Cambio de score fundamental a partir del cual se considera que el
# contexto se dio la vuelta de verdad y no es simple oscilación de ruido.
UMBRAL_CAMBIO_CONTEXTO = 0.30


def vigilar_contexto(mercado: str, score_original: float, etiqueta_original: str,
                     direccion_predicha: str,
                     efectividad_historica: Optional[dict] = None) -> dict:
    """
    Vuelve a leer el fundamental y lo compara con el que había cuando se
    emitió la señal. Responde a la pregunta operativa real: **¿el motivo
    por el que se entró sigue existiendo?**

    Un análisis técnico no ve venir un anuncio de la Fed, un arancel
    nuevo o una escalada militar. Si el contexto se da la vuelta a mitad
    de operación, el usuario tiene que enterarse mientras la posición
    está abierta, no en el reporte del día siguiente.

    Devuelve una alerta cuando:
      · El score fundamental se invirtió respecto a la dirección abierta
        y el cambio supera el umbral (no simple ruido).
      · Apareció un evento de ALTO IMPACTO (Fed, guerra, aranceles,
        orden ejecutiva) que no estaba al abrir.

    Fail-soft: si las fuentes no responden, devuelve alerta=False y la
    operación sigue su curso normal — nunca inventa una alarma.
    """
    try:
        actual = analizar_fundamental(mercado, efectividad_historica)
    except Exception as e:
        logger.warning(f"Vigilancia de contexto no disponible para {mercado}: {e}")
        return {"alerta": False, "motivo": "", "disponible": False}

    score_actual = float(actual.get("score", 0.0))
    delta = score_actual - float(score_original or 0.0)
    alcista = direccion_predicha == "ALCISTA"

    motivos = []
    # 1) ¿El contexto se volvió en contra de la posición abierta?
    en_contra = (alcista and score_actual < -0.10) or ((not alcista) and score_actual > 0.10)
    giro = abs(delta) >= UMBRAL_CAMBIO_CONTEXTO and (
        (alcista and delta < 0) or ((not alcista) and delta > 0))
    if en_contra and giro:
        motivos.append(
            f"El contexto fundamental pasó de {etiqueta_original} a "
            f"{actual.get('etiqueta')} y ahora está EN CONTRA de la posición "
            f"{direccion_predicha} (score {score_original:+.2f} → {score_actual:+.2f}).")

    # 2) ¿Apareció un evento de alto impacto?
    eventos = actual.get("alto_impacto") or []
    if eventos:
        motivos.append("Evento de alto impacto detectado: " + eventos[0][:140])

    return {
        "alerta": bool(motivos),
        "motivo": " ".join(motivos),
        "disponible": True,
        "score_actual": round(score_actual, 3),
        "etiqueta_actual": actual.get("etiqueta"),
        "n_alto_impacto": actual.get("n_alto_impacto", 0),
    }


def diagnosticar_fuentes(mercado: str = "XAUUSD") -> dict:
    """
    Comprueba UNA POR UNA qué fuentes responden y cuántos titulares
    relevantes aporta cada una. Existe porque en producción el
    fundamental salía siempre "sin noticias relevantes" y no había forma
    de saber si era porque las fuentes fallaban, porque el filtro de
    relevancia era demasiado estrecho, o porque de verdad no había nada.

    Sin este diagnóstico, "el fundamental no funciona" es una intuición;
    con él es un dato con nombre y apellido.
    """
    resultado = {"mercado": mercado, "fuentes": [], "total_relevantes": 0}
    categorias = MERCADO_A_CATEGORIAS.get(mercado.upper(), {"macro"})
    limite = time.time() - HORAS_VENTANA_NOTICIAS * 3600

    for nombre, url, cats in FEEDS_RSS:
        if not (cats & categorias):
            resultado["fuentes"].append({"fuente": nombre, "estado": "no aplica a este mercado"})
            continue
        t0 = time.time()
        try:
            items = _fetch_feed(nombre, url)
            recientes = [i for i in items if i["epoch"] >= limite]
            relevantes = [i for i in recientes if es_relevante_para(i["titular"], mercado)]
            resultado["fuentes"].append({
                "fuente": nombre,
                "estado": "OK" if items else "sin respuesta o vacío",
                "titulares": len(items),
                "recientes_48h": len(recientes),
                "relevantes": len(relevantes),
                "segundos": round(time.time() - t0, 2),
                "ejemplo": (relevantes[0]["titular"][:90] if relevantes
                            else (recientes[0]["titular"][:90] if recientes else "")),
            })
            resultado["total_relevantes"] += len(relevantes)
        except Exception as e:
            resultado["fuentes"].append({"fuente": nombre, "estado": f"ERROR: {e}"})

    for nombre, url, cats in FEEDS_REDDIT:
        if not (cats & categorias):
            continue
        try:
            items = _fetch_reddit(nombre, url)
            relevantes = [i for i in items if es_relevante_para(i["titular"], mercado)]
            resultado["fuentes"].append({
                "fuente": nombre, "estado": "OK" if items else "sin respuesta",
                "titulares": len(items), "relevantes": len(relevantes),
                "ejemplo": relevantes[0]["titular"][:90] if relevantes else "",
            })
            resultado["total_relevantes"] += len(relevantes)
        except Exception as e:
            resultado["fuentes"].append({"fuente": nombre, "estado": f"ERROR: {e}"})

    return resultado
