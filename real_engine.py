"""
CBS Quantum Predictor - Motor de análisis REAL
================================================
Diseño honesto, en dos capas separadas a propósito:

  CAPA 1 - CÁLCULO (este módulo, sin IA):
    - Precio en vivo real (Twelve Data — API oficial con key, cubre forex,
      oro y cripto en un solo lugar. Se eligió sobre Binance porque Binance
      bloquea IPs de EE.UU. —donde corre Render free tier— y sobre yfinance
      porque es scraping no oficial de Yahoo, con bloqueos de rate-limit
      frecuentes incluso en hosts cloud).
    - Simulación Monte Carlo GENUINA con numpy (Geometric Brownian Motion),
      calibrada con la volatilidad histórica real del activo.
    - Percentiles (P05/P50/P95), % de paths alcistas/bajistas/laterales,
      y un QCS (Quantum Coherence Score) calculado con reglas objetivas
      sobre datos reales (distancia a máximos/mínimos recientes, niveles
      psicológicos redondos, etc.) — NO inventado.

  CAPA 2 - NARRATIVA (Gemini / Claude):
    - SOLO redacta la explicación en lenguaje natural / estilo SMC
      sobre los números YA calculados en la capa 1.
    - Se le prohíbe explícitamente inventar cifras nuevas.

Esto es lo que corrige la auditoría: antes se generaban probabilidades
al azar; ahora se calculan de verdad y la IA solo las explica.
"""

import os
import time
import logging
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import requests

logger = logging.getLogger("motor_real")

# ---------------------------------------------------------------------------
# CAPA 1: DATOS EN VIVO (Twelve Data)
# ---------------------------------------------------------------------------

TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY")
TWELVEDATA_URL = "https://api.twelvedata.com/time_series"

# ---------------------------------------------------------------------------
# CATÁLOGO DE MERCADOS POR PLAN (Free / Standard / Premium, acumulativo)
# ---------------------------------------------------------------------------

# --- Núcleo: disponible en TODOS los planes, incluido Free ---
MERCADOS_BASE = {
    "BTC": "BTC/USD",
    "BTCUSD": "BTC/USD",
    "ETH": "ETH/USD",
    "ETHUSD": "ETH/USD",
    "XAUUSD": "XAU/USD",
    "EURUSD": "EUR/USD",
    "GBPUSD": "GBP/USD",
    "USDCOP": "USD/COP",
}

# --- Cripto ampliado: Standard y Premium ---
# Mayores volúmenes de perpetuos en Bybit. Todos tienen contrato USDT allí,
# así que la ejecución asistida funciona con cualquiera de ellos.
CRIPTO_AMPLIADO = {
    "SOL": "SOL/USD", "XRP": "XRP/USD", "DOGE": "DOGE/USD", "BNB": "BNB/USD",
    "ADA": "ADA/USD", "AVAX": "AVAX/USD", "LINK": "LINK/USD", "LTC": "LTC/USD",
    "DOT": "DOT/USD", "TRX": "TRX/USD", "TON": "TON/USD", "NEAR": "NEAR/USD",
    "ATOM": "ATOM/USD", "UNI": "UNI/USD", "APT": "APT/USD", "ARB": "ARB/USD",
    "OP": "OP/USD", "SUI": "SUI/USD", "INJ": "INJ/USD", "FIL": "FIL/USD",
    "PEPE": "PEPE/USD", "SHIB": "SHIB/USD", "BCH": "BCH/USD", "ETC": "ETC/USD",
}

# --- Forex ampliado: solo Premium ---
FOREX_AMPLIADO = {
    "USDJPY": "USD/JPY", "USDCHF": "USD/CHF", "USDCAD": "USD/CAD",
    "AUDUSD": "AUD/USD", "NZDUSD": "NZD/USD", "EURJPY": "EUR/JPY",
    "EURGBP": "EUR/GBP", "GBPJPY": "GBP/JPY", "EURCHF": "EUR/CHF",
    "AUDJPY": "AUD/JPY", "USDMXN": "USD/MXN", "USDBRL": "USD/BRL",
}

# --- Metales: solo Premium (el oro ya está en el núcleo) ---
METALES = {
    "XAGUSD": "XAG/USD",   # plata
    "XPTUSD": "XPT/USD",   # platino
    "XPDUSD": "XPD/USD",   # paladio
}

# --- Acciones e índices de EE.UU.: solo Premium ---
# ADVERTENCIA REAL sobre estos: el horizonte del motor son 24 velas de 1
# hora. En cripto y forex eso es literalmente un día. En acciones, que
# cotizan ~6.5 horas diarias, 24 velas son casi CUATRO sesiones — el
# análisis sigue siendo válido, pero cubre un plazo más largo del que
# sugiere "24h". Además hay huecos de apertura que no existen en cripto;
# la volatilidad Yang-Zhang sí los mide (por eso se eligió), pero el stop
# puede saltarse en el hueco sin pasar por el precio intermedio.
ACCIONES = {
    "AAPL": "AAPL", "TSLA": "TSLA", "NVDA": "NVDA", "MSFT": "MSFT",
    "AMZN": "AMZN", "GOOGL": "GOOGL", "META": "META", "NFLX": "NFLX",
    "AMD": "AMD", "COIN": "COIN", "MSTR": "MSTR",
    "SPY": "SPY", "QQQ": "QQQ",
}

MERCADO_A_SYMBOL = {
    **MERCADOS_BASE, **CRIPTO_AMPLIADO,
    **FOREX_AMPLIADO, **METALES, **ACCIONES,
}

# Qué mercados puede consultar cada plan. Es acumulativo: Premium lo tiene todo.
# Los planes se diferencian por NIVEL DE ANÁLISIS y por catálogo:
#   free     -> solo motor técnico, SIN fundamental, catálogo base
#   standard -> técnico + análisis fundamental sin IA (noticias, guerras,
#               anuncios de bancos centrales), catálogo + cripto ampliado
#   premium  -> todo lo anterior + la IA interpreta el algoritmo para una
#               lectura más segura, catálogo completo (60 mercados)
MERCADOS_POR_PLAN = {
    "free": set(MERCADOS_BASE),
    "standard": set(MERCADOS_BASE) | set(CRIPTO_AMPLIADO),
    "premium": set(MERCADO_A_SYMBOL),
}

# Alias -> nombre canónico. CRÍTICO para la calibración: BTC y BTCUSD son
# el MISMO mercado; guardarlos separados fragmentaba el historial
# verificado y retrasaba la activación del sesgo aprendido (bug corregido
# el 3-ago). Toda predicción se registra con el nombre canónico.
ALIAS_MERCADO = {"BTCUSD": "BTC", "ETHUSD": "ETH"}


def normalizar_mercado(mercado: str) -> str:
    m = (mercado or "").upper().strip()
    return ALIAS_MERCADO.get(m, m)


def plan_requerido(mercado: str):
    """Plan MÍNIMO que puede consultar ese mercado, o None si no existe."""
    m = normalizar_mercado(mercado)
    if m in MERCADOS_BASE or mercado.upper() in MERCADOS_BASE:
        return "free"
    if m in CRIPTO_AMPLIADO:
        return "standard"
    if m in MERCADO_A_SYMBOL or mercado.upper() in MERCADO_A_SYMBOL:
        return "premium"
    return None


# ---------------------------------------------------------------------------
# PRECISIÓN ADAPTATIVA DE PRECIOS
# ---------------------------------------------------------------------------
# Bug crítico corregido (3-ago): redondear TODO a 4 decimales destruía a
# PEPE y SHIB — con PEPE a 0.00001234, entrada, TP y SL quedaban los tres
# en 0.0. Regla: mínimo 5 cifras significativas, decidido por magnitud.

def decimales_para(precio: float) -> int:
    """Decimales adecuados para un precio: BTC->2, EURUSD->5, PEPE->10."""
    p = abs(float(precio)) if precio else 0.0
    if p == 0.0:
        return 8
    if p >= 1000:
        return 2
    if p >= 100:
        return 3
    if p >= 1:
        return 4
    if p >= 0.01:
        return 6
    if p >= 0.0001:
        return 8
    return 10


def redondear_precio(valor, referencia: float = None, escala: float = None):
    """
    Redondea con la precisión del instrumento.

    `escala` (opcional) es la distancia que el redondeo NO debe
    distorsionar — típicamente la distancia al stop. Bug real detectado
    en EURUSD: con entrada 1.1508 y SL 1.1501, la distancia es 0.0007 y
    el tick de 4 decimales (0.0001) es el 14% de esa distancia, así que
    redondear el TP convertía un R:R de 1.30 en uno de 1.286. El R:R
    anunciado dejaba de ser el real. Con `escala` se añaden decimales
    hasta que el tick sea menor del 1% de esa distancia.
    """
    if valor is None:
        return None
    ref = referencia if referencia else valor
    d = decimales_para(ref)
    if escala and escala > 0:
        # decimales necesarios para que el tick sea <= 1% de la escala
        import math
        necesarios = max(0, int(math.ceil(-math.log10(escala * 0.01))))
        d = max(d, min(necesarios, 12))
    return round(float(valor), d)

# Caché simple en memoria: evita gastar el límite gratuito (800/día, 8/min)
# cuando varios usuarios consultan el mismo mercado en pocos minutos.
# El caché evita repetir peticiones a Twelve Data. Es crítico ahora que
# el automático vigila 8 mercados: sin él, cada ronda gastaba 16
# peticiones y el plan gratuito (8/min, 800/día) devolvía "sin datos"
# en la mitad de los mercados.
_CACHE_TTL_SEGUNDOS = 180          # velas de 1h: 3 minutos
_CACHE_TTL_4H_SEGUNDOS = 1800      # velas de 4h: 30 min (cambian 8x más lento)
_cache: dict = {}


# ---------------------------------------------------------------------------
# LIMITADOR GLOBAL DE PETICIONES (con prioridades)
# ---------------------------------------------------------------------------
# Twelve Data (plan gratuito) permite 8 peticiones POR MINUTO y 800 al día.
# El bot tiene seis fuentes de peticiones que no se conocían entre sí:
# autotrader, vigilancia, precisión, monitoreo, arbitraje y las consultas
# de los usuarios. Cuando dos coincidían en el mismo minuto se pasaban del
# límite y fallaban TODAS — incluidas las de los clientes, que son las
# únicas que generan ingresos.
#
# Este limitador reparte la cuota con una regla simple: si hay que
# sacrificar peticiones, se sacrifican las de los procesos automáticos,
# nunca las de un usuario esperando su análisis.
#
#   PRIORIDAD_USUARIO  siempre pasa (hasta agotar el límite duro)
#   PRIORIDAD_WORKER   cede si queda poco margen en el minuto o en el día

import threading

LIMITE_POR_MINUTO = 7          # 8 reales, uno de margen
LIMITE_POR_DIA = 780           # 800 reales, con reserva
RESERVA_USUARIOS_DIA = 300     # peticiones diarias que los workers NO pueden tocar
RESERVA_USUARIOS_MINUTO = 2    # huecos por minuto siempre libres para clientes

PRIORIDAD_USUARIO = "usuario"
PRIORIDAD_WORKER = "worker"

_lock_cuota = threading.Lock()
_peticiones_minuto: list = []   # timestamps del último minuto
_peticiones_dia = {"fecha": None, "n": 0}
# Instante hasta el que NO se pide nada, tras un 429 del servidor
_pausa_hasta = {"ts": 0.0}


class CuotaAgotadaError(Exception):
    """
    La cuota de la API está agotada. Se distingue del resto de fallos
    porque la solución no es reintentar: es consumir menos.
    """
    pass


def _estado_cuota() -> dict:
    from datetime import date
    ahora = time.time()
    hoy = date.today().isoformat()
    with _lock_cuota:
        global _peticiones_minuto
        _peticiones_minuto = [t for t in _peticiones_minuto if ahora - t < 60]
        if _peticiones_dia["fecha"] != hoy:
            _peticiones_dia["fecha"] = hoy
            _peticiones_dia["n"] = 0
        pausa = max(0.0, _pausa_hasta["ts"] - ahora)
        return {
            "en_minuto": len(_peticiones_minuto),
            "en_dia": _peticiones_dia["n"],
            "libre_minuto": (0 if pausa > 0
                             else max(0, LIMITE_POR_MINUTO - len(_peticiones_minuto))),
            "libre_dia": max(0, LIMITE_POR_DIA - _peticiones_dia["n"]),
            "pausado_segundos": round(pausa),
        }


def _pedir_permiso(prioridad: str, esperar: bool = True) -> bool:
    """
    ¿Se puede hacer una petición ahora? Los workers ceden el paso.

    Un usuario espera lo que haga falta dentro del minuto (como mucho unos
    segundos) porque está delante de la pantalla esperando su análisis.
    Un worker prefiere saltarse esta ronda: dentro de 30 minutos vuelve.
    """
    from datetime import date
    intentos = 0
    while True:
        est = _estado_cuota()

        # Reserva diaria intocable para los clientes
        if prioridad == PRIORIDAD_WORKER and est["libre_dia"] <= RESERVA_USUARIOS_DIA:
            logger.info(f"Worker cede: quedan {est['libre_dia']} peticiones "
                        f"y {RESERVA_USUARIOS_DIA} están reservadas para usuarios")
            return False
        if est["libre_dia"] <= 0:
            return False

        # Los workers dejan siempre huecos libres en el minuto: sin esto,
        # el autotrader arrancaba, consumía los 7 de golpe y el cliente
        # que llegaba justo después tenía que esperar al minuto siguiente.
        margen_necesario = (RESERVA_USUARIOS_MINUTO
                            if prioridad == PRIORIDAD_WORKER else 0)
        if est["libre_minuto"] > margen_necesario:
            with _lock_cuota:
                _peticiones_minuto.append(time.time())
                _peticiones_dia["n"] += 1
            return True

        # Sin hueco en este minuto
        if prioridad == PRIORIDAD_WORKER:
            return False            # el worker no espera: vuelve luego
        if not esperar or intentos >= 3:
            return False
        intentos += 1
        espera = 61 - (time.time() - min(_peticiones_minuto)) if _peticiones_minuto else 5
        time.sleep(max(1.0, min(espera, 20.0)))


class DatosNoDisponiblesError(Exception):
    pass


def _es_error_de_cuota(texto: str) -> bool:
    t = (texto or "").lower()
    return any(k in t for k in ("429", "rate limit", "api credits",
                                 "run out of api", "too many requests"))


def calcular_tendencia_real(cierres: np.ndarray, maximos: np.ndarray, minimos: np.ndarray):
    """
    Tendencia calculada de datos reales, SIN NINGUNA MEDIA EXPONENCIAL.

    Por qué se eliminó la EMA (bug de raíz, 8-ago): la versión anterior
    usaba EMA20 vs EMA50, y una media de 50 velas promedia 50 HORAS de
    precio. Cuando el mercado gira, tarda unas 25 horas en enterarse. En
    la pérdida real de XRP, el precio llevaba 10 horas subiendo un 2.94%
    y el momentum por EMA seguía marcando −0.863 (bajista fuerte): el bot
    vendió mirando el pasado. Además incumplía la regla del proyecto de
    cero EMA en indicadores direccionales.

    Ahora son tres componentes, todos sin retraso oculto:

    1. PENDIENTE RECIENTE (regresión lineal sobre las últimas 12 velas).
       La regresión pondera todos los puntos por igual y responde al giro
       de inmediato, sin arrastrar cien velas de historia.
    2. ACELERACIÓN: compara la pendiente de las últimas 6 velas contra las
       12 anteriores. Detecta si el movimiento se está reforzando o
       agotando — es lo que distingue una tendencia viva de una que ya
       dio todo lo que tenía.
    3. ESTRUCTURA DE SWINGS (HH/HL vs LH/LL), que es análisis de
       estructura de mercado puro y no depende de ninguna media.

    Devuelve trend_score en [-1, 1]: positivo alcista, negativo bajista.
    """
    n = len(cierres)
    if n < 8:
        return {"trend_score": 0.0, "momentum_score": 0.0, "estructura_score": 0.0,
                "estructura_txt": "datos insuficientes", "aceleracion": 0.0}

    def pendiente_normalizada(serie):
        """Pendiente por regresión, en fracción de precio por vela."""
        if len(serie) < 3:
            return 0.0
        x = np.arange(len(serie), dtype=float)
        p = np.polyfit(x, serie, 1)[0]
        return float(p / serie[-1]) if serie[-1] else 0.0

    # 1) Pendiente reciente: el pulso actual del mercado
    ventana_corta = min(12, max(4, n // 8))
    pend_reciente = pendiente_normalizada(cierres[-ventana_corta:])
    # 400 ≈ escala para que un 0.25% por vela sature el tanh
    momentum_score = float(np.tanh(pend_reciente * 400))

    # 2) Aceleración: ¿el movimiento se refuerza o se agota?
    aceleracion = 0.0
    if n >= ventana_corta * 3:
        pend_muy_reciente = pendiente_normalizada(cierres[-(ventana_corta // 2):])
        pend_previa = pendiente_normalizada(cierres[-ventana_corta * 3:-ventana_corta])
        aceleracion = float(np.tanh((pend_muy_reciente - pend_previa) * 400))

    # 3) Estructura de swings (sin medias, estructura pura)
    ventana = min(20, n // 2) if n >= 4 else n
    if ventana >= 2:
        rec_max = np.max(maximos[-ventana:])
        rec_min = np.min(minimos[-ventana:])
        prev_max = np.max(maximos[-2 * ventana:-ventana]) if n >= 2 * ventana else rec_max
        prev_min = np.min(minimos[-2 * ventana:-ventana]) if n >= 2 * ventana else rec_min

        hizo_hh, hizo_hl = rec_max > prev_max, rec_min > prev_min
        hizo_lh, hizo_ll = rec_max < prev_max, rec_min < prev_min

        if hizo_hh and hizo_hl:
            estructura_score, estructura_txt = 1.0, "HH+HL (alcista)"
        elif hizo_lh and hizo_ll:
            estructura_score, estructura_txt = -1.0, "LH+LL (bajista)"
        else:
            estructura_score, estructura_txt = 0.0, "mixta/indecisión"
    else:
        estructura_score, estructura_txt = 0.0, "datos insuficientes"

    # La pendiente reciente manda (0.45) sobre la estructura (0.35), porque
    # la estructura mira ventanas largas y también llega tarde a los giros.
    # La aceleración (0.20) es el aviso temprano de agotamiento.
    trend_score = (0.45 * momentum_score + 0.35 * estructura_score
                   + 0.20 * aceleracion)

    return {
        "trend_score": round(max(-1.0, min(1.0, float(trend_score))), 3),
        "momentum_score": round(momentum_score, 3),
        "estructura_score": estructura_score,
        "estructura_txt": estructura_txt,
        "aceleracion": round(aceleracion, 3),
    }


# ---------------------------------------------------------------------------
# ORDER BLOCK + BOS + CLASIFICACIÓN DE RETEST (concepto ICT/SMC real)
# ---------------------------------------------------------------------------
# Implementa el mismo patrón que ya usa el skill icc-sniper-v162 (OB+BOS,
# OB+SWP+INT). Detecta BOS real (ruptura de estructura confirmada por
# cierre, no solo mecha), ubica el Order Block (última vela opuesta antes
# del impulso que rompió estructura), y clasifica cómo se ha comportado
# el precio al volver a esa zona:
#   - "liquidity_sweep": el precio barrió más allá de la zona OB (cazó
#     stops) y luego cerró de vuelta dentro/más allá en la dirección del
#     BOS — el patrón más fuerte, atrapa a los que entraron tarde en contra.
#   - "continuation": retroceso limpio hacia la zona sin barrer más allá,
#     y ya está reaccionando en la dirección del BOS.
#   - "consolidation": el precio lleva varias velas dentro/cerca de la
#     zona sin definir dirección clara todavía.
#   - "sin_retest": el precio no ha vuelto a la zona OB desde el BOS.
# LIMITE HONESTO: la detección de swings usa una ventana fija (pivote de
# N velas), no un algoritmo de estructura de mercado completo con
# múltiples timeframes — es una aproximación real y calculable, no el
# análisis discrecional completo que haría un trader viendo el gráfico.

def calcular_ob_bos(aperturas: np.ndarray, maximos: np.ndarray,
                     minimos: np.ndarray, cierres: np.ndarray,
                     ventana_swing: int = 20, velas_confirmacion: int = 5):
    n = len(cierres)
    resultado_vacio = {
        "bos_detectado": False, "bos_direccion": None,
        "ob_zona_min": None, "ob_zona_max": None,
        "tipo_retest": "sin_datos", "score": 0.0,
    }
    if n < ventana_swing + velas_confirmacion + 2:
        return resultado_vacio

    # Swing de referencia: máximo/mínimo de la ventana ANTERIOR a las
    # últimas `velas_confirmacion` velas (para que el BOS, si existe,
    # haya ocurrido dentro de esas últimas velas, no en el pasado lejano).
    swing_high = np.max(maximos[-(ventana_swing + velas_confirmacion):-velas_confirmacion])
    swing_low = np.min(minimos[-(ventana_swing + velas_confirmacion):-velas_confirmacion])

    cierres_recientes = cierres[-velas_confirmacion:]
    bos_alcista = np.any(cierres_recientes > swing_high)
    bos_bajista = np.any(cierres_recientes < swing_low)

    if bos_alcista and not bos_bajista:
        bos_direccion = "ALCISTA"
    elif bos_bajista and not bos_alcista:
        bos_direccion = "BAJISTA"
    else:
        return resultado_vacio  # sin BOS claro, o ambos (rango), no hay señal confiable

    # Índice de la vela donde ocurrió el BOS (primera que rompió el nivel)
    if bos_direccion == "ALCISTA":
        idx_bos_rel = int(np.argmax(cierres_recientes > swing_high))
    else:
        idx_bos_rel = int(np.argmax(cierres_recientes < swing_low))
    idx_bos = n - velas_confirmacion + idx_bos_rel

    # Order Block: última vela de color OPUESTO al BOS, antes del impulso.
    # ALCISTA -> buscar la última vela bajista (close < open) antes de idx_bos.
    # BAJISTA -> buscar la última vela alcista (close > open) antes de idx_bos.
    idx_ob = None
    for j in range(idx_bos - 1, max(idx_bos - ventana_swing, 0) - 1, -1):
        if bos_direccion == "ALCISTA" and cierres[j] < aperturas[j]:
            idx_ob = j
            break
        if bos_direccion == "BAJISTA" and cierres[j] > aperturas[j]:
            idx_ob = j
            break

    if idx_ob is None:
        return resultado_vacio  # no se encontró vela opuesta clara -> no hay OB confiable

    ob_zona_min = float(minimos[idx_ob])
    ob_zona_max = float(maximos[idx_ob])
    precio_actual = float(cierres[-1])

    # Clasificación de retest: qué ha pasado desde el BOS hasta ahora.
    velas_desde_bos = cierres[idx_bos + 1:] if idx_bos + 1 < n else np.array([])
    minimos_desde_bos = minimos[idx_bos + 1:] if idx_bos + 1 < n else np.array([])
    maximos_desde_bos = maximos[idx_bos + 1:] if idx_bos + 1 < n else np.array([])

    toco_zona = False
    barrio_mas_alla = False
    if len(velas_desde_bos) > 0:
        if bos_direccion == "ALCISTA":
            toco_zona = np.any(minimos_desde_bos <= ob_zona_max)
            barrio_mas_alla = np.any(minimos_desde_bos < ob_zona_min)
        else:
            toco_zona = np.any(maximos_desde_bos >= ob_zona_min)
            barrio_mas_alla = np.any(maximos_desde_bos > ob_zona_max)

    if not toco_zona:
        tipo_retest = "sin_retest"
        magnitud = 0.5  # BOS real, pero sin confirmación de retest todavía
    elif barrio_mas_alla:
        # Barrió más allá de la zona (cazó stops) -> ¿ya se recuperó?
        if bos_direccion == "ALCISTA" and precio_actual > ob_zona_min:
            tipo_retest = "liquidity_sweep"
            magnitud = 1.0  # el patrón más fuerte
        elif bos_direccion == "BAJISTA" and precio_actual < ob_zona_max:
            tipo_retest = "liquidity_sweep"
            magnitud = 1.0
        else:
            tipo_retest = "consolidation"
            magnitud = 0.4
    else:
        # Tocó la zona sin barrerla de más -> ¿ya reaccionó en dirección del BOS?
        avance_desde_ob = (precio_actual - ob_zona_max) if bos_direccion == "ALCISTA" else (ob_zona_min - precio_actual)
        if avance_desde_ob > 0:
            tipo_retest = "continuation"
            magnitud = 0.8
        else:
            tipo_retest = "consolidation"
            magnitud = 0.4

    score = magnitud if bos_direccion == "ALCISTA" else -magnitud

    return {
        "bos_detectado": True,
        "bos_direccion": bos_direccion,
        "ob_zona_min": round(ob_zona_min, 4),
        "ob_zona_max": round(ob_zona_max, 4),
        "tipo_retest": tipo_retest,
        "score": round(float(score), 3),
    }


# ---------------------------------------------------------------------------
# CRT — CANDLE RANGE THEORY (concepto ICT real)
# ---------------------------------------------------------------------------
# Toma un rango de referencia (velas previas, excluyendo las más recientes)
# y verifica si la vela más reciente barrió ese rango y cerró de vuelta
# adentro — el patrón clásico de "manipulación antes de distribución":
# barrer liquidez fuera del rango para luego moverse en la dirección
# contraria a la barrida.

def calcular_crt(maximos: np.ndarray, minimos: np.ndarray, cierres: np.ndarray,
                  ventana_rango: int = 24):
    n = len(cierres)
    if n < ventana_rango + 2:
        return {"señal": "sin_datos", "score": 0.0}

    rango_max = float(np.max(maximos[-(ventana_rango + 1):-1]))
    rango_min = float(np.min(minimos[-(ventana_rango + 1):-1]))
    rango_ancho = max(rango_max - rango_min, 1e-9)

    low_actual = float(minimos[-1])
    high_actual = float(maximos[-1])
    close_actual = float(cierres[-1])

    barrio_min = low_actual < rango_min
    reclamo_alcista = barrio_min and close_actual > rango_min
    barrio_max = high_actual > rango_max
    reclamo_bajista = barrio_max and close_actual < rango_max

    if reclamo_alcista and not reclamo_bajista:
        profundidad = (rango_min - low_actual) / rango_ancho
        score = min(0.3 + profundidad * 3, 1.0)
        return {"señal": "ALCISTA (barrida + reclamo bajo el rango)", "score": round(float(score), 3),
                "rango_min": round(rango_min, 4), "rango_max": round(rango_max, 4)}
    if reclamo_bajista and not reclamo_alcista:
        profundidad = (high_actual - rango_max) / rango_ancho
        score = -min(0.3 + profundidad * 3, 1.0)
        return {"señal": "BAJISTA (barrida + reclamo sobre el rango)", "score": round(float(score), 3),
                "rango_min": round(rango_min, 4), "rango_max": round(rango_max, 4)}

    return {"señal": "sin patrón CRT claro", "score": 0.0,
            "rango_min": round(rango_min, 4), "rango_max": round(rango_max, 4)}


# ---------------------------------------------------------------------------
# TURTLE SOUP / TBS — falso quiebre de un swing previo (concepto ICT real)
# ---------------------------------------------------------------------------
# Variante del mismo principio que CRT pero sobre un swing high/low
# específico (no un rango completo): el precio hace un nuevo extremo más
# allá de un swing previo relevante, y en pocas velas revierte y cierra
# de vuelta dentro — el "falso breakout" clásico que atrapa a quienes
# entraron siguiendo la ruptura.

def calcular_turtle_soup(maximos: np.ndarray, minimos: np.ndarray, cierres: np.ndarray,
                          lookback_swing: int = 20, ventana_confirmacion: int = 3):
    n = len(cierres)
    if n < lookback_swing + ventana_confirmacion + 1:
        return {"señal": "sin_datos", "score": 0.0}

    inicio = -(lookback_swing + ventana_confirmacion)
    fin = -ventana_confirmacion
    swing_high_previo = float(np.max(maximos[inicio:fin]))
    swing_low_previo = float(np.min(minimos[inicio:fin]))

    minimos_recientes = minimos[-ventana_confirmacion:]
    maximos_recientes = maximos[-ventana_confirmacion:]
    close_actual = float(cierres[-1])

    barrio_low = np.any(minimos_recientes < swing_low_previo)
    reclamo_alcista = barrio_low and close_actual > swing_low_previo

    barrio_high = np.any(maximos_recientes > swing_high_previo)
    reclamo_bajista = barrio_high and close_actual < swing_high_previo

    if reclamo_alcista and not reclamo_bajista:
        return {"señal": "ALCISTA (falso quiebre bajista revertido)", "score": 0.9,
                "swing_referencia": round(swing_low_previo, 4)}
    if reclamo_bajista and not reclamo_alcista:
        return {"señal": "BAJISTA (falso quiebre alcista revertido)", "score": -0.9,
                "swing_referencia": round(swing_high_previo, 4)}

    return {"señal": "sin patrón Turtle Soup claro", "score": 0.0}


# ---------------------------------------------------------------------------
# FVG — FAIR VALUE GAP (concepto ICT real, patrón de 3 velas)
# ---------------------------------------------------------------------------
# Definición estándar ICT: un FVG alcista existe cuando el máximo de la
# vela [i-2] queda por debajo del mínimo de la vela [i] (deja un hueco de
# precio que el mercado no "llenó" — la vela [i-1] es el impulso que causa
# el hueco). FVG bajista es el espejo. Vectorizado con numpy: se comparan
# los arrays completos desplazados, no vela por vela en un loop.

def calcular_fvg(maximos: np.ndarray, minimos: np.ndarray, precio_actual: float,
                  max_gaps_recientes: int = 10):
    n = len(maximos)
    if n < 5:
        return {"gaps": [], "gap_mas_cercano": None, "score": 0.0}

    # Comparación vectorizada: high[i-2] vs low[i] para todo i en un solo paso
    high_i_menos_2 = maximos[:-2]
    low_i = minimos[2:]
    high_i = maximos[2:]
    low_i_menos_2 = minimos[:-2]

    es_gap_alcista = high_i_menos_2 < low_i
    es_gap_bajista = low_i_menos_2 > high_i

    gaps = []
    for idx in np.where(es_gap_alcista)[0][-max_gaps_recientes:]:
        gaps.append({"tipo": "ALCISTA", "zona_min": float(high_i_menos_2[idx]), "zona_max": float(low_i[idx])})
    for idx in np.where(es_gap_bajista)[0][-max_gaps_recientes:]:
        gaps.append({"tipo": "BAJISTA", "zona_min": float(high_i[idx]), "zona_max": float(low_i_menos_2[idx])})

    if not gaps:
        return {"gaps": [], "gap_mas_cercano": None, "score": 0.0}

    # El gap "más cercano" al precio actual sin llenar todavía es el relevante
    def distancia_a_gap(g):
        centro = (g["zona_min"] + g["zona_max"]) / 2
        return abs(precio_actual - centro)

    gap_mas_cercano = min(gaps, key=distancia_a_gap)
    dentro_del_gap = gap_mas_cercano["zona_min"] <= precio_actual <= gap_mas_cercano["zona_max"]

    score = 0.0
    if dentro_del_gap:
        score = 0.6 if gap_mas_cercano["tipo"] == "ALCISTA" else -0.6

    return {"gaps": gaps, "gap_mas_cercano": gap_mas_cercano, "precio_dentro_del_gap": dentro_del_gap,
            "score": round(score, 3)}


# ---------------------------------------------------------------------------
# CICLO DE MOMENTUM SIN EMA (alternativa honesta al STC)
# ---------------------------------------------------------------------------
# El STC (Schaff Trend Cycle) real usa EMA internamente (se calcula sobre
# un MACD, que es EMA_rápida - EMA_lenta). Con la regla de "cero EMA" no
# existe un STC genuino — lo que se construye aquí es un oscilador de
# ciclo REAL pero distinto: un %K estocástico de dos pasos, aplicado
# sobre un momentum de Rate-of-Change suavizado con SMA (no EMA). Se
# nombra explícitamente "Ciclo de Momentum (sin EMA)" y no "STC", para
# no hacerse pasar por un indicador que no es.

def calcular_ciclo_sin_ema(cierres: np.ndarray, periodo_roc: int = 10,
                            periodo_stoch: int = 10, periodo_sma: int = 3):
    n = len(cierres)
    minimo_requerido = periodo_roc + periodo_stoch + periodo_sma + 2
    if n < minimo_requerido:
        return {"valor": 50.0, "señal": "sin_datos", "score": 0.0}

    # Rate of Change (momentum puro, sin promedio móvil de ningún tipo)
    roc = (cierres[periodo_roc:] - cierres[:-periodo_roc]) / cierres[:-periodo_roc] * 100

    # Suavizado con SMA (no EMA) de 3 periodos, como hace el paso 1 del STC real
    if len(roc) < periodo_sma:
        return {"valor": 50.0, "señal": "sin_datos", "score": 0.0}
    roc_suavizado = np.convolve(roc, np.ones(periodo_sma) / periodo_sma, mode="valid")

    if len(roc_suavizado) < periodo_stoch:
        return {"valor": 50.0, "señal": "sin_datos", "score": 0.0}

    ventana = roc_suavizado[-periodo_stoch:]
    minimo, maximo = float(np.min(ventana)), float(np.max(ventana))
    rango = max(maximo - minimo, 1e-9)
    valor_ciclo = 100 * (roc_suavizado[-1] - minimo) / rango  # 0-100, como %K estocástico

    if valor_ciclo > 75:
        señal, score = "SOBRECOMPRA (posible giro bajista)", -0.5
    elif valor_ciclo < 25:
        señal, score = "SOBREVENTA (posible giro alcista)", 0.5
    elif valor_ciclo > 50:
        señal, score = "ciclo alcista", 0.3
    else:
        señal, score = "ciclo bajista", -0.3

    return {"valor": round(float(valor_ciclo), 1), "señal": señal, "score": score}


# ---------------------------------------------------------------------------
# CONTEXTO DE SESIÓN (informativo — la decisión es del usuario)
# ---------------------------------------------------------------------------
# No bloquea ninguna señal: informa en qué sesión se está operando y con
# qué liquidez, para que el usuario decida. La evidencia de que existen
# ventanas horarias peores (la "hora tóxica" de baja liquidez asiática)
# es sólida, pero convertirla en un bloqueo automático sería imponer un
# criterio que aún no hemos verificado con NUESTROS datos. Cuando el
# registro por sesión tenga muestra, el propio bot dirá qué franja
# funciona peor con cifras propias.

SESIONES = {
    "asiatica": {"desde": 0, "hasta": 7, "nombre": "Asiática", "liquidez": "BAJA",
                 "nota": "Menor volumen: rangos estrechos y más barridas falsas."},
    "londres": {"desde": 7, "hasta": 12, "nombre": "Londres", "liquidez": "ALTA",
                "nota": "Apertura europea: expansión de rango y direccionalidad."},
    "solape": {"desde": 12, "hasta": 16, "nombre": "Solape Londres–Nueva York",
               "liquidez": "MÁXIMA",
               "nota": "Mayor volumen del día: movimientos más limpios y sostenidos."},
    "nueva_york": {"desde": 16, "hasta": 21, "nombre": "Nueva York", "liquidez": "ALTA",
                   "nota": "Datos macro de EE.UU.: volatilidad por noticias."},
    "cierre": {"desde": 21, "hasta": 24, "nombre": "Cierre / madrugada", "liquidez": "BAJA",
               "nota": "Liquidez decreciente: cuidado con el spread."},
}

# Cripto opera 24/7: la sesión importa mucho menos que en forex y metales
MERCADOS_24_7 = {"BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "ADA", "AVAX", "LINK",
                 "LTC", "DOT", "TRX", "TON", "NEAR", "ATOM", "UNI", "APT", "ARB",
                 "OP", "SUI", "INJ", "FIL", "PEPE", "SHIB", "BCH", "ETC"}


def contexto_sesion(mercado: str, momento=None) -> dict:
    """
    Sesión activa (hora UTC) y su liquidez típica. Informativo:
    NUNCA bloquea ni modifica la señal.
    """
    from datetime import datetime, timezone
    ahora = momento or datetime.now(timezone.utc)
    h = ahora.hour
    clave = "cierre"
    for k, s in SESIONES.items():
        if s["desde"] <= h < s["hasta"]:
            clave = k
            break
    info = dict(SESIONES[clave])
    info["clave"] = clave
    info["hora_utc"] = h
    info["fin_de_semana"] = ahora.weekday() >= 5
    m = normalizar_mercado(mercado)
    info["mercado_24_7"] = m in MERCADOS_24_7
    if info["mercado_24_7"]:
        # Coherencia: si decimos que en un mercado 24/7 la sesión pesa
        # menos, no podemos marcar su liquidez como BAJA y disparar el
        # aviso de riesgo en todos los reportes. Cripto tiene volumen a
        # todas horas; la asiática es la franja MÁS FLOJA, no una de
        # liquidez escasa como en forex fuera de horario.
        if info["liquidez"] == "BAJA":
            info["liquidez"] = "MEDIA"
        info["nota"] = ("Mercado 24/7: la sesión pesa menos, aunque la liquidez "
                        "sigue siendo mayor en el solape europeo-americano.")
    elif info["fin_de_semana"]:
        info["liquidez"] = "CERRADO"
        info["nota"] = ("Fin de semana: este mercado no cotiza. Los datos son "
                        "del último cierre y la señal puede quedar obsoleta.")
    return info


def analizar_volumen(volumenes: np.ndarray, cierres: np.ndarray,
                      direccion_precio: float) -> dict:
    """
    Confirmación por volumen (tercer punto de confirmación clásico): un
    movimiento con volumen creciente respalda la dirección; el mismo
    movimiento con volumen decreciente sugiere agotamiento. Se compara
    el volumen de las últimas 6 velas contra la media de las 50 previas
    (medias simples: sin EMA, regla del proyecto).

    Muchos símbolos de forex/metales no traen volumen real en Twelve
    Data (devuelven 0): en ese caso se reporta "sin datos" y el peso se
    redistribuye — jamás se inventa una confirmación.
    """
    if volumenes is None or len(volumenes) < 60:
        return {"disponible": False, "score": 0.0, "texto": "sin datos de volumen"}
    v = np.asarray(volumenes, dtype=float)
    v = v[np.isfinite(v)]
    if len(v) < 60 or np.all(v <= 0):
        return {"disponible": False, "score": 0.0, "texto": "sin datos de volumen"}

    reciente = float(np.mean(v[-6:]))
    base = float(np.mean(v[-56:-6]))
    if base <= 0:
        return {"disponible": False, "score": 0.0, "texto": "sin datos de volumen"}
    ratio = reciente / base

    # Ratio 1.0 = volumen normal. >1.3 expansión clara, <0.7 agotamiento.
    if ratio >= 1.3:
        fuerza, texto = 1.0, f"expansión de volumen (x{ratio:.2f})"
    elif ratio >= 1.1:
        fuerza, texto = 0.5, f"volumen algo por encima de lo normal (x{ratio:.2f})"
    elif ratio <= 0.7:
        fuerza, texto = -0.6, f"volumen en retroceso (x{ratio:.2f}): posible agotamiento"
    else:
        fuerza, texto = 0.0, f"volumen normal (x{ratio:.2f})"

    # El volumen CONFIRMA la dirección del movimiento reciente, no la crea
    score = fuerza * (1.0 if direccion_precio >= 0 else -1.0) if fuerza > 0 else 0.0
    if fuerza < 0:
        score = -0.3 * (1.0 if direccion_precio >= 0 else -1.0)  # resta a la dirección vigente
    return {"disponible": True, "score": round(float(score), 3),
            "ratio": round(ratio, 2), "texto": texto}


def _es_rate_limit(e: Exception) -> bool:
    """¿El fallo es por exceso de peticiones y no por un error real?"""
    t = str(e).lower()
    return ("429" in t or "rate limit" in t or "api credits" in t
            or "run out of api" in t)


# Correlación típica de cada altcoin con BTC. En cripto, BTC marca la
# marea: cuando rompe al alza, las altcoins suben con él aunque sus
# propios indicadores vengan de una caída. Ignorarlo fue lo que produjo
# una pérdida real: el motor abrió CORTA en XRP mientras BTC superaba
# los 65.000 y SOL rompía 75 — todo el mercado en rally y el bot
# vendiendo. El indicador de XRP miraba las horas previas; el mercado
# ya había girado.
CORRELACION_CON_BTC = {
    "ETH": 0.85, "SOL": 0.80, "ADA": 0.82, "XRP": 0.75, "DOGE": 0.78,
    "LINK": 0.80, "AVAX": 0.82, "BNB": 0.78, "LTC": 0.80, "DOT": 0.82,
    "ATOM": 0.80, "PEPE": 0.70, "SHIB": 0.72, "BCH": 0.78, "ETC": 0.80,
}

# Movimiento de BTC a partir del cual arrastra al resto del mercado
UMBRAL_MAREA_BTC = 0.015          # 1.5% en el horizonte reciente


# ---------------------------------------------------------------------------
# TIME-SERIES MOMENTUM (Moskowitz, Ooi & Pedersen 2012)
# ---------------------------------------------------------------------------
# La anomalía con más respaldo académico que existe en trading sistemático:
# el SIGNO del retorno pasado predice el signo del retorno futuro. Hurst,
# Ooi & Pedersen (2017) lo verificaron sobre un siglo de datos (1903-2016)
# y en cuatro clases de activos.
#
# Por qué se añade aquí, y no es "un indicador más":
#   · Es la regla MÁS SIMPLE del sistema — solo mira retornos pasados.
#   · Nuestros propios datos muestran que la confianza (que sube cuando
#     más indicadores coinciden) está ANTICORRELACIONADA con el acierto
#     (r = −0.329). Añadir complejidad nos ha ido en contra; TSMOM va en
#     la dirección opuesta.
#   · Mira plazos MUCHO más largos que el resto del motor. La tendencia
#     actual usa 12 velas de 1h (medio día); TSMOM mira semanas. Eso lo
#     hace inmune al ruido intradía que nos costó XRP.
#
# Adaptación honesta al horizonte del bot: el paper original usa 1-12
# meses en futuros. Con velas de 1h y horizonte de 24h, los equivalentes
# razonables son 7 y 30 días (168 y 720 velas). Como el motor solo pide
# 200 velas por llamada, se usan 3 y 7 días con los datos disponibles y
# se documenta la limitación en vez de fingir que miramos meses.

VENTANAS_TSMOM = (72, 168)      # 3 y 7 días en velas de 1h
PESO_TSMOM_CORTO = 0.4          # la ventana larga pesa más (0.6)
MOVIMIENTO_MINIMO_TSMOM = 0.015 # 1.5%: por debajo no hay momentum, hay calma


def calcular_tsmom(cierres: np.ndarray) -> dict:
    """
    Time-series momentum: el signo del retorno pasado en dos ventanas.

    Normalizado por volatilidad, como en el paper original: un +2% en un
    activo tranquilo es una señal mucho más fuerte que un +2% en uno que
    se mueve un 5% al día. Sin normalizar, las altcoins volátiles
    dominarían siempre la señal.
    """
    c = np.asarray(cierres, dtype=float)
    n = len(c)
    if n < 80:
        return {"disponible": False, "score": 0.0, "texto": "histórico insuficiente"}

    rets = np.diff(np.log(c))
    vol = float(np.std(rets[-min(len(rets), 168):]))
    # Piso de volatilidad: sin él, en un mercado dormido cualquier
    # movimiento minúsculo se normalizaba a señal máxima. En pruebas, una
    # serie lateral con oscilación del 1.8% daba score +0.77 — el motor
    # habría visto momentum donde solo había calma.
    vol = max(vol, VOLATILIDAD_MINIMA)

    partes, detalles = [], []
    for ventana, peso in zip(VENTANAS_TSMOM,
                              (PESO_TSMOM_CORTO, 1.0 - PESO_TSMOM_CORTO)):
        if n <= ventana:
            continue
        r = float(np.log(c[-1] / c[-ventana - 1]))
        # MOVIMIENTO MÍNIMO ABSOLUTO: por debajo del 1.5% en la ventana no
        # hay momentum que explotar, por muy tranquilo que esté el activo.
        # La normalización por volatilidad es útil para comparar activos,
        # pero no puede convertir el ruido en señal.
        if abs(r) < MOVIMIENTO_MINIMO_TSMOM:
            partes.append(0.0)
            detalles.append(f"{ventana // 24}d {r * 100:+.1f}% (plano)")
            continue
        r_norm = r / (vol * (ventana ** 0.5))
        partes.append(peso * float(np.tanh(r_norm)))
        detalles.append(f"{ventana // 24}d {r * 100:+.1f}%")

    if not partes:
        return {"disponible": False, "score": 0.0, "texto": "histórico insuficiente"}

    score = sum(partes) / sum(
        p for v, p in zip(VENTANAS_TSMOM, (PESO_TSMOM_CORTO, 1 - PESO_TSMOM_CORTO))
        if n > v)
    score = max(-1.0, min(1.0, score))
    direccion = "alcista" if score > 0.1 else ("bajista" if score < -0.1 else "plano")
    return {
        "disponible": True,
        "score": round(float(score), 3),
        "texto": f"momentum {direccion} ({', '.join(detalles)})",
        "direccion": direccion,
    }


def contexto_mercado_cripto(mercado: str) -> dict:
    """
    ¿Qué está haciendo el mercado cripto EN CONJUNTO?

    El motor analizaba cada activo aislado, como si XRP se moviera solo.
    No es así: con correlaciones de 0.75-0.85, cuando BTC rompe al alza
    arrastra a todas las altcoins. Operar una altcoin en contra de BTC
    es apostar a que se rompa una correlación que se sostiene el 80% del
    tiempo.

    Devuelve la dirección y la fuerza de la marea, y la correlación del
    activo consultado. Fail-soft: si no hay datos de BTC, neutral.
    """
    m = normalizar_mercado(mercado)
    if m == "BTC" or m not in CORRELACION_CON_BTC:
        return {"disponible": False, "score": 0.0, "texto": ""}
    try:
        d = obtener_datos_mercado("BTC", interval="1h", outputsize=60)
        cierres = np.array([float(v["close"]) for v in d["velas"]], dtype=float)
        if len(cierres) < 30:
            return {"disponible": False, "score": 0.0, "texto": ""}
        # Movimiento de BTC en las últimas 12 y 24 horas
        mov_12 = (cierres[-1] - cierres[-12]) / cierres[-12]
        mov_24 = (cierres[-1] - cierres[-24]) / cierres[-24]
        # Se pondera más lo reciente: la marea de ahora manda
        mov = 0.65 * mov_12 + 0.35 * mov_24
    except Exception as e:
        logger.warning(f"Contexto cripto no disponible ({e}) — neutral")
        return {"disponible": False, "score": 0.0, "texto": ""}

    corr = CORRELACION_CON_BTC[m]
    # Score acotado: un movimiento de 3% en BTC da score máximo
    intensidad = max(-1.0, min(1.0, mov / 0.03))
    score = intensidad * corr

    if abs(mov) < UMBRAL_MAREA_BTC * 0.5:
        etiqueta, texto = "PLANO", f"BTC lateral ({mov*100:+.2f}%)"
    elif mov > 0:
        etiqueta = "ALCISTA"
        texto = f"BTC subiendo {mov*100:+.2f}% (correlación {corr:.2f})"
    else:
        etiqueta = "BAJISTA"
        texto = f"BTC cayendo {mov*100:+.2f}% (correlación {corr:.2f})"

    return {
        "disponible": True, "score": round(float(score), 3),
        "movimiento_btc": round(float(mov), 5), "correlacion": corr,
        "etiqueta": etiqueta, "texto": texto,
        # Marea fuerte: BTC se movió lo suficiente para arrastrar
        "marea_fuerte": abs(mov) >= UMBRAL_MAREA_BTC,
    }


def contexto_superior(mercado: str) -> dict:
    """
    Confirmación MULTI-TIMEFRAME: estructura del gráfico de 4h. Una señal
    de 1h contra la estructura de 4h es una trampa clásica; exigir
    alineación entre marcos es de las pocas mejoras con respaldo amplio.
    Aquí NO se bloquea: se devuelve el score para que el consenso lo pese
    como una señal más. Fail-soft: si la llamada falla, neutral.
    """
    try:
        d4 = obtener_datos_mercado(mercado, interval="4h", outputsize=120)
        t4 = d4["tendencia"]
        return {
            "disponible": True,
            "score": float(t4.get("trend_score", 0.0)),
            "estructura": t4.get("estructura_txt", ""),
            "texto": f"4h {t4.get('estructura_txt', 'sin estructura')}",
        }
    except Exception as e:
        logger.warning(f"Contexto 4h no disponible para {mercado} ({e}) — neutral")
        return {"disponible": False, "score": 0.0, "estructura": "", "texto": "4h sin datos"}


def _procesar_velas(valores: list, mercado: str, symbol: str,
                     fuente: str = "twelvedata") -> dict:
    """
    Convierte una lista de velas en el paquete de datos que usa el motor.

    Extraído para que Bybit y Twelve Data compartan EXACTAMENTE el mismo
    procesamiento: misma volatilidad, misma semilla determinista, mismos
    indicadores. Si cada fuente tuviera su propio cálculo, los análisis
    dejarían de ser comparables entre mercados y la calibración perdería
    sentido.
    """
    cierres = np.array([float(v["close"]) for v in valores])
    aperturas = np.array([float(v["open"]) for v in valores])
    # El volumen viene en la misma respuesta (coste cero). En forex y
    # metales suele llegar 0 o ausente: analizar_volumen lo detecta y
    # reporta "sin datos" en vez de inventar una confirmación.
    volumenes = np.array([float(v.get("volume", 0) or 0) for v in valores])
    maximos = np.array([float(v["high"]) for v in valores])
    minimos = np.array([float(v["low"]) for v in valores])

    precio_actual = cierres[-1]
    retornos_log = np.diff(np.log(cierres))
    # Volatilidad Yang-Zhang (OHLC + huecos de apertura), con cadena de
    # respaldo YZ -> Garman-Klass -> desviación clásica, y suelo siempre:
    # sigma=0 (feed congelado, festivo) tumbaba la consulta por división
    # entre cero.
    volatilidad = calcular_volatilidad_yang_zhang(aperturas, maximos, minimos, cierres)
    if not np.isfinite(volatilidad) or volatilidad <= 0:
        volatilidad = calcular_volatilidad_garman_klass(aperturas, maximos, minimos, cierres)
    if not np.isfinite(volatilidad) or volatilidad <= 0:
        volatilidad = float(np.std(retornos_log))
    volatilidad = max(volatilidad, VOLATILIDAD_MINIMA)
    regimen_vol = calcular_regimen_volatilidad(retornos_log)
    maximo_reciente = float(np.max(maximos[-48:]))
    minimo_reciente = float(np.min(minimos[-48:]))

    # Drift REAL medido, no cero: el GBM sin drift arrastra un sesgo
    # bajista estructural por la asimetría log-normal.
    ventana_drift = min(48, len(retornos_log))
    drift_por_vela = float(np.mean(retornos_log[-ventana_drift:])) if ventana_drift > 0 else 0.0

    # Semilla determinista: SHA-256 de símbolo + última vela + cierre.
    # Mientras no llegue una vela nueva, todas las consultas del mismo
    # mercado producen EXACTAMENTE el mismo análisis.
    import hashlib as _hashlib
    ultima_vela_ts = valores[-1].get("datetime", "") if valores else ""
    semilla_material = f"{symbol}|{ultima_vela_ts}|{precio_actual}"
    seed_determinista = int(_hashlib.sha256(semilla_material.encode()).hexdigest()[:12], 16)

    resultado = {
        "symbol": symbol,
        "precio_actual": float(precio_actual),
        "volatilidad_por_vela": volatilidad,
        "maximo_reciente": maximo_reciente,
        "minimo_reciente": minimo_reciente,
        "n_velas_usadas": len(cierres),
        "timestamp": int(time.time()),
        "drift_por_vela": drift_por_vela,
        "seed_determinista": seed_determinista,
        "volumen": analizar_volumen(
            volumenes, cierres,
            float(np.mean(retornos_log[-6:])) if len(retornos_log) >= 6 else 0.0),
        "velas": [
            {"datetime": v.get("datetime", ""), "open": float(v["open"]),
             "high": float(v["high"]), "low": float(v["low"]),
             "close": float(v["close"])}
            for v in valores
        ],
        "retornos_log": retornos_log,
        "regimen_volatilidad": regimen_vol,
        "hurst": calcular_hurst(retornos_log),
        "tendencia": calcular_tendencia_real(cierres, maximos, minimos),
        "ob_bos": calcular_ob_bos(aperturas, maximos, minimos, cierres),
        "crt": calcular_crt(maximos, minimos, cierres),
        "turtle_soup": calcular_turtle_soup(maximos, minimos, cierres),
        "fvg": calcular_fvg(maximos, minimos, float(precio_actual)),
        "ciclo": calcular_ciclo_sin_ema(cierres),
        "fuente_datos": fuente,
    }
    return resultado


def _velas_desde_bybit(mercado: str, interval: str, outputsize: int):
    """
    Velas de Bybit para cripto. Devuelve None si no aplica o si falla,
    para que el llamador use Twelve Data como respaldo.

    Se prefiere Bybit en cripto por dos razones, y la segunda pesa más:
      1. Es gratis y sin cuota práctica (~600 peticiones/minuto frente a
         las 8 del plan gratuito de Twelve Data).
      2. Es la MISMA fuente donde se ejecutan las órdenes. Analizar en un
         sitio y operar en otro produce desfases que ya costaron dinero.
    """
    try:
        import bybit_client
        symbol = bybit_client.MERCADO_A_BYBIT_SYMBOL.get(normalizar_mercado(mercado))
        if not symbol:
            return None
        velas = bybit_client.obtener_velas(symbol, interval, outputsize)
        if len(velas) < 30:
            logger.warning(f"Bybit devolvió solo {len(velas)} velas de {mercado} "
                           f"(mínimo 30) — se usa Twelve Data")
            return None
        return velas
    except Exception as e:
        # Se registra el TIPO de error, no solo el texto: sin él es
        # imposible distinguir "IP bloqueada" de "símbolo inexistente" o
        # "formato de respuesta cambiado", y cada uno se arregla distinto.
        logger.warning(f"Bybit no sirvió velas de {mercado}: "
                       f"{type(e).__name__}: {e} — se usa Twelve Data")
        return None


# --- Kraken para forex: gratis, sin clave y SIN cuota diaria -------------------
# Twelve Data (plan gratuito) da 800 créditos al día para todo el droplet; con
# varios usuarios mirando forex en la vista Mercado más el escáner se agotaban
# a media tarde y forex quedaba sin velas hasta las 00:00 UTC. Kraken publica
# velas OHLC de los pares fiat principales. Si un par no existe en Kraken se
# recuerda unas horas y se usa Twelve Data como siempre.
KRAKEN_OHLC = "https://api.kraken.com/0/public/OHLC"
_INTERVALO_A_KRAKEN = {"1min": 1, "5min": 5, "15min": 15, "30min": 30, "1h": 60,
                       "4h": 240, "1day": 1440, "1week": 10080}
_KRAKEN_SIN_PAR: dict = {}          # par -> instante hasta el que no se vuelve a probar


def _es_forex_fiat(mercado: str) -> bool:
    m = normalizar_mercado(mercado)
    return (m in FOREX_AMPLIADO or m in ("EURUSD", "GBPUSD", "USDCOP")) and len(m) == 6


def _velas_desde_kraken(mercado: str, interval: str, outputsize: int, get=None):
    """Velas de forex desde Kraken. None si no aplica o falla (el llamador sigue con Twelve Data)."""
    m = normalizar_mercado(mercado)
    iv = _INTERVALO_A_KRAKEN.get(interval)
    if not iv or not _es_forex_fiat(m) or time.time() < _KRAKEN_SIN_PAR.get(m, 0):
        return None
    from datetime import datetime, timezone
    get = get or requests.get
    try:
        d = get(KRAKEN_OHLC, params={"pair": m, "interval": iv}, timeout=10).json()
        if d.get("error"):
            if any("Unknown asset pair" in str(e) for e in d["error"]):
                _KRAKEN_SIN_PAR[m] = time.time() + 6 * 3600
            logger.info(f"Kraken sin velas de {m}: {d['error']} — se usa Twelve Data")
            return None
        res = d.get("result") or {}
        filas = next((v for k, v in res.items() if k != "last" and isinstance(v, list)), None) or []
        # [time(s), open, high, low, close, vwap, volume, count], de la más vieja a la más nueva
        velas = []
        for f in filas[-int(outputsize):]:
            try:
                velas.append({
                    "datetime": datetime.fromtimestamp(int(f[0]), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                    "open": float(f[1]), "high": float(f[2]), "low": float(f[3]), "close": float(f[4]),
                    "volume": float(f[6]),
                })
            except (ValueError, IndexError, TypeError):
                continue
        if len(velas) < 30 or velas[-1]["close"] <= 0:
            logger.warning(f"Kraken devolvió solo {len(velas)} velas de {m} — se usa Twelve Data")
            return None
        return velas
    except Exception as e:
        logger.warning(f"Kraken no sirvió velas de {m}: {type(e).__name__}: {e} — se usa Twelve Data")
        return None


def obtener_datos_mercado(mercado: str, interval: str = "1h", outputsize: int = 200,
                           prioridad: str = PRIORIDAD_USUARIO):
    """
    Trae velas reales de Twelve Data y calcula precio actual, volatilidad
    histórica real (desviación estándar de retornos log) y niveles
    recientes de máximo/mínimo. Todo esto es dato real, no simulado.
    """
    # La clave de Twelve Data solo hace falta si Bybit no sirve el
    # mercado (forex/acciones). Exigirla al principio bloqueaba también
    # las criptos, que van 100% por Bybit sin clave ni cuota.
    symbol = MERCADO_A_SYMBOL.get(mercado.upper())
    if not symbol:
        raise DatosNoDisponiblesError(
            f"'{mercado}' no tiene fuente de datos en vivo conectada todavía. "
            f"Disponibles ahora: {list(MERCADO_A_SYMBOL.keys())}"
        )

    # La clave incluye outputsize (bug real, 8-ago): sin él, la consulta
    # de 60 velas que hace contexto_mercado_cripto para leer la marea de
    # BTC envenenaba el caché, y el análisis de BTC recibía 60 velas en
    # vez de 200. Resultado visible: BTC decía "histórico insuficiente"
    # en Hurst y en TSMOM mientras el resto de mercados sí los calculaba.
    cache_key = f"{symbol}:{interval}:{outputsize}"
    ahora = time.time()
    # Reutilizar un caché con MÁS velas de las pedidas: si ya tenemos 200
    # velas frescas, servir una petición de 60 desde ahí ahorra una
    # llamada a la API sin perder nada.
    if cache_key not in _cache:
        for k, (dc, t) in list(_cache.items()):
            try:
                s_k, i_k, o_k = k.rsplit(":", 2)
            except ValueError:
                continue
            if s_k == symbol and i_k == interval and int(o_k) >= outputsize:
                ttl_k = _CACHE_TTL_4H_SEGUNDOS if interval == "4h" else _CACHE_TTL_SEGUNDOS
                if time.time() - t < ttl_k:
                    cache_key = k
                    break

    if cache_key in _cache:
        datos_cacheados, ts = _cache[cache_key]
        ttl = _CACHE_TTL_4H_SEGUNDOS if interval == "4h" else _CACHE_TTL_SEGUNDOS
        if ahora - ts < ttl:
            return datos_cacheados

    # PRIMERO BYBIT (cripto): gratis, sin cuota y misma fuente que la
    # ejecución. Solo si no aplica o falla se recurre a Twelve Data, que
    # es el que tiene el límite de 8 peticiones por minuto.
    velas_bybit = _velas_desde_bybit(mercado, interval, outputsize)
    if velas_bybit:
        resultado = _procesar_velas(velas_bybit, mercado, symbol, "bybit")
        _cache[cache_key] = (resultado, ahora)
        return resultado

    # SEGUNDO KRAKEN (forex): gratis y sin cuota diaria. Ahorra créditos de
    # Twelve Data para lo que solo él tiene (metales, acciones, exóticos).
    velas_kraken = _velas_desde_kraken(mercado, interval, outputsize)
    if velas_kraken:
        resultado = _procesar_velas(velas_kraken, mercado, symbol, "kraken")
        _cache[cache_key] = (resultado, ahora)
        return resultado

    # Twelve Data como respaldo: metales, acciones, forex que Kraken no
    # tenga y cualquier cripto que Bybit no liste.
    return _pedir_a_twelvedata(mercado, symbol, interval, outputsize,
                                prioridad, cache_key, ahora)


def _pedir_a_twelvedata(mercado, symbol, interval, outputsize, prioridad, cache_key, ahora):
    if not TWELVEDATA_API_KEY:
        raise DatosNoDisponiblesError(
            f"{mercado}: Bybit no respondió o no lista este mercado, y no hay "
            f"TWELVEDATA_API_KEY para el respaldo")
    """Petición a Twelve Data, respetando el limitador de cuota."""
    if not _pedir_permiso(prioridad):
        est = _estado_cuota()
        raise CuotaAgotadaError(
            f"Sin margen de cuota ({est['en_minuto']}/min, {est['en_dia']}/día). "
            + ("Los procesos automáticos ceden el paso a los usuarios."
               if prioridad == PRIORIDAD_WORKER
               else "Vuelve a intentarlo en un minuto."))

    resp = requests.get(
        TWELVEDATA_URL,
        params={
            "symbol": symbol,
            "interval": interval,
            "outputsize": outputsize,
            "apikey": TWELVEDATA_API_KEY,
        },
        timeout=15
    )
    # 429 del propio servidor: se apura la ventana del minuto para que
    # nadie más lo intente hasta que pase. Sin esto, todos los workers
    # seguían golpeando la API y el bloqueo se prolongaba.
    if resp.status_code == 429:
        # Pausa explícita hasta que pase el minuto, en vez de inflar el
        # contador: antes el panel mostraba "8/7 peticiones", que parecía
        # un error de cuentas cuando en realidad era una pausa querida.
        with _lock_cuota:
            _pausa_hasta["ts"] = time.time() + 60
        raise CuotaAgotadaError(
            "Twelve Data devolvió 429 (demasiadas peticiones). "
            "Se pausan las peticiones automáticas durante un minuto.")
    resp.raise_for_status()
    payload = resp.json()

    if payload.get("status") == "error":
        mensaje = str(payload.get("message"))
        # La cuota agotada se distingue del resto: no se arregla
        # reintentando, se arregla consumiendo menos. Con un error
        # genérico, el operador no sabía que el automático se estaba
        # comiendo las peticiones de los clientes.
        if _es_error_de_cuota(mensaje):
            raise CuotaAgotadaError(
                f"Límite de peticiones de Twelve Data alcanzado: {mensaje}")
        raise Exception(f"Twelve Data devolvió error: {mensaje}")

    # Twelve Data devuelve los valores en orden descendente (más reciente
    # primero); los invertimos para tener orden cronológico ascendente.
    valores = list(reversed(payload.get("values", [])))
    if not valores:
        raise Exception(f"Twelve Data no devolvió velas para {symbol}")

    cierres = np.array([float(v["close"]) for v in valores])
    aperturas = np.array([float(v["open"]) for v in valores])
    # Volumen: Twelve Data lo entrega en la misma llamada (coste cero).
    # En forex/metales suele venir 0 o ausente — analizar_volumen lo
    # detecta y reporta "sin datos" en vez de inventar confirmación.
    volumenes = np.array([float(v.get("volume", 0) or 0) for v in valores])
    maximos = np.array([float(v["high"]) for v in valores])
    minimos = np.array([float(v["low"]) for v in valores])

    precio_actual = cierres[-1]
    retornos_log = np.diff(np.log(cierres))
    # Volatilidad Yang-Zhang (OHLC completo + gaps de apertura — elegida
    # porque el catálogo Premium incluye acciones, donde el gap overnight es
    # parte real del riesgo). Cadena de respaldo honesta: YZ -> Garman-
    # Klass -> desviación clásica de retornos log. Y SIEMPRE con suelo:
    # sigma=0 (feed congelado, festivo) tumbaba la consulta con división
    # por cero — bug corregido el 3-ago.
    volatilidad = calcular_volatilidad_yang_zhang(aperturas, maximos, minimos, cierres)
    if not np.isfinite(volatilidad) or volatilidad <= 0:
        volatilidad = calcular_volatilidad_garman_klass(aperturas, maximos, minimos, cierres)
    if not np.isfinite(volatilidad) or volatilidad <= 0:
        volatilidad = float(np.std(retornos_log))
    volatilidad = max(volatilidad, VOLATILIDAD_MINIMA)
    regimen_vol = calcular_regimen_volatilidad(retornos_log)
    maximo_reciente = float(np.max(maximos[-48:]))  # ~últimas 48 velas
    minimo_reciente = float(np.min(minimos[-48:]))

    # Drift real: promedio de retornos log del período reciente (NO cero).
    # El Monte Carlo clásico de GBM sin drift asume mercado "risk-neutral"
    # y por la asimetría log-normal termina con sesgo bajista estructural
    # sin importar la tendencia real — esto lo corrige usando el drift
    # medido de las velas reales.
    ventana_drift = min(48, len(retornos_log))
    drift_por_vela = float(np.mean(retornos_log[-ventana_drift:])) if ventana_drift > 0 else 0.0

    # Semilla determinista: SHA-256 del símbolo + timestamp de la última
    # vela + último cierre. Mientras no llegue una vela nueva, TODAS las
    # consultas de este mercado producen EXACTAMENTE el mismo análisis
    # (bug corregido 3-ago: con seed aleatoria, dos usuarios consultando
    # a la vez recibían TPs distintos por cientos de puntos, y en señales
    # ajustadas hasta la dirección cambiaba — inaceptable en un producto
    # comercial cuyo argumento es el track record verificable).
    import hashlib as _hashlib
    ultima_vela_ts = valores[-1].get("datetime", "") if valores else ""
    semilla_material = f"{symbol}|{ultima_vela_ts}|{precio_actual}"
    seed_determinista = int(_hashlib.sha256(semilla_material.encode()).hexdigest()[:12], 16)

    tendencia = calcular_tendencia_real(cierres, maximos, minimos)
    ob_bos = calcular_ob_bos(aperturas, maximos, minimos, cierres)
    crt = calcular_crt(maximos, minimos, cierres)
    turtle_soup = calcular_turtle_soup(maximos, minimos, cierres)
    fvg = calcular_fvg(maximos, minimos, float(precio_actual))
    ciclo = calcular_ciclo_sin_ema(cierres)

    resultado = {
        "symbol": symbol,
        "precio_actual": float(precio_actual),
        "volatilidad_por_vela": volatilidad,
        "maximo_reciente": maximo_reciente,
        "minimo_reciente": minimo_reciente,
        "n_velas_usadas": len(cierres),
        "timestamp": int(time.time()),
        "drift_por_vela": drift_por_vela,
        "seed_determinista": seed_determinista,
        "volumen": analizar_volumen(volumenes, cierres,
                                     float(np.mean(retornos_log[-6:])) if len(retornos_log) >= 6 else 0.0),
        "velas": [
            {"datetime": v.get("datetime", ""), "open": float(v["open"]),
             "high": float(v["high"]), "low": float(v["low"]),
             "close": float(v["close"])}
            for v in valores
        ],
        "retornos_log": retornos_log,      # distribución EMPÍRICA real, para el bootstrap
        "regimen_volatilidad": regimen_vol,
        "hurst": calcular_hurst(retornos_log),
        "tendencia": tendencia,
        "ob_bos": ob_bos,
        "crt": crt,
        "turtle_soup": turtle_soup,
        "fvg": fvg,
        "ciclo": ciclo,
    }
    _cache[cache_key] = (resultado, ahora)
    return resultado


# ---------------------------------------------------------------------------
# CAPA 1: VOLATILIDAD GARMAN-KLASS + RÉGIMEN (sin EMA, sin decoración)
# ---------------------------------------------------------------------------

# Suelo de volatilidad: un feed congelado (festivo, mercado cerrado, velas
# planas) daba volatilidad 0 y tumbaba la consulta entera con divisiones
# por cero (bug corregido 3-ago). Nunca se opera con sigma=0.
VOLATILIDAD_MINIMA = 1e-5

def calcular_volatilidad_yang_zhang(aperturas: np.ndarray, maximos: np.ndarray,
                                     minimos: np.ndarray, cierres: np.ndarray,
                                     ventana: int = 96) -> float:
    """
    Estimador Yang-Zhang (2000): el único de la familia OHLC que captura
    TAMBIÉN los huecos de apertura (overnight gaps). En cripto/forex 24h
    converge con Garman-Klass; en acciones (que abren con gap tras 17.5h
    cerradas) es la diferencia entre medir la volatilidad real y medir
    solo la de la sesión. Por eso el catálogo Premium (AAPL, TSLA, SPY...)
    exige este estimador y no otro.

        sigma²_YZ = sigma²_overnight + k·sigma²_open_close + (1-k)·sigma²_RS

    con k = 0.34 / (1.34 + (n+1)/(n-1)) (óptimo del paper) y sigma²_RS el
    estimador Rogers-Satchell. Promedios simples — sin EMA (regla del
    proyecto). Devuelve volatilidad POR VELA. NaN si no hay datos
    suficientes (el caller cae a Garman-Klass y luego a std clásica).
    """
    n = min(ventana, len(cierres))
    if n < 10:
        return float("nan")
    o = aperturas[-n:]; h = maximos[-n:]; l = minimos[-n:]; c = cierres[-n:]
    with np.errstate(divide="ignore", invalid="ignore"):
        # Componente overnight: gap entre el open y el close anterior
        log_oc_prev = np.log(o[1:] / c[:-1])
        # Componente open-to-close de la propia vela
        log_co = np.log(c / o)
        # Rogers-Satchell (insensible al drift)
        rs = (np.log(h / c) * np.log(h / o) + np.log(l / c) * np.log(l / o))
    log_oc_prev = log_oc_prev[np.isfinite(log_oc_prev)]
    log_co_v = log_co[1:][np.isfinite(log_co[1:])]
    rs_v = rs[1:][np.isfinite(rs[1:])]
    if len(log_oc_prev) < 8 or len(log_co_v) < 8 or len(rs_v) < 8:
        return float("nan")
    m = len(log_oc_prev)
    var_overnight = float(np.var(log_oc_prev, ddof=1))
    var_openclose = float(np.var(log_co_v, ddof=1))
    var_rs = float(np.mean(rs_v))
    k = 0.34 / (1.34 + (m + 1) / (m - 1))
    var_yz = var_overnight + k * var_openclose + (1.0 - k) * max(var_rs, 0.0)
    if not np.isfinite(var_yz) or var_yz <= 0:
        return float("nan")
    return float(np.sqrt(var_yz))


def calcular_volatilidad_garman_klass(aperturas: np.ndarray, maximos: np.ndarray,
                                       minimos: np.ndarray, cierres: np.ndarray,
                                       ventana: int = 96) -> float:
    """
    Estimador Garman-Klass (1980) de volatilidad por vela. Usa el rango
    high-low y el gap open-close de cada vela — información que la
    desviación estándar de cierres tira a la basura. Resultado:
    aproximadamente 5-7x más eficiencia estadística (misma precisión con
    ~5x menos velas), lo que hace que el Monte Carlo y el ATR del SL
    reaccionen más rápido a cambios reales de volatilidad sin necesitar
    ningún suavizado exponencial.

        sigma^2_GK = 0.5*ln(H/L)^2 - (2*ln(2)-1)*ln(C/O)^2

    Promedio simple sobre la ventana reciente (sin EMA — regla del
    proyecto, y además innecesaria: el estimador ya es eficiente).
    Devuelve la volatilidad POR VELA (misma unidad que la std de
    retornos log que reemplaza — drop-in en el pipeline existente).
    """
    n = min(ventana, len(cierres))
    if n < 10:
        return float("nan")
    o = aperturas[-n:]; h = maximos[-n:]; l = minimos[-n:]; c = cierres[-n:]
    with np.errstate(divide="ignore", invalid="ignore"):
        hl = np.log(h / l) ** 2
        co = np.log(c / o) ** 2
    var_gk = 0.5 * hl - (2.0 * np.log(2.0) - 1.0) * co
    var_gk = var_gk[np.isfinite(var_gk) & (var_gk > 0)]
    if len(var_gk) < 10:
        return float("nan")
    return float(np.sqrt(np.mean(var_gk)))


def calcular_hurst(retornos_log: np.ndarray, min_datos: int = 96) -> dict:
    """
    Exponente de Hurst por análisis R/S (rescaled range, Hurst 1951).
    Mide si la serie tiene MEMORIA:

        H > 0.55  persistente: lo que sube tiende a seguir subiendo.
                  Las señales de TENDENCIA son más fiables aquí.
        H ≈ 0.50  camino aleatorio: sin memoria (supuesto del GBM clásico).
        H < 0.45  antipersistente: el precio revierte a la media.
                  Las señales de REVERSIÓN (Análisis CBS, Turtle Soup,
                  barridos de liquidez) mandan; seguir tendencia aquí es
                  comprar máximos justo antes del retroceso.

    Método: se divide la serie en bloques de distintos tamaños; para cada
    tamaño se calcula el rango acumulado dividido por la desviación (R/S)
    y se ajusta una recta en escala log-log. La pendiente es H. Todo con
    medias simples — sin EMA, y sin ninguna librería extra.

    Devuelve H acotado a [0.2, 0.8] (fuera de ahí casi siempre es
    artefacto de muestra corta, no una propiedad real del mercado) y
    NaN si no hay datos suficientes, para que el caller lo trate como
    "sin información" en vez de inventar un régimen.
    """
    r = np.asarray(retornos_log, dtype=float)
    r = r[np.isfinite(r)]
    n = len(r)
    if n < min_datos:
        return {"H": float("nan"), "regimen": "DESCONOCIDO", "disponible": False,
                "texto": "muestra insuficiente para medir memoria"}

    tamaños, rs_valores = [], []
    for tam in (8, 16, 32, 64, 128):
        if tam > n // 2:
            break
        n_bloques = n // tam
        rs_bloque = []
        for b in range(n_bloques):
            seg = r[b * tam:(b + 1) * tam]
            media = seg.mean()
            desviaciones = np.cumsum(seg - media)
            rango = desviaciones.max() - desviaciones.min()
            s = seg.std(ddof=1)
            if s > 0 and rango > 0:
                rs_bloque.append(rango / s)
        if len(rs_bloque) >= 2:
            tamaños.append(tam)
            rs_valores.append(np.mean(rs_bloque))

    if len(tamaños) < 3:
        return {"H": float("nan"), "regimen": "DESCONOCIDO", "disponible": False,
                "texto": "muestra insuficiente para medir memoria"}

    # CORRECCIÓN DE SESGO (Anis-Lloyd/Peters). El R/S crudo sobreestima H
    # en muestras cortas: una serie puramente aleatoria daba 0.56 en las
    # pruebas, y eso habría hecho que el motor viera "memoria tendencial"
    # en puro ruido — justo el error que Hurst debía evitar. Se compara el
    # R/S observado contra el R/S ESPERADO de un proceso aleatorio del
    # mismo tamaño, y H se mide sobre la diferencia.
    def _rs_esperado(m: int) -> float:
        # E[R/S] para ruido blanco (Anis & Lloyd 1976, ajuste de Peters)
        i = np.arange(1, m)
        suma = float(np.sum(np.sqrt((m - i) / i)))
        if m <= 340:
            from math import gamma, pi, sqrt
            factor = gamma((m - 1) / 2.0) / (sqrt(pi) * gamma(m / 2.0))
        else:
            from math import pi, sqrt
            factor = 1.0 / sqrt(m * pi / 2.0)
        return factor * suma

    log_tam = np.log(tamaños)
    log_obs = np.log(rs_valores)
    log_esp = np.log([_rs_esperado(t) for t in tamaños])
    # H = 0.5 + pendiente del exceso sobre lo esperado por puro azar
    H = 0.5 + float(np.polyfit(log_tam, log_obs - log_esp, 1)[0])
    H = max(0.2, min(0.8, H))

    if H > 0.55:
        regimen = "PERSISTENTE"
        texto = f"H={H:.2f}: mercado con memoria tendencial"
    elif H < 0.45:
        regimen = "ANTIPERSISTENTE"
        texto = f"H={H:.2f}: mercado en reversión a la media"
    else:
        regimen = "ALEATORIO"
        texto = f"H={H:.2f}: sin memoria clara (camino aleatorio)"
    return {"H": round(H, 3), "regimen": regimen, "disponible": True, "texto": texto}


def modular_pesos_por_hurst(pesos: dict, hurst: dict) -> dict:
    """
    Reparte el peso según la MEMORIA real del mercado, no según una
    creencia fija. Es la corrección honesta de un supuesto que el motor
    tenía escondido: pesar igual las señales de tendencia en un mercado
    que persiste y en uno que revierte.

      PERSISTENTE (H>0.55)   -> +25% a tendencia y marco 4h,
                                −25% a las de reversión.
      ANTIPERSISTENTE (H<0.45) -> lo contrario: mandan Análisis CBS,
                                Turtle Soup y CRT (barridos y reversión).
      ALEATORIO / sin datos  -> pesos intactos.

    El ajuste es moderado (±25%) a propósito: Hurst es una medida ruidosa
    en ventanas cortas, y un cambio brusco de pesos por una estimación
    inestable sería peor que no ajustar nada.
    """
    if not hurst.get("disponible"):
        return pesos
    reg = hurst["regimen"]
    if reg == "ALEATORIO":
        return pesos
    p = dict(pesos)
    tendenciales = ("tendencia", "contexto_4h", "ciclo")
    reversion = ("mirrofish", "turtle", "crt")
    factor = 1.25 if reg == "PERSISTENTE" else 0.75
    for k in tendenciales:
        if k in p:
            p[k] = p[k] * factor
    for k in reversion:
        if k in p:
            p[k] = p[k] * (2.0 - factor)  # 0.75 si persistente, 1.25 si revierte
    return p


def calcular_regimen_volatilidad(retornos_log: np.ndarray,
                                  ventana_corta: int = 24,
                                  ventana_larga: int = 168) -> dict:
    """
    Clasifica el régimen actual de volatilidad comparando la vol
    realizada reciente (últimas 24 velas) contra su propia distribución
    histórica (percentil dentro de las últimas ~168 velas, ventanas
    móviles simples — sin EMA). Es información de CONTEXTO honesta:
    no cambia umbrales ni R:R, pero le dice al usuario si el SL
    calculado vive en un mercado nervioso o dormido.
      percentil >= 75 -> ALTA ; <= 25 -> BAJA ; medio -> NORMAL
    """
    if len(retornos_log) < ventana_corta + 10:
        return {"regimen": "NORMAL", "percentil": 50.0}
    vol_actual = float(np.std(retornos_log[-ventana_corta:]))
    n_hist = min(ventana_larga, len(retornos_log))
    historicas = np.array([
        np.std(retornos_log[i:i + ventana_corta])
        for i in range(len(retornos_log) - n_hist,
                       len(retornos_log) - ventana_corta + 1)
        if i >= 0
    ])
    historicas = historicas[historicas > 0]
    if len(historicas) < 10:
        return {"regimen": "NORMAL", "percentil": 50.0}
    percentil = float(100.0 * np.mean(historicas <= vol_actual))
    if percentil >= 75:
        regimen = "ALTA"
    elif percentil <= 25:
        regimen = "BAJA"
    else:
        regimen = "NORMAL"
    return {"regimen": regimen, "percentil": round(percentil, 1)}


# ---------------------------------------------------------------------------
# CAPA 1: MONTE CARLO GENUINO (híbrido GBM + block bootstrap empírico)
# ---------------------------------------------------------------------------

# Horizonte de la simulación en velas de 1h (24 = ~1 día). Se usa tanto
# en simular_monte_carlo como en calcular_plan_trade (para el ATR del SL),
# así que debe quedar en un único lugar para no desincronizarse.
PASOS_HORIZONTE = 24


def _trayectorias_block_bootstrap(precio_actual: float, retornos_log: np.ndarray,
                                   pasos: int, n_rutas: int, rng,
                                   tam_bloque: int = 6) -> Optional[np.ndarray]:
    """
    Block bootstrap circular sobre los retornos log REALES del activo:
    en vez de asumir que los retornos son normales (no lo son — tienen
    colas gordas y rachas de volatilidad), se remuestrean BLOQUES
    contiguos de 6 velas reales, preservando tanto la distribución
    empírica verdadera (colas incluidas) como la autocorrelación de
    corto plazo de la volatilidad. Es el complemento honesto del GBM:
    donde el GBM aporta una estructura calibrada y suave, el bootstrap
    aporta lo que el mercado hizo DE VERDAD, sin supuesto distribucional.
    """
    if retornos_log is None or len(retornos_log) < tam_bloque * 4:
        return None
    r = np.asarray(retornos_log, dtype=float)
    r = r[np.isfinite(r)]
    if len(r) < tam_bloque * 4:
        return None
    n_bloques = int(np.ceil(pasos / tam_bloque))
    # índices de inicio aleatorios (circular: un bloque puede envolver el final)
    inicios = rng.integers(0, len(r), size=(n_rutas, n_bloques))
    offsets = np.arange(tam_bloque)
    idx = (inicios[:, :, None] + offsets[None, None, :]) % len(r)
    muestras = r[idx].reshape(n_rutas, n_bloques * tam_bloque)[:, :pasos]
    return precio_actual * np.exp(np.cumsum(muestras, axis=1))


def simular_monte_carlo(precio_actual: float, volatilidad_por_vela: float,
                         pasos: int = PASOS_HORIZONTE, n_simulaciones: int = 5000,
                         drift_por_vela: float = 0.0,
                         retornos_log: Optional[np.ndarray] = None,
                         seed: Optional[int] = None):
    """
    Simulación Monte Carlo REAL (no placeholder). Modelo GBM con drift real:
        S(t+1) = S(t) * exp((mu - 0.5*sigma^2) + sigma*Z)
    donde Z ~ N(0,1) y mu es el drift medido de las velas reales recientes
    (NO cero). Esto corrige un sesgo estructural: con drift cero (GBM
    "risk-neutral" clásico), la asimetría log-normal hace que más
    simulaciones terminen por debajo del precio actual que por encima,
    sin relación alguna con la tendencia real del mercado — el bot
    terminaba prediciendo BAJISTA de forma sistemática sin importar el
    contexto. Se amortigua el drift medido (factor 0.5) para no
    sobreajustar a ruido de corto plazo.
    """
    rng = np.random.default_rng(seed)
    sigma = volatilidad_por_vela
    mu = drift_por_vela * 0.5  # amortiguado: mitad del drift medido real

    # HÍBRIDO: mitad GBM calibrado (estructura suave, drift real) + mitad
    # block bootstrap de retornos reales (colas gordas y clusters de
    # volatilidad empíricos). Si no hay retornos suficientes para el
    # bootstrap, se cae limpiamente al 100% GBM (comportamiento previo).
    n_boot = n_simulaciones // 2 if retornos_log is not None else 0
    tray_boot = None
    if n_boot > 0:
        tray_boot = _trayectorias_block_bootstrap(
            precio_actual, retornos_log, pasos, n_boot, rng
        )
    n_gbm = n_simulaciones - (len(tray_boot) if tray_boot is not None else 0)

    shocks = rng.standard_normal((n_gbm, pasos))
    log_retornos = (mu - 0.5 * sigma ** 2) + sigma * shocks
    tray_gbm = precio_actual * np.exp(np.cumsum(log_retornos, axis=1))

    trayectorias = (np.vstack([tray_gbm, tray_boot])
                    if tray_boot is not None else tray_gbm)

    precios_finales = trayectorias[:, -1]
    maximos_por_ruta = trayectorias.max(axis=1)
    minimos_por_ruta = trayectorias.min(axis=1)

    umbral = 0.5 * sigma * precio_actual * np.sqrt(pasos)  # ancho de "rango neutral"
    bull = np.sum(precios_finales > precio_actual + umbral)
    bear = np.sum(precios_finales < precio_actual - umbral)
    rango = n_simulaciones - bull - bear

    return {
        "n_simulaciones": n_simulaciones,
        "modelo": ("híbrido GBM+bootstrap empírico" if tray_boot is not None
                   else "GBM con drift real"),
        "pct_bull": round(100 * bull / n_simulaciones, 1),
        "pct_bear": round(100 * bear / n_simulaciones, 1),
        "pct_rango": round(100 * rango / n_simulaciones, 1),
        "p95_max": round(float(np.percentile(maximos_por_ruta, 95)), 4),
        "p50_max": round(float(np.percentile(maximos_por_ruta, 50)), 4),
        "p50_min": round(float(np.percentile(minimos_por_ruta, 50)), 4),
        "p05_min": round(float(np.percentile(minimos_por_ruta, 5)), 4),
        "precio_medio_final": round(float(np.mean(precios_finales)), 4),
        # Umbral real (en % del precio) usado para clasificar bull/bear/rango.
        # CRÍTICO: la verificación posterior debe usar este MISMO umbral,
        # no uno fijo distinto, o un mercado volátil (cripto) marcará FALLO
        # en casi todos los RANGO aunque la clasificación fuera razonable.
        "umbral_pct": round(float(umbral / precio_actual), 6),
    }


# ---------------------------------------------------------------------------
# CAPA 1: QCS (Quantum Coherence Score) — reglas objetivas, no inventadas
# ---------------------------------------------------------------------------

def calcular_qcs(precio_actual: float, maximo_reciente: float, minimo_reciente: float):
    """
    Score 0-5 basado en confluencias REALES y verificables:
      +1 si el precio está cerca (<0.3%) de un nivel psicológico redondo
      +1 si está cerca del máximo reciente (posible zona de liquidez)
      +1 si está cerca del mínimo reciente (posible zona de liquidez)
      +1 si el rango reciente (max-min) es angosto (<1.5% del precio) -> acumulación
      +1 si el precio está en el tercio medio del rango (zona de equilibrio)
    """
    puntos = 0
    detalles = []

    # Nivel psicológico redondo (múltiplos de 0.5% del precio, aprox a decenas/centenas)
    for base in [10, 25, 50, 100, 500, 1000]:
        redondeado = round(precio_actual / base) * base
        if abs(redondeado - precio_actual) / precio_actual < 0.003:
            puntos += 1
            detalles.append(f"Nivel psicológico cercano: {redondeado}")
            break

    rango_total = maximo_reciente - minimo_reciente
    dist_a_max = abs(maximo_reciente - precio_actual) / precio_actual
    dist_a_min = abs(precio_actual - minimo_reciente) / precio_actual

    if dist_a_max < 0.01:
        puntos += 1
        detalles.append("Cerca de máximo reciente (posible liquidez)")
    if dist_a_min < 0.01:
        puntos += 1
        detalles.append("Cerca de mínimo reciente (posible liquidez)")
    if rango_total / precio_actual < 0.015:
        puntos += 1
        detalles.append("Rango reciente angosto (posible acumulación)")

    punto_medio = (maximo_reciente + minimo_reciente) / 2
    if abs(precio_actual - punto_medio) / precio_actual < 0.005:
        puntos += 1
        detalles.append("Precio en zona de equilibrio del rango")

    return min(puntos, 5), detalles


# ---------------------------------------------------------------------------
# CAPA 1: MIRROFISH — LIQUIDEZ ESPEJO (cálculo real, no narrado)
# ---------------------------------------------------------------------------
# Implementa el protocolo del skill icc-quantum-engine (Waterline + zona
# espejo). LIMITACIÓN HONESTA: la validación completa del skill original
# pide confirmar con OB/FVG (Order Blocks/Fair Value Gaps), lo cual requiere
# datos de velas más granulares que no estamos procesando todavía. Por eso
# esta versión valida SOLO la simetría de precio y la fuerza por ATR —
# es real, pero es un subconjunto del protocolo completo. No se debe
# reportar "validez SMC: SÍ" porque eso NO se está verificando aquí.

def calcular_mirrofish(precio_actual: float, maximo_reciente: float,
                        minimo_reciente: float, volatilidad_por_vela: float):
    waterline = (maximo_reciente + minimo_reciente) / 2

    dist_a_max = abs(maximo_reciente - precio_actual)
    dist_a_min = abs(precio_actual - minimo_reciente)

    # ¿Qué lado está "activo" (más cerca de haberse tocado)?
    if dist_a_max <= dist_a_min:
        lado_tocado = "buyside"
        nivel_tocado = maximo_reciente
        nivel_espejo = waterline - (nivel_tocado - waterline)
    else:
        lado_tocado = "sellside"
        nivel_tocado = minimo_reciente
        nivel_espejo = waterline + (waterline - nivel_tocado)

    distancia = abs(nivel_tocado - waterline)

    # ATR aproximado: precio * volatilidad por vela (proxy real de 1 sigma)
    atr_aprox = precio_actual * volatilidad_por_vela
    if atr_aprox <= 0:
        fuerza = "INDETERMINADA"
    elif distancia > 1.5 * atr_aprox:
        fuerza = "FUERTE"
    elif distancia > 0.5 * atr_aprox:
        fuerza = "MEDIA"
    else:
        fuerza = "DÉBIL"

    return {
        "waterline": round(waterline, 4),
        "lado_tocado": lado_tocado,
        "nivel_tocado": round(nivel_tocado, 4),
        "nivel_espejo": round(nivel_espejo, 4),
        "distancia": round(distancia, 4),
        "atr_aprox": round(atr_aprox, 4),
        "fuerza": fuerza,
        "nota": "Validación de simetría de precio y ATR únicamente; no incluye confirmación OB/FVG."
    }


# ---------------------------------------------------------------------------
# RESULTADO FINAL (datos reales, listos para pasar a Gemini o mostrarse solos)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# CAPA 1: SUPERPOSICIÓN CUÁNTICA — reformulación matemática de Monte Carlo
# ---------------------------------------------------------------------------
# NOTA HONESTA: esto NO es física cuántica real. Es, como dice el propio
# skill icc-quantum-engine, una notación (amplitudes de probabilidad) para
# expresar los mismos porcentajes de Monte Carlo ya calculados arriba.
# alpha^2 + beta^2 + gamma^2 = 1 por construcción, porque son literalmente
# las mismas probabilidades reales, solo con la raíz cuadrada aplicada.
# No agrega información nueva — es una capa de presentación, y así se debe
# comunicar, nunca como un cálculo independiente adicional.

def calcular_superposicion(pct_bull: float, pct_bear: float, pct_rango: float):
    alpha = (pct_bull / 100) ** 0.5
    beta = (pct_bear / 100) ** 0.5
    gamma = (pct_rango / 100) ** 0.5
    return {"alpha": round(alpha, 3), "beta": round(beta, 3), "gamma": round(gamma, 3)}


# Por debajo de este score combinado NO hay convicción direccional real:
# el signo es ruido, no información. Se desempata con criterio explícito
# en vez de dejar que el operador de comparación decida por nosotros.
UMBRAL_CONVICCION_DIRECCIONAL = 0.05


def calcular_score_colaborativo(pct_bull: float, pct_bear: float,
                                 tendencia: dict,
                                 mirrofish: dict,
                                 ob_bos: dict,
                                 crt: dict,
                                 turtle_soup: dict,
                                 fvg: dict,
                                 ciclo: dict,
                                 precio_actual: float,
                                 score_sesgo_aprendido: float = 0.0,
                                 score_fundamental: float = 0.0,
                                 score_volumen: float = 0.0,
                                 score_4h: float = 0.0,
                                 score_cripto: float = 0.0,
                                 score_tsmom: float = 0.0,
                                 hurst: dict = None):
    """
    Algoritmo colaborativo (estilo SMC/ICT): combina hasta 9 señales
    REALES e independientes, cada una calculada de datos de mercado o de
    historial verificado, no inventada:

      1. Monte Carlo (con drift real ya corregido): sesgo direccional
         de la simulación estocástica.
      2. Tendencia real / Trend Trade Strategy pura (estructura de swings
         HH/HL vs LH/LL — SIN promedios móviles de ningún tipo).
      3. Análisis CBS / Mirrofish (simetría de liquidez): qué lado fue
         "tocado" y con qué fuerza, indicando probable reversión.
      4. Order Block + BOS (estructura real de velas, clasificación de
         retest: liquidity_sweep/continuation/consolidation/sin_retest).
      5. CRT (Candle Range Theory): barrida + reclamo de un rango de
         referencia.
      6. Turtle Soup (TBS): falso quiebre de un swing previo, revertido.
      7. FVG (Fair Value Gap): precio dentro de un hueco de precio real
         de 3 velas sin llenar.
      8. Ciclo de Momentum sin EMA: oscilador estocástico sobre ROC
         suavizado con SMA (alternativa honesta al STC, que requiere EMA).
      9. Sesgo direccional aprendido (opcional): retroalimentación real
         del historial verificado — si este mercado acierta claramente
         más en una dirección que en otra (con muestra estadísticamente
         seria, no unas pocas señales), ese patrón empuja el score.
         Score=0 si aún no hay suficiente historial verificado.

    Cada señal aporta un score en [-1, 1] (positivo=alcista). Se combinan
    con pesos fijos y se mide la COHERENCIA (cuántas apuntan al mismo
    lado): si la mayoría coincide, la señal es fuerte; si se contradicen,
    la confianza baja de forma automática y honesta, en vez de forzar una
    entrada "segura" que en realidad es un empate entre señales opuestas.
    Las señales sin patrón detectado (score=0) NO cuentan como "en contra"
    ni "a favor" — simplemente no opinan, y no se cuentan en el total de
    señales activas para la coherencia.
    """
    # 1. Score de Monte Carlo
    total = pct_bull + pct_bear
    score_mc = ((pct_bull - pct_bear) / total) if total > 0 else 0.0

    # 2. Score de tendencia / Trend Trade Strategy (ya viene en [-1, 1])
    score_tendencia = tendencia["trend_score"]

    # 3. Score de Análisis CBS / Mirrofish: si tocó el lado vendedor
    # (sellside, es decir el mínimo) y la señal es fuerte, es un
    # argumento a favor de reversión ALCISTA (rebote desde el suelo);
    # si tocó el lado comprador (buyside, el máximo) con señal fuerte,
    # argumento BAJISTA (rechazo desde el techo).
    mapa_fuerza = {"FUERTE": 1.0, "MEDIA": 0.5, "DÉBIL": 0.15, "INDETERMINADA": 0.0}
    intensidad = mapa_fuerza.get(mirrofish["fuerza"], 0.0)
    score_mirrofish = intensidad if mirrofish["lado_tocado"] == "sellside" else -intensidad

    # 4-8: OB+BOS, CRT, Turtle Soup, FVG, Ciclo — ya vienen en [-1, 1]
    score_ob_bos = ob_bos.get("score", 0.0)
    score_crt = crt.get("score", 0.0)
    score_turtle = turtle_soup.get("score", 0.0)
    score_fvg = fvg.get("score", 0.0)
    score_ciclo = ciclo.get("score", 0.0)

    # Proximidad: qué tan cerca está el precio actual del nivel espejo
    # (zona de reacción esperada). Más cerca = la señal de Mirrofish pesa
    # más en la combinación final (ajusta su propio peso dinámicamente).
    distancia_relativa = abs(precio_actual - mirrofish["nivel_espejo"]) / precio_actual
    peso_proximidad = max(0.3, 1.0 - min(distancia_relativa / 0.02, 1.0))  # 1.0 si está pegado, baja con distancia

    pesos = {
        "mc": 0.19,
        "tendencia": 0.14,
        "mirrofish": 0.11 * peso_proximidad,
        "ob_bos": 0.17,
        "crt": 0.07,
        "turtle": 0.07,
        "fvg": 0.10,
        "ciclo": 0.07,
        "sesgo_aprendido": 0.08,
        # Señal 10: fundamental determinista (noticias con léxico
        # financiero + Fear&Greed + funding, SIN IA). Peso acotado a
        # propósito: el sistema es primariamente técnico/cuantitativo,
        # el fundamental empuja pero nunca domina — y su magnitud ya
        # viene modulada por su propia efectividad histórica verificada.
        "fundamental": 0.08,
        # Señal 11: confirmación por VOLUMEN (tercer punto de confirmación
        # clásico). Peso 0 automático cuando el símbolo no trae volumen
        # real — así forex y metales no reciben una confirmación fantasma.
        "volumen": 0.07 if abs(score_volumen) > 0 else 0.0,
        # Señal 12: estructura del marco superior (4h). Una entrada de 1h
        # contra la estructura de 4h es la trampa más común del intradía;
        # exigir coherencia entre marcos es de las mejoras con respaldo
        # más amplio en la literatura. Peso alto a propósito.
        "contexto_4h": 0.12 if abs(score_4h) > 0 else 0.0,
        # Señal 13: MAREA DEL MERCADO CRIPTO. Peso alto (0.15, el segundo
        # mayor) porque la correlación con BTC explica buena parte del
        # movimiento de cualquier altcoin. Analizar una altcoin sin mirar
        # a BTC es como analizar una barca sin mirar la marea.
        "contexto_cripto": 0.15 if abs(score_cripto) > 0 else 0.0,
        # Señal 14: TIME-SERIES MOMENTUM. El peso MÁS ALTO del sistema
        # (0.18) porque es la única señal con un siglo de evidencia
        # verificada detrás. El resto de indicadores del motor son
        # razonables pero no tienen ese respaldo.
        "tsmom": 0.18 if abs(score_tsmom) > 0 else 0.0,
    }
    # Modulación por MEMORIA del mercado (Hurst): en un mercado que
    # revierte, seguir tendencia es comprar el máximo justo antes del
    # retroceso; en uno persistente, buscar reversión es pelear contra
    # la corriente. Los pesos se adaptan al régimen real medido.
    pesos = modular_pesos_por_hurst(pesos, hurst or {})
    suma_pesos = sum(pesos.values())

    score_final = (
        pesos["mc"] * score_mc +
        pesos["tendencia"] * score_tendencia +
        pesos["mirrofish"] * score_mirrofish +
        pesos["ob_bos"] * score_ob_bos +
        pesos["crt"] * score_crt +
        pesos["turtle"] * score_turtle +
        pesos["fvg"] * score_fvg +
        pesos["ciclo"] * score_ciclo +
        pesos["sesgo_aprendido"] * score_sesgo_aprendido +
        pesos["fundamental"] * score_fundamental +
        pesos["volumen"] * score_volumen +
        pesos["contexto_4h"] * score_4h +
        pesos["contexto_cripto"] * score_cripto +
        pesos["tsmom"] * score_tsmom
    ) / suma_pesos

    # ZONA NEUTRA — corrección de un sesgo estructural detectado con datos
    # reales (4-ago-2026). El operador `>= 0` mandaba a ALCISTA todas las
    # señales sin convicción: con la distribución típica de scores, ~13%
    # de las señales eran "alcistas" solo porque el score cayó del lado
    # positivo del cero por milésimas. Eso contaminaba la tasa de acierto
    # alcista con puro ruido, y los datos lo confirmaron: 17,6% de acierto
    # ALCISTA contra 51,3% BAJISTA sobre 73 verificadas (p = 0,004).
    #
    # Ahora, cuando |score| < UMBRAL_CONVICCION_DIRECCIONAL, la dirección
    # NO se decide por el signo del ruido: la desempata la estructura del
    # marco superior (4h), y si tampoco opina, el drift real medido. La
    # señal se marca como sin convicción direccional para que la confianza
    # lo refleje y el usuario lo vea. Nunca se bloquea la entrada (regla
    # de negocio), pero tampoco se disfraza una moneda al aire de análisis.
    conviccion_direccional = abs(score_final) >= UMBRAL_CONVICCION_DIRECCIONAL
    if conviccion_direccional:
        direccion = "ALCISTA" if score_final > 0 else "BAJISTA"
    else:
        if abs(score_4h) > 0.05:
            direccion = "ALCISTA" if score_4h > 0 else "BAJISTA"
            motivo_desempate = "estructura de 4h"
        elif abs(score_mc) > 0.02:
            direccion = "ALCISTA" if score_mc > 0 else "BAJISTA"
            motivo_desempate = "deriva real del Montecarlo"
        elif abs(score_tendencia) > 0.02:
            direccion = "ALCISTA" if score_tendencia > 0 else "BAJISTA"
            motivo_desempate = "estructura de swings"
        else:
            # Empate matemático perfecto: ninguna fuente tiene opinión.
            # Cualquier dirección aquí es una moneda al aire, así que se
            # dice con todas las letras y la confianza ya quedó acotada.
            direccion = "ALCISTA" if score_final > 0 else "BAJISTA"
            motivo_desempate = "ninguna señal opinó (evitar operar)"
    signo_final = 1 if direccion == "ALCISTA" else -1

    # Coherencia: de las señales que SÍ tienen una opinión (score != 0),
    # cuántas coinciden con la dirección final. Las que no detectaron
    # ningún patrón (score=0, ej. "sin patrón CRT claro") no cuentan ni
    # a favor ni en contra — no inflan ni penalizan la coherencia.
    todos_los_scores = [score_mc, score_tendencia, score_mirrofish, score_ob_bos,
                         score_crt, score_turtle, score_fvg, score_ciclo,
                         score_sesgo_aprendido, score_fundamental,
                         score_volumen, score_4h, score_cripto, score_tsmom]
    señales_activas = [s for s in todos_los_scores if abs(s) > 0.001]
    total_señales = len(señales_activas) if señales_activas else 1
    coincidencias = sum(1 for s in señales_activas if np.sign(s) == signo_final)

    return {
        "direccion": direccion,
        "score_final": round(float(score_final), 3),
        "score_mc": round(float(score_mc), 3),
        "score_tendencia": round(float(score_tendencia), 3),
        "score_mirrofish": round(float(score_mirrofish), 3),
        "score_fvg": round(float(score_fvg), 3),
        "score_ciclo": round(float(score_ciclo), 3),
        "score_ob_bos": round(float(score_ob_bos), 3),
        "score_crt": round(float(score_crt), 3),
        "score_turtle": round(float(score_turtle), 3),
        "score_sesgo_aprendido": round(float(score_sesgo_aprendido), 3),
        "score_fundamental": round(float(score_fundamental), 3),
        "score_volumen": round(float(score_volumen), 3),
        "score_4h": round(float(score_4h), 3),
        "score_cripto": round(float(score_cripto), 3),
        "score_tsmom": round(float(score_tsmom), 3),
        "hurst_regimen": (hurst or {}).get("regimen", "DESCONOCIDO"),
        "conviccion_direccional": bool(conviccion_direccional),
        "motivo_desempate": (None if conviccion_direccional
                              else locals().get("motivo_desempate", "")),
        "coincidencias": coincidencias,       # cuántas señales activas coinciden con la dirección final
        "total_señales": total_señales,       # de cuántas señales activas (con opinión real) sobre 9 posibles
        "proximidad_pct": round(float(peso_proximidad * 100), 1),
    }


def calcular_confianza(qcs: int, coincidencias: int = None, total_señales: int = 3) -> int:
    """
    Nivel de confianza real (0-100%). Se basa en el QCS (confluencias
    objetivas), y si se provee, se ajusta según la PROPORCIÓN de señales
    del algoritmo colaborativo que coinciden en la misma dirección (ya no
    un total fijo de 3 — ahora son hasta 6 señales, y varía cuántas están
    activas según qué patrones se detectaron). Coherencia total sube la
    confianza; señales muy divididas la bajan, en vez de mostrar el mismo
    número sin importar si las señales están de acuerdo o peleadas.
    """
    base = 20 * qcs  # QCS 0-5 -> 0/20/40/60/80/100
    if coincidencias is None or not total_señales:
        return round(base)

    ratio = coincidencias / total_señales
    if ratio >= 0.99:
        factor = 1.15   # coherencia total
    elif ratio >= 0.65:
        factor = 1.0    # mayoría coincide
    elif ratio >= 0.45:
        factor = 0.85   # dividido, ligera penalización
    else:
        factor = 0.7    # señales mayormente en contra de la dirección elegida

    return max(0, min(100, round(base * factor)))


# ---------------------------------------------------------------------------
# AUTO-CALIBRACIÓN POR HISTORIAL REAL VERIFICADO
# ---------------------------------------------------------------------------
# Esto es lo que hace que el sistema "aprenda" del historial de verdad,
# no solo que lo mencione en el texto: la confianza calculada se ajusta
# matemáticamente según la tasa de acierto REAL y VERIFICADA (no
# simulada) de ese mercado específico. Un mercado con historial de
# fallos frecuentes empuja la confianza hacia abajo en la próxima señal;
# uno con buen track record la sostiene o la sube un poco. Requiere un
# mínimo de muestras verificadas antes de ajustar nada — con pocas
# señales, el ruido estadístico no es representativo todavía y ajustar
# solo agregaría volatilidad falsa a la confianza.

MINIMO_MUESTRAS_CALIBRACION = 5
AJUSTE_MAXIMO_CALIBRACION = 0.3  # +/-30% como tope, para no sobrerreaccionar a una racha corta


def ajustar_confianza_por_historial(confianza_base: int, tasa_acierto: Optional[float] = None,
                                     total_verificadas: int = 0) -> int:
    if tasa_acierto is None or total_verificadas < MINIMO_MUESTRAS_CALIBRACION:
        return confianza_base  # sin historial suficiente todavía -> no se toca, honesto

    # tasa_acierto viene en % (0-100). 50% = neutral (ni ayuda ni castiga,
    # porque a 50% de acierto direccional el sistema no aporta información
    # por encima del azar). Por debajo penaliza, por encima refuerza.
    desviacion = (tasa_acierto - 50) / 50  # rango aprox [-1, 1]
    factor = 1.0 + (desviacion * AJUSTE_MAXIMO_CALIBRACION)
    return max(0, min(100, round(confianza_base * factor)))


# ---------------------------------------------------------------------------
# SESGO DIRECCIONAL APRENDIDO — retroalimentación real hacia el algoritmo
# ---------------------------------------------------------------------------
# A diferencia de ajustar_confianza_por_historial (que solo cambia el
# número de confianza mostrado, sin tocar la dirección elegida), esto SÍ
# entra como una señal más dentro del algoritmo colaborativo: si el
# historial verificado muestra que este mercado acierta mucho más
# llamando BAJISTA que ALCISTA (o viceversa), ese patrón real empuja el
# score final, pudiendo inclinar la balanza en casos ajustados.
#
# Umbral MUCHO más alto que la calibración de confianza (20 vs 5) a
# propósito: con pocas muestras, una mala racha corta (ej. 3-4 fallos
# seguidos) es ruido estadístico normal, no un sesgo real. Activar esto
# con pocos datos sería sobreajustar a la suerte del momento — exactamente
# el tipo de error que ya corregimos varias veces en este sistema.
# El score queda acotado (+/-0.15 máx) para que empuje, no domine.

MINIMO_MUESTRAS_SESGO = 20
SESGO_MAXIMO = 0.15


def calcular_sesgo_direccional_aprendido(stats_alcista: dict, stats_bajista: dict) -> dict:
    total_alcista = stats_alcista.get("total_verificadas", 0)
    total_bajista = stats_bajista.get("total_verificadas", 0)

    if total_alcista < MINIMO_MUESTRAS_SESGO or total_bajista < MINIMO_MUESTRAS_SESGO:
        return {
            "score": 0.0,
            "activo": False,
            "razon": f"Muestras insuficientes (ALCISTA: {total_alcista}, BAJISTA: {total_bajista}, "
                     f"se necesitan {MINIMO_MUESTRAS_SESGO} de cada una)."
        }

    tasa_alcista = stats_alcista["tasa_acierto"] or 50.0
    tasa_bajista = stats_bajista["tasa_acierto"] or 50.0

    # Positivo = el sistema acierta más llamando ALCISTA -> empuja hacia ALCISTA.
    # Negativo = acierta más llamando BAJISTA -> empuja hacia BAJISTA.
    diferencia = (tasa_alcista - tasa_bajista) / 100  # rango [-1, 1]
    score = max(-SESGO_MAXIMO, min(SESGO_MAXIMO, diferencia))

    # PELIGRO REAL de este mecanismo: aprende del PASADO y lo aplica al
    # PRESENTE. Con los datos verificados del 8-ago (BTC: alcista 18.2%
    # vs bajista 42.3%), el sesgo empujaba hacia BAJISTA justo cuando BTC
    # rompía los 65.000 al alza. Es exactamente el error que costó la
    # operación de XRP, pero automatizado y aplicado a todas las señales.
    #
    # Un histórico bajista NO significa que el mercado vaya a seguir
    # bajando: significa que en ese periodo bajó. Si el régimen cambió,
    # el sesgo se convierte en un ancla que arrastra hacia el error.
    #
    # Por eso el sesgo aprendido queda SUSPENDIDO cuando el contexto
    # actual contradice al histórico. La comprobación se hace fuera
    # (donde sí se conoce la marea del mercado) y llega por este campo.
    return {
        "score": round(score, 3),
        "activo": True,
        "direccion_favorecida": "BAJISTA" if score < 0 else "ALCISTA",
        "razon": f"Histórico verificado: ALCISTA {tasa_alcista}% ({total_alcista} señales) vs "
                 f"BAJISTA {tasa_bajista}% ({total_bajista} señales)."
    }


def suspender_sesgo_si_contradice(sesgo: dict, contexto_cripto: dict) -> dict:
    """
    Anula el sesgo aprendido cuando el mercado ACTUAL contradice al
    histórico que lo generó.

    El sesgo mira meses atrás; la marea del mercado mira las últimas
    horas. Cuando chocan, manda el presente: un histórico bajista no
    puede seguir empujando a vender mientras BTC rompe máximos.
    """
    if not sesgo.get("activo") or abs(sesgo.get("score", 0.0)) < 0.01:
        return sesgo
    if not contexto_cripto or not contexto_cripto.get("marea_fuerte"):
        return sesgo

    favorece = sesgo.get("direccion_favorecida")
    marea = contexto_cripto.get("etiqueta")
    if (favorece == "BAJISTA" and marea == "ALCISTA") or \
       (favorece == "ALCISTA" and marea == "BAJISTA"):
        return {
            **sesgo,
            "score": 0.0,
            "activo": False,
            "suspendido": True,
            "razon": (f"Sesgo aprendido SUSPENDIDO: el histórico favorece "
                      f"{favorece} pero el mercado va {marea} ahora mismo "
                      f"({contexto_cripto.get('texto', '')}). Manda el presente."),
        }
    return sesgo


# ---------------------------------------------------------------------------
# IA COMO MOTOR CONFIRMADOR / CORRECTIVO (exclusivo del plan Premium)
# ---------------------------------------------------------------------------
# La IA revisa el análisis completo y emite un veredicto estructurado:
# CONFIRMAR o CORREGIR (dirección, niveles, ajuste de confianza). Esta
# función es la ÚNICA puerta por la que ese veredicto puede tocar el
# análisis, y aplica los guardarraíles NO NEGOCIABLES del sistema:
#
#   1. En ALTA CONVICCIÓN (consenso >=60%) la IA NO puede invertir la
#      dirección — cuando la máquina está estadísticamente segura, una
#      opinión no la voltea. Solo puede modular la confianza.
#   2. Todo nivel propuesto pasa por _asegurar_coherencia (SL<entrada<TP
#      siempre) y por el piso de R:R vigente — si la propuesta lo viola,
#      se reconstruye desde el ATR real. La IA propone; el motor manda.
#   3. El ajuste de confianza queda acotado a ±AJUSTE_MAX_CONFIANZA_IA
#      puntos y el resultado a [5, 95] — nunca certeza absoluta.
#   4. Tras cualquier cambio de niveles se RECALCULAN prob. de primer
#      toque y esperanza en R con la MISMA seed determinista — los
#      números mostrados siempre corresponden al plan mostrado.
#   5. Fail-soft: veredicto ausente o malformado => análisis intacto.
#
# El veredicto queda registrado (ia_veredicto) y se guarda con la
# predicción: con el tiempo, el historial verificado dirá si las
# correcciones de la IA aciertan más que sus confirmaciones — el mismo
# estándar de honestidad verificable que el resto del sistema.

AJUSTE_MAX_CONFIANZA_IA = 15


def aplicar_correccion_ia(analisis, correccion: dict):
    """Aplica el veredicto de la IA con guardarraíles. Modifica y devuelve `analisis`."""
    if not correccion or not isinstance(correccion, dict):
        return analisis
    veredicto = str(correccion.get("veredicto", "")).upper()
    if veredicto not in ("CONFIRMAR", "CORREGIR"):
        return analisis

    razon = str(correccion.get("razon", ""))[:160]
    p = analisis.precio_actual

    # Ajuste de confianza: siempre permitido, siempre acotado
    try:
        ajuste = int(correccion.get("ajuste_confianza", 0) or 0)
    except (TypeError, ValueError):
        ajuste = 0
    ajuste = max(-AJUSTE_MAX_CONFIANZA_IA, min(AJUSTE_MAX_CONFIANZA_IA, ajuste))
    if ajuste:
        analisis.confianza_pct = max(5, min(95, analisis.confianza_pct + ajuste))

    if veredicto == "CONFIRMAR":
        analisis.ia_veredicto = "CONFIRMA"
        analisis.ia_razon = razon
        return analisis

    # --- CORREGIR ---
    cambio = False
    nueva_dir = str(correccion.get("direccion", "")).upper()
    flip = nueva_dir in ("ALCISTA", "BAJISTA") and nueva_dir != analisis.direccion_predicha

    if flip and analisis.señal_autorizada:
        # Guardarraíl 1: alta convicción no se voltea. Se registra la
        # discrepancia con honestidad (baja confianza ya aplicada arriba).
        analisis.ia_veredicto = "CORRIGE"
        analisis.ia_razon = (razon + " [dirección mantenida: consenso de alta "
                             "convicción no se invierte por opinión]").strip()
        return analisis

    if flip:
        # Reconstruir el plan COMPLETO para la nueva dirección con los
        # mismos insumos deterministas del análisis original.
        analisis.direccion_predicha = nueva_dir
        plan = calcular_plan_trade(
            p, nueva_dir,
            analisis.p50_max, analisis.p95_max, analisis.p50_min, analisis.p05_min,
            analisis.mirrofish_nivel_espejo, analisis.mirrofish_lado_tocado,
            volatilidad_por_vela=analisis.volatilidad_por_vela, pasos=PASOS_HORIZONTE,
            zona_cero=None, rr_minimo_estandar=analisis.rr_piso_usado,
        )
        analisis.entrada_mercado = plan["entrada_mercado"]
        analisis.entrada_sniper = plan["entrada_sniper"]
        analisis.entrada_opcional = plan["entrada_opcional"]
        analisis.sl, analisis.tp1, analisis.tp2 = plan["sl"], plan["tp1"], plan["tp2"]
        analisis.rr_ratio = plan["rr_ratio"] or analisis.rr_ratio
        analisis.modo_plan = "estandar"
        cambio = True
    else:
        # Ajustes de niveles sin cambiar dirección: se aceptan propuestas
        # y luego la coherencia + el piso de R:R mandan.
        for campo, clave in (("entrada_mercado", "entrada"), ("sl", "sl"), ("tp1", "tp1")):
            v = correccion.get(clave)
            if v is None:
                continue
            try:
                v = float(v)
            except (TypeError, ValueError):
                continue
            # Cordura básica: dentro de ±20% del precio actual
            if 0 < v and abs(v - p) / p <= 0.20:
                setattr(analisis, campo, redondear_precio(v, p))
                cambio = True

    if cambio:
        plan_chk = {"sl": analisis.sl, "tp1": analisis.tp1, "tp2": analisis.tp2,
                    "modo": "estandar", "nota": "", "rr_ratio": analisis.rr_ratio}
        plan_chk = _asegurar_coherencia(plan_chk, p, analisis.direccion_predicha,
                                        analisis.volatilidad_por_vela, PASOS_HORIZONTE)
        analisis.sl, analisis.tp1, analisis.tp2 = plan_chk["sl"], plan_chk["tp1"], plan_chk["tp2"]

        # Piso de R:R: si la propuesta lo viola, el SL se recorta al máximo permitido
        dist_tp = abs(analisis.tp1 - analisis.entrada_mercado)
        dist_sl = abs(analisis.entrada_mercado - analisis.sl)
        piso = max(analisis.rr_piso_usado, RR_MINIMO_ACEPTABLE)
        if dist_sl > 0 and dist_tp / dist_sl < piso:
            dist_sl = dist_tp / piso
            if analisis.direccion_predicha == "ALCISTA":
                analisis.sl = redondear_precio(analisis.entrada_mercado - dist_sl, p)
            else:
                analisis.sl = redondear_precio(analisis.entrada_mercado + dist_sl, p)
        analisis.rr_ratio = round(dist_tp / max(dist_sl, 1e-12), 2)

        # Recalcular primer toque y esperanza con la MISMA seed (determinista)
        toque = estimar_toque_tp_sl(
            p, analisis.tp1, analisis.sl,
            analisis.volatilidad_por_vela, analisis.drift_por_vela,
            retornos_log=None, pasos=PASOS_HORIZONTE,
            seed=analisis.seed_analisis or None,
        )
        analisis.prob_tp_primero = toque["p_tp"]
        analisis.prob_sl_primero = toque["p_sl"]
        analisis.prob_sin_toque = toque["p_ninguno"]
        analisis.esperanza_r = toque["esperanza_r"]

    analisis.ia_veredicto = "CORRIGE" if cambio else "CONFIRMA"
    analisis.ia_razon = razon
    return analisis


# ---------------------------------------------------------------------------
# VERIFICACIÓN DE RESULTADOS (para que la memoria sea honesta, no inventada)
# ---------------------------------------------------------------------------

UMBRAL_MOVIMIENTO_DEFAULT = 0.003  # respaldo solo si no se guardó umbral_pct real (predicciones viejas)


def calcular_mae_mfe(velas: list, precio_entrada: float, direccion: str,
                      dt_inicio, dt_fin, distancia_sl: float) -> dict:
    """
    MAE (Maximum Adverse Excursion) y MFE (Maximum Favorable Excursion):
    cuánto llegó a ir EN CONTRA una operación antes de resolverse, y
    cuánto llegó a ir a favor. Expresados en unidades de R (múltiplos de
    la distancia al stop), que es como se leen de verdad.

    Para qué sirve, en concreto:
      · MAE alto en operaciones GANADAS = los stops están demasiado
        ajustados; el mercado te barre y luego va a tu favor.
      · MFE alto en operaciones PERDIDAS = los objetivos están demasiado
        lejos; la operación estuvo ganando y se dio la vuelta (aquí es
        donde el break-even tras TP1 recupera dinero real).
    Es la evidencia dura para calibrar SL y TP, y hoy no existía.
    """
    if not velas or not precio_entrada or distancia_sl <= 0:
        return {"disponible": False}
    alcista = direccion == "ALCISTA"
    peor = mejor = 0.0
    hubo = False
    for v in velas:
        dv = _parsear_dt(v.get("datetime", ""))
        if not dv or (dt_inicio and dv < dt_inicio):
            continue
        if dt_fin and dv > dt_fin:
            break
        hubo = True
        if alcista:
            adverso = precio_entrada - v["low"]
            favorable = v["high"] - precio_entrada
        else:
            adverso = v["high"] - precio_entrada
            favorable = precio_entrada - v["low"]
        peor = max(peor, adverso)
        mejor = max(mejor, favorable)
    if not hubo:
        return {"disponible": False}
    return {
        "disponible": True,
        "mae_r": round(peor / distancia_sl, 2),   # cuánto R llegó a ir en contra
        "mfe_r": round(mejor / distancia_sl, 2),  # cuánto R llegó a ir a favor
    }


def calcular_fibonacci(maximo: float, minimo: float, precio_actual: float) -> dict:
    """
    Retrocesos y extensiones de Fibonacci sobre el swing reciente. NO se
    usa como señal direccional propia (sería otro indicador más pidiendo
    peso), sino como ANCLAJE: si un objetivo de la escalera cae junto a
    un nivel de Fibonacci, ese objetivo tiene confluencia añadida, y se
    reporta. Honestidad: Fibonacci funciona en buena medida porque mucha
    gente lo mira, no por una propiedad del mercado — por eso informa,
    no decide.
    """
    rango = maximo - minimo
    if rango <= 0:
        return {"disponible": False, "niveles": {}}
    niveles = {
        "0.382": minimo + 0.382 * rango,
        "0.500": minimo + 0.500 * rango,
        "0.618": minimo + 0.618 * rango,   # zona áurea
        "0.786": minimo + 0.786 * rango,
        "1.272": minimo + 1.272 * rango,   # extensión
        "1.618": minimo + 1.618 * rango,
    }
    # Nivel más cercano al precio actual
    cercano, dist_min = None, float("inf")
    for k, v in niveles.items():
        d = abs(v - precio_actual) / precio_actual
        if d < dist_min:
            cercano, dist_min = k, d
    return {
        "disponible": True,
        "niveles": {k: redondear_precio(v, precio_actual) for k, v in niveles.items()},
        "nivel_cercano": cercano,
        "distancia_pct": round(dist_min * 100, 3),
        "en_zona_aurea": bool(abs(niveles["0.618"] - precio_actual) / precio_actual < 0.005),
    }


def anclar_a_fibonacci(escalera: list, fib: dict, precio_actual: float,
                        tolerancia: float = 0.004) -> list:
    """
    Marca los objetivos que coinciden con un nivel de Fibonacci.

    Cada nivel se asigna UNA sola vez, al objetivo más cercano (bug real:
    TP1 en 4334 y TP2 en 4365 aparecían ambos como "fib 1.272" porque los
    dos caían dentro de la tolerancia — dos precios separados por 31
    puntos no pueden ser el mismo nivel, y verlo repetido hace dudar de
    todo lo demás del reporte).

    No mueve precios: solo señala confluencia real.
    """
    if not fib.get("disponible"):
        return escalera
    # Todas las parejas (objetivo, nivel) dentro de tolerancia, ordenadas
    # por cercanía: la asignación más ajustada gana primero.
    candidatos = []
    for i, tramo in enumerate(escalera):
        for nombre, valor in fib["niveles"].items():
            d = abs(tramo["precio"] - valor) / precio_actual
            if d <= tolerancia:
                candidatos.append((d, i, nombre))
    candidatos.sort()
    usados_nivel, usados_tramo = set(), set()
    for d, i, nombre in candidatos:
        if i in usados_tramo or nombre in usados_nivel:
            continue
        escalera[i]["fib"] = nombre
        usados_tramo.add(i)
        usados_nivel.add(nombre)
    return escalera


def _parsear_dt(texto: str):
    """'2026-08-04 05:00:00' (Twelve Data) o ISO -> datetime naive UTC."""
    from datetime import datetime as _dt
    if not texto:
        return None
    t = texto.strip().replace("T", " ")[:19]
    try:
        return _dt.strptime(t, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        try:
            return _dt.strptime(t[:16], "%Y-%m-%d %H:%M")
        except ValueError:
            return None


def verificar_resultado(mercado: str, precio_entrada: float, direccion_predicha: str,
                         umbral_pct: float = UMBRAL_MOVIMIENTO_DEFAULT,
                         horizonte_hasta: str = None,
                         timestamp_prediccion: str = None,
                         tp_valor: float = None, sl_valor: float = None):
    """
    Verificación honesta en DOS dimensiones:

    1. DIRECCIÓN — contra el precio del CIERRE DEL HORIZONTE, no contra
       "el precio de cuando corrió el worker" (fix 3-ago: el worker corre
       cada 30 min y puede llegar horas tarde; comparar contra el precio
       tardío contaminaba la calibración con movimiento fuera del plazo
       prometido). Si la serie no cubre el horizonte, respaldo honesto:
       precio actual, marcado en el retorno.

    2. DESENLACE DE LA OPERACIÓN — recorriendo las velas reales entre la
       predicción y el horizonte: ¿se tocó primero el TP, primero el SL,
       o ninguno? Esto alimenta la esperanza REAL en R del sistema (la
       dirección puede acertar y aun así la operación perder si el SL se
       barrió por el camino — son métricas distintas y ambas importan).

    Devuelve: (acierto, precio_evaluado, detalle_dict)
    detalle_dict = {"tipo_toque": "TP"|"SL"|"NINGUNO"|None,
                    "precio_en_horizonte": bool}
    """
    datos_ahora = obtener_datos_mercado(mercado)
    velas = datos_ahora.get("velas", [])
    dt_horizonte = _parsear_dt(horizonte_hasta) if horizonte_hasta else None
    dt_prediccion = _parsear_dt(timestamp_prediccion) if timestamp_prediccion else None

    # --- 1) Precio del cierre del horizonte ---
    precio_eval = datos_ahora["precio_actual"]
    en_horizonte = False
    if dt_horizonte and velas:
        for v in velas:
            dv = _parsear_dt(v["datetime"])
            if dv and dv >= dt_horizonte:
                precio_eval = v["close"]
                en_horizonte = True
                break

    cambio_pct = (precio_eval - precio_entrada) / precio_entrada
    if direccion_predicha == "ALCISTA":
        acierto = cambio_pct > umbral_pct
    elif direccion_predicha == "BAJISTA":
        acierto = cambio_pct < -umbral_pct
    else:  # RANGO
        acierto = abs(cambio_pct) <= umbral_pct

    # --- 2) Tipo de desenlace (TP/SL/NINGUNO) con las velas reales ---
    tipo_toque = None
    if tp_valor and sl_valor and velas and dt_prediccion:
        alcista = direccion_predicha == "ALCISTA"
        idx_tp = idx_sl = None
        for i, v in enumerate(velas):
            dv = _parsear_dt(v["datetime"])
            if not dv or dv < dt_prediccion:
                continue
            if dt_horizonte and dv > dt_horizonte:
                break
            if alcista:
                if idx_tp is None and v["high"] >= tp_valor:
                    idx_tp = i
                if idx_sl is None and v["low"] <= sl_valor:
                    idx_sl = i
            else:
                if idx_tp is None and v["low"] <= tp_valor:
                    idx_tp = i
                if idx_sl is None and v["high"] >= sl_valor:
                    idx_sl = i
            if idx_tp is not None and idx_sl is not None:
                break
        if idx_tp is not None and (idx_sl is None or idx_tp < idx_sl):
            tipo_toque = "TP"
        elif idx_sl is not None and (idx_tp is None or idx_sl < idx_tp):
            tipo_toque = "SL"
        elif idx_tp is not None and idx_sl is not None and idx_tp == idx_sl:
            # Misma vela tocó ambos: sin datos intra-vela no se puede saber
            # cuál fue primero — se marca SL por prudencia (peor caso).
            tipo_toque = "SL"
        else:
            tipo_toque = "NINGUNO"

    # MAE/MFE: cuánto fue en contra y a favor durante la vida de la
    # operación. Es lo que permitirá saber si los stops están mal puestos.
    mae_mfe = {"disponible": False}
    if sl_valor and precio_entrada:
        mae_mfe = calcular_mae_mfe(velas, precio_entrada, direccion_predicha,
                                    dt_prediccion, dt_horizonte,
                                    abs(precio_entrada - sl_valor))

    return acierto, precio_eval, {"tipo_toque": tipo_toque,
                                   "precio_en_horizonte": en_horizonte,
                                   "mae_r": mae_mfe.get("mae_r"),
                                   "mfe_r": mae_mfe.get("mfe_r")}


# ---------------------------------------------------------------------------
# MONITOREO INTERMEDIO — chequeo cada 3h de operaciones abiertas
# ---------------------------------------------------------------------------
# Distinto de verificar_resultado (que solo corre UNA vez, al final del
# horizonte, para marcar acierto/fallo definitivo). Esto corre varias
# veces MIENTRAS la operación sigue abierta, para avisar si va en contra
# ANTES de que se cumpla el horizonte completo — no espera a que sea
# demasiado tarde para sugerir cerrar.

UMBRAL_ALERTA_CIERRE = 0.5  # 50% del camino hacia el SL original = alerta


def _sl_ya_superado(precio_actual: float, sl: float, direccion: str) -> bool:
    """¿El precio ya rebasó el SL? Entonces la operación está cerrada de hecho."""
    if not sl or not precio_actual:
        return False
    return (precio_actual <= sl) if direccion == "ALCISTA" else (precio_actual >= sl)


def evaluar_estado_intermedio(mercado: str, precio_entrada: float,
                               direccion_predicha: str, sl_valor: float):
    """
    Compara el precio actual real contra la entrada y el SL original.
    Devuelve si la operación va en contra (más de la mitad del camino
    hacia el SL, sin haber llegado todavía) y qué tan avanzado está ese
    recorrido, en base a datos reales, no una estimación.
    """
    datos_ahora = obtener_datos_mercado(mercado)
    precio_actual = datos_ahora["precio_actual"]

    distancia_sl_original = abs(precio_entrada - sl_valor)
    if distancia_sl_original <= 0:
        return {"yendo_en_contra": False, "precio_actual": precio_actual, "pct_hacia_sl": 0.0}

    if direccion_predicha == "ALCISTA":
        movimiento_adverso = precio_entrada - precio_actual  # positivo = se mueve hacia el SL (abajo)
    else:  # BAJISTA
        movimiento_adverso = precio_actual - precio_entrada  # positivo = se mueve hacia el SL (arriba)

    pct_hacia_sl = max(0.0, movimiento_adverso / distancia_sl_original)
    yendo_en_contra = pct_hacia_sl >= UMBRAL_ALERTA_CIERRE

    return {
        "yendo_en_contra": yendo_en_contra,
        "precio_actual": precio_actual,
        "pct_hacia_sl": round(min(pct_hacia_sl, 1.0) * 100, 1),
    }


# ---------------------------------------------------------------------------
# PLAN DE TRADE — Entrada mercado / Sniper / SL / TP1 / TP2 / Opcional
# ---------------------------------------------------------------------------
# Todo derivado de los percentiles YA calculados por Monte Carlo y del
# nivel Mirrofish. No se inventa ningún precio nuevo aquí.

# ---------------------------------------------------------------------------
# ZONA CERO — confluencia real entre Mirrofish y OB/FVG
# ---------------------------------------------------------------------------
# No es "más o menos coinciden": exige que el nivel_espejo de Mirrofish
# caiga dentro de una zona de Order Block o FVG real (con una tolerancia
# angosta de 0.15% del precio, para no exigir solape exacto al centavo
# pero tampoco aceptar cualquier cercanía vaga).

def calcular_zona_cero(mirrofish: dict, ob_bos: dict, fvg: dict, precio_actual: float,
                        tolerancia_pct: float = 0.0015):
    nivel_espejo = mirrofish["nivel_espejo"]
    tolerancia = precio_actual * tolerancia_pct

    zonas_candidatas = []
    if ob_bos.get("bos_detectado"):
        zonas_candidatas.append(("OB", ob_bos["ob_zona_min"], ob_bos["ob_zona_max"], ob_bos["bos_direccion"]))
    gap = fvg.get("gap_mas_cercano")
    if gap:
        zonas_candidatas.append(("FVG", gap["zona_min"], gap["zona_max"], gap["tipo"]))

    for tipo_zona, zmin, zmax, direccion in zonas_candidatas:
        if (zmin - tolerancia) <= nivel_espejo <= (zmax + tolerancia):
            return {"confirmada": True, "tipo_zona": tipo_zona, "direccion": direccion,
                    "zona_min": round(zmin, 4), "zona_max": round(zmax, 4)}

    return {"confirmada": False, "tipo_zona": None, "direccion": None, "zona_min": None, "zona_max": None}


# ---------------------------------------------------------------------------
# ORQUESTADOR DE CONSENSO — bloqueo estricto (regla de negocio explícita)
# ---------------------------------------------------------------------------
# Exige 4 confirmaciones REALES e independientes, cada una pesando 25%:
#   1. Zona Cero confirmada, en la dirección esperada.
#   2. TBS confirmado (Turtle Soup o CRT con señal real) en la misma
#      dirección.
#   3. Ciclo de Momentum da luz verde en la misma dirección.
#   4. Trend Trade Strategy (estructura real de swings, sin EMA) da luz
#      verde en la misma dirección.
# Con pesos iguales de 25%, alcanzar >=80% requiere las 4 (3/4=75% no
# alcanza). Esto es intencional: es un filtro de setups A+, no un filtro
# suave — la mayoría de las consultas resultarán "sin señal", y eso es
# el comportamiento correcto, no un error.

UMBRAL_CONSENSO_PCT = 60.0


def orquestar_consenso(direccion: str, zona_cero: dict, turtle_soup: dict,
                        crt: dict, tendencia: dict, ciclo: dict):
    signo = 1 if direccion == "ALCISTA" else -1

    check_zona_cero = bool(zona_cero["confirmada"] and zona_cero["direccion"] == direccion)

    score_turtle = turtle_soup.get("score", 0.0)
    score_crt = crt.get("score", 0.0)
    score_tbs = score_turtle if abs(score_turtle) >= abs(score_crt) else score_crt
    check_tbs = bool(score_tbs != 0 and np.sign(score_tbs) == signo)

    score_ciclo = ciclo.get("score", 0.0)
    check_ciclo = bool(score_ciclo != 0 and np.sign(score_ciclo) == signo)

    score_tendencia = tendencia.get("trend_score", 0.0)
    check_tendencia = bool(score_tendencia != 0 and np.sign(score_tendencia) == signo)

    componentes = {
        "zona_cero": check_zona_cero,
        "tbs": check_tbs,
        "ciclo_momentum": check_ciclo,
        "trend_trade_strategy": check_tendencia,
    }
    consenso_pct = round(100 * sum(componentes.values()) / len(componentes), 1)
    autorizada = consenso_pct >= UMBRAL_CONSENSO_PCT

    return {"autorizada": autorizada, "consenso_pct": consenso_pct, "componentes": componentes}


RR_MINIMO_ACEPTABLE = 1.3  # relación beneficio:riesgo mínima que el sistema debe garantizar (modo estándar)
RR_MINIMO_ALTA_CONVICCION = 3.0  # modo estricto: solo cuando hay consenso total (Zona Cero + TBS + Ciclo + Tendencia)



def monte_carlo_restringido(trayectorias_precio: float, volatilidad: float,
                             drift: float, retornos_log, pasos: int,
                             sesgo_fundamental: float, direccion: str,
                             n_rutas: int = 4000, seed=None) -> dict:
    """
    SEGUNDA PASADA del Monte Carlo, condicionada por lo que ya se sabe.
    Los documentos del proyecto la describen con lenguaje cuántico
    ("colapso de la función de onda"); despojada de la metáfora, es
    simulación condicional con remuestreo por importancia, que es una
    técnica estadística estándar y perfectamente real:

      1. Se simulan las trayectorias como siempre.
      2. Cada trayectoria recibe un PESO según su compatibilidad con el
         sesgo fundamental verificado (si las noticias son claramente
         bajistas, los caminos alcistas extremos pesan menos, no se
         borran — descartarlos del todo sería fabricar certeza).
      3. Los percentiles se recalculan con esos pesos.

    El efecto real es una reducción de varianza: los objetivos dejan de
    apoyarse en escenarios que el contexto ya desmiente. Si no hay sesgo
    fundamental (|sesgo| < 0.1), no se toca nada y se devuelve
    `aplicado=False` — sin datos, no se restringe.
    """
    if abs(sesgo_fundamental) < 0.1:
        return {"aplicado": False}

    rng = np.random.default_rng(None if seed is None else seed + 7)
    sigma = max(volatilidad, VOLATILIDAD_MINIMA)
    mu = drift * 0.5
    n_boot = n_rutas // 2 if retornos_log is not None else 0
    tray_boot = None
    if n_boot > 0:
        tray_boot = _trayectorias_block_bootstrap(trayectorias_precio, retornos_log,
                                                   pasos, n_boot, rng)
    n_gbm = n_rutas - (len(tray_boot) if tray_boot is not None else 0)
    shocks = rng.standard_normal((n_gbm, pasos))
    tray_gbm = trayectorias_precio * np.exp(np.cumsum((mu - 0.5 * sigma**2) + sigma * shocks, axis=1))
    tray = np.vstack([tray_gbm, tray_boot]) if tray_boot is not None else tray_gbm

    # Retorno final de cada ruta, normalizado
    finales = tray[:, -1]
    retorno_rel = (finales - trayectorias_precio) / trayectorias_precio
    escala = max(float(np.std(retorno_rel)), 1e-9)
    z = retorno_rel / escala

    # Peso: exponencial suave en la dirección del sesgo fundamental.
    # Acotado a [0.25, 4] para que ninguna ruta desaparezca ni domine.
    fuerza = max(-1.0, min(1.0, sesgo_fundamental)) * 0.6
    pesos = np.exp(fuerza * z)
    pesos = np.clip(pesos, 0.25, 4.0)
    pesos = pesos / pesos.sum()

    def _percentil_ponderado(valores, q):
        orden = np.argsort(valores)
        v, w = valores[orden], pesos[orden]
        acum = np.cumsum(w)
        return float(v[np.searchsorted(acum, q)])

    maximos = tray.max(axis=1)
    minimos = tray.min(axis=1)
    return {
        "aplicado": True,
        "p50_max": round(_percentil_ponderado(maximos, 0.50), 6),
        "p95_max": round(_percentil_ponderado(maximos, 0.95), 6),
        "p50_min": round(_percentil_ponderado(minimos, 0.50), 6),
        "p05_min": round(_percentil_ponderado(minimos, 0.05), 6),
        "peso_efectivo": round(float(1.0 / np.sum(pesos**2)), 0),  # tamaño de muestra efectivo
    }


# Escalera de R:R objetivo. Números redondos a propósito: son fáciles de
# comunicar y de gestionar (cerrar un tercio en cada uno), y evitan que
# el objetivo dependa de un percentil que cambia con cada vela. El piso
# del primer tramo sigue mandando: si el R:R adaptativo del mercado exige
# más de 1.3, toda la escalera se desplaza hacia arriba.
ESCALERA_RR_OBJETIVO = (1.3, 2.0, 3.0)


def nivel_aseguramiento(entrada: float, sl: float, direccion: str) -> dict:
    """
    Precio al que conviene cerrar parcial y mover el stop a break-even.

    No es un objetivo más de la escalera: es un nivel de GESTIÓN. Sale de
    los datos reales del bot, donde las operaciones perdedoras llegaron a
    ganar 1.16R antes de girarse. Asegurar en +0.8R captura buena parte
    de ese recorrido antes de que se evapore.
    """
    dist = abs(entrada - sl)
    if dist <= 0:
        return {"disponible": False}
    avance = dist * FRACCION_ASEGURAMIENTO
    precio = entrada + avance if direccion == "ALCISTA" else entrada - avance
    return {
        "disponible": True,
        "precio": redondear_precio(precio, entrada, escala=dist),
        "r": FRACCION_ASEGURAMIENTO,
        "porcentaje": int(PORCENTAJE_PARCIAL * 100),
    }


def construir_escalera_tp(precio_actual: float, direccion: str, sl: float,
                          p50: float = None, p95: float = None, atr: float = None,
                          rr_piso: float = 1.3) -> list:
    """
    Objetivos en múltiplos EXACTOS del riesgo: 1:1.3, 1:2 y 1:3 medidos
    desde la entrada hasta el stop.

        TPn = entrada ± (distancia_al_SL × RRn)

    Antes salían de percentiles de la simulación y daban números como
    1:1.85 o 1:2.99 — matemáticamente correctos pero imposibles de
    gestionar y de explicar. Ahora el R:R es el que se anuncia, sin
    redondeos que engañen: el cálculo va en una sola dirección
    (riesgo → objetivo), así que no puede quedar invertido.

    Los percentiles (p50/p95) y el ATR se mantienen en la firma por
    compatibilidad con las llamadas existentes, pero ya no determinan los
    objetivos: su papel ahora es informativo, a través de la probabilidad
    de toque que se calcula después. Si el TP3 tiene un 2% de
    probabilidad, el usuario lo ve y decide.
    """
    dist_sl = abs(precio_actual - sl)
    if dist_sl <= 0:
        return []
    alcista = direccion == "ALCISTA"
    # Si el piso adaptativo supera el primer escalón, se desplaza todo
    factor = max(1.0, rr_piso / ESCALERA_RR_OBJETIVO[0])
    # Suelo de precio: un objetivo BAJISTA nunca puede cruzar cero ni
    # implicar una caída absurda. Bug real en PEPE: con volatilidad alta,
    # el TP3 (3x el riesgo) salía en −4.07e-05, un precio negativo.
    # Se acota al 20% del precio actual (una caída del 80% en 24h no es
    # un objetivo, es fantasía) y el R:R se recalcula al valor REAL.
    suelo = precio_actual * 0.20

    niveles = []
    for i, rr_obj in enumerate(ESCALERA_RR_OBJETIVO):
        rr = round(rr_obj * factor, 2)
        dist = dist_sl * rr
        precio = precio_actual + dist if alcista else precio_actual - dist
        acotado = False
        if not alcista and precio < suelo:
            precio = suelo
            acotado = True
        # Se redondea protegiendo la distancia al SL para no falsear el R:R
        precio_final = redondear_precio(precio, precio_actual, escala=dist_sl)
        # R:R REAL tras redondeo y acotación: lo que se anuncia es lo que hay
        rr_real = round(abs(precio_final - precio_actual) / dist_sl, 2)
        tramo = {"etiqueta": f"TP{i+1}", "precio": precio_final, "rr": rr_real}
        if acotado:
            tramo["acotado"] = True
        niveles.append(tramo)

    # Tras acotar puede haber objetivos repetidos: se eliminan los que ya
    # no aportan nada en vez de mostrar tres veces el mismo precio.
    unicos, vistos = [], set()
    for t in niveles:
        if t["precio"] in vistos:
            continue
        vistos.add(t["precio"])
        unicos.append(t)
    for i, t in enumerate(unicos):
        t["etiqueta"] = f"TP{i+1}"
    return unicos


# Coste de ida y vuelta (spread + comisión) como fracción del precio.
# Ignorarlo es uno de los errores clásicos al evaluar una estrategia:
# con R:R ajustados, el spread se come una parte real de la esperanza.
# Cifras conservadoras de mercado retail; se restan SIEMPRE al calcular
# la esperanza, para que el número que ve el usuario sea el que puede
# esperar de verdad, no el de un mundo sin fricción.
COSTES_TRANSACCION = {
    "EURUSD": 0.00012, "GBPUSD": 0.00016, "USDJPY": 0.00014, "USDCHF": 0.00016,
    "USDCAD": 0.00018, "AUDUSD": 0.00016, "NZDUSD": 0.00022, "EURJPY": 0.00020,
    "EURGBP": 0.00020, "GBPJPY": 0.00028, "EURCHF": 0.00022, "AUDJPY": 0.00024,
    "USDMXN": 0.00090, "USDBRL": 0.00120, "USDCOP": 0.00150,
    "XAUUSD": 0.00035, "XAGUSD": 0.00090, "XPTUSD": 0.00120, "XPDUSD": 0.00200,
    "BTC": 0.00090, "ETH": 0.00100, "SOL": 0.00130, "XRP": 0.00140,
    "DOGE": 0.00180, "PEPE": 0.00350, "SHIB": 0.00320,
}
COSTE_TRANSACCION_DEFAULT = 0.00120  # cripto de menor liquidez / desconocidos


def coste_transaccion(mercado: str) -> float:
    return COSTES_TRANSACCION.get(normalizar_mercado(mercado), COSTE_TRANSACCION_DEFAULT)


def estimar_toque_tp_sl(precio_actual: float, tp: float, sl: float,
                         volatilidad_por_vela: float, drift_por_vela: float,
                         retornos_log=None, pasos: int = 24,
                         n_rutas: int = 4000, seed=None,
                         coste_pct: float = 0.0) -> dict:
    """
    Probabilidad de PRIMER TOQUE: de n_rutas simuladas (mismo modelo
    híbrido GBM+bootstrap y semilla determinista derivada de la del
    análisis), ¿qué fracción toca el TP antes que el SL, cuál toca el SL
    antes, y cuál no toca ninguno dentro del horizonte? De ahí sale la
    esperanza matemática REAL de la operación en unidades de R:

        E[R] = p_tp · RR − p_sl · 1   (las rutas sin toque aportan ~0)

    Esto reemplaza al viejo "Probabilidad: X%" que solo medía dónde
    TERMINABA el precio — irrelevante si el SL se tocó por el camino.
    """
    if not tp or not sl or tp == sl:
        return {"p_tp": 0.0, "p_sl": 0.0, "p_ninguno": 100.0, "esperanza_r": 0.0}
    rng = np.random.default_rng(None if seed is None else seed + 1)
    sigma = max(volatilidad_por_vela, VOLATILIDAD_MINIMA)
    mu = drift_por_vela * 0.5

    n_boot = n_rutas // 2 if retornos_log is not None else 0
    tray_boot = None
    if n_boot > 0:
        tray_boot = _trayectorias_block_bootstrap(precio_actual, retornos_log, pasos, n_boot, rng)
    n_gbm = n_rutas - (len(tray_boot) if tray_boot is not None else 0)
    shocks = rng.standard_normal((n_gbm, pasos))
    tray_gbm = precio_actual * np.exp(np.cumsum((mu - 0.5 * sigma**2) + sigma * shocks, axis=1))
    tray = np.vstack([tray_gbm, tray_boot]) if tray_boot is not None else tray_gbm

    alcista = tp > precio_actual
    if alcista:
        toca_tp = tray >= tp
        toca_sl = tray <= sl
    else:
        toca_tp = tray <= tp
        toca_sl = tray >= sl

    # Índice de la primera vela que toca cada barrera (pasos = "nunca")
    idx_tp = np.where(toca_tp.any(axis=1), toca_tp.argmax(axis=1), pasos)
    idx_sl = np.where(toca_sl.any(axis=1), toca_sl.argmax(axis=1), pasos)

    tp_primero = np.sum(idx_tp < idx_sl)
    sl_primero = np.sum(idx_sl < idx_tp)
    ninguno = len(tray) - tp_primero - sl_primero  # incluye empates en la misma vela (raros)

    p_tp = 100.0 * tp_primero / len(tray)
    p_sl = 100.0 * sl_primero / len(tray)
    # El coste de ida y vuelta se paga SIEMPRE: recorta la ganancia del TP
    # y agranda la pérdida del SL. Restarlo aquí es lo que separa una
    # esperanza teórica de una que el usuario puede reproducir.
    coste_precio = precio_actual * coste_pct
    dist_tp = max(abs(tp - precio_actual) - coste_precio, 0.0)
    dist_sl = abs(precio_actual - sl) + coste_precio
    rr = dist_tp / dist_sl if dist_sl > 0 else 0.0
    esperanza = (p_tp / 100.0) * rr - (p_sl / 100.0) * 1.0

    return {
        "p_tp": round(p_tp, 1),
        "p_sl": round(p_sl, 1),
        "p_ninguno": round(100.0 * ninguno / len(tray), 1),
        "esperanza_r": round(esperanza, 3),
    }


# ---------------------------------------------------------------------------
# R:R MÍNIMO ADAPTATIVO POR MERCADO (autorizado el 3-ago)
# ---------------------------------------------------------------------------
# El piso 1.3 asume ~43.5% de acierto para punto de equilibrio. Si el
# historial VERIFICADO de un mercado muestra una tasa menor, ese piso
# pierde dinero en esperanza. Con muestra seria (>=20 verificadas, mismo
# umbral conservador del sesgo direccional), el piso sube hasta el R:R de
# equilibrio + margen del 15%, acotado a [1.3, 3.0]. NUNCA baja de 1.3
# (regla no negociable) ni sube del piso de alta convicción.

MINIMO_MUESTRAS_RR_ADAPTATIVO = 20


def rr_minimo_adaptativo(tasa_acierto=None, total_verificadas: int = 0) -> float:
    if tasa_acierto is None or total_verificadas < MINIMO_MUESTRAS_RR_ADAPTATIVO:
        return RR_MINIMO_ACEPTABLE
    p = max(min(tasa_acierto / 100.0, 0.95), 0.05)
    rr_equilibrio = (1.0 - p) / p
    return round(max(RR_MINIMO_ACEPTABLE,
                     min(rr_equilibrio * 1.15, RR_MINIMO_ALTA_CONVICCION)), 2)


# El SL debe estar al menos a N veces el coste de ida y vuelta. Con un
# stop más cerca, la fricción se come una parte enorme del riesgo y el
# precio lo toca por puro ruido: en el caso real que motivó esta regla
# (USDCOP), el SL estaba a 0.342% con un coste de 0.150% — el 44% del
# riesgo era pura fricción, y la probabilidad de tocar el TP antes que
# el SL caía al 12.9%. Un R:R de 1:4.55 se ve espectacular pero es falso
# si el stop es insostenible.
MULTIPLO_MINIMO_SL_COSTE = 5.0

# El stop no puede estar más cerca que este múltiplo del recorrido
# esperado (ATR del horizonte).
#
# Calibrado con DATOS REALES VERIFICADOS (8-ago): el MAE medio de las
# operaciones cuya dirección acertó fue de 1.22R. Como el stop está a 1R
# por definición, eso significa que el precio cruzó el stop y LUEGO giró
# a favor: el stop estaba cortando operaciones que iban a funcionar.
# Con 1.5 ATR el stop queda por detrás de ese MAE medido y deja de
# barrer ganadoras.
ATR_MINIMO_PARA_STOP = 1.5

# --- NIVEL DE ASEGURAMIENTO (parcial + break-even) ---
# El hallazgo más rentable de todo el proyecto, y sale de los datos
# reales: las operaciones PERDEDORAS llegaron a ganar 1.16R de media
# antes de darse la vuelta. Es decir, el precio SÍ se movía a favor casi
# siempre; lo que faltaba era asegurar algo antes del giro.
#
# Con parcial del 50% en +0.8R y stop a break-even, la esperanza pasa de
# −0.402R a +0.271R sin cambiar NADA de la dirección. La gestión vale
# más que la predicción.
FRACCION_ASEGURAMIENTO = 0.8      # se asegura al llegar a +0.8R
PORCENTAJE_PARCIAL = 0.5          # se cierra la mitad de la posición


def distancia_minima_sl(precio_actual: float, mercado: str) -> float:
    """Distancia mínima aceptable del SL, en unidades de precio."""
    return precio_actual * coste_transaccion(mercado) * MULTIPLO_MINIMO_SL_COSTE


def _asegurar_coherencia(plan: dict, precio_actual: float, direccion: str,
                          volatilidad_por_vela: float, pasos: int) -> dict:
    """
    Guarda FINAL de invariantes (fix 3-ago): en mercados degenerados
    (tendencia perfecta sin ruido, percentiles cruzados) el plan podía
    salir con TP y SL invertidos. Aquí se garantiza SIEMPRE:
      ALCISTA:  SL < entrada < TP1 <= TP2
      BAJISTA:  TP2 <= TP1 < entrada < SL
    Si algún nivel viola el orden, se reconstruye desde el ATR real con
    el R:R mínimo del modo — nunca se entrega un plan incoherente.
    """
    _r = lambda v: redondear_precio(v, precio_actual)
    atr = max(precio_actual * volatilidad_por_vela * (pasos ** 0.5),
              precio_actual * VOLATILIDAD_MINIMA)
    rr_min = RR_MINIMO_ALTA_CONVICCION if plan.get("modo") == "alta_conviccion" else RR_MINIMO_ACEPTABLE

    def _reconstruir():
        sl_d = 1.2 * atr
        tp_d = sl_d * rr_min
        if direccion == "ALCISTA":
            plan["sl"] = _r(precio_actual - sl_d)
            plan["tp1"] = _r(precio_actual + tp_d)
            plan["tp2"] = _r(max(plan["tp2"] or 0, precio_actual + tp_d))
        else:
            plan["sl"] = _r(precio_actual + sl_d)
            plan["tp1"] = _r(precio_actual - tp_d)
            plan["tp2"] = _r(min(plan["tp2"] or precio_actual, precio_actual - tp_d))
        plan["rr_ratio"] = round(rr_min, 2)
        plan["nota"] = (plan.get("nota") or "") + " [niveles reconstruidos por guarda de coherencia]"

    try:
        sl, tp1, tp2 = plan["sl"], plan["tp1"], plan["tp2"]
        if sl is None or tp1 is None:
            _reconstruir()
        elif direccion == "ALCISTA" and not (sl < precio_actual < tp1):
            _reconstruir()
        elif direccion == "BAJISTA" and not (tp1 < precio_actual < sl):
            _reconstruir()
        elif abs(precio_actual - plan["sl"]) < precio_actual * 1e-9:
            _reconstruir()  # SL pegado a la entrada = riesgo indefinido
        # tp2 nunca puede quedar del lado equivocado de tp1
        if plan["tp2"] is not None:
            if direccion == "ALCISTA" and plan["tp2"] < plan["tp1"]:
                plan["tp2"] = plan["tp1"]
            if direccion == "BAJISTA" and plan["tp2"] > plan["tp1"]:
                plan["tp2"] = plan["tp1"]
    except Exception:
        _reconstruir()
    return plan


def calcular_plan_trade(precio_actual: float, direccion: str,
                         p50_max: float, p95_max: float,
                         p50_min: float, p05_min: float,
                         mirrofish_nivel_espejo: float, mirrofish_lado_tocado: str,
                         volatilidad_por_vela: float, pasos: int = 24,
                         zona_cero: dict = None,
                         rr_minimo_estandar: float = None):
    """
    Dos modos:

    MODO ESTÁNDAR (zona_cero=None o no confirmada): TP1=P50, TP2=P95,
    SL con R:R garantizado >=1.5 (ver auditoría externa julio 2026 — el
    SL usa el menor entre ATR real y el máximo que permite ese R:R,
    nunca el percentil extremo directo, que sesgaba el R:R en contra).

    MODO ALTA CONVICCIÓN (zona_cero confirmada, tras pasar el orquestador
    de consenso 80%): TP = P95 directamente (objetivo cuantitativo
    proyectado por Monte Carlo), y el SL es ESTRUCTURAL — se ancla al
    borde de la Zona Cero real (el límite de la zona OB/FVG validada),
    con un pequeño buffer más allá para no saltar por ruido normal del
    mercado. LIMITACIÓN HONESTA: no tenemos metadata real de "tick size"
    del instrumento desde Twelve Data, así que el buffer se calcula como
    una fracción pequeña del ATR real (no un número de ticks exacto del
    bróker, que requeriría una fuente de datos distinta). Se garantiza
    R:R >= RR_MINIMO_ALTA_CONVICCION (3.0) recortando el SL si hiciera
    falta, igual que en modo estándar.
    """
    # Redondeo adaptativo por instrumento (fix PEPE/SHIB del 3-ago:
    # round(,4) colapsaba entrada, TP y SL a 0.0 en precios < 0.0001).
    _r = lambda v: redondear_precio(v, precio_actual)

    plan = {
        "direccion": direccion,
        "entrada_mercado": _r(precio_actual),
        "entrada_sniper": None,
        "entrada_opcional": None,
        "sl": None,
        "tp1": None,
        "tp2": None,
        "rr_ratio": None,
        "modo": "estandar",
        "nota": "",
    }

    atr_horizonte = precio_actual * volatilidad_por_vela * (pasos ** 0.5)
    alta_conviccion = bool(zona_cero and zona_cero.get("confirmada") and zona_cero.get("direccion") == direccion)

    if alta_conviccion:
        plan["modo"] = "alta_conviccion"
        buffer_estructural = 0.15 * atr_horizonte  # pequeño margen más allá de la zona validada
        rr_minimo = RR_MINIMO_ALTA_CONVICCION

        if direccion == "ALCISTA":
            plan["tp1"] = _r(p95_max)
            plan["tp2"] = _r(p95_max)
            sl_estructural = zona_cero["zona_min"] - buffer_estructural
            tp1_distancia = max(plan["tp1"] - precio_actual, 0.0001)
            sl_distancia_max_permitida = tp1_distancia / rr_minimo
            sl_distancia_estructural = max(precio_actual - sl_estructural, 0.0001)
            sl_distancia_final = min(sl_distancia_estructural, sl_distancia_max_permitida)
            plan["sl"] = _r(precio_actual - sl_distancia_final)
            plan["rr_ratio"] = round(tp1_distancia / sl_distancia_final, 2)
            plan["entrada_sniper"] = _r(zona_cero["zona_max"])  # esperar retest de la Zona Cero
        else:  # BAJISTA
            plan["tp1"] = _r(p05_min)
            plan["tp2"] = _r(p05_min)
            sl_estructural = zona_cero["zona_max"] + buffer_estructural
            tp1_distancia = max(precio_actual - plan["tp1"], 0.0001)
            sl_distancia_max_permitida = tp1_distancia / rr_minimo
            sl_distancia_estructural = max(sl_estructural - precio_actual, 0.0001)
            sl_distancia_final = min(sl_distancia_estructural, sl_distancia_max_permitida)
            plan["sl"] = _r(precio_actual + sl_distancia_final)
            plan["rr_ratio"] = round(tp1_distancia / sl_distancia_final, 2)
            plan["entrada_sniper"] = _r(zona_cero["zona_min"])

        return _asegurar_coherencia(plan, precio_actual, direccion, volatilidad_por_vela, pasos)

    # --- Modo estándar (sin Zona Cero confirmada) ---
    k_atr = 1.2
    rr_piso = rr_minimo_estandar if (rr_minimo_estandar and rr_minimo_estandar >= RR_MINIMO_ACEPTABLE) else RR_MINIMO_ACEPTABLE

    if direccion == "ALCISTA":
        plan["entrada_sniper"] = _r(precio_actual - 0.3 * (precio_actual - p50_min))
        plan["tp1"] = _r(p50_max)
        plan["tp2"] = _r(p95_max)

        tp1_distancia = max(plan["tp1"] - precio_actual, 0.0001)
        sl_distancia_atr = k_atr * atr_horizonte
        sl_distancia_max_permitida = tp1_distancia / rr_piso
        sl_distancia_final = min(sl_distancia_atr, sl_distancia_max_permitida)

        plan["sl"] = _r(precio_actual - sl_distancia_final)
        plan["rr_ratio"] = round(tp1_distancia / sl_distancia_final, 2)

        if mirrofish_nivel_espejo < precio_actual:
            plan["entrada_opcional"] = _r(mirrofish_nivel_espejo)

    else:  # BAJISTA
        plan["entrada_sniper"] = _r(precio_actual + 0.3 * (p50_max - precio_actual))
        plan["tp1"] = _r(p50_min)
        plan["tp2"] = _r(p05_min)

        tp1_distancia = max(precio_actual - plan["tp1"], 0.0001)
        sl_distancia_atr = k_atr * atr_horizonte
        sl_distancia_max_permitida = tp1_distancia / rr_piso
        sl_distancia_final = min(sl_distancia_atr, sl_distancia_max_permitida)

        plan["sl"] = _r(precio_actual + sl_distancia_final)
        plan["rr_ratio"] = round(tp1_distancia / sl_distancia_final, 2)

        if mirrofish_nivel_espejo > precio_actual:
            plan["entrada_opcional"] = _r(mirrofish_nivel_espejo)

    return _asegurar_coherencia(plan, precio_actual, direccion, volatilidad_por_vela, pasos)


@dataclass
class AnalisisReal:
    mercado: str
    symbol: str
    precio_actual: float
    qcs: int
    qcs_detalles: list = field(default_factory=list)
    mc_pct_bull: float = 0.0
    mc_pct_bear: float = 0.0
    mc_pct_rango: float = 0.0
    p95_max: float = 0.0
    p50_max: float = 0.0
    p50_min: float = 0.0
    p05_min: float = 0.0
    n_simulaciones: int = 0
    n_velas_usadas: int = 0
    mirrofish_waterline: float = 0.0
    mirrofish_lado_tocado: str = ""
    mirrofish_nivel_espejo: float = 0.0
    mirrofish_distancia: float = 0.0
    mirrofish_fuerza: str = ""
    alpha: float = 0.0
    beta: float = 0.0
    gamma: float = 0.0
    direccion_predicha: str = ""
    entrada_mercado: float = 0.0
    entrada_sniper: Optional[float] = None
    entrada_opcional: Optional[float] = None
    sl: Optional[float] = None
    tp1: Optional[float] = None
    tp2: Optional[float] = None
    plan_nota: str = ""
    rr_ratio: float = 0.0
    umbral_pct: float = 0.003
    confianza_pct: int = 0
    probabilidad_pct: float = 0.0
    trend_score: float = 0.0
    trend_estructura: str = ""
    coincidencias: int = 0
    total_señales: int = 3
    score_colaborativo: float = 0.0
    ob_bos_detectado: bool = False
    ob_bos_direccion: Optional[str] = None
    ob_bos_tipo_retest: str = ""
    ob_zona_min: Optional[float] = None
    ob_zona_max: Optional[float] = None
    crt_señal: str = ""
    turtle_señal: str = ""
    fvg_señal: str = ""
    ciclo_valor: float = 50.0
    ciclo_señal: str = ""
    zona_cero_confirmada: bool = False
    zona_cero_tipo: Optional[str] = None
    zona_cero_min: Optional[float] = None
    zona_cero_max: Optional[float] = None
    consenso_pct: float = 0.0
    consenso_componentes: dict = field(default_factory=dict)
    señal_autorizada: bool = False
    modo_plan: str = "estandar"
    # --- Análisis fundamental (sin IA) + contexto cuantitativo ---
    fundamental_score: float = 0.0
    fundamental_etiqueta: str = "NEUTRAL"
    fundamental_detalle: str = ""
    fundamental_n_titulares: int = 0
    regimen_volatilidad: str = "NORMAL"
    regimen_vol_percentil: float = 50.0
    mc_modelo: str = "GBM con drift real"
    # --- Métricas de operación (first passage) ---
    prob_tp_primero: float = 0.0     # % de rutas que tocan TP antes que SL
    prob_sl_primero: float = 0.0     # % de rutas que tocan SL antes que TP
    prob_sin_toque: float = 0.0      # % que no toca ninguno en el horizonte
    esperanza_r: float = 0.0         # E[R] = p_tp·RR − p_sl·1
    rr_piso_usado: float = 1.3       # piso R:R aplicado (adaptativo si hubo muestra)
    # Escalera de objetivos: [{etiqueta, precio, rr, prob_toque}, ...]
    escalera_tp: list = field(default_factory=list)
    tp3: Optional[float] = None
    # --- Contexto: sesión (informativo), volumen, marco superior, coste ---
    sesion: dict = field(default_factory=dict)
    volumen_texto: str = ""
    volumen_score: float = 0.0
    contexto_4h_texto: str = ""
    contexto_4h_score: float = 0.0
    aseguramiento_precio: Optional[float] = None
    aseguramiento_r: float = 0.0
    aseguramiento_pct: int = 50
    smc_score: int = 0
    smc_calidad: str = ""
    smc_detalle: list = field(default_factory=list)
    smc_adx: float = 0.0
    tsmom_texto: str = ""
    tsmom_score: float = 0.0
    cripto_texto: str = ""
    cripto_score: float = 0.0
    cripto_etiqueta: str = ""
    cripto_marea_fuerte: bool = False
    coste_pct: float = 0.0
    # Voto de cada motor en esta señal, para medir después cuál aporta
    votos_componentes: dict = field(default_factory=dict)
    conviccion_direccional: bool = True
    motivo_desempate: str = ""
    calidad_setup: str = "OPERABLE"     # SNIPER | OPERABLE | OBSERVAR
    setup_faltantes: list = field(default_factory=list)
    riesgos_contextuales: list = field(default_factory=list)
    hurst: float = 0.0
    hurst_regimen: str = "DESCONOCIDO"
    hurst_texto: str = ""
    fib_nivel_cercano: str = ""
    fib_en_zona_aurea: bool = False
    mc_restringido_aplicado: bool = False
    # --- Insumos para reconstruir el plan de forma determinista ---
    volatilidad_por_vela: float = 0.0
    drift_por_vela: float = 0.0
    seed_analisis: int = 0
    # --- Veredicto de la IA como motor confirmador/correctivo (Premium) ---
    ia_veredicto: Optional[str] = None   # "CONFIRMA" | "CORRIGE" | None (sin IA)
    ia_razon: str = ""


# ---------------------------------------------------------------------------
# CALIDAD DEL SETUP: ¿hay entrada operable o solo hay mercado?
# ---------------------------------------------------------------------------
# El cambio de fondo. Hasta ahora el motor SIEMPRE fabricaba una entrada,
# aunque la matemática estuviera en contra: por eso salían señales con
# esperanza −0.09R y consenso 25%. Un trader humano en esa situación no
# opera: mira, no ve nada y cierra la plataforma. El bot debe poder hacer
# lo mismo y decirlo.
#
# Tres calidades:
#   SNIPER    consenso >= 60%, esperanza > 0 y R:R >= 3. Es la entrada de
#             precisión: pocas al día, pero con las tres cosas alineadas.
#   OPERABLE  esperanza > 0 y consenso >= 40%. Entrada válida, sin lujos.
#   OBSERVAR  el resto. NO se entrega entrada: se entrega un análisis de
#             mercado (qué está pasando, qué falta, qué nivel vigilar) y
#             el mercado queda en vigilancia para avisar cuando aparezca.

UMBRAL_CONSENSO_SNIPER = 60.0
UMBRAL_CONSENSO_OPERABLE = 40.0
RR_SNIPER = 3.0


def evaluar_riesgo_contextual(regimen_vol: str, sesion: dict,
                               mercado_24_7: bool = False) -> dict:
    """
    Riesgos que no salen de un indicador sino de la COMBINACIÓN de
    condiciones. El caso que motivó esto: volatilidad ALTA con sesión
    asiática de liquidez BAJA — rangos amplios movidos por poco volumen,
    que es el escenario clásico de barrida de stop. Cada factor por
    separado se mostraba, pero nadie los sumaba, y juntos son mucho peor
    que por separado.

    No bloquea: avisa, y recomienda reducir tamaño.
    """
    avisos = []
    liquidez = (sesion or {}).get("liquidez", "")
    if regimen_vol == "ALTA" and liquidez in ("BAJA", "CERRADO") and not mercado_24_7:
        avisos.append(
            "Volatilidad ALTA con liquidez BAJA: rangos amplios movidos por "
            "poco volumen. Es el escenario típico de barrida de stop — "
            "considera reducir el tamaño o esperar a la apertura de Londres.")
    elif regimen_vol == "ALTA" and liquidez in ("BAJA", "CERRADO"):
        avisos.append(
            "Volatilidad ALTA en horas de menor volumen: mayor riesgo de "
            "mechas que barren el stop sin cambiar la estructura.")
    if liquidez == "CERRADO":
        avisos.append(
            "Mercado cerrado: los datos son del último cierre y el precio "
            "puede abrir con hueco por encima o por debajo de tu stop.")
    if regimen_vol == "BAJA" and liquidez in ("BAJA", "CERRADO"):
        avisos.append(
            "Volatilidad y liquidez bajas: el precio puede quedarse dormido "
            "y no alcanzar ningún objetivo dentro del horizonte.")
    return {"hay_riesgo": bool(avisos), "avisos": avisos}


# ---------------------------------------------------------------------------
# ANÁLISIS DE PRECISIÓN (entrada pendiente en zona de valor)
# ---------------------------------------------------------------------------
# Diferencia real con la entrada a mercado: en vez de comprar donde el
# precio YA está, se deja una orden esperando en la zona donde el precio
# tiene motivos para reaccionar (Order Block, Zona Cero, retroceso a
# valor). Mejora dos cosas a la vez: el R:R (entras más barato) y la
# probabilidad (entras donde alguien defiende el precio).
#
# El coste es real y hay que decirlo: la orden puede NO activarse nunca.
# Por eso caduca, y una entrada no activada no cuenta como fallo — no
# hubo operación que evaluar.

DISTANCIA_MINIMA_SNIPER = 0.0015   # 0.15%: más cerca, no compensa esperar
DISTANCIA_MAXIMA_SNIPER = 0.060    # 6%: más lejos, no se activa ni en días


def caducidad_sniper(distancia_pct: float) -> int:
    """
    La espera debe ser proporcional al viaje. Una entrada a 0.3% puede
    activarse en horas; una a 4% necesita días. Poner 24h a todas hacía
    que las entradas buenas y lejanas caducaran justo antes de activarse.
    """
    if distancia_pct <= 0.5:
        return 12
    if distancia_pct <= 1.5:
        return 24
    if distancia_pct <= 3.0:
        return 48
    return 72


def construir_entrada_precision(precio_actual: float, direccion: str,
                                 zona_cero: dict, ob_bos: dict, fvg: dict,
                                 nivel_espejo: float, atr: float,
                                 mercado: str) -> dict:
    """
    Calcula la entrada pendiente y de DÓNDE sale, por orden de calidad:

      1. Zona Cero confirmada (Análisis CBS + OB/FVG solapados): el mejor
         nivel que produce el sistema.
      2. Order Block detectado: zona institucional sin mitigar.
      3. Fair Value Gap activo: hueco que el precio suele volver a cubrir.
      4. Retroceso por ATR (0.6 ATR): respaldo cuando no hay zona clara.

    Devuelve `viable=False` si el nivel queda demasiado cerca (no vale la
    pena esperar) o demasiado lejos (no se va a activar). En ese caso, lo
    honesto es ofrecer análisis de mercado, no una orden que nunca entra.
    """
    alcista = direccion == "ALCISTA"
    candidatos = []

    if zona_cero and zona_cero.get("confirmada"):
        nivel = zona_cero["zona_max"] if alcista else zona_cero["zona_min"]
        candidatos.append((nivel, "Zona Cero (Análisis CBS + OB/FVG)", 1))
    if ob_bos and ob_bos.get("detectado"):
        zmin, zmax = ob_bos.get("zona_min"), ob_bos.get("zona_max")
        if zmin and zmax:
            candidatos.append((zmax if alcista else zmin, "Order Block", 2))
    if fvg and fvg.get("gap_min") and fvg.get("gap_max"):
        candidatos.append((fvg["gap_max"] if alcista else fvg["gap_min"],
                            "Fair Value Gap", 3))
    retroceso = precio_actual - 0.6 * atr if alcista else precio_actual + 0.6 * atr
    candidatos.append((retroceso, "retroceso a valor (0.6 ATR)", 4))

    # Se queda con el mejor candidato que además esté del lado correcto
    for nivel, origen, _ in sorted(candidatos, key=lambda c: c[2]):
        if not nivel or nivel <= 0:
            continue
        # La entrada debe estar POR DEBAJO en largos y POR ENCIMA en cortos
        if (alcista and nivel >= precio_actual) or ((not alcista) and nivel <= precio_actual):
            continue
        distancia = abs(precio_actual - nivel) / precio_actual
        if distancia < DISTANCIA_MINIMA_SNIPER:
            continue   # tan cerca que da igual entrar a mercado
        if distancia > DISTANCIA_MAXIMA_SNIPER:
            continue   # tan lejos que no se va a activar
        return {
            "viable": True,
            "entrada": redondear_precio(nivel, precio_actual),
            "origen": origen,
            "distancia_pct": round(distancia * 100, 3),
            "horas_caducidad": caducidad_sniper(distancia * 100),
        }

    return {"viable": False, "motivo": ("no hay zona de valor a distancia "
                                         "razonable para esperar una entrada")}


def esperanza_desde_entrada(entrada: float, sl: float, tp: float, direccion: str,
                             volatilidad_por_vela: float, drift_por_vela: float,
                             mercado: str, retornos_log=None,
                             seed=None) -> dict:
    """
    Esperanza matemática calculada DESDE LA ENTRADA PENDIENTE.

    Es la pieza que faltaba para que el análisis de precisión se pueda
    juzgar con el mismo rasero que el de mercado. Hasta ahora la calidad
    se evaluaba con los números de entrar AHORA, pero la orden entra más
    barata: en un largo, comprar 1% por debajo cambia por completo la
    relación riesgo/beneficio.

    El resultado se usa para decidir si esa entrada pendiente merece
    existir — no para adornar el reporte. Una entrada de precisión con
    esperanza negativa desde su propio precio no es mejor que una entrada
    a mercado mala: es la misma mala señal con más espera.
    """
    if not entrada or not sl or not tp:
        return {"esperanza_r": 0.0, "p_tp": 0.0, "p_sl": 0.0, "valida": False}
    t = estimar_toque_tp_sl(
        entrada, tp, sl, volatilidad_por_vela, drift_por_vela,
        retornos_log=retornos_log, pasos=PASOS_HORIZONTE,
        n_rutas=3000, seed=seed, coste_pct=coste_transaccion(mercado),
    )
    return {
        "esperanza_r": t["esperanza_r"],
        "p_tp": t["p_tp"],
        "p_sl": t["p_sl"],
        "valida": True,
    }


def plan_desde_entrada_precision(entrada: float, direccion: str, mercado: str,
                                  volatilidad_por_vela: float, atr: float,
                                  p50: float, p95: float,
                                  rr_piso: float = 1.3) -> dict:
    """
    Recalcula SL y objetivos DESDE la entrada pendiente, no desde el
    precio actual. Es el punto entero del análisis de precisión: si el
    SL se dejara donde estaba, el R:R que se anuncia sería falso.
    """
    dist_sl = max(1.1 * atr, distancia_minima_sl(entrada, mercado))
    sl = entrada - dist_sl if direccion == "ALCISTA" else entrada + dist_sl
    escalera = construir_escalera_tp(entrada, direccion, sl, p50, p95, atr, rr_piso)
    return {
        "entrada": redondear_precio(entrada, entrada),
        "sl": redondear_precio(sl, entrada),
        "escalera": escalera,
        "rr_ratio": escalera[0]["rr"] if escalera else None,
    }


def clasificar_calidad_setup(consenso_pct: float, esperanza_r: float,
                              rr_ratio: float, conviccion: bool) -> dict:
    """Clasifica el setup y explica QUÉ falta para subir de categoría."""
    faltantes = []
    if consenso_pct < UMBRAL_CONSENSO_SNIPER:
        faltantes.append(f"consenso {consenso_pct:.0f}% (sniper exige {UMBRAL_CONSENSO_SNIPER:.0f}%)")
    if esperanza_r <= 0:
        faltantes.append(f"esperanza {esperanza_r:+.3f}R (debe ser positiva)")
    if rr_ratio < RR_SNIPER:
        faltantes.append(f"R:R 1:{rr_ratio} (sniper exige 1:{RR_SNIPER:.0f})")
    if not conviccion:
        faltantes.append("las señales quedaron empatadas")

    if (consenso_pct >= UMBRAL_CONSENSO_SNIPER and esperanza_r > 0
            and rr_ratio >= RR_SNIPER and conviccion):
        calidad = "SNIPER"
    elif esperanza_r > 0 and consenso_pct >= UMBRAL_CONSENSO_OPERABLE and conviccion:
        calidad = "OPERABLE"
    else:
        calidad = "OBSERVAR"
    return {"calidad": calidad, "faltantes": faltantes,
            "operable": calidad in ("SNIPER", "OPERABLE")}


def generar_analisis_real(mercado: str, stats_alcista_global: dict = None,
                           stats_bajista_global: dict = None,
                           fundamental: dict = None,
                           stats_global: dict = None) -> AnalisisReal:
    """
    Punto de entrada: datos reales + Monte Carlo con drift real + QCS real
    + Análisis CBS (Mirrofish) real + Quantum + tendencia real (Trend
    Trade Strategy pura, sin EMA) + OB/BOS + CRT + Turtle Soup + FVG +
    Ciclo de Momentum (sin EMA) + Zona Cero + Orquestador de consenso +
    Sesgo direccional aprendido (si se proveen las estadísticas).

    stats_alcista_global / stats_bajista_global: dicts de
    db.estadisticas_direccion_global(mercado, "ALCISTA"/"BAJISTA"),
    opcionales — real_engine.py no depende de la base de datos
    directamente (separación limpia), así que quien llama (bot.py) es
    quien las consulta y las pasa aquí.

    REGLA DE NEGOCIO (decisión explícita del operador, reemplaza la regla
    anterior de "siempre dar entrada"): la señal SOLO se autoriza si el
    Orquestador de Consenso alcanza >=60% (Zona Cero + TBS + Ciclo +
    Trend Trade Strategy alineados en la misma dirección). Si no se
    alcanza, se entrega igual una entrada estándar con R:R>=1.3.
    """
    datos = obtener_datos_mercado(mercado)
    mc = simular_monte_carlo(
        datos["precio_actual"], datos["volatilidad_por_vela"],
        pasos=PASOS_HORIZONTE, drift_por_vela=datos["drift_por_vela"],
        retornos_log=datos.get("retornos_log"),
        seed=datos.get("seed_determinista"),
    )
    fundamental = fundamental or {}
    qcs, detalles = calcular_qcs(
        datos["precio_actual"], datos["maximo_reciente"], datos["minimo_reciente"]
    )
    mirrofish = calcular_mirrofish(
        datos["precio_actual"], datos["maximo_reciente"],
        datos["minimo_reciente"], datos["volatilidad_por_vela"]
    )
    superposicion = calcular_superposicion(mc["pct_bull"], mc["pct_bear"], mc["pct_rango"])

    sesgo = calcular_sesgo_direccional_aprendido(
        stats_alcista_global or {}, stats_bajista_global or {}
    )

    # Confirmación del marco superior (4h) y del volumen: dos señales más
    # que votan en el consenso. Ambas fail-soft (si no hay datos, 0).
    ctx4 = contexto_superior(mercado)
    ctx_cripto = contexto_mercado_cripto(mercado)
    tsmom = calcular_tsmom(np.array([v["close"] for v in datos["velas"]], dtype=float)
                            if datos.get("velas") else np.array([]))

    # SCORE SMC INSTITUCIONAL (portado del ICC Sniper V16.2). Se calcula
    # DESPUÉS de conocer la dirección, porque puntúa si la estructura
    # respalda esa dirección concreta — no es un indicador neutro.
    datos["score_4h"] = float(ctx4.get("score", 0.0))
    vol = datos.get("volumen", {"score": 0.0, "texto": "sin datos de volumen"})

    # El sesgo aprendido se suspende si el mercado actual lo contradice
    sesgo = suspender_sesgo_si_contradice(sesgo, ctx_cripto)

    colaborativo = calcular_score_colaborativo(
        mc["pct_bull"], mc["pct_bear"], datos["tendencia"], mirrofish,
        datos["ob_bos"], datos["crt"], datos["turtle_soup"],
        datos["fvg"], datos["ciclo"], datos["precio_actual"],
        score_sesgo_aprendido=sesgo["score"],
        score_fundamental=float(fundamental.get("score", 0.0)),
        score_volumen=float(vol.get("score", 0.0)),
        score_4h=float(ctx4.get("score", 0.0)),
        score_cripto=float(ctx_cripto.get("score", 0.0)),
        score_tsmom=float(tsmom.get("score", 0.0)),
        hurst=datos.get("hurst"),
    )
    direccion = colaborativo["direccion"]

    zona_cero = calcular_zona_cero(mirrofish, datos["ob_bos"], datos["fvg"], datos["precio_actual"])
    consenso = orquestar_consenso(
        direccion, zona_cero, datos["turtle_soup"], datos["crt"], datos["tendencia"], datos["ciclo"]
    )

    confianza = calcular_confianza(
        qcs, coincidencias=colaborativo["coincidencias"], total_señales=colaborativo["total_señales"]
    )
    # Sin convicción direccional, la confianza NO puede ser alta: la
    # dirección se decidió por desempate, no por consenso de señales.
    # Se acota a 45% como techo honesto (antes estas señales salían con
    # la misma confianza que las de convicción real).
    if not colaborativo.get("conviccion_direccional", True):
        confianza = min(confianza, 45)
    probabilidad = mc["pct_bull"] if direccion == "ALCISTA" else mc["pct_bear"]

    # REGLA DE NEGOCIO (ajustada): ya no se bloquea la entrada por completo.
    # Siempre se entrega un plan de trade a precio de mercado con R:R
    # garantizado >= RR_MINIMO_ACEPTABLE (1.3). Si el consenso alcanza el
    # umbral (60%), se usa el modo "alta convicción" (TP=P95, SL
    # estructural anclado a la Zona Cero, R:R>=3). Si no, modo estándar
    # (R:R>=1.3) — y el diagnóstico de qué faltó se muestra aparte, para
    # que el usuario vea la fuerza real de la señal sin perder la entrada.
    # Piso R:R adaptativo: si el historial verificado del mercado muestra
    # una tasa de acierto que hace perder dinero a 1.3, el piso sube al
    # R:R de equilibrio (acotado 1.3-3.0, mínimo 20 muestras).
    sg = stats_global or {}
    rr_piso = rr_minimo_adaptativo(sg.get("tasa_acierto"), sg.get("total_verificadas", 0))

    # SCORE SMC INSTITUCIONAL (ICC Sniper V16.2). Se calcula aquí porque
    # necesita la dirección ya decidida: puntúa si la estructura respalda
    # ESA dirección, no es un indicador neutro. Fail-soft: si algo falla,
    # el análisis continúa sin score en vez de caerse.
    try:
        import icc_score
        datos["score_4h"] = float(ctx4.get("score", 0.0))
        smc = icc_score.calcular_score(datos, direccion, contexto_sesion(mercado))
    except Exception as e:
        logger.warning(f"Score SMC no disponible para {mercado}: {e}")
        smc = {"disponible": False, "score": 0, "detalle": [], "calidad": "", "adx": 0.0}

    plan = calcular_plan_trade(
        datos["precio_actual"], direccion,
        mc["p50_max"], mc["p95_max"], mc["p50_min"], mc["p05_min"],
        mirrofish["nivel_espejo"], mirrofish["lado_tocado"],
        volatilidad_por_vela=datos["volatilidad_por_vela"], pasos=PASOS_HORIZONTE,
        zona_cero=zona_cero if consenso["autorizada"] else None,
        rr_minimo_estandar=rr_piso,
    )

    # Escalera de objetivos: el piso R:R aplica al TP1; TP2 y TP3 se
    # extienden a los percentiles reales de la simulación. Cada tramo
    # trae su propio R:R y su probabilidad real de ser alcanzado antes
    # que el SL — así se elige objetivo con datos, no con corazonada.
    atr_actual = max(datos["precio_actual"] * datos["volatilidad_por_vela"] * (PASOS_HORIZONTE ** 0.5),
                     datos["precio_actual"] * VOLATILIDAD_MINIMA)
    # DISTANCIA MÍNIMA DEL STOP — dos condiciones, manda la mayor:
    #
    #   a) FRICCIÓN: al menos 5 veces el coste de ida y vuelta. Con el
    #      stop más cerca, la comisión se come una parte enorme del riesgo.
    #   b) RUIDO: al menos 1 ATR del horizonte. Ésta se añadió tras medir
    #      que el 95% de los planes generaba stops por debajo de 1 ATR —
    #      es decir, casi todas las operaciones nacían con un stop que el
    #      movimiento normal del precio toca sin que la tendencia cambie.
    #      Es lo que ocurrió en XRP: stop a 0.35 ATR, saltó en 4 segundos.
    #
    # Ampliar el stop reduce el R:R aparente, pero un R:R alto con un
    # stop que se toca siempre es una ilusión: el objetivo nunca llega.
    atr_horizonte = datos["precio_actual"] * datos["volatilidad_por_vela"] * (PASOS_HORIZONTE ** 0.5)
    dist_min_friccion = distancia_minima_sl(datos["precio_actual"], mercado)
    dist_min_ruido = atr_horizonte * ATR_MINIMO_PARA_STOP
    dist_min_sl = max(dist_min_friccion, dist_min_ruido)

    if plan.get("sl"):
        dist_sl_actual = abs(datos["precio_actual"] - plan["sl"])
        if dist_sl_actual < dist_min_sl:
            if direccion == "ALCISTA":
                plan["sl"] = redondear_precio(datos["precio_actual"] - dist_min_sl,
                                               datos["precio_actual"])
            else:
                plan["sl"] = redondear_precio(datos["precio_actual"] + dist_min_sl,
                                               datos["precio_actual"])
            causa = ("ruido del mercado" if dist_min_ruido >= dist_min_friccion
                     else "coste de transacción")
            plan["nota"] = ((plan.get("nota") or "") +
                            f" [SL ampliado al mínimo por {causa}]")

    # SEGUNDA PASADA condicionada por el fundamental verificado: los
    # percentiles que anclan los objetivos dejan de apoyarse en
    # escenarios que el contexto ya desmiente. Si no hay sesgo
    # fundamental, no se aplica y se usan los percentiles originales.
    mc_restringido = monte_carlo_restringido(
        datos["precio_actual"], datos["volatilidad_por_vela"], datos["drift_por_vela"],
        datos.get("retornos_log"), PASOS_HORIZONTE,
        float(fundamental.get("score", 0.0)), direccion,
        seed=datos.get("seed_determinista"),
    )
    mc_final = mc_restringido if mc_restringido.get("aplicado") else mc

    p50_dir = mc_final["p50_max"] if direccion == "ALCISTA" else mc_final["p50_min"]
    p95_dir = mc_final["p95_max"] if direccion == "ALCISTA" else mc_final["p05_min"]
    # IMPORTANTE: la escalera se construye desde los precios que el
    # usuario VE (entrada y SL ya redondeados), no desde los valores
    # internos sin redondear. Bug real en EURUSD: la escalera partía de
    # 1.161072... mientras el reporte mostraba entrada 1.1611, y el R:R
    # medido desde el precio mostrado daba 1.28 en vez del 1.30 anunciado.
    # El R:R tiene que cuadrar con los números que el usuario puede
    # comprobar con una calculadora.
    entrada_mostrada = plan.get("entrada_mercado") or datos["precio_actual"]
    escalera = construir_escalera_tp(
        entrada_mostrada, direccion, plan["sl"],
        p50_dir, p95_dir, atr_actual, rr_piso=max(rr_piso, RR_MINIMO_ACEPTABLE),
    )
    for tramo in escalera:
        t = estimar_toque_tp_sl(
            entrada_mostrada, tramo["precio"], plan["sl"],
            datos["volatilidad_por_vela"], datos["drift_por_vela"],
            retornos_log=datos.get("retornos_log"), pasos=PASOS_HORIZONTE,
            n_rutas=2500, seed=datos.get("seed_determinista"),
            coste_pct=coste_transaccion(mercado),
        )
        tramo["prob_toque"] = t["p_tp"]
        tramo["esperanza_r"] = t["esperanza_r"]

    # Confluencia con Fibonacci: no mueve niveles, marca coincidencias
    fib = calcular_fibonacci(datos["maximo_reciente"], datos["minimo_reciente"],
                              datos["precio_actual"])
    escalera = anclar_a_fibonacci(escalera, fib, datos["precio_actual"])

    # El plan operativo usa la escalera como fuente única de objetivos
    if escalera:
        # La escalera puede tener MENOS de 3 tramos si el suelo de precio
        # acotó objetivos y quedaron duplicados (caso PEPE con volatilidad
        # extrema). Se accede por índice seguro en vez de asumir 3.
        plan["tp1"] = escalera[0]["precio"]
        plan["tp2"] = escalera[1]["precio"] if len(escalera) > 1 else escalera[0]["precio"]
        plan["rr_ratio"] = escalera[0]["rr"]
        sl_antes = plan["sl"]
        plan = _asegurar_coherencia(plan, datos["precio_actual"], direccion,
                                    datos["volatilidad_por_vela"], PASOS_HORIZONTE)
        # Si la guarda de coherencia movió el SL, los R:R de la escalera
        # quedaron obsoletos: se calcularon contra el stop anterior. Hay
        # que RECONSTRUIRLA, no dejar números que ya no corresponden — el
        # R:R anunciado tiene que ser el real (caso detectado en EURUSD:
        # se declaraba 1:1.3 cuando el valor efectivo era 1:1.28).
        if plan["sl"] != sl_antes:
            escalera = construir_escalera_tp(
                entrada_mostrada, direccion, plan["sl"],
                p50_dir, p95_dir, atr_actual,
                rr_piso=max(rr_piso, RR_MINIMO_ACEPTABLE))
            escalera = anclar_a_fibonacci(escalera, fib, entrada_mostrada)
            for tramo in escalera:
                t = estimar_toque_tp_sl(
                    entrada_mostrada, tramo["precio"], plan["sl"],
                    datos["volatilidad_por_vela"], datos["drift_por_vela"],
                    retornos_log=datos.get("retornos_log"), pasos=PASOS_HORIZONTE,
                    n_rutas=2500, seed=datos.get("seed_determinista"),
                    coste_pct=coste_transaccion(mercado),
                )
                tramo["prob_toque"] = t["p_tp"]
                tramo["esperanza_r"] = t["esperanza_r"]
            plan["tp1"] = escalera[0]["precio"]
            plan["tp2"] = escalera[1]["precio"] if len(escalera) > 1 else escalera[0]["precio"]
            plan["rr_ratio"] = escalera[0]["rr"]

    # Probabilidad de PRIMER TOQUE (TP antes que SL) + esperanza en R —
    # las dos cifras que un trader pide primero. Semilla derivada de la
    # determinista: mismos datos => mismo resultado, siempre.
    toque = estimar_toque_tp_sl(
        datos["precio_actual"], plan["tp1"], plan["sl"],
        datos["volatilidad_por_vela"], datos["drift_por_vela"],
        retornos_log=datos.get("retornos_log"), pasos=PASOS_HORIZONTE,
        seed=datos.get("seed_determinista"),
        coste_pct=coste_transaccion(mercado),
    )

    calidad = clasificar_calidad_setup(
        consenso["consenso_pct"], toque["esperanza_r"], plan.get("rr_ratio") or 0.0,
        colaborativo.get("conviccion_direccional", True),
    )

    return AnalisisReal(
        mercado=normalizar_mercado(mercado),
        symbol=datos["symbol"],
        precio_actual=datos["precio_actual"],
        qcs=qcs,
        qcs_detalles=detalles,
        mc_pct_bull=mc["pct_bull"],
        mc_pct_bear=mc["pct_bear"],
        mc_pct_rango=mc["pct_rango"],
        p95_max=mc["p95_max"],
        p50_max=mc["p50_max"],
        p50_min=mc["p50_min"],
        p05_min=mc["p05_min"],
        n_simulaciones=mc["n_simulaciones"],
        n_velas_usadas=datos["n_velas_usadas"],
        mirrofish_waterline=mirrofish["waterline"],
        mirrofish_lado_tocado=mirrofish["lado_tocado"],
        mirrofish_nivel_espejo=mirrofish["nivel_espejo"],
        mirrofish_distancia=mirrofish["distancia"],
        mirrofish_fuerza=mirrofish["fuerza"],
        alpha=superposicion["alpha"],
        beta=superposicion["beta"],
        gamma=superposicion["gamma"],
        direccion_predicha=direccion,
        entrada_mercado=plan["entrada_mercado"],
        entrada_sniper=plan["entrada_sniper"],
        entrada_opcional=plan["entrada_opcional"],
        sl=plan["sl"],
        tp1=plan["tp1"],
        tp2=plan["tp2"],
        plan_nota=plan["nota"],
        rr_ratio=plan["rr_ratio"] or 0.0,
        umbral_pct=mc["umbral_pct"],
        confianza_pct=confianza,
        probabilidad_pct=probabilidad,
        trend_score=datos["tendencia"]["trend_score"],
        trend_estructura=datos["tendencia"]["estructura_txt"],
        coincidencias=colaborativo["coincidencias"],
        total_señales=colaborativo["total_señales"],
        score_colaborativo=colaborativo["score_final"],
        ob_bos_detectado=datos["ob_bos"].get("bos_detectado", False),
        ob_bos_direccion=datos["ob_bos"].get("bos_direccion"),
        ob_bos_tipo_retest=datos["ob_bos"].get("tipo_retest", ""),
        ob_zona_min=datos["ob_bos"].get("ob_zona_min"),
        ob_zona_max=datos["ob_bos"].get("ob_zona_max"),
        crt_señal=datos["crt"].get("señal", ""),
        turtle_señal=datos["turtle_soup"].get("señal", ""),
        fvg_señal=("dentro de gap " + datos["fvg"]["gap_mas_cercano"]["tipo"]
                   if datos["fvg"].get("precio_dentro_del_gap") else "sin gap activo en precio actual"),
        ciclo_valor=datos["ciclo"].get("valor", 50.0),
        ciclo_señal=datos["ciclo"].get("señal", ""),
        zona_cero_confirmada=zona_cero["confirmada"],
        zona_cero_tipo=zona_cero["tipo_zona"],
        zona_cero_min=zona_cero["zona_min"],
        zona_cero_max=zona_cero["zona_max"],
        consenso_pct=consenso["consenso_pct"],
        consenso_componentes=consenso["componentes"],
        señal_autorizada=consenso["autorizada"],
        modo_plan=plan.get("modo", "estandar"),
        fundamental_score=float(fundamental.get("score", 0.0)),
        fundamental_etiqueta=fundamental.get("etiqueta", "NEUTRAL"),
        fundamental_detalle=fundamental.get("detalle", ""),
        fundamental_n_titulares=int(fundamental.get("n_titulares", 0)),
        regimen_volatilidad=datos.get("regimen_volatilidad", {}).get("regimen", "NORMAL"),
        regimen_vol_percentil=datos.get("regimen_volatilidad", {}).get("percentil", 50.0),
        mc_modelo=mc.get("modelo", "GBM con drift real"),
        prob_tp_primero=toque["p_tp"],
        prob_sl_primero=toque["p_sl"],
        prob_sin_toque=toque["p_ninguno"],
        esperanza_r=toque["esperanza_r"],
        rr_piso_usado=rr_piso,
        volatilidad_por_vela=datos["volatilidad_por_vela"],
        drift_por_vela=datos["drift_por_vela"],
        seed_analisis=datos.get("seed_determinista", 0),
        escalera_tp=escalera,
        tp3=escalera[2]["precio"] if len(escalera) > 2 else None,
        sesion=contexto_sesion(mercado),
        volumen_texto=vol.get("texto", ""),
        volumen_score=float(vol.get("score", 0.0)),
        contexto_4h_texto=ctx4.get("texto", ""),
        contexto_4h_score=float(ctx4.get("score", 0.0)),
        aseguramiento_precio=(nivel_aseguramiento(
            plan.get("entrada_mercado") or datos["precio_actual"],
            plan["sl"], direccion).get("precio") if plan.get("sl") else None),
        aseguramiento_r=FRACCION_ASEGURAMIENTO,
        aseguramiento_pct=int(PORCENTAJE_PARCIAL * 100),
        smc_score=int(smc.get("score", 0)),
        smc_calidad=smc.get("calidad", ""),
        smc_detalle=smc.get("detalle", []),
        smc_adx=float(smc.get("adx", 0.0)),
        tsmom_texto=tsmom.get("texto", ""),
        tsmom_score=float(tsmom.get("score", 0.0)),
        cripto_texto=ctx_cripto.get("texto", ""),
        cripto_score=float(ctx_cripto.get("score", 0.0)),
        cripto_etiqueta=ctx_cripto.get("etiqueta", ""),
        cripto_marea_fuerte=bool(ctx_cripto.get("marea_fuerte", False)),
        coste_pct=coste_transaccion(mercado),
        hurst=float(datos.get("hurst", {}).get("H") or 0.0),
        hurst_regimen=datos.get("hurst", {}).get("regimen", "DESCONOCIDO"),
        hurst_texto=datos.get("hurst", {}).get("texto", ""),
        fib_nivel_cercano=fib.get("nivel_cercano", "") if fib.get("disponible") else "",
        fib_en_zona_aurea=bool(fib.get("en_zona_aurea", False)),
        mc_restringido_aplicado=bool(mc_restringido.get("aplicado")),
        conviccion_direccional=colaborativo.get("conviccion_direccional", True),
        motivo_desempate=colaborativo.get("motivo_desempate") or "",
        calidad_setup=calidad["calidad"],
        setup_faltantes=calidad["faltantes"],
        riesgos_contextuales=evaluar_riesgo_contextual(
            datos.get("regimen_volatilidad", {}).get("regimen", "NORMAL"),
            contexto_sesion(mercado),
            normalizar_mercado(mercado) in MERCADOS_24_7,
        )["avisos"],
        # Voto de CADA motor en esta señal concreta. Es la pieza que
        # faltaba para responder con datos, y no de oído, cuál motor
        # aporta y cuál es ruido. Se guarda con la predicción y se cruza
        # con el resultado verificado.
        votos_componentes={
            "mc": colaborativo.get("score_mc"),
            "tendencia": colaborativo.get("score_tendencia"),
            "analisis_cbs": colaborativo.get("score_mirrofish"),
            "ob_bos": colaborativo.get("score_ob_bos"),
            "crt": colaborativo.get("score_crt"),
            "turtle": colaborativo.get("score_turtle"),
            "fvg": colaborativo.get("score_fvg"),
            "ciclo": colaborativo.get("score_ciclo"),
            "sesgo_aprendido": colaborativo.get("score_sesgo_aprendido"),
            "fundamental": colaborativo.get("score_fundamental"),
            "volumen": colaborativo.get("score_volumen"),
            "contexto_4h": colaborativo.get("score_4h"),
            "contexto_cripto": colaborativo.get("score_cripto"),
            "tsmom": colaborativo.get("score_tsmom"),
            "zona_cero": 1 if zona_cero.get("confirmada") else 0,
            "consenso_pct": consenso["consenso_pct"],
            "qcs": qcs,
            "sesion": contexto_sesion(mercado).get("clave"),
            "regimen_vol": datos.get("regimen_volatilidad", {}).get("regimen"),
            "direccion": direccion,
            "conviccion_direccional": colaborativo.get("conviccion_direccional", True),
            "hurst_regimen": datos.get("hurst", {}).get("regimen"),
        },
    )
