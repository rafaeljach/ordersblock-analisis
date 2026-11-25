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
- Una pasada del motor por mercado y DOS planes: a mercado y en zona (OB, FVG,
  zona cero, Mirrofish). Antes solo miraba la entrada a mercado, cuya esperanza
  por Monte Carlo sale casi siempre ~0 menos la comisión: en la práctica el
  escáner no encontraba nunca nada (0 planes en sombra en toda su historia).
- Dos puertas distintas:
    · oportunidad (lo que ve el cliente): la de siempre, sin cambios.
    · candidato en sombra (?sombra=1): filtro estructural más amplio, solo para
      registrarlo y verificarlo con precio real. Los números deciden después.
- Diagnóstico: cada mercado queda con el motivo por el que se descartó.
- Nunca lanza: un mercado que falla se anota con su error y se sigue.
"""

import json
import logging
import math
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
# Candidatos en sombra: 2 de 4 componentes del consenso, R:R mínimo del sistema y
# esperanza positiva después de la comisión. No se muestran al cliente.
SOMBRA_CONSENSO_MIN = float(os.getenv("ORDERSBLOCK_ESCANER_SOMBRA_CONSENSO_MIN", "50"))
SOMBRA_RR_MIN = float(os.getenv("ORDERSBLOCK_ESCANER_SOMBRA_RR_MIN", "1.3"))

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


def _resumir_zona(p: dict) -> dict:
    p = p or {}
    if not p.get("disponible"):
        return {"disponible": False, "motivo": (p.get("motivo") or "")[:160],
                "esperanza_r": p.get("esperanza_r")}
    objs = p.get("objetivos") or []
    return {"disponible": True, "entrada": p.get("entrada"), "sl": p.get("sl"),
            "tp1": objs[0].get("precio") if objs else None, "rr": p.get("rr"),
            "esperanza_r": p.get("esperanza_r"), "prob_tp_pct": p.get("prob_tp_pct"),
            "origen_zona": p.get("origen_zona"), "distancia_pct": p.get("distancia_pct")}


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
        "liquidez": (d.get("sesion") or {}).get("liquidez") if isinstance(d.get("sesion"), dict) else None,
        "zona": _resumir_zona(d.get("plan_precision")),
        "actualizado": time.time(),
        "error": None,
    }


def es_oportunidad(r: dict) -> bool:
    if not r or r.get("error"):
        return False
    if r.get("direccion") not in ("ALCISTA", "BAJISTA"):
        return False
    esp, cons = _num(r.get("esperanza_r")), _num(r.get("consenso_pct"))   # mismos umbrales; un texto ya no revienta
    if esp is None or cons is None:
        return False
    if r.get("entrada") is None or r.get("sl") is None or r.get("tp1") is None:
        return False
    return esp >= ESPERANZA_MIN and cons >= CONSENSO_MIN


def _num(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _plan_sombra_valido(p: dict, direccion: str, precio=None, zona: bool = False) -> bool:
    """Plan medible: numeros finitos, geometria del lado correcto, R:R y esperanza suficientes."""
    if not p:
        return False
    e, sl, tp, esp, rr = (_num(p.get(k)) for k in ("entrada", "sl", "tp1", "esperanza_r", "rr"))
    if None in (e, sl, tp, esp, rr):
        return False
    alcista = direccion == "ALCISTA"
    if not ((sl < e < tp) if alcista else (tp < e < sl)):
        return False
    pr = _num(precio)
    if zona and pr is not None and (e > pr * 1.0005 if alcista else e < pr * 0.9995):
        return False   # orden limite del lado equivocado del precio: no es una zona de retroceso
    return esp > 0 and rr >= SOMBRA_RR_MIN


def motivo_descarte(r: dict) -> str:
    """Por qué un mercado no es candidato (o 'candidato'). Sirve al diagnóstico."""
    if not r or r.get("error"):
        return "error de datos"
    if r.get("direccion") not in ("ALCISTA", "BAJISTA"):
        return "sin direccion"
    if r.get("liquidez") == "CERRADO":
        return "mercado cerrado"
    if (_num(r.get("consenso_pct")) or 0) < SOMBRA_CONSENSO_MIN:
        return "consenso bajo"
    z = r.get("zona") or {}
    d = r.get("direccion")
    if z.get("disponible") and _plan_sombra_valido(z, d, r.get("precio"), zona=True):
        return "candidato"
    if _plan_sombra_valido(r, d):
        return "candidato"
    if not z.get("disponible") and (_num(r.get("esperanza_r")) or 0) <= 0:
        return "sin zona y esperanza a mercado <= 0"
    return "R:R o esperanza insuficientes"


def candidato_sombra(r: dict):
    """Plan para registrar en sombra, o None. Prefiere la entrada en zona."""
    if motivo_descarte(r) != "candidato":
        return None
    z = r.get("zona") or {}
    base = {"mercado": r["mercado"], "direccion": r["direccion"], "consenso_pct": _num(r.get("consenso_pct")),
            "smc_score": r.get("smc_score"), "precio": r.get("precio"), "sombra": True,
            "actualizado": r.get("actualizado")}
    if z.get("disponible") and _plan_sombra_valido(z, r["direccion"], r.get("precio"), zona=True):
        return {**base, "tipo_plan": "zona", "entrada": _num(z["entrada"]), "sl": _num(z["sl"]), "tp1": _num(z["tp1"]),
                "rr": _num(z.get("rr")), "esperanza_r": _num(z.get("esperanza_r")), "prob_tp_pct": _num(z.get("prob_tp_pct")),
                "origen_zona": z.get("origen_zona")}
    return {**base, "tipo_plan": "mercado", "entrada": _num(r["entrada"]), "sl": _num(r["sl"]), "tp1": _num(r["tp1"]),
            "rr": _num(r.get("rr")), "esperanza_r": _num(r.get("esperanza_r")), "prob_tp_pct": _num(r.get("prob_tp_pct"))}


def puntuacion(r: dict) -> float:
    """Ordena: esperanza ponderada por consenso; desempata por probabilidad de TP."""
    return (_num(r.get("esperanza_r")) or 0) * ((_num(r.get("consenso_pct")) or 0) / 100.0) \
        + 0.001 * (_num(r.get("prob_tp_pct")) or 0)


def analizar_uno(mercado: str) -> dict:
    try:
        d = servicio.analizar_escaner(mercado)
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


def diagnostico(mercados: dict) -> dict:
    out = {}
    for r in mercados.values():
        try:
            k = motivo_descarte(r)
        except Exception:  # noqa: BLE001 — un estado guardado raro no tumba la ruta
            k = "estado invalido"
        out[k] = out.get(k, 0) + 1
    return out


def oportunidades(excluir: str = None, n: int = 5, sombra: bool = False) -> dict:
    with _lock:
        mercados = dict(_estado["mercados"])
        actualizado = _estado["actualizado"]
    ex = (excluir or "").upper()
    if sombra:
        ops = []
        for m, r in mercados.items():
            if m == ex:
                continue
            try:
                c = candidato_sombra(r)
            except Exception:  # noqa: BLE001
                c = None
            if c:
                ops.append(c)
    else:
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
        "umbral": ({"consenso_min": SOMBRA_CONSENSO_MIN, "rr_min": SOMBRA_RR_MIN, "esperanza_min": 0}
                   if sombra else {"esperanza_min": ESPERANZA_MIN, "consenso_min": CONSENSO_MIN}),
        "sombra": bool(sombra),
        "diagnostico": diagnostico(mercados),
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
