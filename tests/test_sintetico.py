"""
Pruebas con datos sintéticos (sin red): el motor recibe velas generadas
y se comprueba todo el ciclo — análisis, precisión, predicción de
bloque, registro y verificación contra el precio "real" posterior.

Ejecutar desde la carpeta del paquete:
    python -m tests.test_sintetico
"""

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)
os.environ["ORDERSBLOCK_DB"] = os.path.join(tempfile.mkdtemp(), "prueba.db")
os.environ.pop("TWELVEDATA_API_KEY", None)

import numpy as np

import bybit_client
import fundamental_engine
import memoria
import predicciones
import real_engine as engine
import servicio

_SEG = {"1min": 60, "5min": 300, "15min": 900, "1h": 3600, "4h": 14400, "1day": 86400}
_deriva = {"valor": 0.0006}


def velas_falsas(symbol, interval="1h", limit=200, testnet=False):
    """GBM con deriva controlable; semilla por símbolo e intervalo."""
    rng = np.random.default_rng(abs(hash((symbol, interval))) % 2**32)
    paso = _SEG.get(interval, 3600)
    n = min(limit, 1000)
    base = 100.0 if "BTC" not in symbol else 65000.0
    r = rng.normal(_deriva["valor"], 0.006, n)
    cierres = base * np.exp(np.cumsum(r))
    fin = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    out = []
    for i, c in enumerate(cierres):
        t = fin - timedelta(seconds=paso * (n - 1 - i))
        o = cierres[i - 1] if i else c
        out.append({"datetime": t.strftime("%Y-%m-%d %H:%M:%S"),
                    "open": float(o), "high": float(max(o, c) * 1.002),
                    "low": float(min(o, c) * 0.998), "close": float(c),
                    "volume": float(rng.uniform(100, 1000))})
    return out


bybit_client.obtener_velas = velas_falsas
fundamental_engine.analizar_fundamental = lambda m, ef=None: {
    "score": 0.2, "etiqueta": "ALCISTA", "n_titulares": 7, "detalle": "sintético",
    "titulares": []}


def comprobar(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  ✔", msg)


def main():
    memoria.inicializar()
    for mercado in ("BTC", "ETH", "XRP"):
        print(f"\n== Análisis de mercado {mercado}")
        a = servicio.analizar(mercado, "mercado", True, usuario="test")
        p = a["plan"]
        comprobar(a["direccion"] in ("ALCISTA", "BAJISTA", "RANGO"), f"dirección {a['direccion']}")
        comprobar(p["entrada"] and p["sl"], "entrada y SL presentes (nunca se bloquea la señal)")
        if a["direccion"] == "ALCISTA":
            comprobar(p["sl"] < p["entrada"] < p["objetivos"][0]["precio"], "SL < entrada < TP1")
        elif a["direccion"] == "BAJISTA":
            comprobar(p["sl"] > p["entrada"] > p["objetivos"][0]["precio"], "SL > entrada > TP1")
        comprobar(p["rr"] is None or p["rr"] >= 1.3 - 1e-9, f"R:R ≥ 1.3 ({p['rr']})")
        comprobar(len(a["radar"]) >= 12, "radar con todos los motores")
        comprobar("Mirrofish" not in a["texto"] and "mirrofish" not in str(a["radar"]).lower(),
                  "sin la palabra prohibida en texto visible")
        comprobar(isinstance(a["texto"], str) and mercado in a["texto"], "texto plano generado")

        print(f"== Precisión {mercado}")
        b = servicio.analizar(mercado, "precision", False)
        bp = b["plan"]
        if bp.get("disponible"):
            comprobar(bp["rr"] >= 1.3 - 1e-9, f"precisión R:R ≥ 1.3 ({bp['rr']})")
            comprobar(bp["esperanza_r"] is None or bp["esperanza_r"] > 0, "esperanza > 0 si se entrega")
        else:
            comprobar(bool(bp.get("motivo")), f"sin zona, motivo explicado: {bp['motivo'][:60]}…")

    print("\n== Predicciones de bloque")
    for h in predicciones.HORIZONTES:
        r = servicio.predecir("BTC", h, cuota=0.5)
        comprobar(15.0 <= r["probabilidad_pct"] <= 85.0, f"{h}m → {r['direccion']} {r['probabilidad_pct']}% "
                  f"vence {r['vence_utc']}")
        comprobar(r["vence_utc"].endswith(tuple(f"{m:02d}" for m in range(0, 60, h))) or h == 60,
                  f"vencimiento de reloj redondo ({r['vence_utc']})")
    try:
        servicio.predecir("XAUUSD", 15)
        raise AssertionError("debería rechazar mercados no cripto")
    except servicio.ErrorServicio as e:
        comprobar(e.codigo == 404, "predicciones solo cripto")

    print("\n== Ciclo de aprendizaje")
    # Se fuerza el vencimiento al pasado para resolver ya.
    with memoria._conexion() as c:
        c.execute("UPDATE predicciones SET vence_en = '2000-01-01T00:00:00'")
        c.execute("UPDATE senales SET horizonte_hasta = ?, creada = ?",
                  ((datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)).isoformat(),
                   (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=24)).isoformat()))
    res = servicio.resolver_pendientes()
    comprobar(res["predicciones"] == 4, f"4 predicciones resueltas ({res})")
    comprobar(res["senales"] >= 3 and res["errores"] == 0, f"señales verificadas ({res})")
    rs = memoria.resumen()
    comprobar(rs["predicciones"]["resueltas"] == 4, "memoria refleja lo resuelto")

    print("\n== Memoria correctiva con historial INVERTIDO (motor que falla 30%)")
    rng = np.random.default_rng(7)
    venc = "2000-01-01T00:00:00"
    for i in range(60):
        pid = memoria.registrar_prediccion("ETH", 15, 100.0, 100.0, 0.62, 0.62, 0.6, venc)
        memoria.resolver_prediccion(pid, 99.0, bool(rng.random() < 0.30))
    r = servicio.predecir("ETH", 15)
    comprobar(r["calibracion"]["factor"] < 0 or (r["memoria"] or {}).get("invertir"),
              f"la lectura se invierte al aprender (factor {r['calibracion']['factor']})")

    print("\n== Escáner de oportunidades")
    import escaner
    os.environ["ORDERSBLOCK_ESCANER_MERCADOS"] = "BTC,ETH,NOEXISTE"
    escaner.PAUSA_SEG = 0
    escaner.ARCHIVO = os.path.join(tempfile.mkdtemp(), "escaner.json")
    antes = memoria.resumen("BTC")["senales"]["emitidas"]
    est = escaner.ciclo()
    comprobar(set(est["mercados"]) == {"BTC", "ETH"}, "recorre solo los mercados conocidos del universo")
    comprobar(all(not r.get("error") for r in est["mercados"].values()), "los dos mercados se analizaron sin error")
    comprobar(os.path.exists(escaner.ARCHIVO), "el resultado se guarda en disco")
    op = escaner.oportunidades("BTC", 5)
    comprobar(op["escaneados"] == 2 and all(o["mercado"] != "BTC" for o in op["oportunidades"]),
              "oportunidades excluye el mercado consultado")
    falso = {"mercado": "X", "direccion": "ALCISTA", "consenso_pct": 70, "esperanza_r": 0.3,
             "entrada": 1, "sl": 0.9, "tp1": 1.2, "prob_tp_pct": 60}
    comprobar(escaner.es_oportunidad(falso) and not escaner.es_oportunidad({**falso, "esperanza_r": -0.1})
              and not escaner.es_oportunidad({**falso, "consenso_pct": 40}),
              "solo cuenta como oportunidad con esperanza positiva y consenso suficiente")
    comprobar(escaner.puntuacion(falso) > escaner.puntuacion({**falso, "consenso_pct": 55}),
              "a igual esperanza, mas consenso puntua mas alto")
    comprobar(memoria.resumen("BTC")["senales"]["emitidas"] == antes, "el escaner no registra senales en la memoria")

    print("\n== Sin dependencias de IA")
    for f in os.listdir(RAIZ):
        if f.endswith(".py"):
            txt = open(os.path.join(RAIZ, f), encoding="utf-8").read()
            import re
            comprobar(not re.search(r"^\s*(import|from)\s+(anthropic|claude_engine|openai)\b",
                                    txt, re.M), f"{f} no importa ninguna IA")
    print("\nTODO OK")


if __name__ == "__main__":
    main()
