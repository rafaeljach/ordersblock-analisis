"""
Noticias de geopolitica y mercados para ordersblock.com, en un solo lugar.

Un servicio aparte (ordersblock-noticias) lee fuentes publicas y gratuitas cada
1 a 5 minutos, quita duplicados, marca impacto y activos afectados, guarda solo
TITULAR + fuente + hora + enlace al original (nunca el articulo) y deja que
traductor.py traduzca al espanol, ingles y portugues. La API del motor sirve
/noticias leyendo la misma base: el navegador del usuario la consulta directo,
sin pasar por Cloudflare Workers.

Uso:
    python noticias.py probar-fuentes   # lee cada fuente una vez y muestra si responde (no guarda)
    python noticias.py una-vuelta       # lee lo que toque, guarda y traduce una vez
    python noticias.py servicio         # bucle del servicio (lo usa systemd)

Las fuentes se pueden cambiar sin tocar el codigo con fuentes_noticias.json
(misma forma que FUENTES) en la carpeta del motor.
"""

import hashlib
import html
import json
import logging
import os
import re
import sqlite3
import sys
import threading
import time
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

logger = logging.getLogger("ordersblock.noticias")

CARPETA = os.path.dirname(os.path.abspath(__file__))
RUTA_DB = os.getenv("ORDERSBLOCK_NOTICIAS_DB", os.path.join(CARPETA, "noticias.db"))
RUTA_FUENTES = os.getenv("ORDERSBLOCK_NOTICIAS_FUENTES", os.path.join(CARPETA, "fuentes_noticias.json"))
RETENCION_DIAS = int(os.getenv("ORDERSBLOCK_NOTICIAS_DIAS", "7"))
MAX_EDAD_ENTRADA_H = 48          # lo que llega con mas de 48 h no se guarda
MAX_POR_FUENTE = 40
TIMEOUT = 12
UA = "ordersblock-noticias/1.0 (+https://ordersblock.com; titulares con enlace a la fuente)"

# (id, nombre, url, idioma, categoria, cada_seg). Categorias: mercados, geopolitica, bancos, oficial, defensa, cripto, radar
FUENTES = [
    # Minuto a minuto, lo que mueve el mercado (investingLive ya no publica RSS y FXStreet bloquea lectores: fuera)
    ("cnbc_world", "CNBC", "https://www.cnbc.com/id/100727362/device/rss/rss.html", "en", "mercados", 90),
    ("cnbc_markets", "CNBC Mercados", "https://www.cnbc.com/id/20910258/device/rss/rss.html", "en", "mercados", 90),
    ("marketwatch", "MarketWatch", "https://feeds.content.dowjones.io/public/rss/mw_topstories", "en", "mercados", 120),
    # Geopolitica mundial
    ("aljazeera", "Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml", "en", "geopolitica", 120),
    ("bbc_world", "BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml", "en", "geopolitica", 120),
    ("guardian_world", "The Guardian", "https://www.theguardian.com/world/rss", "en", "geopolitica", 180),
    ("france24_en", "France 24", "https://www.france24.com/en/rss", "en", "geopolitica", 180),
    ("dw_en", "DW", "https://rss.dw.com/rdf/rss-en-world", "en", "geopolitica", 180),
    ("euronews", "Euronews", "https://www.euronews.com/rss?level=theme&name=news", "en", "geopolitica", 180),
    ("npr_world", "NPR World", "https://feeds.npr.org/1004/rss.xml", "en", "geopolitica", 300),
    ("un_news", "Noticias ONU", "https://news.un.org/feed/subscribe/es/news/all/rss.xml", "es", "geopolitica", 300),
    # En espanol (sin traducir)
    ("bbc_mundo", "BBC Mundo", "https://feeds.bbci.co.uk/mundo/rss.xml", "es", "geopolitica", 180),
    ("france24_es", "France 24 Español", "https://www.france24.com/es/rss", "es", "geopolitica", 180),
    ("dw_es", "DW Español", "https://rss.dw.com/rdf/rss-sp-all", "es", "geopolitica", 300),
    # Oficiales y bancos centrales
    ("whitehouse", "Casa Blanca", "https://www.whitehouse.gov/briefings-statements/feed/", "en", "oficial", 300),
    ("fed", "Reserva Federal", "https://www.federalreserve.gov/feeds/press_all.xml", "en", "bancos", 300),
    ("fed_discursos", "Fed – discursos", "https://www.federalreserve.gov/feeds/speeches.xml", "en", "bancos", 600),
    ("ecb", "BCE", "https://www.ecb.europa.eu/rss/press.html", "en", "bancos", 300),
    ("boe", "Banco de Inglaterra", "https://www.bankofengland.co.uk/rss/news", "en", "bancos", 600),
    # Defensa y conflictos
    ("defensenews", "Defense News", "https://www.defensenews.com/arc/outboundfeeds/rss/?outputType=xml", "en", "defensa", 300),
    ("twz", "The War Zone", "https://www.twz.com/feed", "en", "defensa", 300),
    ("foreignpolicy", "Foreign Policy", "https://foreignpolicy.com/feed/", "en", "defensa", 600),
    # Cripto
    ("coindesk", "CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/", "en", "cripto", 180),
    ("cointelegraph", "Cointelegraph", "https://cointelegraph.com/rss", "en", "cripto", 180),
    # Radar global (GDELT: monitoreo abierto de noticias del mundo; solo dominios de confianza)
    ("gdelt", "Radar GDELT", "https://api.gdeltproject.org/api/v2/doc/doc?query=(war%20OR%20missile%20OR%20sanctions%20OR%20invasion%20OR%20ceasefire%20OR%20coup%20OR%20nuclear%20OR%20OPEC%20OR%20tariffs)%20sourcelang:english&mode=artlist&format=json&maxrecords=75&timespan=60min&sort=datedesc",
     "en", "radar", 300),
]
# Solo para el radar GDELT: medios reconocidos (GDELT trae de todo, incluidos sitios de baja calidad)
DOMINIOS_RADAR = {
    "reuters.com", "apnews.com", "bloomberg.com", "ft.com", "wsj.com", "cnbc.com", "nytimes.com",
    "washingtonpost.com", "politico.com", "axios.com", "theguardian.com", "bbc.com", "bbc.co.uk",
    "aljazeera.com", "france24.com", "dw.com", "euronews.com", "npr.org", "economist.com",
    "foreignpolicy.com", "defensenews.com", "breakingdefense.com", "twz.com", "timesofisrael.com",
    "kyivindependent.com", "scmp.com", "nikkei.com", "asia.nikkei.com", "elpais.com", "efe.com",
    "lemonde.fr", "spiegel.de", "japantimes.co.jp", "thehill.com", "cbsnews.com", "abcnews.go.com",
    "nbcnews.com", "cnn.com", "usnews.com", "barrons.com", "marketwatch.com", "investing.com",
}

# Impacto y activos: palabras clave en ingles, espanol y portugues (sin acentos, en minusculas)
# Palabra o frase exacta; con * al final cuenta como raiz (sanction* = sanction, sanctions, sanctioned)
_ALTO = [
    "war", "wars", "missile*", "airstrike*", "air strike*", "strikes", "strike on", "invasion", "invad*",
    "nuclear", "attack*", "explosion*", "coup", "coups", "martial law", "assassinat*", "ceasefire", "truce", "sanction*",
    "constitutional crisis", "impeach*", "curfew", "state of siege", "government collapse*", "snap election*",
    "crisis constitucional", "autogolpe", "estado de sitio", "toque de queda", "destitucion", "juicio politico",
    "embargo", "blockade", "hormuz", "red sea", "taiwan strait", "mobiliz*", "state of emergency",
    "declares emergency", "default*", "rate cut*", "rate hike*", "emergency meeting", "hack*", "exploit*",
    "breaking", "urgent",
    "guerra", "misil*", "ataque*", "bombarde*", "invasion", "golpe de estado", "alto el fuego", "sancion*",
    "bloqueo", "estado de excepcion", "recorte de tasas", "subida de tasas", "hackeo", "ultima hora", "urgente",
    "cessar fogo", "sancoes",
]
_MEDIO = [
    "tariff*", "trade talks", "trade war", "opec*", "inflation", "cpi", "jobs report", "payrolls", "gdp", "recession",
    "fed", "federal reserve", "powell", "fomc", "ecb", "lagarde", "boe", "bailey", "boj", "ueda", "treasury",
    "yields", "election*", "protest*", "drone*", "troops", "military", "border", "iran*", "israel*", "gaza",
    "ukrain*", "russia*", "china", "chinese", "north korea*", "venezuela*", "houthi*", "saudi*", "oil", "gold",
    "bitcoin", "etf*", "arancel*", "inflacion", "empleo", "pib", "recesion", "eleccion*", "protesta*", "tropas",
    "frontera", "petroleo", "oro", "tasas", "banco central",
]
_ACTIVOS = {
    "XAU": ["gold", "bullion", "safe haven", "oro", "ouro", "war", "guerra", "missile*", "misil*", "nuclear",
            "sanction*", "sancion*", "central bank buying", "attack*", "ataque*", "strikes", "airstrike*", "invasion",
            "ceasefire", "truce", "alto el fuego", "cessar fogo", "escalat*", "escalada", "coup", "golpe de estado",
            "constitutional crisis", "crisis constitucional"],
    "USD": ["fed", "federal reserve", "fomc", "powell", "treasury", "treasuries", "yields", "dollar", "dolar",
            "payrolls", "cpi", "jobs report", "nonfarm", "white house", "casa blanca", "tariff*", "arancel*"],
    "OIL": ["oil", "crude", "brent", "wti", "opec*", "petroleo", "hormuz", "red sea", "houthi*", "saudi*", "iran*",
            "refiner*", "pipeline*", "lng"],
    "EUR": ["ecb", "lagarde", "eurozone", "euro area", "bce", "eurozona", "germany", "alemania", "euro"],
    "GBP": ["boe", "bank of england", "bailey", "sterling", "libra esterlina"],
    "JPY": ["boj", "bank of japan", "ueda", "yen", "japan*"],
    "BTC": ["bitcoin", "btc", "crypto*", "cripto*", "binance", "coinbase", "stablecoin*", "ethereum", "ether"],
}

_lock_db = threading.Lock()


# ---------------------------------------------------------------- base de datos

def conectar(ruta: str = None) -> sqlite3.Connection:
    c = sqlite3.connect(ruta or RUTA_DB, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=15000")
    return c


def _leer(ruta: str = None) -> sqlite3.Connection:
    """Conexion de solo lectura para la API (no crea el archivo ni cambia el modo de la base)."""
    r = ruta or RUTA_DB
    if not os.path.exists(r):
        raise sqlite3.OperationalError("sin base de noticias")
    c = sqlite3.connect(f"file:{r}?mode=ro", uri=True, timeout=5)
    c.row_factory = sqlite3.Row
    return c


def inicializar(ruta: str = None) -> None:
    with _lock_db, conectar(ruta) as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS noticias (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            clave TEXT UNIQUE NOT NULL,
            titulo_norm TEXT NOT NULL,
            fuente_id TEXT NOT NULL,
            fuente TEXT NOT NULL,
            idioma TEXT NOT NULL,
            categoria TEXT NOT NULL,
            titulo TEXT NOT NULL,
            enlace TEXT NOT NULL,
            publicado INTEGER NOT NULL,
            recibido INTEGER NOT NULL,
            impacto INTEGER NOT NULL DEFAULT 0,
            activos TEXT NOT NULL DEFAULT '',
            t_es TEXT, t_en TEXT, t_pt TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_not_pub ON noticias(publicado DESC);
        CREATE INDEX IF NOT EXISTS ix_not_norm ON noticias(titulo_norm);
        CREATE TABLE IF NOT EXISTS fuentes_estado (
            fuente_id TEXT PRIMARY KEY,
            ultimo_intento INTEGER, ultimo_ok INTEGER, error TEXT, items INTEGER DEFAULT 0,
            nuevos INTEGER DEFAULT 0, fallos INTEGER DEFAULT 0, etag TEXT, modificado TEXT
        );
        """)
        if "politica" not in [r[1] for r in c.execute("PRAGMA table_info(noticias)")]:
            c.execute("ALTER TABLE noticias ADD COLUMN politica INTEGER NOT NULL DEFAULT 0")
        # Reclasificar lo guardado con las reglas actuales (unos miles de titulares: milisegundos).
        # Asi un cambio de palabras clave vale tambien para lo que ya estaba en la base.
        cambios = []
        for nid, titulo, cat, imp, act, pol in c.execute("SELECT id, titulo, categoria, impacto, activos, politica FROM noticias"):
            i2, a2 = clasificar(titulo, cat)
            p2 = es_politica(titulo, cat)
            if (i2, a2, p2) != (imp, act, pol):
                cambios.append((i2, a2, p2, nid))
        if cambios:
            c.executemany("UPDATE noticias SET impacto=?, activos=?, politica=? WHERE id=?", cambios)


# ---------------------------------------------------------------- utilidades de texto

_TAG = re.compile(r"<[^>]+>")
_BLOQUE = re.compile(r"<(script|style)[^>]*>.*?</\1\s*>", re.I | re.S)
_ESP = re.compile(r"\s+")


def limpiar(texto, maximo: int = 280) -> str:
    t = str(texto or "")
    for _ in range(2):            # algunos feeds escapan dos veces (&amp;lt;script&amp;gt;)
        t = html.unescape(_TAG.sub(" ", _BLOQUE.sub(" ", t)))
    t = _TAG.sub(" ", _BLOQUE.sub(" ", t))
    t = _ESP.sub(" ", t).strip()
    return t[:maximo].rstrip() + ("…" if len(t) > maximo else "")


def normalizar(texto: str) -> str:
    t = unicodedata.normalize("NFKD", str(texto or "").lower())
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def limpiar_enlace(url: str) -> str:
    """Solo http(s). Quita parametros de rastreo (utm_*, fbclid...) para deduplicar mejor."""
    u = str(url or "").strip()
    try:
        p = urlsplit(u)
    except ValueError:
        return ""
    if p.scheme not in ("http", "https") or not p.netloc or len(u) > 2000:
        return ""
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
         if not k.lower().startswith("utm_") and k.lower() not in ("fbclid", "gclid", "ocid", "cmpid", "at_medium", "at_campaign")]
    return urlunsplit((p.scheme, p.netloc.lower(), p.path, urlencode(q), ""))


def dominio(url: str) -> str:
    try:
        h = urlsplit(url).netloc.lower().split(":")[0]
    except ValueError:
        return ""
    return h[4:] if h.startswith("www.") else h


def parsear_fecha(texto) -> int:
    """RFC 822 (RSS), ISO 8601 (Atom) o 20261030T201500Z (GDELT). 0 si no se entiende."""
    s = str(texto or "").strip()
    if not s:
        return 0
    try:
        return int(parsedate_to_datetime(s).timestamp())
    except (TypeError, ValueError, IndexError):
        pass
    m = re.match(r"^(\d{8})T(\d{6})Z$", s)
    if m:
        try:
            return int(datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            return 0
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return int(d.timestamp())
    except ValueError:
        return 0


def _patron(palabras) -> re.Pattern:
    partes = []
    for p in palabras:
        raiz = p.endswith("*")
        w = re.escape(normalizar(p.rstrip("*")))
        partes.append(r"\b" + w + (r"" if raiz else r"\b"))
    return re.compile("|".join(partes))


# Politica y gobiernos (filtro "Gobiernos"): vale para cualquier fuente, no solo las oficiales
_POLITICA = [
    "president*", "prime minister*", "premier", "parliament*", "congress*", "senate", "senator*", "government*",
    "cabinet", "minister*", "election*", "vote", "votes", "voting", "referendum", "coup", "coups", "constitution*",
    "impeach*", "opposition", "protest*", "regime", "dictator*", "supreme court", "martial law", "chancellor",
    "kremlin", "white house", "downing street", "elysee", "nato summit", "g7", "g20", "un security council",
    "presidente*", "primer ministro", "primera ministra", "parlamento", "congreso", "senado", "gobierno*",
    "gabinete", "ministr*", "eleccion*", "elecciones", "votacion*", "referendo", "referendum", "golpe de estado",
    "autogolpe", "constitucion*", "destitucion", "juicio politico", "oposicion", "protesta*", "regimen",
    "dictad*", "corte suprema", "tribunal supremo", "casa blanca", "consejo de seguridad",
    "governo", "eleicoes", "eleicao", "golpe", "parlamento", "congresso", "ministro*",
]
_RE_ALTO, _RE_MEDIO = _patron(_ALTO), _patron(_MEDIO)
_RE_POLITICA = _patron(_POLITICA)


def es_politica(titulo: str, categoria: str) -> int:
    return 1 if categoria == "oficial" or _RE_POLITICA.search(normalizar(titulo)) else 0
_RE_ACTIVOS = {a: _patron(p) for a, p in _ACTIVOS.items()}


def clasificar(titulo: str, categoria: str):
    """(impacto 0/1/2, activos 'XAU,OIL'). Reglas simples y transparentes, sin IA."""
    t = normalizar(titulo)
    impacto = 2 if _RE_ALTO.search(t) else 1 if (_RE_MEDIO.search(t) or categoria in ("bancos", "oficial")) else 0
    activos = [a for a, rx in _RE_ACTIVOS.items() if rx.search(t)]
    return impacto, ",".join(activos)


# ---------------------------------------------------------------- lectura de fuentes

def _texto(e):
    return (e.text or "").strip() if e is not None else ""


def parsear_xml(contenido: bytes) -> list:
    """RSS 2.0, RSS 1.0 (RDF) y Atom con la libreria estandar. Devuelve [{titulo, enlace, fecha}]."""
    if contenido.lstrip()[:200].lower().startswith((b"<!doctype html", b"<html")):
        raise ValueError("la direccion devuelve una pagina web, no un feed RSS")
    raiz = ET.fromstring(contenido)
    salida = []
    for nodo in raiz.iter():
        tag = nodo.tag.split("}")[-1].lower()
        if tag not in ("item", "entry"):
            continue
        titulo, enlace, fecha = "", "", 0
        for h in nodo:
            ht = h.tag.split("}")[-1].lower()
            if ht == "title":
                titulo = "".join(h.itertext()).strip()
            elif ht == "link":
                href = h.get("href")
                rel = (h.get("rel") or "alternate").lower()
                if href and rel == "alternate":
                    enlace = href
                elif not href and not enlace:
                    enlace = _texto(h)
            elif ht == "guid" and not enlace and (h.get("isPermaLink") or "true").lower() == "true" and _texto(h).startswith("http"):
                enlace = _texto(h)
            elif ht in ("pubdate", "published", "updated", "date", "issued") and not fecha:
                fecha = parsear_fecha(_texto(h))
        if titulo and enlace:
            salida.append({"titulo": titulo, "enlace": enlace, "fecha": fecha})
        if len(salida) >= MAX_POR_FUENTE:
            break
    return salida


def parsear_gdelt(contenido: bytes) -> list:
    texto = contenido.decode("utf-8", "replace").strip()
    if not texto or texto == "{}":
        return []                                  # GDELT responde vacio cuando no hay articulos en la ventana
    if not texto.startswith("{"):
        raise ValueError("GDELT respondio: " + texto[:120])
    d = json.loads(texto)
    salida = []
    for a in (d.get("articles") or [])[:MAX_POR_FUENTE * 2]:
        url = a.get("url") or ""
        dom = dominio(url)
        if not any(dom == x or dom.endswith("." + x) for x in DOMINIOS_RADAR):
            continue
        salida.append({"titulo": a.get("title") or "", "enlace": url, "fecha": parsear_fecha(a.get("seendate")),
                       "fuente": dom})
    return salida[:MAX_POR_FUENTE]


def cargar_fuentes() -> list:
    lista = FUENTES
    if os.path.exists(RUTA_FUENTES):
        try:
            with open(RUTA_FUENTES, encoding="utf-8") as f:
                otra = json.load(f)
            lista = [tuple(x) for x in otra if isinstance(x, list) and len(x) == 6]
        except (OSError, ValueError) as e:
            logger.warning(f"fuentes_noticias.json no valido ({e}); se usan las fuentes por defecto")
    return lista


def leer_fuente(fuente, get=requests.get, etag: str = None, modificado: str = None):
    """Devuelve (items_limpios, etag, modificado, codigo). 304 = sin cambios."""
    fid, nombre, url, idioma, categoria, _ = fuente
    cab = {"User-Agent": UA, "Accept": "application/rss+xml, application/atom+xml, application/xml, application/json;q=0.9, */*;q=0.5"}
    if etag:
        cab["If-None-Match"] = etag
    if modificado:
        cab["If-Modified-Since"] = modificado
    r = get(url, headers=cab, timeout=TIMEOUT)
    codigo = getattr(r, "status_code", 200)
    if codigo == 304:
        return [], etag, modificado, 304
    if codigo >= 400:
        raise RuntimeError(f"HTTP {codigo}")
    contenido = r.content[:3_000_000]
    crudos = parsear_gdelt(contenido) if "gdeltproject.org" in url else parsear_xml(contenido)
    ahora = int(time.time())
    items = []
    for x in crudos:
        titulo = limpiar(x["titulo"])
        enlace = limpiar_enlace(x["enlace"])
        if len(titulo) < 12 or not enlace:
            continue
        fecha = x["fecha"] if 0 < x["fecha"] <= ahora + 300 else ahora
        if ahora - fecha > MAX_EDAD_ENTRADA_H * 3600:
            continue
        nombre_fuente = nombre if fid != "gdelt" else (x.get("fuente") or nombre)
        items.append({"fuente_id": fid, "fuente": nombre_fuente, "idioma": idioma, "categoria": categoria,
                      "titulo": titulo, "enlace": enlace, "publicado": min(fecha, ahora)})
    h = getattr(r, "headers", {}) or {}
    return items, h.get("ETag"), h.get("Last-Modified"), codigo


def guardar(items: list, ruta: str = None) -> int:
    """Inserta lo nuevo. Mismo enlace o mismo titular en las ultimas 24 h = duplicado (gana el primero)."""
    if not items:
        return 0
    ahora = int(time.time())
    nuevos = 0
    with _lock_db, conectar(ruta) as c:
        for it in items:
            clave = hashlib.sha1(it["enlace"].encode()).hexdigest()
            norm = normalizar(it["titulo"])[:90]
            if c.execute("SELECT 1 FROM noticias WHERE clave=? OR (titulo_norm=? AND recibido>?)",
                         (clave, norm, ahora - 86400)).fetchone():
                continue
            impacto, activos = clasificar(it["titulo"], it["categoria"])
            politica = es_politica(it["titulo"], it["categoria"])
            t = {"es": None, "en": None, "pt": None}
            if it["idioma"] in t:
                t[it["idioma"]] = it["titulo"]
            cur = c.execute("""INSERT OR IGNORE INTO noticias (clave, titulo_norm, fuente_id, fuente, idioma, categoria, titulo,
                         enlace, publicado, recibido, impacto, activos, t_es, t_en, t_pt, politica) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (clave, norm, it["fuente_id"], it["fuente"], it["idioma"], it["categoria"], it["titulo"], it["enlace"],
                       it["publicado"], ahora, impacto, activos, t["es"], t["en"], t["pt"], politica))
            nuevos += max(cur.rowcount, 0)
    return nuevos


def _estado_fuente(c, fid):
    r = c.execute("SELECT * FROM fuentes_estado WHERE fuente_id=?", (fid,)).fetchone()
    return dict(r) if r else {"fuente_id": fid, "ultimo_intento": 0, "ultimo_ok": 0, "fallos": 0, "etag": None, "modificado": None}


def una_vuelta(get=requests.get, ruta: str = None, forzar: bool = False) -> dict:
    """Lee las fuentes a las que les toca (segun cada_seg y espera por fallos) y guarda lo nuevo."""
    inicializar(ruta)
    ahora = int(time.time())
    resumen = {"leidas": 0, "nuevos": 0, "errores": 0}
    for fuente in cargar_fuentes():
        fid, cada = fuente[0], int(fuente[5])
        with _lock_db, conectar(ruta) as c:
            est = _estado_fuente(c, fid)
        espera = min(cada * (2 ** min(est["fallos"], 5)), 1800)
        if not forzar and ahora - (est["ultimo_intento"] or 0) < espera:
            continue
        resumen["leidas"] += 1
        try:
            items, etag, mod, codigo = leer_fuente(fuente, get=get, etag=est.get("etag"), modificado=est.get("modificado"))
            n = guardar(items, ruta)
            resumen["nuevos"] += n
            with _lock_db, conectar(ruta) as c:
                c.execute("""INSERT INTO fuentes_estado (fuente_id, ultimo_intento, ultimo_ok, error, items, nuevos, fallos, etag, modificado)
                             VALUES (?,?,?,NULL,?,?,0,?,?) ON CONFLICT(fuente_id) DO UPDATE SET ultimo_intento=excluded.ultimo_intento,
                             ultimo_ok=excluded.ultimo_ok, error=NULL, items=excluded.items, nuevos=nuevos+excluded.nuevos, fallos=0,
                             etag=excluded.etag, modificado=excluded.modificado""",
                          (fid, ahora, ahora, len(items) if codigo != 304 else est.get("items", 0), n, etag, mod))
        except Exception as e:   # red, XML roto, HTTP 4xx/5xx: la fuente espera mas y las demas siguen
            resumen["errores"] += 1
            msg = f"{type(e).__name__}: {str(e)[:160]}"
            logger.warning(f"noticias {fid}: {msg}")
            with _lock_db, conectar(ruta) as c:
                c.execute("""INSERT INTO fuentes_estado (fuente_id, ultimo_intento, error, fallos) VALUES (?,?,?,1)
                             ON CONFLICT(fuente_id) DO UPDATE SET ultimo_intento=excluded.ultimo_intento, error=excluded.error,
                             fallos=fallos+1""", (fid, ahora, msg))
    with _lock_db, conectar(ruta) as c:
        c.execute("DELETE FROM noticias WHERE recibido < ?", (ahora - RETENCION_DIAS * 86400,))
    return resumen


# ---------------------------------------------------------------- consulta (la usa la API)

CATEGORIAS = ("mercados", "geopolitica", "bancos", "oficial", "defensa", "cripto", "radar")


def consultar(idioma: str = "es", categoria: str = "", solo_alto: bool = False, activo: str = "",
              desde_id: int = 0, n: int = 60, ruta: str = None) -> dict:
    idioma = idioma if idioma in ("es", "en", "pt") else "es"
    n = max(1, min(int(n or 60), 150))
    sql = "SELECT * FROM noticias WHERE id > ?"
    args = [int(desde_id or 0)]
    filtro_gob = categoria == "oficial"          # boton "Gobiernos": oficiales + politica de cualquier fuente
    if categoria in CATEGORIAS and not filtro_gob:
        sql += " AND categoria = ?"
        args.append(categoria)
    if solo_alto:
        sql += " AND impacto = 2"
    if activo and re.fullmatch(r"[A-Z]{3}", activo):
        sql += " AND (',' || activos || ',') LIKE ?"
        args.append(f"%,{activo},%")
    sql += " ORDER BY publicado DESC, id DESC LIMIT ?"
    args.append(n)
    try:
        with _leer(ruta) as c:
            if filtro_gob:
                # Mientras el servicio de noticias no haya migrado la base (columna nueva), solo las oficiales
                tiene = "politica" in [r[1] for r in c.execute("PRAGMA table_info(noticias)")]
                sql = sql.replace("WHERE id > ?", "WHERE id > ? AND " + ("(categoria = 'oficial' OR politica = 1)" if tiene else "categoria = 'oficial'"))
            filas = c.execute(sql, args).fetchall()
            ultimo = c.execute("SELECT MAX(id), MAX(recibido) FROM noticias").fetchone()
    except sqlite3.OperationalError:     # base aun no creada: el servicio de noticias no ha corrido
        return {"noticias": [], "ultimo_id": 0, "actualizado": None, "idioma": idioma}
    salida = []
    for f in filas:
        traducido = f["t_" + idioma]
        salida.append({
            "id": f["id"], "fuente": f["fuente"], "categoria": f["categoria"],
            "titulo": traducido or f["titulo"], "original": f["titulo"], "idioma_original": f["idioma"],
            "traducido": bool(traducido) and f["idioma"] != idioma, "enlace": f["enlace"],
            "publicado": datetime.fromtimestamp(f["publicado"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "impacto": f["impacto"], "activos": [a for a in (f["activos"] or "").split(",") if a],
        })
    return {"noticias": salida, "ultimo_id": ultimo[0] or 0, "idioma": idioma,
            "actualizado": datetime.fromtimestamp(ultimo[1], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if ultimo[1] else None}


def estado(ruta: str = None) -> dict:
    try:
        with _leer(ruta) as c:
            fuentes = [dict(r) for r in c.execute("SELECT fuente_id, ultimo_intento, ultimo_ok, error, items, nuevos, fallos FROM fuentes_estado ORDER BY fuente_id")]
            tot = c.execute("""SELECT COUNT(*), SUM(idioma != 'es' AND t_es IS NULL), SUM(recibido > strftime('%s','now') - 3600),
                               MAX(recibido) FROM noticias""").fetchone()
    except sqlite3.OperationalError:
        return {"activo": False, "fuentes": [], "total": 0}
    ahora = int(time.time())
    return {"activo": bool(tot[3]) and ahora - tot[3] < 1800, "total": tot[0], "sin_traducir_es": tot[1] or 0,
            "ultima_hora": tot[2] or 0, "fuentes": fuentes}


# ---------------------------------------------------------------- servicio

def servicio() -> None:
    """Bucle del servicio ordersblock-noticias: leer fuentes y traducir lo pendiente, cada 30 s."""
    import traductor
    inicializar()
    logger.info(f"servicio de noticias activo: {len(cargar_fuentes())} fuentes, base {RUTA_DB}")
    while True:
        try:
            r = una_vuelta()
            if r["leidas"]:
                logger.info(f"noticias: {r}")
            t = traductor.traducir_pendientes(RUTA_DB)
            if t:
                logger.info(f"traducidas: {t}")
        except Exception as e:
            logger.error(f"noticias: {type(e).__name__}: {e}")
        time.sleep(30)


def probar_fuentes() -> int:
    """Lee cada fuente una vez y muestra el resultado. No guarda nada."""
    ok = 0
    for f in cargar_fuentes():
        t0 = time.time()
        try:
            items, _, _, codigo = leer_fuente(f)
            reciente = max((x["publicado"] for x in items), default=0)
            edad = f"{int((time.time() - reciente) / 60)} min" if reciente else "-"
            print(f"OK    {f[0]:16} {len(items):3} titulares  ultimo hace {edad:>8}  ({time.time() - t0:.1f}s)  {items[0]['titulo'][:70] if items else ''}")
            ok += 1 if items else 0
        except Exception as e:
            print(f"FALLA {f[0]:16} {type(e).__name__}: {str(e)[:90]}")
    print(f"\n{ok} de {len(cargar_fuentes())} fuentes con titulares")
    return ok


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    orden = sys.argv[1] if len(sys.argv) > 1 else ""
    if orden == "probar-fuentes":
        sys.exit(0 if probar_fuentes() else 1)
    elif orden == "una-vuelta":
        print(una_vuelta(forzar=True))
        import traductor
        print("traducidas:", traductor.traducir_pendientes(RUTA_DB))
    elif orden == "servicio":
        servicio()
    else:
        print(__doc__)
        sys.exit(2)
