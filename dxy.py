"""
Índice del dólar (DXY) para el panel de correlación de la vista Mercado (oro y plata).

Sin costo y sin pasar por el Worker de Cloudflare:
  1. Yahoo Finance (DX-Y.NYB): el índice real del ICE, público y sin clave.
  2. Si Yahoo no responde desde el droplet, se CALCULA con la fórmula oficial del ICE
     con los pares de Kraken (público, sin clave ni cuota diaria):

        DXY = 50.14348112 · EURUSD^-0.576 · USDJPY^0.136 · GBPUSD^-0.119
                          · USDCAD^0.091  · USDSEK^0.042 · USDCHF^0.036

     Kraken no cotiza la corona sueca: USDSEK se estima como EURSEK / EURUSD con
     EURSEK ≈ 11.2 (la corona sigue al euro; con un 4,2 % de peso, el error en el
     nivel es de décimas). Si USDCAD o USDCHF no responden se usa su último valor
     típico; su peso conjunto es 12,7 % y solo afecta al nivel, no a la forma.
     La fuente lo dice ("calculado"), para que nadie lo confunda con el índice real.
"""

import logging
import time

import requests

logger = logging.getLogger("ordersblock.dxy")

YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/DX-Y.NYB"
KRAKEN_OHLC = "https://api.kraken.com/0/public/OHLC"
CABECERAS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) ordersblock-analisis", "Accept": "application/json"}

# Temporalidad del panel (minutos) -> (intervalo Yahoo, rango Yahoo, minutos para agrupar o None)
_YAHOO_TF = {"15": ("15m", "30d", None), "60": ("1h", "60d", None), "240": ("1h", "60d", 240), "1440": ("1d", "1y", None)}
PESOS = {"EURUSD": -0.576, "USDJPY": 0.136, "GBPUSD": -0.119, "USDCAD": 0.091, "USDCHF": 0.036}
TIPICOS = {"USDCAD": 1.37, "USDCHF": 0.88}       # solo si Kraken no da el par
EURSEK = 11.2
CONSTANTE = 50.14348112
CACHE_SEG = 120
_cache: dict = {}


def formula(eurusd, usdjpy, gbpusd, usdcad, usdchf, usdsek=None) -> float:
    """Fórmula oficial del ICE para el U.S. Dollar Index."""
    usdsek = usdsek if usdsek else EURSEK / eurusd
    return (CONSTANTE * eurusd ** -0.576 * usdjpy ** 0.136 * gbpusd ** -0.119
            * usdcad ** 0.091 * usdsek ** 0.042 * usdchf ** 0.036)


def _agrupar(velas, minutos):
    """Agrupa velas en bloques de `minutos` alineados a la hora UTC (H4 = 00, 04, 08...)."""
    out, bloque = [], None
    for v in velas:
        k = v["t"] // (minutos * 60000)
        if bloque is None or bloque["k"] != k:
            if bloque:
                out.append({x: bloque[x] for x in ("t", "o", "h", "l", "c")})
            bloque = {"k": k, "t": k * minutos * 60000, "o": v["o"], "h": v["h"], "l": v["l"], "c": v["c"]}
        else:
            bloque["h"] = max(bloque["h"], v["h"])
            bloque["l"] = min(bloque["l"], v["l"])
            bloque["c"] = v["c"]
    if bloque:
        out.append({x: bloque[x] for x in ("t", "o", "h", "l", "c")})
    return out


def desde_yahoo(tf: str, get=requests.get):
    iv, rango, agrupar = _YAHOO_TF[tf]
    d = get(YAHOO, params={"interval": iv, "range": rango}, headers=CABECERAS, timeout=10).json()
    res = ((d.get("chart") or {}).get("result") or [None])[0]
    if not res:
        raise ValueError("Yahoo sin resultado")
    q = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    velas = []
    for i, ts in enumerate(res.get("timestamp") or []):
        try:
            o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        except (KeyError, IndexError):
            continue
        if None in (o, h, l, c) or c <= 0:
            continue
        velas.append({"t": int(ts) * 1000, "o": float(o), "h": float(h), "l": float(l), "c": float(c)})
    if agrupar:
        velas = _agrupar(velas, agrupar)
    if len(velas) < 30:
        raise ValueError(f"Yahoo dio solo {len(velas)} velas")
    return velas, "yahoo:DX-Y.NYB"


def desde_kraken(tf: str, get=requests.get):
    series = {}
    for par in PESOS:
        try:
            d = get(KRAKEN_OHLC, params={"pair": par, "interval": int(tf)}, headers=CABECERAS, timeout=10).json()
            res = d.get("result") or {}
            filas = next((v for k, v in res.items() if k != "last" and isinstance(v, list)), None)
            if d.get("error") or not filas:
                raise ValueError(str(d.get("error")))
            series[par] = {int(f[0]) * 1000: (float(f[1]), float(f[4])) for f in filas}
        except Exception as e:
            if par in TIPICOS:
                logger.info(f"DXY calculado: {par} no disponible en Kraken ({e}); se usa su valor típico")
                continue
            raise ValueError(f"Kraken sin {par}: {e}")
    comunes = sorted(set.intersection(*(set(s) for s in series.values())))
    velas = []
    for t in comunes:
        def val(par, j):
            return series[par][t][j] if par in series else TIPICOS[par]
        o = formula(val("EURUSD", 0), val("USDJPY", 0), val("GBPUSD", 0), val("USDCAD", 0), val("USDCHF", 0))
        c = formula(val("EURUSD", 1), val("USDJPY", 1), val("GBPUSD", 1), val("USDCAD", 1), val("USDCHF", 1))
        velas.append({"t": t, "o": round(o, 3), "h": round(max(o, c), 3), "l": round(min(o, c), 3), "c": round(c, 3)})
    if len(velas) < 30:
        raise ValueError(f"solo {len(velas)} velas alineadas")
    return velas, "calculado: formula ICE con pares de Kraken"


def velas_dxy(tf: str, n: int, get=requests.get) -> dict:
    """Velas del DXY (Yahoo; si falla, calculado con Kraken). Cache de 2 minutos por temporalidad."""
    if tf not in _YAHOO_TF:
        raise ValueError("tf debe ser 15, 60, 240 o 1440")
    ahora = time.time()
    hit = _cache.get(tf)
    if hit and ahora - hit[0] < CACHE_SEG:
        velas, fuente = hit[1]
    else:
        try:
            velas, fuente = desde_yahoo(tf, get)
        except Exception as e:
            logger.info(f"DXY: Yahoo no sirvió ({type(e).__name__}: {e}); se calcula con Kraken")
            try:
                velas, fuente = desde_kraken(tf, get)
            except Exception:
                if hit:                               # último dato bueno antes que nada
                    velas, fuente = hit[1]
                else:
                    raise
        _cache[tf] = (ahora, (velas, fuente))
    v = velas[-int(n):]
    return {"mercado": "DXY", "tf": int(tf), "velas": v, "precio_actual": v[-1]["c"], "fuente": fuente,
            "actualizado_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
