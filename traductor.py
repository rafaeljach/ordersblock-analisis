"""
Traductor local y gratuito de titulares (modelos de Argos Translate sobre CTranslate2).

No usa la libreria argostranslate completa (arrastraria torch/stanza, cientos de MB):
solo ctranslate2 + sentencepiece, que es lo que Argos usa por debajo. Un titular es
una sola frase, asi que no hace falta separar oraciones.

Para no quitarle memoria al bot ni a la billetera (mismo droplet), se carga UN modelo
a la vez: se traducen todos los pendientes de ese par y se descarga antes del siguiente.

    python traductor.py instalar   # baja los modelos (indice oficial de Argos) a ./modelos
    python traductor.py probar     # traduce titulares de ejemplo y muestra memoria usada
"""

import gc
import hashlib
import io
import json
import logging
import os
import re
import resource
import shutil
import sqlite3
import sys
import time
import zipfile

import requests

logger = logging.getLogger("ordersblock.traductor")

CARPETA = os.path.dirname(os.path.abspath(__file__))
MODELOS = os.getenv("ORDERSBLOCK_MODELOS", os.path.join(CARPETA, "modelos"))
INDICE = "https://raw.githubusercontent.com/argosopentech/argospm-index/main/index.json"
# Pares que hacen falta: lo que llega en ingles va a espanol y portugues; lo que llega en espanol, a ingles y portugues
PARES = [("en", "es"), ("en", "pt"), ("es", "en"), ("es", "pt")]
LOTE = 24
MAX_POR_PAR = int(os.getenv("ORDERSBLOCK_TRADUCIR_POR_VUELTA", "120"))
MAX_MB_MODELO = 400


def carpeta_par(o: str, d: str) -> str:
    return os.path.join(MODELOS, f"{o}_{d}")


def disponible(o: str, d: str) -> bool:
    c = carpeta_par(o, d)
    return os.path.isdir(os.path.join(c, "model")) and os.path.isfile(os.path.join(c, "sentencepiece.model"))


class Par:
    """Un modelo cargado: traduce listas de titulares de o -> d."""

    def __init__(self, o: str, d: str):
        self.origen = o
        import ctranslate2
        import sentencepiece as spm
        c = carpeta_par(o, d)
        with open(os.path.join(c, "metadata.json"), encoding="utf-8") as f:
            meta = json.load(f)
        self.prefijo = meta.get("target_prefix", "") or ""
        self.sp = spm.SentencePieceProcessor(model_file=os.path.join(c, "sentencepiece.model"))
        self.tr = ctranslate2.Translator(os.path.join(c, "model"), device="cpu", compute_type="int8",
                                         inter_threads=1, intra_threads=int(os.getenv("ORDERSBLOCK_TRADUCTOR_HILOS", "1")))

    def traducir(self, textos: list) -> list:
        tokens = [self.sp.encode(preparar(t, self.origen), out_type=str) for t in textos]
        pref = [[self.prefijo]] * len(tokens) if self.prefijo else None
        res = self.tr.translate_batch(tokens, target_prefix=pref, replace_unknowns=True, max_batch_size=LOTE,
                                      beam_size=2, num_hypotheses=1, length_penalty=0.2, max_decoding_length=160)
        salida = []
        for r in res:
            v = self.sp.decode_pieces(r.hypotheses[0]).replace("▁", " ")
            if self.prefijo and v.startswith(self.prefijo):
                v = v[len(self.prefijo):]
            salida.append(v.strip())
        return salida

    def cerrar(self):
        self.tr = None
        self.sp = None
        gc.collect()


# Glosario de mercado: se reescribe el ingles ANTES de traducir para que el modelo no confunda terminos
# (el titular original se guarda intacto). Ej.: "rate cuts" salia como "recortes de tarifas".
_GLOSARIO_EN = [
    (r"\bFed's\b", "Federal Reserve"), (r"\bthe Fed\b", "the Federal Reserve"), (r"\bFed\b", "Federal Reserve"),
    (r"\brate (cut|cuts|hike|hikes|decision|decisions)\b", r"interest rate \1"),
    (r"\bhits? (a |an )?(fresh |new )?record high\b", "reaches a new record high"),
    (r"\bhits? (a |an )?(fresh |new )?record low\b", "falls to a new record low"),
    (r"\bhits? (a |an )?(\d+[- ](week|month|year)) (high|low)\b", r"reaches a \2 \4"),
    (r"\bsoar(s?)\b", r"rise\1 sharply"), (r"\bplunge(s?)\b", r"fall\1 sharply"), (r"\btumble(s?)\b", r"fall\1 sharply"),
    (r"\bjump(s?)\b", r"rise\1"), (r"\bslump(s?)\b", r"fall\1"), (r"\brallies\b", "rises"), (r"\brally\b", "rise"),
    (r"\bbps\b", "basis points"), (r"\bYoY\b", "year on year"), (r"\bMoM\b", "month on month"),
    (r"\bNFP\b", "US jobs report"), (r"\bCPI\b", "consumer price index"),
]
_RX_GLOSARIO = [(re.compile(a, re.I), b) for a, b in _GLOSARIO_EN]


def preparar(texto: str, origen: str) -> str:
    if origen != "en":
        return texto
    for rx, sustituto in _RX_GLOSARIO:
        texto = rx.sub(sustituto, texto)
    return texto


def _aceptable(original: str, traducido: str) -> bool:
    """Descarta salidas rotas: vacias, repetitivas o absurdamente largas."""
    if not traducido or len(traducido) < 4:
        return False
    if len(traducido) > max(40, 3 * len(original)):
        return False
    palabras = traducido.lower().split()
    return not (len(palabras) >= 6 and len(set(palabras)) <= len(palabras) // 3)


def traducir_pendientes(ruta_db: str, crear_par=Par) -> dict:
    """Traduce lo que falta, un par cada vez. Devuelve {par: cuantos}. Sin modelos no hace nada."""
    hechos = {}
    for o, d in PARES:
        if crear_par is Par and not disponible(o, d):
            continue
        with sqlite3.connect(ruta_db, timeout=15) as c:
            filas = c.execute(f"SELECT id, titulo FROM noticias WHERE idioma=? AND t_{d} IS NULL ORDER BY publicado DESC LIMIT ?",
                              (o, MAX_POR_PAR)).fetchall()
        if not filas:
            continue
        par = None
        try:
            par = crear_par(o, d)
            n = 0
            for i in range(0, len(filas), LOTE):
                lote = filas[i:i + LOTE]
                out = par.traducir([t for _, t in lote])
                with sqlite3.connect(ruta_db, timeout=15) as c:
                    for (nid, original), trad in zip(lote, out):
                        # Si la salida no sirve se guarda el original: no se reintenta en bucle y se muestra sin traducir
                        ok = _aceptable(original, trad)
                        c.execute(f"UPDATE noticias SET t_{d}=? WHERE id=?", (trad if ok else original, nid))
                        n += 1 if ok else 0
            hechos[f"{o}>{d}"] = n
        except Exception as e:
            logger.error(f"traductor {o}>{d}: {type(e).__name__}: {e}")
        finally:
            if par is not None:
                par.cerrar()
            del par
            gc.collect()
    return hechos


# ---------------------------------------------------------------- instalacion de modelos

def _extraer_seguro(datos: bytes, destino: str) -> None:
    """Descomprime el .argosmodel en destino sin permitir rutas fuera de la carpeta."""
    with zipfile.ZipFile(io.BytesIO(datos)) as z:
        nombres = z.namelist()
        raiz = nombres[0].split("/")[0]
        tmp = destino + ".tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp)
        for n in nombres:
            dest = os.path.realpath(os.path.join(tmp, n))
            if not dest.startswith(os.path.realpath(tmp) + os.sep):
                raise ValueError(f"ruta peligrosa en el paquete: {n}")
        z.extractall(tmp)
        origen = os.path.join(tmp, raiz) if os.path.isdir(os.path.join(tmp, raiz)) else tmp
        shutil.rmtree(destino, ignore_errors=True)
        shutil.move(origen, destino)
        shutil.rmtree(tmp, ignore_errors=True)


def instalar(get=requests.get) -> dict:
    os.makedirs(MODELOS, exist_ok=True)
    indice = get(INDICE, timeout=30).json()
    hechos = {}
    for o, d in PARES:
        if disponible(o, d):
            hechos[f"{o}>{d}"] = "ya estaba"
            continue
        pkg = next((p for p in indice if p.get("from_code") == o and p.get("to_code") == d and p.get("type", "translate") == "translate"), None)
        if not pkg:
            hechos[f"{o}>{d}"] = "no existe en el indice"
            continue
        enlace = next((u for u in pkg.get("links", []) if u.startswith("https://")), None)
        r = get(enlace, timeout=300)
        r.raise_for_status()
        datos = r.content
        if len(datos) > MAX_MB_MODELO * 1024 * 1024:
            raise ValueError(f"{enlace}: paquete demasiado grande")
        huella = hashlib.sha256(datos).hexdigest()
        _extraer_seguro(datos, carpeta_par(o, d))
        with open(os.path.join(carpeta_par(o, d), "ORIGEN.txt"), "w") as f:
            f.write(f"{enlace}\nsha256 {huella}\nversion {pkg.get('package_version')}\n")
        hechos[f"{o}>{d}"] = f"instalado v{pkg.get('package_version')} ({len(datos) // 1048576} MB, sha256 {huella[:16]}…)" + \
            ("" if disponible(o, d) else " PERO sin sentencepiece.model: no compatible")
    return hechos


EJEMPLOS = {
    "en": ["Israel strikes Iranian air defenses as oil prices jump 4%",
           "Fed's Powell says rate cuts are not guaranteed in December",
           "Russia launches massive drone attack on Kyiv overnight",
           "Gold hits record high as investors seek safe haven",
           "Houthi rebels claim missile attack on tanker in the Red Sea"],
    "es": ["Rusia y Ucrania acuerdan un alto el fuego de 30 días",
           "El BCE mantiene los tipos de interés sin cambios"],
}


def probar() -> int:
    falta = [f"{o}>{d}" for o, d in PARES if not disponible(o, d)]
    if falta:
        print("Faltan modelos:", ", ".join(falta), "→ ejecuta: python traductor.py instalar")
    ok = 0
    for o, d in PARES:
        if not disponible(o, d):
            continue
        t0 = time.time()
        par = Par(o, d)
        cargado = time.time() - t0
        t1 = time.time()
        out = par.traducir(EJEMPLOS[o])
        print(f"\n== {o} > {d}  (carga {cargado:.1f}s, {len(out)} titulares en {time.time() - t1:.1f}s)")
        for a, b in zip(EJEMPLOS[o], out):
            print(f"  {a}\n   → {b}")
        ok += 1
        par.cerrar()
    pico = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024
    print(f"\nMemoria maxima usada por este proceso: {pico} MB")
    return ok


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    orden = sys.argv[1] if len(sys.argv) > 1 else ""
    if orden == "instalar":
        for k, v in instalar().items():
            print(f"{k}: {v}")
    elif orden == "probar":
        sys.exit(0 if probar() else 1)
    else:
        print(__doc__)
        sys.exit(2)
