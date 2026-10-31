"""Pruebas del servicio de noticias (sin red): lectura RSS/RDF/Atom/GDELT, limpieza, duplicados,
espera por fallos, impacto y activos, traduccion por pares (traductor simulado), consulta y API /noticias."""
import io, os, sys, tempfile, time, zipfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TMP = tempfile.mkdtemp()
os.environ["ORDERSBLOCK_DB"] = os.path.join(TMP, "t.db")
os.environ["ORDERSBLOCK_NOTICIAS_DB"] = os.path.join(TMP, "noticias.db")
os.environ["ORDERSBLOCK_NOTICIAS_FUENTES"] = os.path.join(TMP, "no_existe.json")
os.environ["ORDERSBLOCK_API_KEY"] = "k" * 32
from email.utils import formatdate
import noticias, traductor

ok = True
def chk(nombre, cond, extra=""):
    global ok
    ok = ok and bool(cond)
    print(("OK    " if cond else "FALLA ") + nombre + (("  " + str(extra)) if extra else ""))

AHORA = time.time()
rfc = lambda seg: formatdate(AHORA - seg, usegmt=True)
iso = lambda seg: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(AHORA - seg))

RSS = f"""<?xml version="1.0"?><rss version="2.0"><channel><title>X</title>
<item><title><![CDATA[Israel strikes Iran nuclear sites &amp; oil jumps]]></title><link>https://ejemplo.com/a?utm_source=rss&amp;id=7</link><pubDate>{rfc(120)}</pubDate></item>
<item><title>Fed's Powell: &lt;b&gt;no rush&lt;/b&gt; to cut rates</title><link>https://ejemplo.com/b</link><pubDate>{rfc(600)}</pubDate></item>
<item><title>Noticia vieja de hace tres dias sin interes</title><link>https://ejemplo.com/vieja</link><pubDate>{rfc(3 * 86400)}</pubDate></item>
<item><title>Enlace peligroso que no debe guardarse</title><link>javascript:alert(1)</link></item>
<item><title><![CDATA[<script>alert(1)</script>Titular con etiqueta script dentro]]></title><link>https://ejemplo.com/c</link></item>
</channel></rss>""".encode()
RDF = f"""<?xml version="1.0"?><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/">
<item rdf:about="https://dw.com/1"><title>Russia launches drone attack on Kyiv</title><link>https://dw.com/1</link><dc:date>{iso(300)}</dc:date></item>
<item rdf:about="https://dw.com/2"><title>Israel strikes Iran nuclear sites &amp; oil jumps</title><link>https://dw.com/otra</link><dc:date>{iso(60)}</dc:date></item>
</rdf:RDF>""".encode()
ATOM = f"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Guerra en Ucrania: Rusia lanza misiles sobre Kiev</title>
<link rel="alternate" href="https://bbc.com/mundo/x"/><link rel="enclosure" href="https://bbc.com/img.jpg"/><updated>{iso(240)}</updated></entry></feed>""".encode()
GDELT = ('{"articles":[{"url":"https://www.reuters.com/world/x","title":"Missile hits port in Odesa","seendate":"%s","domain":"reuters.com"},'
         '{"url":"https://sitio-dudoso.xyz/y","title":"Rumor sin fuente sobre guerra mundial","seendate":"%s"}]}'
         % (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(AHORA - 500)), time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(AHORA - 500)))).encode()

class R:
    def __init__(self, contenido=b"", codigo=200, cab=None):
        self.content, self.status_code, self.headers = contenido, codigo, cab or {}

pedidos = []
def get_falso(url, headers=None, timeout=None):
    pedidos.append((url, dict(headers or {})))
    if "rss1" in url: return R(RSS, 200, {"ETag": '"v1"'})
    if "rdf" in url: return R(RDF)
    if "atom" in url: return R(ATOM)
    if "gdeltproject.org" in url: return R(GDELT)
    if "roto" in url: return R(b"<html>error", 500)
    raise RuntimeError("sin red")

noticias.FUENTES = [
    ("f_rss", "Fuente RSS", "https://f/rss1", "en", "mercados", 60),
    ("f_rdf", "DW", "https://f/rdf", "en", "geopolitica", 60),
    ("f_atom", "BBC Mundo", "https://f/atom", "es", "geopolitica", 60),
    ("gdelt", "Radar GDELT", "https://api.gdeltproject.org/api/v2/doc/doc?x", "en", "radar", 60),
    ("f_rota", "Rota", "https://f/roto", "en", "geopolitica", 60),
]

# 1. Lectores
it = noticias.parsear_xml(RSS)
chk("1. RSS 2.0: titular de CDATA, enlace y fecha", it[0]["titulo"].startswith("Israel strikes") and it[0]["enlace"].startswith("https://ejemplo.com/a") and it[0]["fecha"] > 0)
chk("2. RSS 1.0 (RDF, como DW) con dc:date", len(noticias.parsear_xml(RDF)) == 2 and noticias.parsear_xml(RDF)[0]["fecha"] > 0)
at = noticias.parsear_xml(ATOM)
chk("3. Atom: usa el enlace 'alternate', no la imagen", at[0]["enlace"] == "https://bbc.com/mundo/x")
chk("4. Fechas RFC 822, ISO 8601 y GDELT", all(abs(noticias.parsear_fecha(x) - (AHORA - 120)) < 5 for x in (rfc(120), iso(120), time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(AHORA - 120)))))
chk("5. Enlaces: quita utm_*, rechaza javascript: y data:", noticias.limpiar_enlace("https://ejemplo.com/a?utm_source=rss&id=7") == "https://ejemplo.com/a?id=7"
    and noticias.limpiar_enlace("javascript:alert(1)") == "" and noticias.limpiar_enlace("data:text/html,x") == "")
chk("6. Titulares sin HTML (ni <b> ni <script> ni su contenido, tambien doble escapado)", "<" not in noticias.limpiar("Fed's Powell: <b>no rush</b>")
    and noticias.limpiar("<script>alert(1)</script>Hola") == "Hola" and noticias.limpiar("&amp;lt;script&amp;gt;alert(1)&amp;lt;/script&amp;gt;Hola") == "Hola")

# 2. Una vuelta completa
r = noticias.una_vuelta(get=get_falso)
from sqlite3 import connect
c = connect(os.environ["ORDERSBLOCK_NOTICIAS_DB"])
filas = c.execute("SELECT fuente, titulo, enlace, impacto, activos FROM noticias").fetchall()
titulos = [f[1] for f in filas]
chk("7. Guarda lo nuevo de 4 fuentes y la rota no frena a las demas", r["errores"] == 1 and r["leidas"] == 5 and len(filas) >= 6, (r, len(filas)))
chk("8. Duplicado por titular (mismo titular en DW): se guarda una sola vez", sum(1 for t in titulos if t.startswith("Israel strikes")) == 1)
chk("9. Lo de hace 3 dias y el enlace javascript: no se guardan", not any("vieja" in t.lower() or "peligroso" in t.lower() for t in titulos))
chk("10. GDELT: solo medios de la lista (Reuters si, sitio dudoso no) y la fuente es el medio real",
    any(f[0] == "reuters.com" for f in filas) and not any("dudoso" in f[2] for f in filas))
imp = {f[1][:20]: (f[3], f[4]) for f in filas}
chk("11. Impacto y activos: ataque a Iran = ALTO con oro y petroleo; Powell = medio con USD",
    imp.get("Israel strikes Iran ") == (2, "XAU,OIL") and imp.get("Fed's Powell: no rus") == (1, "USD"), imp)

# 3. Espera por fallos y peticiones condicionales
pedidos.clear()
noticias.una_vuelta(get=get_falso)
chk("12. Vuelta inmediata: ninguna fuente se vuelve a pedir antes de su intervalo", len(pedidos) == 0, len(pedidos))
c.execute("UPDATE fuentes_estado SET ultimo_intento = ultimo_intento - 70"); c.commit()
pedidos.clear(); noticias.una_vuelta(get=get_falso)
urls = [u for u, _ in pedidos]
chk("13. Pasado el intervalo: se piden las sanas; la rota espera el doble (sigue en pausa)", "https://f/rss1" in urls and "https://f/roto" not in urls, urls)
cab_rss = next(h for u, h in pedidos if u == "https://f/rss1")
chk("14. Peticion condicional con ETag (304 = sin descargar de nuevo)", cab_rss.get("If-None-Match") == '"v1"')

# 4. Traduccion por pares (traductor simulado)
class ParFalso:
    def __init__(self, o, d): self.o, self.d = o, d
    def traducir(self, textos): return [("ROTO ROTO ROTO ROTO ROTO ROTO ROTO" if "Powell" in t else f"[{self.d}] {t}") for t in textos]
    def cerrar(self): pass
hecho = traductor.traducir_pendientes(os.environ["ORDERSBLOCK_NOTICIAS_DB"], crear_par=ParFalso)
chk("15. Traduce en->es, en->pt, es->en, es->pt, un par cada vez", set(hecho) == {"en>es", "en>pt", "es>en", "es>pt"}, hecho)
t = c.execute("SELECT t_es, t_pt, t_en FROM noticias WHERE titulo LIKE 'Russia launches%'").fetchone()
chk("16. Titular en ingles: queda en espanol y portugues, y el ingles es el original", t[0].startswith("[es]") and t[1].startswith("[pt]") and t[2] == "Russia launches drone attack on Kyiv", t)
p = c.execute("SELECT t_es FROM noticias WHERE titulo LIKE 'Fed%'").fetchone()
chk("17. Salida rota del traductor: se guarda el original (no basura, no reintento sin fin)", p[0].startswith("Fed's Powell"), p)
chk("18. Segunda pasada: nada pendiente", traductor.traducir_pendientes(os.environ["ORDERSBLOCK_NOTICIAS_DB"], crear_par=ParFalso) == {})
chk("19. Sin modelos instalados el traductor real no hace nada ni falla", traductor.traducir_pendientes(os.environ["ORDERSBLOCK_NOTICIAS_DB"]) == {})

# 5. Consulta
d = noticias.consultar("es")
r0 = next(x for x in d["noticias"] if x["original"].startswith("Russia"))
chk("20. Consulta en espanol: titular traducido, original aparte y marca 'traducido'", r0["titulo"].startswith("[es]") and r0["traducido"] and r0["idioma_original"] == "en")
bbc = next(x for x in d["noticias"] if x["fuente"] == "BBC Mundo")
chk("21. Fuente en espanol consultada en espanol: sin marca de traducido", not bbc["traducido"] and bbc["titulo"].startswith("Guerra"))
chk("22. Orden: lo mas reciente primero", [x["publicado"] for x in d["noticias"]] == sorted([x["publicado"] for x in d["noticias"]], reverse=True))
chk("23. Filtros: alto impacto, activo XAU y categoria", all(x["impacto"] == 2 for x in noticias.consultar("es", solo_alto=True)["noticias"])
    and all("XAU" in x["activos"] for x in noticias.consultar("es", activo="XAU")["noticias"])
    and all(x["categoria"] == "mercados" for x in noticias.consultar("es", categoria="mercados")["noticias"]))
chk("24. 'desde' trae solo lo nuevo", noticias.consultar("es", desde_id=d["ultimo_id"])["noticias"] == [])

# 6. API
import api
from starlette.testclient import TestClient
with TestClient(api.app) as cli:
    a = cli.get("/noticias?lang=pt&n=5", headers={"Origin": "https://ordersblock.com", "CF-Connecting-IP": "1.1.1.1"})
    j = a.json()
    chk("25. /noticias publico (sin clave), en portugues, CORS solo para ordersblock.com", a.status_code == 200 and j["ok"] and len(j["datos"]["noticias"]) == 5
        and a.headers.get("access-control-allow-origin") == "https://ordersblock.com", a.status_code)
    chk("26. Parametros invalidos: 400", cli.get("/noticias?n=abc", headers={"CF-Connecting-IP": "1.1.1.2"}).status_code == 400)
    e = cli.get("/noticias/estado", headers={"CF-Connecting-IP": "1.1.1.3"}).json()["datos"]
    chk("27. /noticias/estado: fuentes con su error y total", e["total"] >= 6 and any(f["fuente_id"] == "f_rota" and f["error"] for f in e["fuentes"]))
    os.environ_backup = noticias.RUTA_DB
    noticias.RUTA_DB = os.path.join(TMP, "no_hay.db"); api._cache_noticias.clear()
    v = cli.get("/noticias?lang=es&n=7", headers={"CF-Connecting-IP": "1.1.1.4"}).json()
    chk("28. Sin base de noticias (servicio sin arrancar): lista vacia, sin error y sin crear el archivo", v["ok"] and v["datos"]["noticias"] == [] and not os.path.exists(noticias.RUTA_DB))
    noticias.RUTA_DB = os.environ_backup

# 7. Instalacion de modelos: nada fuera de la carpeta
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w") as z:
    z.writestr("pkg/metadata.json", "{}"); z.writestr("../../fuera.txt", "x")
try:
    traductor._extraer_seguro(buf.getvalue(), os.path.join(TMP, "m"))
    chk("29. Paquete con rutas '../' rechazado", False)
except ValueError:
    chk("29. Paquete con rutas '../' rechazado", not os.path.exists(os.path.join(TMP, "..", "fuera.txt")))

print("\nTODO OK" if ok else "\nHAY FALLAS"); sys.exit(0 if ok else 1)
