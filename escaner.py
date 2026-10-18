"""
Escáner de oportunidades: recorre un universo de mercados en segundo plano y
guarda el último análisis de cada uno. Cuando el cliente consulta un mercado
sin ventaja, la API devuelve dónde SÍ la hay ahora (sin volver a calcular).

Reglas:
- Un mercado a la vez, con pausa entre cada uno: respeta el límite por minuto
  del proveedor de velas (Twelve Data gratis: 8/min) y no le quita CPU a lo
  que comparte el droplet.
- No registra señales en la memoria (registrar=False): solo el análisis que el
  cliente pide de verdad entra al ciclo de verificación.
- Nunca lanza: un mercado que falla se anota con su error y se sigue.
"""

import json
import logging
import os
import threading
import time

import servicio

logger = logging.getLogger("ordersblock.escaner")

UNIVERSO_DEFECTO = ("BTC,ETH,SOL,XRP,BNB,ADA,DOGE,LINK,AVAX,"
                    "XAUUSD,XAGUSD,EURUSD,GBPUSD,USDJPY,AUDUSD,USDCAD,USDCHF,NZDUSD,EURJPY,GBPJPY")

CADA_SEG = int(os.getenv("ORDERSBLOCK_ESCANER_CADA_SEG", "900"))
PAUSA_SEG = float(os.getenv("ORDERSBLOCK_ESCANER_PAUSA_SEG", "9"))
ARCHIVO = os.getenv("ORDERSBLOCK_ESCANER_ARCHIVO",
                    os.path.join(os.path.dirname(os.getenv("ORDERSBLOCK_DB", "memoria_ordersblock.db")) or ".",
                                 "escaner.json"))

# Umbrales de "oportunidad": esperanza positiva y consenso suficiente.
ESPERANZA_MIN = float(os.getenv("ORDERSBLOCK_ESCANER_ESPERANZA_MIN", "0.05"))
CONSENSO_MIN = float(os.getenv("ORDERSBLOCK_ESCANER_CONSENSO_MIN", "55"))

_lock = threading.Lock()
_estado: dict = {"actualizado": None, "ciclo_seg": None, "mercados": {}}
_hilo = None
_parar = threading.Event()


def universo() -> list:
    crudo = os.getenv("ORDERSBLOCK_ESCANER_MERCADOS", UNIVERSO_DEFECTO)
    out = []
    for m in crudo.split(","):
        m = m.strip().upper()
        if not m:
            continue
        try:
            n = servicio.engine.normalizar_mercado(m)
        except Exception:
            continue
        if n in servicio.engine.MERCADO_A_SYMBOL and n not in out:
            out.append(n)
    return out


def _resumir(mercado: str, d: dict) -> dict:
    plan = d.get("plan") or {}
    objs = plan.get("objetivos") or []
    return {
        "mercado": mercado,
        "direccion": d.get("direccion"),
        "consenso_pct": d.get("consenso_pct"),
        "coincidencias": d.get("coincidencias"),
        "calidad": d.get("calidad"),
        "precio": d.get("precio_actual"),
        "entrada": plan.get("entrada"),
        "sl": plan.get("sl"),
        "tp1": objs[0].get("precio") if objs else None,
        "rr": plan.get("rr"),
        "prob_tp_pct": plan.get("prob_tp_antes_que_sl_pct"),
        "esperanza_r": plan.get("esperanza_r"),
        "volatilidad": d.get("volatilidad"),
        "smc_score": (d.get("smc") or {}).get("score"),
        "sesion": (d.get("sesion") or {}).get("nombre") if isinstance(d.get("sesion"), dict) else d.get("sesion"),
        "actualizado": time.time(),
        "error": None,
    }


def es_oportunidad(r: dict) -> bool:
    if not r or r.get("error"):
        return False
    if r.get("direccion") not in ("ALCISTA", "BAJISTA"):
        return False
    esp, cons = r.get("esperanza_r"), r.get("consenso_pct")
    if esp is None or cons is None:
        return False
    if r.get("entrada") is None or r.get("sl") is None or r.get("tp1") is None:
        return False
    return esp >= ESPERANZA_MIN and cons >= CONSENSO_MIN


def puntuacion(r: dict) -> float:
    """Ordena: esperanza ponderada por consenso; desempata por probabilidad de TP."""
    return (r.get("esperanza_r") or 0) * ((r.get("consenso_pct") or 0) / 100.0) \
        + 0.001 * (r.get("prob_tp_pct") or 0)


def analizar_uno(mercado: str) -> dict:
    try:
        d = servicio.analizar(mercado, "mercado", con_fundamental=False, registrar=False)
        return _resumir(mercado, d)
    except servicio.ErrorServicio as e:
        return {"mercado": mercado, "error": e.mensaje, "actualizado": time.time()}
    except Exception as e:  # noqa: BLE001
        return {"mercado": mercado, "error": f"{type(e).__name__}: {e}", "actualizado": time.time()}


def ciclo() -> dict:
    t0 = time.time()
    for m in universo():
        if _parar.is_set():
            break
        r = analizar_uno(m)
        with _lock:
            _estado["mercados"][m] = r
            _estado["actualizado"] = time.time()
        if r.get("error"):
            logger.info(f"escaner {m}: {r['error'][:80]}")
        _guardar()
        _parar.wait(PAUSA_SEG)
    with _lock:
        _estado["ciclo_seg"] = round(time.time() - t0)
    _guardar()
    return dict(_estado)


def oportunidades(excluir: str = None, n: int = 5) -> dict:
    with _lock:
        mercados = dict(_estado["mercados"])
        actualizado = _estado["actualizado"]
    ex = (excluir or "").upper()
    ops = [r for m, r in mercados.items() if m != ex and es_oportunidad(r)]
    ops.sort(key=puntuacion, reverse=True)
    ahora = time.time()
    for r in ops:
        r["hace_min"] = round((ahora - (r.get("actualizado") or ahora)) / 60)
    return {
        "actualizado_hace_min": round((ahora - actualizado) / 60) if actualizado else None,
        "escaneados": len(mercados),
        "con_error": sum(1 for r in mercados.values() if r.get("error")),
        "sin_ventaja": sum(1 for r in mercados.values() if not r.get("error") and not es_oportunidad(r)),
        "umbral": {"esperanza_min": ESPERANZA_MIN, "consenso_min": CONSENSO_MIN},
        "oportunidades": ops[:max(1, min(int(n or 5), 20))],
        "aviso_legal": "Análisis probabilístico. No es asesoría financiera.",
    }


def _guardar():
    try:
        with _lock:
            copia = json.dumps(_estado)
        tmp = ARCHIVO + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(copia)
        os.replace(tmp, ARCHIVO)
    except Exception as e:  # noqa: BLE001
        logger.debug(f"escaner: no se pudo guardar ({e})")


def _cargar():
    try:
        with open(ARCHIVO, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("mercados"), dict):
            with _lock:
                _estado.update(d)
    except Exception:
        pass


def _bucle():
    _parar.wait(45)      # deja arrancar la API y el verificador primero
    while not _parar.is_set():
        try:
            ciclo()
        except Exception as e:  # noqa: BLE001
            logger.error(f"escaner: {type(e).__name__}: {e}")
        _parar.wait(CADA_SEG)


def arrancar():
    global _hilo
    if _hilo:
        return
    _cargar()
    _parar.clear()
    _hilo = threading.Thread(target=_bucle, name="escaner", daemon=True)
    _hilo.start()
    logger.info(f"escaner activo: {len(universo())} mercados cada {CADA_SEG}s (pausa {PAUSA_SEG}s)")


def detener():
    _parar.set()
