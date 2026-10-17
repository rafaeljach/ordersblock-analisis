"""
Capa de servicio de ordersblock: el análisis SIN IA del CBS Quantum
Predictor, desacoplado de Telegram, de los créditos y de Claude.

Hace exactamente lo que hace el bot en su ruta sin IA:
  1. Lee la memoria verificada (sesgo direccional, piso de R:R, voz del
     fundamental).
  2. Corre el motor cuantitativo completo (real_engine).
  3. Arma el plan: entrada de mercado o entrada de precisión.
  4. Registra la señal para verificarla al vencer el horizonte.

Y para los bloques de 5/15/30/60 minutos:
  1. Probabilidad de cruce por Montecarlo con vencimiento de reloj.
  2. Calibración con el historial (factor de −1 a +1).
  3. Memoria correctiva (inversión y asimetría direccional).
  4. Registro para resolverla al vencer.

Nada aquí llama a una IA. `import anthropic` no existe en este paquete.
"""

import dataclasses
import logging
import math

import real_engine as engine
import predicciones
import memoria

logger = logging.getLogger("ordersblock.servicio")

# Mismos mercados que la sección 🎲 Predicciones del bot: cripto de alta
# liquidez en Bybit. En activos finos el bloque de 5 minutos es ruido.
MERCADOS_PREDICCION = ["BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "LINK", "AVAX"]


class ErrorServicio(Exception):
    """Error presentable al usuario final (código HTTP + mensaje)."""
    def __init__(self, codigo: int, mensaje: str):
        super().__init__(mensaje)
        self.codigo = codigo
        self.mensaje = mensaje


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------

def _limpio(v):
    """JSON no admite NaN/inf ni tipos de numpy: se convierten aquí."""
    if isinstance(v, dict):
        return {str(k): _limpio(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_limpio(x) for x in v]
    if hasattr(v, "item") and not isinstance(v, (str, bytes)):
        try:
            v = v.item()
        except Exception:
            pass
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    return v


def fmt_precio(valor, referencia=None) -> str:
    if valor is None:
        return "—"
    try:
        v = float(valor)
        d = engine.decimales_para(float(referencia or valor))
        return f"{v:,.{d}f}" if abs(v) >= 1000 else f"{v:.{d}f}"
    except Exception:
        return str(valor)


def mercados_disponibles() -> dict:
    return {
        "cripto": sorted(set(engine.CRIPTO_AMPLIADO) | {"BTC", "ETH"}),
        "forex": sorted(k for k in engine.MERCADO_A_SYMBOL
                        if k in engine.FOREX_AMPLIADO or k in ("EURUSD", "GBPUSD", "USDCOP")),
        "metales": sorted(set(engine.METALES) | {"XAUUSD"}),
        "acciones": sorted(engine.ACCIONES),
        "predicciones": MERCADOS_PREDICCION,
        "nota": ("Cripto va por Bybit (gratis, sin clave). Forex, metales y "
                 "acciones usan Bybit TradFi cuando está listado y, si no, "
                 "Twelve Data (requiere TWELVEDATA_API_KEY)."),
    }


def _fundamental_seguro(mercado: str) -> dict:
    """Fail-soft absoluto: si las noticias fallan, el análisis técnico sigue."""
    try:
        import fundamental_engine
        return fundamental_engine.analizar_fundamental(
            mercado, memoria.efectividad_fundamental(mercado)) or {}
    except Exception as e:
        logger.warning(f"Fundamental no disponible para {mercado}: {e}")
        return {}


# ---------------------------------------------------------------------------
# Radar de motores (versión de datos: la app decide cómo pintarlo)
# ---------------------------------------------------------------------------

def _voto(alcista: bool, bajista: bool) -> str:
    return "ALCISTA" if alcista else ("BAJISTA" if bajista else "NEUTRAL")


def radar(a, incluye_fundamental: bool) -> list:
    mc = a.mc_pct_bull - a.mc_pct_bear
    filas = [
        ("Montecarlo", _voto(mc > 3, mc < -3),
         f"{a.mc_pct_bull}% arriba / {a.mc_pct_bear}% abajo ({a.mc_modelo})"),
        ("Trend Trade Strategy", _voto(a.trend_score > 0.05, a.trend_score < -0.05),
         a.trend_estructura),
        ("Análisis CBS",
         _voto(a.mirrofish_lado_tocado == "sellside" and a.mirrofish_fuerza != "INDETERMINADA",
               a.mirrofish_lado_tocado == "buyside" and a.mirrofish_fuerza != "INDETERMINADA"),
         f"señal {str(a.mirrofish_fuerza).lower()}"),
        ("Order Block + BOS",
         _voto(a.ob_bos_direccion == "ALCISTA", a.ob_bos_direccion == "BAJISTA")
         if a.ob_bos_detectado else "NEUTRAL",
         a.ob_bos_tipo_retest if a.ob_bos_detectado else "sin BOS claro"),
        ("Tendencia CBS (CRT)", "NEUTRAL", a.crt_señal or "sin patrón"),
        ("Tendencia CBS (TBS)", "NEUTRAL", a.turtle_señal or "sin patrón"),
        ("FVG", "NEUTRAL", a.fvg_señal or "sin gap activo"),
        ("Ciclo de Momentum", _voto(a.ciclo_valor < 25, a.ciclo_valor > 75),
         f"{a.ciclo_valor:.0f}/100"),
        ("Momentum multi-plazo", _voto(a.tsmom_score > 0.1, a.tsmom_score < -0.1),
         a.tsmom_texto or "n/a"),
        ("Mercado cripto (BTC)", _voto(a.cripto_score > 0.05, a.cripto_score < -0.05),
         a.cripto_texto or "n/a"),
        ("Marco superior 4h", _voto(a.contexto_4h_score > 0.05, a.contexto_4h_score < -0.05),
         a.contexto_4h_texto or "n/a"),
        ("Volumen", _voto(a.volumen_score > 0.05, a.volumen_score < -0.05),
         a.volumen_texto or "sin datos"),
    ]
    if incluye_fundamental:
        filas.append(("Fundamental (sin IA)",
                      _voto(a.fundamental_etiqueta == "ALCISTA", a.fundamental_etiqueta == "BAJISTA"),
                      f"{a.fundamental_n_titulares} titulares"))
    return [{"motor": m, "voto": v, "detalle": str(d)} for m, v, d in filas]


# ---------------------------------------------------------------------------
# ANÁLISIS DE TRADING (24h)
# ---------------------------------------------------------------------------

def analizar(mercado: str, tipo: str = "mercado", con_fundamental: bool = True,
             usuario: str = None, registrar: bool = True) -> dict:
    """
    tipo = "mercado"   -> entrada a precio actual (siempre se entrega, regla
                          de negocio del bot: nunca se bloquea la señal).
    tipo = "precision" -> orden límite en zona de valor; si no hay zona o
                          la esperanza desde ella es ≤ 0, se dice y no se
                          inventa una entrada.
    """
    mercado = engine.normalizar_mercado(mercado)
    if mercado not in engine.MERCADO_A_SYMBOL:
        raise ErrorServicio(404, f"Mercado '{mercado}' no disponible.")
    if tipo not in ("mercado", "precision"):
        raise ErrorServicio(400, "tipo debe ser 'mercado' o 'precision'.")

    st_alc = memoria.estadisticas_direccion(mercado, "ALCISTA")
    st_baj = memoria.estadisticas_direccion(mercado, "BAJISTA")
    st_glb = memoria.estadisticas_mercado(mercado)
    fundamental = _fundamental_seguro(mercado) if con_fundamental else {}

    try:
        a = engine.generar_analisis_real(mercado, st_alc, st_baj, fundamental, st_glb)
    except engine.CuotaAgotadaError:
        raise ErrorServicio(503, "Proveedor de datos saturado. Intenta en unos minutos.")
    except engine.DatosNoDisponiblesError as e:
        raise ErrorServicio(503, str(e))

    base = {
        "mercado": a.mercado,
        "precio_actual": a.precio_actual,
        "direccion": a.direccion_predicha,
        "calidad": a.calidad_setup or ("ALTA CONVICCIÓN" if a.señal_autorizada else "ESTÁNDAR"),
        "confianza_pct": a.confianza_pct,
        "aviso_confianza": (
            "En el histórico verificado del bot, las señales con confianza alta "
            "acertaron MENOS que las de confianza baja. No es probabilidad de éxito."
            if (a.confianza_pct or 0) >= 55 else None),
        "consenso_pct": a.consenso_pct,
        "coincidencias": f"{a.coincidencias}/{a.total_señales}",
        "conviccion_direccional": a.conviccion_direccional,
        "motivo_desempate": a.motivo_desempate,
        "riesgos": list(a.riesgos_contextuales or []),
        "sesion": a.sesion,
        "volatilidad": a.regimen_volatilidad,
        "smc": {"score": a.smc_score, "calidad": a.smc_calidad, "adx": a.smc_adx,
                "detalle": a.smc_detalle},
        "fundamental": ({"etiqueta": a.fundamental_etiqueta, "score": a.fundamental_score,
                         "titulares": a.fundamental_n_titulares,
                         "detalle": a.fundamental_detalle} if con_fundamental else None),
        "radar": radar(a, con_fundamental),
        "memoria": {"verificadas_mercado": st_glb["total_verificadas"],
                    "acierto_mercado_pct": st_glb["tasa_acierto"]},
        "aviso_legal": "Análisis probabilístico. No es asesoría financiera.",
    }

    if tipo == "mercado":
        plan = {
            "tipo": "mercado",
            "entrada": a.entrada_mercado,
            "sl": a.sl,
            "objetivos": a.escalera_tp or [{"etiqueta": "TP1", "precio": a.tp1, "rr": a.rr_ratio}],
            "rr": a.rr_ratio,
            "prob_tp_antes_que_sl_pct": a.prob_tp_primero,
            "esperanza_r": a.esperanza_r,
            "aviso_esperanza": ("Esperanza negativa: repetida muchas veces pierde en promedio."
                                if (a.esperanza_r or 0) < 0 else None),
            "aseguramiento": ({"precio": a.aseguramiento_precio, "r": a.aseguramiento_r,
                               "porcentaje": a.aseguramiento_pct,
                               "instruccion": "Asegura parcial ahí y mueve el stop a la entrada."}
                              if a.aseguramiento_precio else None),
            "coste_ida_vuelta_pct": a.coste_pct,
        }
        if registrar:
            plan["id_seguimiento"] = memoria.registrar_senal(
                mercado, a.direccion_predicha, a.entrada_mercado, a.sl, a.tp1, "mercado",
                a.fundamental_etiqueta if con_fundamental else None, usuario)
        return _limpio({**base, "plan": plan, "texto": texto_analisis(base, plan)})

    # --- PRECISIÓN: misma lógica que el flujo de precisión del bot ---
    sesion = a.sesion or {}
    if sesion.get("liquidez") == "CERRADO":
        plan = {"tipo": "precision", "disponible": False,
                "motivo": sesion.get("nota") or "Mercado cerrado: no se deja orden pendiente."}
        return _limpio({**base, "plan": plan, "texto": texto_analisis(base, plan)})

    atr = max(a.precio_actual * a.volatilidad_por_vela * (24 ** 0.5), a.precio_actual * 1e-5)
    ent = engine.construir_entrada_precision(
        a.precio_actual, a.direccion_predicha,
        {"confirmada": a.zona_cero_confirmada, "zona_min": a.zona_cero_min, "zona_max": a.zona_cero_max},
        {"detectado": a.ob_bos_detectado, "zona_min": a.ob_zona_min, "zona_max": a.ob_zona_max},
        {"gap_min": getattr(a, "fvg_gap_min", None), "gap_max": getattr(a, "fvg_gap_max", None)},
        a.mirrofish_nivel_espejo, atr, mercado)
    if not ent.get("viable"):
        plan = {"tipo": "precision", "disponible": False,
                "motivo": "El precio no está cerca de ninguna zona de valor donde merezca "
                          "la pena dejar una orden esperando."}
        return _limpio({**base, "plan": plan, "texto": texto_analisis(base, plan)})

    alcista = a.direccion_predicha == "ALCISTA"
    pp = engine.plan_desde_entrada_precision(
        ent["entrada"], a.direccion_predicha, mercado, a.volatilidad_por_vela, atr,
        a.p50_max if alcista else a.p50_min, a.p95_max if alcista else a.p05_min,
        rr_piso=a.rr_piso_usado)
    esp = engine.esperanza_desde_entrada(
        pp["entrada"], pp["sl"], pp["escalera"][0]["precio"] if pp["escalera"] else None,
        a.direccion_predicha, a.volatilidad_por_vela, a.drift_por_vela, mercado,
        seed=getattr(a, "seed_analisis", None))
    if esp.get("valida") and esp["esperanza_r"] <= 0:
        plan = {"tipo": "precision", "disponible": False,
                "motivo": (f"Hay zona en {fmt_precio(pp['entrada'])} ({ent['origen']}), pero "
                           f"incluso entrando ahí la esperanza sale {esp['esperanza_r']:+.3f}R "
                           f"({esp['p_tp']}% TP vs {esp['p_sl']}% SL). Esperar no arregla "
                           f"una señal débil.")}
        return _limpio({**base, "plan": plan, "texto": texto_analisis(base, plan)})

    seg = engine.nivel_aseguramiento(pp["entrada"], pp["sl"], a.direccion_predicha)
    plan = {
        "tipo": "precision", "disponible": True,
        "entrada": pp["entrada"], "distancia_pct": ent.get("distancia_pct"),
        "origen_zona": ent.get("origen"),
        "sl": pp["sl"], "objetivos": pp["escalera"], "rr": pp["rr_ratio"],
        "esperanza_r": esp.get("esperanza_r") if esp.get("valida") else None,
        "prob_tp_pct": esp.get("p_tp"), "prob_sl_pct": esp.get("p_sl"),
        "aseguramiento": ({"precio": seg["precio"], "r": 0.8, "porcentaje": 50,
                           "instruccion": "Asegura el 50% ahí y mueve el stop a la entrada."}
                          if seg.get("disponible") else None),
        "caduca_horas": 24,
        "aviso": "El precio puede no volver a esa zona: ese es el coste de no perseguir el mercado.",
    }
    # Una orden pendiente se registra igual que el bot: el horizonte cuenta
    # desde ahora. Si nunca se activa, la verificación lo reflejará como
    # NINGUNO en tipo_toque (no hubo operación).
    if registrar:
        plan["id_seguimiento"] = memoria.registrar_senal(
            mercado, a.direccion_predicha, pp["entrada"], pp["sl"],
            pp["escalera"][0]["precio"] if pp["escalera"] else None, "precision",
            a.fundamental_etiqueta if con_fundamental else None, usuario)
    return _limpio({**base, "plan": plan, "texto": texto_analisis(base, plan)})


def texto_analisis(base: dict, plan: dict) -> str:
    """Texto plano listo para mostrar (sin Markdown: lo pinta cualquier app)."""
    m, p = base["mercado"], base["precio_actual"]
    flecha = "🟢" if base["direccion"] == "ALCISTA" else ("🔴" if base["direccion"] == "BAJISTA" else "⚪")
    l = [f"{m} — {flecha} {base['direccion']} · {base['calidad']}",
         f"Precio actual: {fmt_precio(p)}", ""]
    if plan.get("tipo") == "precision" and not plan.get("disponible"):
        l += [f"🎯 Sin entrada de precisión ahora.", plan.get("motivo", "")]
    else:
        ref = plan.get("entrada") or p
        etiqueta = "Entrada pendiente (límite)" if plan["tipo"] == "precision" else "Entrada a mercado"
        l.append(f"💹 {etiqueta}: {fmt_precio(plan['entrada'], ref)}"
                 + (f" ({plan['distancia_pct']}% · {plan['origen_zona']})"
                    if plan["tipo"] == "precision" else ""))
        for i, t in enumerate(plan.get("objetivos") or []):
            l.append(f"🎯 TP{i+1}: {fmt_precio(t.get('precio'), ref)} (R:R 1:{t.get('rr')})")
        l.append(f"🚫 SL: {fmt_precio(plan['sl'], ref)}")
        if plan.get("esperanza_r") is not None:
            l.append(f"🎲 Esperanza: {plan['esperanza_r']:+.3f}R")
        if plan.get("aviso_esperanza"):
            l.append(f"⚠️ {plan['aviso_esperanza']}")
        if plan.get("aseguramiento"):
            s = plan["aseguramiento"]
            l.append(f"🛡️ Asegura {s['porcentaje']}% en {fmt_precio(s['precio'], ref)} "
                     f"(+{s['r']}R) y mueve el stop a la entrada")
    l += ["", f"🤝 Confianza {base['confianza_pct']}% · consenso {base['consenso_pct']}%"]
    if base.get("aviso_confianza"):
        l.append(f"⚠️ {base['aviso_confianza']}")
    for r in base.get("riesgos", [])[:2]:
        l.append(f"🚨 {r}")
    l += ["", "⚠️ " + base["aviso_legal"]]
    return "\n".join(l)


# ---------------------------------------------------------------------------
# PREDICCIÓN DE BLOQUE (5 / 15 / 30 / 60 min)
# ---------------------------------------------------------------------------

def predecir(mercado: str, minutos: int, strike: float = None, cuota: float = None,
             usuario: str = None, registrar: bool = True) -> dict:
    """
    ¿Estará el precio ARRIBA o ABAJO de la referencia al cierre del bloque?

    `cuota` es el precio del contrato SÍ en Polymarket/Kalshi (0.01–0.99);
    con ella se mide la ventaja. Sin ella solo se da la probabilidad.
    """
    mercado = engine.normalizar_mercado(mercado)
    if mercado not in MERCADOS_PREDICCION:
        raise ErrorServicio(404, f"Predicciones solo cubre: {', '.join(MERCADOS_PREDICCION)}.")
    if minutos not in predicciones.HORIZONTES:
        raise ErrorServicio(400, f"minutos debe ser uno de {list(predicciones.HORIZONTES)}.")
    if cuota is not None and not (0.01 <= cuota <= 0.99):
        raise ErrorServicio(400, "cuota debe estar entre 0.01 y 0.99.")

    intervalo = predicciones.INTERVALO_POR_HORIZONTE.get(minutos, "1min")
    mins_vela = {"1min": 1, "5min": 5, "15min": 15}.get(intervalo, 1)
    try:
        datos = engine.obtener_datos_mercado(mercado, intervalo, 500)
    except Exception as e:
        raise ErrorServicio(503, f"Sin datos de {mercado} ahora ({type(e).__name__}).")

    precio = datos["precio_actual"]
    strike_real = float(strike) if strike else precio

    # Vencimiento de RELOJ: el contrato de las 13:15 vence a las 13:15
    # aunque se consulte a las 13:10 o a las 13:14.
    venc = predicciones.proximo_vencimiento(minutos)
    min_reales = venc["minutos_restantes"]

    prob = predicciones.probabilidad_cruce(
        precio, strike_real, min_reales, datos["volatilidad_por_vela"], mins_vela,
        datos.get("drift_por_vela", 0.0), datos.get("retornos_log"),
        seed=datos.get("seed_determinista"))
    if not prob.get("disponible"):
        raise ErrorServicio(503, "No se pudo calcular la probabilidad ahora.")

    cal = predicciones.factor_calibracion(
        memoria.estadisticas_predicciones(mercado=mercado),
        memoria.estadisticas_mercado(mercado))
    p_cruda = prob["p_arriba"]
    p_cal = predicciones.calibrar(p_cruda, cal["factor"])
    p_cal = max(predicciones.PROB_MINIMA_MOSTRADA, min(predicciones.PROB_MAXIMA_MOSTRADA, p_cal))

    hist = memoria.historial_aprendizaje(mercado=mercado, minutos=minutos)
    if len(hist) < 15:
        hist = memoria.historial_aprendizaje(mercado=mercado)
    mem = predicciones.aprender_de_historial(hist)
    if isinstance(mem, dict):
        mem["ya_invertida"] = cal["factor"] < -0.15
    corr = predicciones.aplicar_memoria(p_cal, mem)
    p_cal = corr["p"]

    ev = predicciones.evaluar_apuesta(p_cal, cuota)

    arriba = p_cal >= 0.5
    dist = abs(p_cal - 0.5)
    conviccion = ("ALTA" if dist >= 0.12 else "MEDIA" if dist >= 0.06
                  else "BAJA" if dist >= 0.02 else "NULA")
    p_dir = p_cal if arriba else 1 - p_cal

    avisos = []
    if cal["factor"] < -0.15:
        avisos.append(f"Lectura invertida por aprendizaje: el historial acierta "
                      f"{cal.get('acierto_historico')}% en este mercado, así que se lee al revés.")
    elif abs(cal["factor"]) < 0.15:
        avisos.append("Sin ventaja demostrada en este mercado: la probabilidad se pega al 50%.")
    avisos += corr.get("cambios", [])
    avisos.append(predicciones.resumen_horizonte(minutos))

    resultado = {
        "mercado": mercado,
        "vence_utc": venc["etiqueta"],
        "vence_iso_utc": _vencimiento_iso(venc, minutos) + "Z",
        "segundos_restantes": venc.get("segundos_restantes"),
        "bloque_min": minutos,
        "precio_actual": precio,
        "referencia": strike_real,
        "direccion": "ARRIBA" if arriba else "ABAJO",
        "probabilidad_pct": round(p_dir * 100, 1),
        "contraria_pct": round((1 - p_dir) * 100, 1),
        "prob_arriba_pct": round(p_cal * 100, 1),
        "conviccion": conviccion,
        "rango_p05_p95": [prob.get("p05"), prob.get("p95")],
        "simulaciones": prob.get("n_rutas"),
        "metodo": prob.get("metodo"),
        "calibracion": {"factor": cal["factor"], "fuente": cal["fuente"],
                        "acierto_historico": cal.get("acierto_historico")},
        "memoria": {k: mem.get(k) for k in ("aplicable", "n", "tasa_acierto", "invertir",
                                            "ajuste_sesgo", "correcciones")} if mem else None,
        "ventaja": ev,
        "avisos": avisos,
        "aviso_legal": "Probabilidad estimada, no garantía. No es asesoría financiera.",
    }

    if registrar:
        resultado["id_seguimiento"] = memoria.registrar_prediccion(
            mercado, minutos, strike_real, precio, p_cal, p_cruda, cal["factor"],
            _vencimiento_iso(venc, minutos), cuota, ev.get("veredicto", ""), usuario)
    return _limpio(resultado)


def _vencimiento_iso(venc: dict, minutos: int) -> str:
    """ISO naive-UTC del vencimiento de reloj, para comparar en SQLite."""
    from datetime import datetime, timedelta, timezone
    for k in ("vence_iso", "vence", "vencimiento"):
        v = venc.get(k)
        if isinstance(v, datetime):
            return v.astimezone(timezone.utc).replace(tzinfo=None).isoformat() if v.tzinfo else v.isoformat()
        if isinstance(v, str) and len(v) >= 16:
            return v.replace("Z", "").split("+")[0]
    seg = float(venc.get("segundos_restantes") or minutos * 60)
    return (datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=seg)).isoformat()


# ---------------------------------------------------------------------------
# CIERRE DEL CICLO DE APRENDIZAJE
# ---------------------------------------------------------------------------

def resolver_pendientes() -> dict:
    """
    Verifica contra el precio REAL todo lo que ya venció. Sin esto el
    motor no sabe si acertó y la memoria nunca se llena — es la pieza
    que hace que aprenda.
    """
    hechas = {"predicciones": 0, "senales": 0, "errores": 0}

    pend = memoria.predicciones_por_vencer()
    precios = {}
    for mkt in sorted({p["mercado"] for p in pend}):
        try:
            precios[mkt] = engine.obtener_datos_mercado(
                mkt, "1min", 60, engine.PRIORIDAD_WORKER)["precio_actual"]
        except Exception as e:
            logger.warning(f"Sin precio para resolver {mkt}: {e}")
    for p in pend:
        pf = precios.get(p["mercado"])
        if pf is None:
            continue
        acerto = (pf > p["strike"]) == (p["prob_modelo"] >= 0.5)
        memoria.resolver_prediccion(p["id"], pf, acerto)
        hechas["predicciones"] += 1

    for s in memoria.senales_por_verificar():
        try:
            acierto, salida, det = engine.verificar_resultado(
                s["mercado"], s["precio_entrada"], s["direccion"],
                umbral_pct=s["umbral_pct"] or 0.003,
                horizonte_hasta=s["horizonte_hasta"], timestamp_prediccion=s["creada"],
                tp_valor=s["tp"], sl_valor=s["sl"])
            fa = None
            fe = s.get("fundamental_etiqueta")
            if fe in ("ALCISTA", "BAJISTA") and s["precio_entrada"]:
                mov = (salida - s["precio_entrada"]) / s["precio_entrada"]
                umbral = s["umbral_pct"] or 0.003
                if abs(mov) > umbral:
                    fa = (mov > 0) == (fe == "ALCISTA")
            memoria.marcar_senal(s["id"], acierto, salida, det.get("tipo_toque"),
                                 det.get("mae_r"), det.get("mfe_r"), fa)
            hechas["senales"] += 1
        except Exception as e:
            hechas["errores"] += 1
            logger.error(f"No se pudo verificar señal {s['id']}: {e}")
    return hechas
