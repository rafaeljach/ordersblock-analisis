"""
CBS Quantum Predictor — Score SMC institucional (adaptado de ICC Sniper V16.2)
==============================================================================
Sistema de puntuación sobre 13 puntos que actúa como ÁRBITRO DE CALIDAD
ESTRUCTURAL de una señal. Portado del ICC Sniper V16.2, que opera con
~70% de acierto en XAUUSD y forex.

QUÉ HACE Y QUÉ NO
-----------------
El consenso de CBS (14 señales ponderadas) decide la DIRECCIÓN.
Este score decide si la ESTRUCTURA merece operarse. Son preguntas
distintas y deliberadamente no se mezclan:

  · Si el score se sumara al consenso, componentes como BOS, sweeps u
    Order Blocks contarían DOS VECES —ya están en el consenso— y
    desequilibrarían una ponderación calibrada sobre 1.100 setups.
  · Como filtro independiente, aporta lo que al consenso le falta: una
    lectura de calidad estructural al estilo institucional, con la misma
    escala y los mismos umbrales que Rafael ya validó en producción.

ADAPTACIONES RESPECTO AL ORIGINAL (y por qué)
---------------------------------------------
El ICC original usa EMA 9/21 (+1) y Golden/Death Cross EMA 50/200 (+2).
Aquí se sustituyen por pendiente de regresión sobre las MISMAS ventanas,
conservando los mismos puntos. No es purismo: el 8 de agosto se eliminó
la EMA 20/50 del motor de CBS porque una media de 50 velas tarda ~25
horas en detectar un giro, y eso costó una operación real en XRP (el
momentum marcaba −0.863 mientras el precio llevaba 10 horas subiendo un
2.94%). Repetir aquí el mismo error anularía esa corrección.

La regresión mide lo mismo —alineación de corto plazo y cruce de
tendencias de medio plazo— sin el retraso estructural de la media.

Los demás componentes se leen de funciones que YA existen en
real_engine, así que no se recalcula nada dos veces.
"""

import logging
import numpy as np

logger = logging.getLogger("icc_score")

# --- Puntos por componente (idénticos al ICC Sniper V16.2) ---
PUNTOS = {
    "bos": 4,
    "sweep": 3,
    "order_block": 2,
    "volumen": 2,
    "adx": 1,
    "alineacion_corta": 1,      # sustituye EMA 9/21
    "htf": 1,
    "cruce_medio": 2,           # sustituye Golden/Death Cross
    "sesion": 1,                # +1 Londres/NY, −1 Asia
    "po3": -2,                  # penalización por recorrido agotado
}
SCORE_MAXIMO = 13

# Umbral mínimo por horizonte. El original los define por timeframe
# (M5→8, M15→8, H1→9, H4→10); CBS trabaja en velas de 1h con horizonte
# de 24h, que equivale al H1 del original.
SCORE_MINIMO_DEFECTO = 9

ADX_MINIMO = 22.0               # InpAdxMin del original
PO3_UMBRAL_ADR = 0.70           # 70% del recorrido diario ya consumido


def calcular_adx(maximos, minimos, cierres, periodo: int = 14) -> float:
    """
    ADX de Wilder, con suavizado por media simple.

    Wilder lo definió con su propio suavizado (equivalente a una media
    exponencial), pero aquí se usa media simple: el ADX solo mide FUERZA
    de tendencia, no dirección, así que el retraso no puede invertir una
    señal — que era el problema real de las EMAs direccionales.
    """
    h, l, c = np.asarray(maximos, float), np.asarray(minimos, float), np.asarray(cierres, float)
    n = len(c)
    if n < periodo * 2 + 2:
        return 0.0

    tr = np.maximum.reduce([h[1:] - l[1:], np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])])
    up = h[1:] - h[:-1]
    dn = l[:-1] - l[1:]
    dm_pos = np.where((up > dn) & (up > 0), up, 0.0)
    dm_neg = np.where((dn > up) & (dn > 0), dn, 0.0)

    # Suavizado de Wilder ACUMULATIVO (no media móvil simple sobre cada
    # ventana). La diferencia importa: con media simple, una serie en
    # rango con oscilación regular daba ADX 91 —marcándola como tendencia
    # fortísima— porque cada tramo entre giros es direccional. Wilder
    # acumula, y así los giros se compensan entre sí como deben.
    def wilder(x, p):
        if len(x) < p:
            return np.array([])
        out = np.empty(len(x) - p + 1)
        out[0] = np.sum(x[:p])
        for i in range(1, len(out)):
            out[i] = out[i - 1] - out[i - 1] / p + x[p + i - 1]
        return out

    tr_s = wilder(tr, periodo)
    dmp_s = wilder(dm_pos, periodo)
    dmn_s = wilder(dm_neg, periodo)
    if len(tr_s) == 0:
        return 0.0
    tr_s = np.where(tr_s == 0, 1e-12, tr_s)
    di_pos = 100.0 * dmp_s / tr_s
    di_neg = 100.0 * dmn_s / tr_s
    suma = np.where((di_pos + di_neg) == 0, 1e-12, di_pos + di_neg)
    dx = 100.0 * np.abs(di_pos - di_neg) / suma
    if len(dx) < periodo:
        return float(np.mean(dx)) if len(dx) else 0.0
    # ADX = suavizado de Wilder del DX
    adx = np.mean(dx[:periodo])
    for i in range(periodo, len(dx)):
        adx = (adx * (periodo - 1) + dx[i]) / periodo
    return float(adx)


def _pendiente(serie) -> float:
    """Pendiente de regresión normalizada por precio (fracción por vela)."""
    s = np.asarray(serie, float)
    if len(s) < 3 or s[-1] == 0:
        return 0.0
    x = np.arange(len(s), dtype=float)
    return float(np.polyfit(x, s, 1)[0] / s[-1])


def alineacion_corta(cierres, alcista: bool) -> bool:
    """
    Sustituto de "EMA 9 vs EMA 21 alineadas".

    Compara la pendiente de las últimas 9 velas con la de las últimas 21:
    si ambas van en la dirección de la señal y la corta es más fuerte que
    la larga, el movimiento se está acelerando — que es exactamente lo
    que un cruce de EMAs rápidas pretende capturar, pero sin arrastrar
    veintiún velas de historia en el cálculo.
    """
    c = np.asarray(cierres, float)
    if len(c) < 25:
        return False
    p9, p21 = _pendiente(c[-9:]), _pendiente(c[-21:])
    if alcista:
        return p9 > 0 and p21 > 0 and p9 >= p21
    return p9 < 0 and p21 < 0 and p9 <= p21


def cruce_medio(cierres, alcista: bool) -> bool:
    """
    Sustituto del Golden/Death Cross (EMA 50 × EMA 200).

    El cruce clásico avisa cuando la tendencia de medio plazo cambia de
    régimen. Aquí se detecta comparando la pendiente de 50 velas contra
    la de 200: el cruce ocurre cuando la de 50 supera a la de 200 en la
    dirección de la señal Y la de 50 apunta a ese lado.

    Con medias, ese cruce llega decenas de velas tarde por construcción.
    Con pendientes se detecta en cuanto el medio plazo cambia de ritmo.
    """
    c = np.asarray(cierres, float)
    if len(c) < 120:
        return False
    p50, p200 = _pendiente(c[-50:]), _pendiente(c[-min(200, len(c)):])
    if alcista:
        return p50 > 0 and p50 > p200
    return p50 < 0 and p50 < p200


def penalizacion_po3(maximos, minimos, cierres) -> bool:
    """
    ¿El recorrido del día ya está agotado? (Power of Three del original)

    Si el precio ya recorrió más del 70% de su rango diario típico, lo que
    queda de movimiento es poco y entrar ahí es perseguir el mercado. Es
    el mismo concepto que el ADR del ICC, medido sobre las últimas 24
    velas de 1h.
    """
    h, l, c = np.asarray(maximos, float), np.asarray(minimos, float), np.asarray(cierres, float)
    if len(c) < 120:
        return False
    # Rango diario típico: media de los rangos de los últimos 5 bloques de 24h
    rangos = [float(np.max(h[-(i + 1) * 24:len(h) - i * 24]) -
                    np.min(l[-(i + 1) * 24:len(l) - i * 24]))
              for i in range(1, 6) if len(h) >= (i + 1) * 24]
    if not rangos:
        return False
    adr = float(np.mean(rangos))
    if adr <= 0:
        return False
    recorrido_hoy = float(np.max(h[-24:]) - np.min(l[-24:]))
    return (recorrido_hoy / adr) >= PO3_UMBRAL_ADR


def calcular_score(datos: dict, direccion: str, sesion: dict = None) -> dict:
    """
    Score SMC de 0 a 13, leyendo los componentes que el motor YA calculó.

    No recalcula BOS, sweeps ni Order Blocks: los toma de `datos`, que
    vienen de real_engine. Así el score y el consenso miran exactamente
    la misma estructura, en vez de dos lecturas que podrían discrepar.
    """
    alcista = (direccion or "").upper() == "ALCISTA"
    velas = datos.get("velas") or []
    if len(velas) < 60:
        return {"disponible": False, "score": 0, "detalle": [],
                "texto": "histórico insuficiente para el score SMC"}

    cierres = np.array([v["close"] for v in velas], float)
    maximos = np.array([v["high"] for v in velas], float)
    minimos = np.array([v["low"] for v in velas], float)

    score = 0
    detalle = []

    # 1) BOS (+4) — el componente de más peso: sin ruptura de estructura
    #    confirmada no hay tesis institucional que sostener.
    ob = datos.get("ob_bos", {}) or {}
    bos_ok = (ob.get("bos_detectado") and
              (ob.get("bos_direccion") == ("ALCISTA" if alcista else "BAJISTA")))
    if bos_ok:
        score += PUNTOS["bos"]
        detalle.append(f"✅ BOS {ob.get('bos_direccion')} (+{PUNTOS['bos']})")
    else:
        detalle.append("⚪ Sin BOS en la dirección (+0)")

    # 2) Sweep de liquidez (+3) — barrido antes del movimiento. En CBS lo
    #    detectan turtle_soup (falso quiebre de swing) y CRT (barrida de
    #    rango); cualquiera de los dos vale.
    ts = datos.get("turtle_soup", {}) or {}
    crt = datos.get("crt", {}) or {}
    signo = 1 if alcista else -1
    sweep_ok = ((ts.get("score", 0) * signo > 0.1) or (crt.get("score", 0) * signo > 0.1))
    if sweep_ok:
        score += PUNTOS["sweep"]
        detalle.append(f"✅ Sweep de liquidez (+{PUNTOS['sweep']})")
    else:
        detalle.append("⚪ Sin barrido de liquidez previo (+0)")

    # 3) Order Block válido (+2)
    ob_ok = bool(ob.get("bos_detectado") and ob.get("ob_zona_min") and
                 ob.get("tipo_retest") in ("liquidity_sweep", "continuation"))
    if ob_ok:
        score += PUNTOS["order_block"]
        detalle.append(f"✅ Order Block con retest {ob.get('tipo_retest')} "
                       f"(+{PUNTOS['order_block']})")
    else:
        detalle.append("⚪ Order Block sin confirmar (+0)")

    # 4) Volumen institucional (+2) — Bybit sí lo entrega; en forex y
    #    metales suele venir vacío y el componente queda neutro.
    vol = datos.get("volumen", {}) or {}
    vol_ok = bool(vol.get("disponible") and vol.get("score", 0) * signo > 0.1)
    if vol_ok:
        score += PUNTOS["volumen"]
        detalle.append(f"✅ Volumen institucional (+{PUNTOS['volumen']})")
    elif not vol.get("disponible"):
        detalle.append("⚪ Volumen: sin datos (+0)")
    else:
        detalle.append("⚪ Volumen sin confirmar (+0)")

    # 5) ADX (+1) — mercado con tendencia definida, no en rango
    adx = calcular_adx(maximos, minimos, cierres)
    if adx >= ADX_MINIMO:
        score += PUNTOS["adx"]
        detalle.append(f"✅ ADX {adx:.1f} ≥ {ADX_MINIMO:.0f} (+{PUNTOS['adx']})")
    else:
        detalle.append(f"⚪ ADX {adx:.1f} (rango) (+0)")

    # 6) Alineación corta (+1) — sustituye EMA 9/21
    if alineacion_corta(cierres, alcista):
        score += PUNTOS["alineacion_corta"]
        detalle.append(f"✅ Momentum corto alineado (+{PUNTOS['alineacion_corta']})")
    else:
        detalle.append("⚪ Momentum corto no alineado (+0)")

    # 7) HTF alineado (+1)
    s4 = float(datos.get("score_4h", 0.0) or 0.0)
    if s4 * signo >= 0.25:
        score += PUNTOS["htf"]
        detalle.append(f"✅ Marco superior alineado (+{PUNTOS['htf']})")
    else:
        detalle.append("⚪ Marco superior sin confirmar (+0)")

    # 8) Cruce de medio plazo (+2) — sustituye Golden/Death Cross
    if cruce_medio(cierres, alcista):
        score += PUNTOS["cruce_medio"]
        detalle.append(f"✅ Cruce de tendencia media (+{PUNTOS['cruce_medio']})")
    else:
        detalle.append("⚪ Sin cruce de medio plazo (+0)")

    # 9) Sesión institucional (+1 / −1)
    ses = (sesion or {}).get("clave", "")
    if ses in ("londres", "solape", "newyork"):
        score += PUNTOS["sesion"]
        detalle.append(f"✅ Sesión institucional (+{PUNTOS['sesion']})")
    elif ses == "asiatica":
        score -= PUNTOS["sesion"]
        detalle.append(f"⚠️ Sesión asiática (−{PUNTOS['sesion']})")

    # 10) Penalización PO3 (−2) — recorrido diario agotado
    if penalizacion_po3(maximos, minimos, cierres):
        score += PUNTOS["po3"]
        detalle.append(f"⚠️ Recorrido diario agotado (>{PO3_UMBRAL_ADR*100:.0f}% ADR) "
                       f"({PUNTOS['po3']})")

    score = max(0, min(SCORE_MAXIMO, score))
    return {
        "disponible": True,
        "score": score,
        "maximo": SCORE_MAXIMO,
        "adx": round(adx, 1),
        "detalle": detalle,
        "calidad": clasificar(score),
        "texto": f"Score SMC {score}/{SCORE_MAXIMO} — {clasificar(score)}",
    }


def clasificar(score: int) -> str:
    """Etiqueta de calidad, con la misma escala que usa el ICC Analyzer."""
    if score >= 11:
        return "EXCELENTE"
    if score >= 9:
        return "BUENO"
    if score >= 7:
        return "ACEPTABLE"
    if score >= 5:
        return "DÉBIL"
    return "INSUFICIENTE"
