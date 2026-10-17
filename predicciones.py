"""
CBS Quantum Predictor — Módulo de mercados de predicción
=========================================================
Probabilidades calibradas para apostar en Polymarket, Kalshi o Binance
Prediction Markets: "¿BTC estará por encima de X dentro de N minutos?"

POR QUÉ ESTE MÓDULO EXISTE Y NO ES UNA SEÑAL MÁS
------------------------------------------------
Una señal de trading y una apuesta binaria responden preguntas distintas:

  · TRADING: cuánto se mueve el precio. A 5 minutos el recorrido típico
    es 0.14% y la comisión de ida y vuelta 0.12% — el coste se lleva el
    83% de lo que se pueda ganar. Por eso el bot NO da señales de
    trading a esos horizontes.
  · APUESTA: solo importa la DIRECCIÓN respecto a un strike. No hay
    comisión por lado; se paga 0.52 por un contrato que vale 1.00 si se
    acierta. El coste deja de ser el problema.

Lo que se vende aquí no es "sube o baja": es una PROBABILIDAD. El valor
está en la diferencia entre la probabilidad del motor y la que implica
el precio de mercado. Sin esa comparación no hay ventaja que comunicar.

CALIBRACIÓN CON DOS FUENTES
---------------------------
El mismo mecanismo de aprendizaje que ya corrige las señales normales
—efectividad verificada por mercado— alimenta aquí la probabilidad:

  1. Historial de SEÑALES normales: miles de predicciones verificadas
     contra el precio real. Dice si el motor acierta más o menos de lo
     que cree en cada mercado.
  2. Historial de APUESTAS resueltas: la fuente más directa, porque mide
     exactamente lo mismo que se está prediciendo.

Mientras la segunda no tenga muestra suficiente, manda la primera. Es la
misma lógica que ya usa el R:R adaptativo: no se inventa calibración, se
hereda de lo que sí está medido.
"""

import logging
import numpy as np

logger = logging.getLogger("predicciones")

# Bloques de vencimiento en el reloj (como Polymarket y Kalshi): un
# contrato de 5 minutos vence en el próximo :00, :05, :10... no cinco
# minutos después de consultarlo.
HORIZONTES = (5, 15, 30, 60)

# Intervalo de velas por horizonte: se busca tener 200+ velas de
# contexto sin irse a un timeframe que borre el detalle del movimiento.
INTERVALO_POR_HORIZONTE = {5: "1min", 15: "1min", 30: "5min", 60: "5min"}

# Muestra mínima para que el historial de apuestas mande sobre el de señales
MINIMO_APUESTAS_PROPIAS = 30
MINIMO_SENALES_GLOBALES = 20

# Ventaja mínima sobre el mercado para recomendar la apuesta. Por debajo
# de esto, la diferencia cabe dentro del error del propio modelo y
# recomendarla sería vender ruido como si fuera información.
VENTAJA_MINIMA = 0.05          # 5 puntos porcentuales

# La probabilidad nunca se presenta como certeza: el motor mide, no adivina
PROB_MAXIMA_MOSTRADA = 0.85
PROB_MINIMA_MOSTRADA = 0.15


def proximo_vencimiento(minutos_bloque: int = 5, ahora=None) -> dict:
    """
    Siguiente frontera de N minutos en el reloj, como en los mercados reales.

    Polymarket y Kalshi no liquidan "15 minutos desde que consultaste":
    liquidan en horas redondas. Un contrato de las 13:15 vence a las
    13:15 tanto si lo compraste a las 13:10 como a las 13:14 — lo que
    cambia es cuánto tiempo le queda, y por tanto la probabilidad.

    Esa diferencia importa: a las 13:14 quedan 60 segundos y el precio
    apenas se moverá; a las 13:10 quedan 5 minutos. Calcular ambos casos
    como "15 minutos" daba probabilidades que no correspondían al
    contrato que el usuario iba a comprar.

    Devuelve el instante de vencimiento y los segundos que faltan.
    """
    from datetime import datetime, timedelta, timezone
    t = ahora or datetime.now(timezone.utc)
    # Minuto del bloque siguiente
    minuto_actual = t.minute
    siguiente = ((minuto_actual // minutos_bloque) + 1) * minutos_bloque
    if siguiente >= 60:
        venc = t.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    else:
        venc = t.replace(minute=siguiente, second=0, microsecond=0)
    restante = (venc - t).total_seconds()
    return {
        "vencimiento": venc,
        "segundos_restantes": max(1.0, restante),
        "minutos_restantes": max(0.02, restante / 60.0),
        "etiqueta": venc.strftime("%H:%M"),
        "bloque": minutos_bloque,
    }


def probabilidad_cruce(precio_actual: float, strike: float, minutos: int,
                        volatilidad_por_vela: float, minutos_por_vela: int,
                        drift_por_vela: float = 0.0,
                        retornos_log=None, n_rutas: int = 20000,
                        seed: int = None) -> dict:
    """
    Probabilidad de que el precio esté POR ENCIMA del strike al vencer.

    Monte Carlo con remuestreo de los retornos reales cuando hay muestra
    suficiente (bootstrap), y normal calibrada cuando no la hay. El
    bootstrap importa aquí más que en horizontes largos: a 5 minutos la
    distribución de retornos tiene colas mucho más gruesas que una
    normal, y asumir normalidad subestima justo los movimientos que
    deciden la apuesta.
    """
    if precio_actual <= 0 or strike <= 0 or minutos <= 0:
        return {"disponible": False}

    # Pasos fraccionarios: a 90 segundos de vencer no se puede redondear
    # a "2 velas de 1 minuto" sin exagerar el movimiento posible. La
    # volatilidad escala con la raíz del tiempo, así que un paso de 1.5
    # es perfectamente representable.
    pasos_frac = max(0.02, float(minutos) / max(minutos_por_vela, 1))
    pasos = max(1, int(round(pasos_frac)))
    vol = max(float(volatilidad_por_vela), 1e-6)
    rng = np.random.default_rng(seed)

    if retornos_log is not None and len(retornos_log) >= 200:
        # Bootstrap: se muestrean retornos REALES del propio mercado
        muestra = np.asarray(retornos_log, dtype=float)
        idx = rng.integers(0, len(muestra), size=(n_rutas, pasos))
        caminos = muestra[idx].sum(axis=1)
        # Ajuste por tiempo fraccionario: si faltan 1.5 velas y se
        # simularon 2, el recorrido se escala por la raíz del cociente
        # (la volatilidad crece con la raíz del tiempo, no linealmente).
        if abs(pasos_frac - pasos) > 0.01:
            caminos = caminos * np.sqrt(pasos_frac / pasos)
        metodo = "bootstrap"
    else:
        caminos = rng.normal(drift_por_vela * pasos_frac,
                              vol * np.sqrt(pasos_frac), n_rutas)
        metodo = "normal"

    finales = precio_actual * np.exp(caminos)
    p_arriba = float(np.mean(finales > strike))

    return {
        "disponible": True,
        "p_arriba": p_arriba,
        "p_abajo": 1.0 - p_arriba,
        "metodo": metodo,
        "pasos": pasos,
        "n_rutas": n_rutas,
        "p50": float(np.percentile(finales, 50)),
        "p05": float(np.percentile(finales, 5)),
        "p95": float(np.percentile(finales, 95)),
    }


def factor_calibracion(stats_apuestas: dict = None,
                        stats_senales: dict = None) -> dict:
    """
    Cuánto acercar la probabilidad cruda hacia 50% según el historial.

    El motor tiende a ser optimista: en las señales normales se midió una
    esperanza real de −0.019R frente a +0.117R predicha. Ese mismo sesgo
    afectaría a las apuestas, así que la probabilidad se encoge hacia el
    50% en proporción a lo mal calibrado que esté el motor en ese mercado.

    Un acierto del 50% significa que el motor no sabe nada y la
    probabilidad debe colapsar a 50%. Un 70% verificado significa que sus
    lecturas valen y se respeta casi entera.
    """
    # Fuente 1 (preferida): apuestas propias ya resueltas
    ap = stats_apuestas or {}
    if ap.get("total", 0) >= MINIMO_APUESTAS_PROPIAS and ap.get("acierto") is not None:
        acierto = float(ap["acierto"])
        fuente = f"{ap['total']} apuestas resueltas"
        peso_muestra = min(1.0, ap["total"] / 100.0)
    else:
        # Fuente 2: historial global de señales verificadas
        se = stats_senales or {}
        if se.get("total_verificadas", 0) < MINIMO_SENALES_GLOBALES or \
           se.get("tasa_acierto") is None:
            return {"factor": 0.6, "fuente": "sin historial (prudente)",
                    "acierto_historico": None}
        acierto = float(se["tasa_acierto"])
        fuente = f"{se['total_verificadas']} señales verificadas"
        peso_muestra = min(1.0, se["total_verificadas"] / 150.0)

    # El factor va de -1 a +1, no de 0 a 1. Esto es lo que convierte el
    # sistema de "callarse cuando falla" a "aprender cuando falla":
    #
    #   acierto 75% -> +1.0  la lectura se respeta entera
    #   acierto 50% ->  0.0  el motor no aporta nada, colapsa a 50%
    #   acierto 30% -> -0.8  la lectura está INVERTIDA y se le da la vuelta
    #
    # Con el rango antiguo (0 a 1), un motor al 30% daba factor 0 y toda
    # probabilidad salía 50.0% exacto — el sistema tiraba a la basura una
    # señal que, leída al revés, acierta el 70%. Fallar sistemáticamente
    # es tan informativo como acertar; lo único que no informa es el 50%.
    bruto = max(-1.0, min(1.0, (acierto - 50.0) / 25.0))

    # La muestra corta se mezcla con un valor prudente, PERO SOLO HACIA
    # ABAJO. Bug real: con 53 señales al 30.2% de acierto, mezclar con
    # 0.6 daba un factor de 0.388 — es decir, un motor que acierta peor
    # que una moneda recibía permiso para dar lecturas fuertes, solo
    # porque la muestra era pequeña. La incertidumbre nunca puede
    # convertirse en confianza: si el historial dice que el motor falla,
    # la duda sobre ese historial no lo mejora.
    if bruto < 0:
        # Historial INVERTIDO: se aplica la inversión, moderada por el
        # tamaño de muestra. Invertir con pocos datos sería perseguir una
        # racha; con muestra sólida es la corrección correcta.
        factor = bruto * peso_muestra
    elif bruto < 0.6:
        factor = bruto          # historial flojo: se respeta tal cual
    else:
        # Historial bueno pero muestra corta: se modera hasta tener datos
        factor = 0.6 * (1 - peso_muestra) + bruto * peso_muestra

    return {"factor": round(factor, 3), "fuente": fuente,
            "acierto_historico": round(acierto, 1),
            "muestra_corta": peso_muestra < 0.5}


def calibrar(p_cruda: float, factor: float) -> float:
    """
    Ajusta la probabilidad según lo que el historial ha demostrado.

    Factor positivo: se respeta la lectura (entera o encogida).
    Factor cero:     colapsa a 50% — el motor no aporta información.
    Factor negativo: se INVIERTE — el historial dice que la lectura
                     apunta al lado equivocado de forma sistemática.
    """
    return 0.5 + (p_cruda - 0.5) * max(-1.0, min(1.0, factor))


def evaluar_apuesta(p_modelo: float, precio_mercado: float = None) -> dict:
    """
    ¿Hay ventaja frente a lo que cotiza el mercado?

    El precio de un contrato binario ES la probabilidad implícita: pagar
    0.60 equivale a decir que el mercado ve un 60%. La ventaja es la
    diferencia entre nuestra probabilidad y esa, y el valor esperado por
    contrato sale directo de ahí.

    Sin precio de mercado no se puede afirmar que haya ventaja: solo se
    informa la probabilidad y se deja la comparación al usuario.
    """
    if precio_mercado is None:
        return {"comparable": False,
                "nota": "Sin la cuota del mercado no se puede medir la ventaja."}

    precio = max(0.01, min(0.99, float(precio_mercado)))
    ventaja = p_modelo - precio
    # Valor esperado por contrato de 1.00
    ev = p_modelo * (1.0 - precio) - (1.0 - p_modelo) * precio

    if ventaja >= VENTAJA_MINIMA:
        veredicto = "APOSTAR SÍ"
        explicacion = (f"El motor ve {p_modelo*100:.0f}% y el mercado paga como si "
                       f"fuera {precio*100:.0f}%: {ventaja*100:.1f} puntos a favor.")
    elif ventaja <= -VENTAJA_MINIMA:
        veredicto = "APOSTAR NO"
        explicacion = (f"El motor ve {p_modelo*100:.0f}% y el mercado paga como si "
                       f"fuera {precio*100:.0f}%: la ventaja está en el lado "
                       f"contrario ({-ventaja*100:.1f} puntos).")
    else:
        veredicto = "SIN VENTAJA"
        explicacion = (f"El motor ve {p_modelo*100:.0f}% y el mercado {precio*100:.0f}%. "
                       f"La diferencia ({abs(ventaja)*100:.1f} pts) cabe dentro del "
                       f"error del propio modelo: apostar aquí es tirar una moneda "
                       f"pagando comisión.")

    # Fracción de Kelly (un cuarto): apostar el Kelly completo maximiza el
    # crecimiento teórico pero produce rachas de pérdidas que casi nadie
    # aguanta con dinero real.
    kelly = 0.0
    if ventaja > 0 and precio < 1:
        kelly = max(0.0, (p_modelo - precio) / (1 - precio))
    return {
        "comparable": True,
        "ventaja": round(ventaja, 4),
        "ev_por_contrato": round(ev, 4),
        "veredicto": veredicto,
        "explicacion": explicacion,
        "kelly_sugerido": round(min(kelly * 0.25, 0.10), 4),
        "prob_implicita_mercado": round(precio, 3),
    }


def confianza_texto(p: float, factor: float, metodo: str) -> str:
    """Una lectura honesta de cuánto fiarse de este número."""
    dist = abs(p - 0.5)
    if factor < -0.2:
        return ("media — lectura INVERTIDA respecto al cálculo bruto, "
                "porque el historial demuestra que el motor falla "
                "sistemáticamente en este mercado")
    if abs(factor) < 0.15:
        return "nula — el motor no aporta información aquí"
    if factor < 0.3:
        return "baja — el historial del motor todavía no respalda esta lectura"
    if dist < 0.05:
        return "el mercado está en equilibrio: casi una moneda al aire"
    if dist >= 0.2 and factor >= 0.7:
        return "alta — lectura clara y motor calibrado en este mercado"
    return "media"


def resumen_horizonte(minutos: int) -> str:
    """Advertencia honesta según el horizonte pedido."""
    if minutos <= 10:
        return ("A este plazo el precio es casi ruido puro: la probabilidad "
                "rara vez se aleja del 50%. Si lo hace, es información real.")
    if minutos <= 30:
        return "Plazo corto: el movimiento aún está dominado por el flujo de órdenes."
    return "Plazo donde la estructura del mercado empieza a pesar más que el ruido."


# ---------------------------------------------------------------------------
# MEMORIA CORRECTIVA
# ---------------------------------------------------------------------------
# El factor de calibración encoge la probabilidad hacia 50% cuando el motor
# acierta poco. Eso evita mentir, pero NO aprende: con un 30% de acierto el
# sistema se calla, cuando en realidad está diciendo algo valioso.
#
# Acertar el 30% de las veces no es ignorancia — es información INVERTIDA.
# Si cada vez que el motor dice "arriba" el precio va abajo, invertir la
# lectura acierta el 70%. Con 50% no hay nada que aprender; con 30% hay
# muchísimo.
#
# Esta memoria hace tres cosas que el factor solo no puede:
#   1. Guarda el historial POR MERCADO Y HORIZONTE. BTC a 5 minutos y ADA
#      a 60 son problemas distintos y promediarlos oculta ambos.
#   2. Detecta sesgo sistemático (siempre optimista, siempre alcista) y lo
#      resta, en vez de limitarse a encoger.
#   3. Corrige la CURVA de calibración: si de todas las veces que dijo 70%
#      acertó el 45%, aprende a decir 45% la próxima vez.
#
# Toda corrección exige muestra mínima y se explica en el reporte. Un
# sistema que se corrige en silencio es indistinguible de uno que falla.

MINIMO_PARA_INVERTIR = 40        # apuestas antes de plantear inversión
UMBRAL_INVERSION = 0.42          # acierto por debajo del cual hay señal invertida
MINIMO_PARA_SESGO = 25           # apuestas antes de corregir sesgo direccional
MAXIMA_CORRECCION_SESGO = 0.15   # como mucho 15 puntos de corrección


def aprender_de_historial(historial: list) -> dict:
    """
    Extrae correcciones aplicables de las apuestas ya resueltas.

    `historial` son filas con: prob_modelo, acerto, direccion_apostada
    (True = arriba). Devuelve las correcciones que el motor debe aplicar
    la próxima vez, cada una con su justificación.

    Es deliberadamente conservador: cada corrección exige muestra propia
    y está acotada. Un sistema que se reajusta con cinco datos persigue
    ruido, y eso es peor que no corregir nada.
    """
    n = len(historial or [])
    if n < 10:
        return {"aplicable": False, "n": n,
                "nota": f"Hacen falta 10 resultados para empezar a aprender ({n})."}

    aciertos = sum(1 for h in historial if h.get("acerto"))
    tasa = aciertos / n

    correcciones = []
    invertir = False
    ajuste_sesgo = 0.0

    # --- 1) ¿La señal está invertida? ---
    # Solo con muestra seria: invertir por error sería catastrófico y
    # una racha corta de mala suerte lo parece.
    if n >= MINIMO_PARA_INVERTIR and tasa <= UMBRAL_INVERSION:
        invertir = True
        correcciones.append(
            f"Señal INVERTIDA: acierto {tasa*100:.0f}% en {n} predicciones. "
            f"Leer al revés acertaría {(1-tasa)*100:.0f}%. Se invierte la lectura.")

    # --- 2) Sesgo direccional ---
    # ¿El motor falla MÁS cuando dice "arriba" que cuando dice "abajo"?
    #
    # La versión anterior comparaba "con qué frecuencia dice arriba"
    # contra "con qué frecuencia sube", y disparaba en motores
    # perfectamente calibrados: en una muestra pequeña esas dos
    # frecuencias difieren por azar, no por sesgo. Lo que de verdad
    # revela un sesgo es la ASIMETRÍA DE ACIERTO entre ambos lados —
    # exactamente lo que se midió en las señales normales, donde las
    # alcistas acertaban 18.2% y las bajistas 44.4%.
    if n >= MINIMO_PARA_SESGO:
        arriba = [h for h in historial if (h.get("prob_modelo") or 0.5) >= 0.5]
        abajo = [h for h in historial if (h.get("prob_modelo") or 0.5) < 0.5]
        if len(arriba) >= 12 and len(abajo) >= 12:
            ac_arriba = sum(1 for h in arriba if h.get("acerto")) / len(arriba)
            ac_abajo = sum(1 for h in abajo if h.get("acerto")) / len(abajo)
            brecha = ac_arriba - ac_abajo
            # Solo se corrige una asimetría GRANDE: por debajo de 20
            # puntos, el intervalo de confianza con estas muestras la
            # incluye de sobra y corregir sería perseguir ruido.
            if abs(brecha) > 0.20:
                ajuste_sesgo = max(-MAXIMA_CORRECCION_SESGO,
                                    min(MAXIMA_CORRECCION_SESGO, brecha * 0.5))
                peor = "alcistas" if brecha < 0 else "bajistas"
                ajuste_sesgo = -abs(ajuste_sesgo) if brecha < 0 else abs(ajuste_sesgo)
                correcciones.append(
                    f"Asimetría direccional: las lecturas {peor} aciertan "
                    f"{min(ac_arriba, ac_abajo)*100:.0f}% frente a "
                    f"{max(ac_arriba, ac_abajo)*100:.0f}% de las contrarias. "
                    f"Se corrige {abs(ajuste_sesgo)*100:.0f} puntos hacia el lado fiable.")

    # --- 3) Curva de calibración por tramo ---
    # De las veces que dijo ~70%, ¿acertó el 70%?
    tramos = {}
    for h in historial:
        p = h.get("prob_modelo")
        if p is None:
            continue
        # Se trabaja con la CONFIANZA (distancia al 50%), no con la
        # probabilidad cruda: decir 75% arriba y 25% arriba son la misma
        # afirmación de fuerza, en direcciones opuestas.
        conf = abs(p - 0.5) + 0.5
        clave = "fuerte" if conf >= 0.65 else ("media" if conf >= 0.57 else "débil")
        tramos.setdefault(clave, []).append(bool(h.get("acerto")))

    curva = {}
    for clave, res in tramos.items():
        if len(res) >= 8:
            real = sum(res) / len(res)
            curva[clave] = {"n": len(res), "acierto_real": round(real * 100, 1)}
            if clave == "fuerte" and real < 0.5:
                correcciones.append(
                    f"Las lecturas FUERTES aciertan solo {real*100:.0f}% "
                    f"({len(res)} casos): la confianza alta no está justificada.")

    return {
        "aplicable": True,
        "n": n,
        "tasa_acierto": round(tasa * 100, 1),
        "invertir": invertir,
        "ajuste_sesgo": round(ajuste_sesgo, 3),
        "curva": curva,
        "correcciones": correcciones,
        "aprendiendo": bool(correcciones),
    }


def aplicar_memoria(p_calibrada: float, memoria: dict) -> dict:
    """
    Aplica las correcciones aprendidas a la probabilidad ya calibrada.

    El orden importa: primero se invierte si hace falta (es un cambio de
    lectura, no un ajuste), y después se corrige el sesgo residual.
    """
    if not memoria or not memoria.get("aplicable"):
        return {"p": p_calibrada, "cambios": []}

    p = p_calibrada
    cambios = []

    # OJO CON LA DOBLE INVERSIÓN: el factor de calibración ya invierte
    # cuando el historial global es malo. Si la memoria invirtiera otra
    # vez, se anularían entre sí y volveríamos al punto de partida. Por
    # eso `aplicar_memoria` recibe la probabilidad YA calibrada y solo
    # invierte si el factor no lo hizo (se indica con `ya_invertida`).
    if memoria.get("invertir") and not memoria.get("ya_invertida"):
        p = 1.0 - p
        cambios.append(f"lectura invertida por historial ({memoria['tasa_acierto']}%)")

    # Si la lectura ya se invirtió, el sesgo direccional queda corregido
    # de raíz: aplicar ambos sobrecorregiría en la misma dirección.
    aj = 0.0 if memoria.get("invertir") else memoria.get("ajuste_sesgo", 0.0)
    if abs(aj) > 0.01:
        p = max(0.05, min(0.95, p + aj))
        cambios.append(f"sesgo corregido {aj*100:+.0f} pts")

    return {"p": p, "cambios": cambios}
