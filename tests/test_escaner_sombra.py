"""
Escáner con entrada en zona y candidatos en sombra (sin red, velas sintéticas).

Antes el escáner solo miraba la entrada a mercado: su esperanza por Monte Carlo
sale casi siempre ~0 menos la comisión y el consenso exige 3 de 4 señales, así
que nunca encontraba nada (0 planes en sombra en producción, nov-2026).

Ejecutar desde la carpeta del paquete:
    python -m tests.test_escaner_sombra
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)
os.environ["ORDERSBLOCK_DB"] = os.path.join(tempfile.mkdtemp(), "prueba.db")
os.environ.pop("TWELVEDATA_API_KEY", None)
MERCADOS = ["BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "LINK", "AVAX"]
os.environ["ORDERSBLOCK_ESCANER_MERCADOS"] = ",".join(MERCADOS)

import numpy as np

import bybit_client
import fundamental_engine
import memoria
import real_engine as engine
import servicio
import escaner

_SEG = {"1min": 60, "5min": 300, "15min": 900, "1h": 3600, "4h": 14400, "1day": 86400}
SEMILLA = {"s": 0}


def velas(symbol, interval="1h", limit=200, testnet=False):
    """Paseo aleatorio de colas gruesas, SIN deriva: el caso realista y el difícil."""
    rng = np.random.default_rng(abs(hash((symbol, interval, SEMILLA["s"]))) % 2**32)
    paso = _SEG.get(interval, 3600)
    n = min(limit, 1000)
    base = 65000.0 if "BTC" in symbol else 100.0
    sig = 0.006 * (2 if interval == "4h" else 1)
    r = rng.standard_t(4, n) * sig / np.sqrt(2)
    c = base * np.exp(np.cumsum(r))
    fin = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    out = []
    for i, x in enumerate(c):
        o = c[i - 1] if i else x
        w = abs(rng.normal(0, sig * 0.6))
        t = fin - timedelta(seconds=paso * (n - 1 - i))
        out.append({"datetime": t.strftime("%Y-%m-%d %H:%M:%S"), "open": float(o),
                    "high": float(max(o, x) * (1 + w)), "low": float(min(o, x) * (1 - w)),
                    "close": float(x), "volume": float(rng.uniform(100, 1000))})
    return out


bybit_client.obtener_velas = velas
fundamental_engine.analizar_fundamental = lambda m, ef=None: {
    "score": 0, "etiqueta": "NEUTRAL", "n_titulares": 0, "detalle": "", "titulares": []}

ok = mal = 0


def chk(cond, msg):
    global ok, mal
    if cond:
        ok += 1
        print("  ✔", msg)
    else:
        mal += 1
        print("  ✘ FALLA", msg)


def limpiar_cache():
    engine._cache.clear()


def main():
    memoria.inicializar()
    escaner.PAUSA_SEG = 0
    escaner.ARCHIVO = os.path.join(tempfile.mkdtemp(), "escaner.json")

    print("\n== Una pasada del motor, dos planes")
    limpiar_cache()
    d = servicio.analizar_escaner("BTC")
    chk("plan" in d and "plan_precision" in d and d["plan"]["tipo"] == "mercado"
        and d["plan_precision"]["tipo"] == "precision", "analizar_escaner devuelve el plan a mercado y el plan en zona")
    limpiar_cache()
    m = servicio.analizar("BTC", "mercado", con_fundamental=False, registrar=False)
    limpiar_cache()
    p = servicio.analizar("BTC", "precision", con_fundamental=False, registrar=False)
    pp = dict(d["plan_precision"])
    if not pp.get("disponible"):
        pp.pop("esperanza_r", None)
    chk(json.dumps(d["plan"], default=str) == json.dumps(m["plan"], default=str)
        and json.dumps(pp, default=str) == json.dumps(p["plan"], default=str),
        "los dos planes son los mismos que da el servicio por separado")
    antes = memoria.resumen("BTC")["senales"]["emitidas"]

    print("\n== Ciclos con varios mercados y semillas (paseo aleatorio sin deriva)")
    total_cand = total_zona = total_ops_viejas = 0
    for s in range(6):
        SEMILLA["s"] = s
        limpiar_cache()
        est = escaner.ciclo()
        op_cliente = escaner.oportunidades(None, 20)
        op_sombra = escaner.oportunidades(None, 20, sombra=True)
        total_ops_viejas += len(op_cliente["oportunidades"])
        total_cand += len(op_sombra["oportunidades"])
        total_zona += sum(1 for c in op_sombra["oportunidades"] if c["tipo_plan"] == "zona")
        diag = op_sombra["diagnostico"]
        if s == 0:
            chk(sum(diag.values()) == len(est["mercados"]) == len(MERCADOS),
                "el diagnostico explica cada mercado escaneado: " + json.dumps(diag, ensure_ascii=False))
            chk(op_cliente.get("diagnostico") == diag and op_cliente["sombra"] is False and op_sombra["sombra"] is True,
                "la respuesta del cliente lleva el mismo diagnostico y marca sombra=false")
            chk(op_cliente["umbral"] == {"esperanza_min": escaner.ESPERANZA_MIN, "consenso_min": escaner.CONSENSO_MIN},
                "el umbral del cliente no cambia (esperanza 0.05, consenso 55)")
        for c in op_sombra["oportunidades"]:
            bien = (c["direccion"] in ("ALCISTA", "BAJISTA") and c["consenso_pct"] >= escaner.SOMBRA_CONSENSO_MIN
                    and c["esperanza_r"] > 0 and c["rr"] >= escaner.SOMBRA_RR_MIN and c["sombra"] is True
                    and None not in (c["entrada"], c["sl"], c["tp1"]))
            alc = c["direccion"] == "ALCISTA"
            geom = (c["sl"] < c["entrada"] < c["tp1"]) if alc else (c["tp1"] < c["entrada"] < c["sl"])
            if not (bien and geom):
                chk(False, "candidato invalido: " + json.dumps(c, default=str)[:300])
    chk(total_cand > 0, f"el escaner ya encuentra candidatos en sombra: {total_cand} en 6 ciclos de {len(MERCADOS)} mercados "
        f"({total_zona} en zona); con la puerta vieja el cliente ve {total_ops_viejas}")
    chk(total_zona > 0, "hay candidatos con entrada en zona (lo que antes nunca se miraba)")
    chk(memoria.resumen("BTC")["senales"]["emitidas"] == antes, "el escaner sigue sin registrar senales en la memoria")

    print("\n== Reglas de la puerta de sombra")
    base = {"mercado": "X", "direccion": "BAJISTA", "consenso_pct": 50, "esperanza_r": -0.02,
            "entrada": 100, "sl": 101, "tp1": 98.5, "rr": 1.5, "smc_score": 3, "precio": 100,
            "zona": {"disponible": True, "entrada": 101, "sl": 102, "tp1": 99, "rr": 2.0, "esperanza_r": 0.04,
                     "origen_zona": "FVG", "distancia_pct": 1.0}}
    c = escaner.candidato_sombra(base)
    chk(c and c["tipo_plan"] == "zona" and c["entrada"] == 101, "prefiere la entrada en zona cuando es valida")
    c = escaner.candidato_sombra({**base, "esperanza_r": 0.03, "zona": {"disponible": False, "motivo": "x"}})
    chk(c and c["tipo_plan"] == "mercado", "sin zona, usa la entrada a mercado si su esperanza es positiva")
    chk(escaner.candidato_sombra({**base, "consenso_pct": 25}) is None
        and escaner.motivo_descarte({**base, "consenso_pct": 25}) == "consenso bajo", "consenso 1 de 4: se descarta y se dice por que")
    chk(escaner.candidato_sombra({**base, "zona": {**base["zona"], "rr": 1.1}}) is None,
        "R:R menor que 1.3: no es candidato")
    chk(escaner.candidato_sombra({**base, "zona": {**base["zona"], "esperanza_r": 0}}) is None,
        "esperanza cero o negativa: no es candidato")
    chk(escaner.candidato_sombra({**base, "direccion": "NEUTRAL"}) is None, "sin direccion: no es candidato")
    chk(escaner.motivo_descarte({"mercado": "X", "error": "x"}) == "error de datos", "un error de datos queda contado como tal")
    chk(escaner.candidato_sombra({**base, "liquidez": "CERRADO", "esperanza_r": 0.03}) is None
        and escaner.motivo_descarte({**base, "liquidez": "CERRADO"}) == "mercado cerrado",
        "mercado cerrado (forex/metales en fin de semana): nunca es candidato")
    chk(escaner.candidato_sombra({**base, "zona": {**base["zona"], "sl": 100.5}}) is None
        or escaner.candidato_sombra({**base, "zona": {**base["zona"], "sl": 100.5}})["tipo_plan"] != "zona",
        "SL del lado equivocado en la zona: la zona no se usa")
    chk(escaner.candidato_sombra({**base, "zona": {**base["zona"], "entrada": 99.0, "tp1": 97, "sl": 100.5}}) is None,
        "venta con la orden limite por debajo del precio: no es zona de retroceso")
    chk(escaner.candidato_sombra({**base, "zona": {**base["zona"], "rr": float("inf")}}) is None
        and escaner.candidato_sombra({**base, "zona": {**base["zona"], "esperanza_r": "abc"}}) is None,
        "infinitos o texto en rr/esperanza: descartados sin romper")
    dg = escaner.diagnostico({"A": "fila rota", "B": {**base, "consenso_pct": "70"}, "C": {**base, "consenso_pct": object()}})
    chk(dg.get("estado invalido") == 1 and dg.get("consenso bajo") == 1 and sum(dg.values()) == 3,
        "estado guardado raro: una fila rota se cuenta como estado invalido, el texto no rompe nada y la ruta no cae " + json.dumps(dg))
    viejo = {"mercado": "X", "direccion": "ALCISTA", "consenso_pct": 70, "esperanza_r": 0.3,
             "entrada": 1, "sl": 0.9, "tp1": 1.2, "prob_tp_pct": 60}
    chk(escaner.es_oportunidad(viejo) and not escaner.es_oportunidad({**viejo, "consenso_pct": 50}),
        "la puerta del cliente es la misma de antes")

    print("\n== Ruta /oportunidades?sombra=1")
    import api
    from starlette.testclient import TestClient
    api.API_KEY = "clave-de-prueba"
    cli = TestClient(api.app)
    r1 = cli.get("/oportunidades?n=20", headers={"X-API-Key": "clave-de-prueba"}).json()
    r2 = cli.get("/oportunidades?n=20&sombra=1", headers={"X-API-Key": "clave-de-prueba"}).json()
    chk(r1["ok"] and r1["datos"]["sombra"] is False and r2["ok"] and r2["datos"]["sombra"] is True
        and all(o.get("sombra") for o in r2["datos"]["oportunidades"]),
        "la ruta respeta el parametro sombra")
    r3 = cli.get("/oportunidades?sombra=1").json()
    chk(r3.get("ok") is False, "sin clave sigue rechazando")

    print(f"\n{ok} correctas · {mal} fallas")
    if mal:
        sys.exit(1)
    print("TODO OK")


if __name__ == "__main__":
    main()
