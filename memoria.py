"""
Memoria del motor para ordersblock — SQLite, sin servidor de base de datos.

POR QUÉ SQLITE Y NO LA BASE DEL BOT
-----------------------------------
El bot de Telegram vive en Render con Postgres; ordersblock vive en el
droplet. Conectarlos obligaría a abrir la base del bot a internet o a
montar un túnel, y un fallo en uno tumbaría al otro. Aquí cada sistema
aprende de lo suyo: la memoria de ordersblock arranca vacía y se llena
con sus propios análisis verificados contra el precio real.

Mientras no haya muestra suficiente, el motor se comporta igual que el
bot con historial vacío: calibración prudente, sin sesgo aprendido, sin
inversión. Los umbrales (20 muestras para el sesgo, 30 apuestas propias,
40 para invertir) son los mismos del bot y no se tocan aquí.

Dos tablas, dos ciclos de aprendizaje:
  · senales       -> análisis de trading (24h). Alimenta el sesgo
                     direccional, el piso de R:R adaptativo y la voz
                     del fundamental.
  · predicciones  -> bloques de 5/15/30/60 min. Alimenta la calibración
                     y la memoria correctiva (inversión, asimetría).
"""

import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

RUTA_DB = os.getenv("ORDERSBLOCK_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                    "memoria_ordersblock.db"))

# SQLite admite un solo escritor a la vez; el candado evita "database is
# locked" cuando el worker resuelve mientras la API registra.
_candado = threading.Lock()


def _ahora() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@contextmanager
def _conexion():
    with _candado:
        conn = sqlite3.connect(RUTA_DB, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


def inicializar():
    with _conexion() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("""
            CREATE TABLE IF NOT EXISTS senales (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                usuario TEXT,
                mercado TEXT NOT NULL,
                tipo TEXT DEFAULT 'mercado',          -- mercado | precision
                direccion TEXT NOT NULL,
                precio_entrada REAL NOT NULL,
                sl REAL, tp REAL,
                umbral_pct REAL DEFAULT 0.003,
                fundamental_etiqueta TEXT,
                creada TEXT NOT NULL,
                horizonte_hasta TEXT NOT NULL,
                verificada INTEGER DEFAULT 0,
                acierto INTEGER,
                precio_salida REAL,
                tipo_toque TEXT,                      -- TP | SL | NINGUNO
                mae_r REAL, mfe_r REAL,
                fundamental_acerto INTEGER
            )""")
        c.execute("CREATE INDEX IF NOT EXISTS ix_sen_pend ON senales (verificada, horizonte_hasta)")
        c.execute("CREATE INDEX IF NOT EXISTS ix_sen_mkt ON senales (mercado, direccion, verificada)")
        c.execute("""
            CREATE TABLE IF NOT EXISTS predicciones (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                usuario TEXT,
                mercado TEXT NOT NULL,
                minutos INTEGER NOT NULL,
                strike REAL NOT NULL,
                precio_inicial REAL NOT NULL,
                prob_modelo REAL NOT NULL,             -- prob. de ARRIBA ya calibrada
                prob_cruda REAL,
                factor_calibracion REAL,
                precio_mercado REAL,
                veredicto TEXT,
                creada TEXT NOT NULL,
                vence_en TEXT NOT NULL,
                resuelta INTEGER DEFAULT 0,
                precio_final REAL,
                acerto INTEGER,
                resuelta_en TEXT
            )""")
        c.execute("CREATE INDEX IF NOT EXISTS ix_pre_pend ON predicciones (resuelta, vence_en)")
        c.execute("CREATE INDEX IF NOT EXISTS ix_pre_mkt ON predicciones (mercado, minutos, resuelta)")


# ---------------------------------------------------------------------------
# SEÑALES DE TRADING
# ---------------------------------------------------------------------------

def registrar_senal(mercado: str, direccion: str, precio_entrada: float,
                    sl: float = None, tp: float = None, tipo: str = "mercado",
                    fundamental_etiqueta: str = None, usuario: str = None,
                    horas: int = 24, umbral_pct: float = 0.003) -> int:
    if direccion not in ("ALCISTA", "BAJISTA"):
        return 0   # RANGO u otros no se verifican como operación direccional
    ahora = _ahora()
    with _conexion() as c:
        cur = c.execute(
            """INSERT INTO senales (usuario, mercado, tipo, direccion, precio_entrada,
                                    sl, tp, umbral_pct, fundamental_etiqueta,
                                    creada, horizonte_hasta)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (usuario, mercado, tipo, direccion, precio_entrada, sl, tp, umbral_pct,
             fundamental_etiqueta, ahora.isoformat(),
             (ahora + timedelta(hours=horas)).isoformat()))
        return cur.lastrowid


def senales_por_verificar(limite: int = 50) -> list:
    with _conexion() as c:
        r = c.execute("""SELECT * FROM senales WHERE verificada = 0 AND horizonte_hasta <= ?
                         ORDER BY horizonte_hasta LIMIT ?""", (_ahora().isoformat(), limite))
        return [dict(x) for x in r.fetchall()]


def marcar_senal(senal_id: int, acierto: bool, precio_salida: float, tipo_toque=None,
                 mae_r=None, mfe_r=None, fundamental_acerto=None):
    with _conexion() as c:
        c.execute("""UPDATE senales SET verificada = 1, acierto = ?, precio_salida = ?,
                                       tipo_toque = ?, mae_r = ?, mfe_r = ?,
                                       fundamental_acerto = ?
                     WHERE id = ?""",
                  (1 if acierto else 0, precio_salida, tipo_toque, mae_r, mfe_r,
                   None if fundamental_acerto is None else (1 if fundamental_acerto else 0),
                   senal_id))


def _stats(where: str, params: tuple) -> dict:
    with _conexion() as c:
        r = c.execute(f"""SELECT COUNT(*) AS total, COALESCE(SUM(acierto), 0) AS aciertos
                          FROM senales WHERE verificada = 1 AND acierto IS NOT NULL
                          AND {where}""", params).fetchone()
    total, aciertos = int(r["total"] or 0), int(r["aciertos"] or 0)
    return {"total_verificadas": total, "aciertos": aciertos, "fallos": total - aciertos,
            "tasa_acierto": round(100 * aciertos / total, 1) if total else None}


def estadisticas_mercado(mercado: str) -> dict:
    """Mismo formato que database.estadisticas_mercado_global del bot."""
    return _stats("mercado = ?", (mercado,))


def estadisticas_direccion(mercado: str, direccion: str) -> dict:
    """Mismo formato que database.estadisticas_direccion_global del bot."""
    return _stats("mercado = ? AND direccion = ?", (mercado, direccion))


def efectividad_fundamental(mercado: str) -> dict:
    """Mismo formato que database.efectividad_fundamental del bot."""
    with _conexion() as c:
        r = c.execute("""SELECT COUNT(*) AS total, COALESCE(SUM(fundamental_acerto), 0) AS ac
                         FROM senales WHERE mercado = ? AND fundamental_acerto IS NOT NULL""",
                      (mercado,)).fetchone()
    total, ac = int(r["total"] or 0), int(r["ac"] or 0)
    return {"total_verificadas": total, "aciertos": ac,
            "tasa_acierto": round(100.0 * ac / total, 1) if total else None}


# ---------------------------------------------------------------------------
# PREDICCIONES DE BLOQUE (5/15/30/60 min)
# ---------------------------------------------------------------------------

def registrar_prediccion(mercado: str, minutos: int, strike: float, precio_inicial: float,
                         prob_modelo: float, prob_cruda: float, factor: float,
                         vence_en: str, precio_mercado=None, veredicto: str = "",
                         usuario: str = None) -> int:
    with _conexion() as c:
        cur = c.execute(
            """INSERT INTO predicciones (usuario, mercado, minutos, strike, precio_inicial,
                                         prob_modelo, prob_cruda, factor_calibracion,
                                         precio_mercado, veredicto, creada, vence_en)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (usuario, mercado, minutos, strike, precio_inicial, prob_modelo, prob_cruda,
             factor, precio_mercado, veredicto, _ahora().isoformat(), vence_en))
        return cur.lastrowid


def predicciones_por_vencer(limite: int = 200) -> list:
    with _conexion() as c:
        r = c.execute("""SELECT * FROM predicciones WHERE resuelta = 0 AND vence_en <= ?
                         ORDER BY vence_en LIMIT ?""", (_ahora().isoformat(), limite))
        return [dict(x) for x in r.fetchall()]


def resolver_prediccion(pred_id: int, precio_final: float, acerto: bool):
    with _conexion() as c:
        c.execute("""UPDATE predicciones SET resuelta = 1, precio_final = ?, acerto = ?,
                                             resuelta_en = ? WHERE id = ?""",
                  (precio_final, 1 if acerto else 0, _ahora().isoformat(), pred_id))


def estadisticas_predicciones(mercado: str = None, minutos: int = None,
                              minimo: int = 10) -> dict:
    """Mismo formato que database.apuestas_estadisticas del bot."""
    cond, params = ["resuelta = 1", "acerto IS NOT NULL"], []
    if mercado:
        cond.append("mercado = ?"); params.append(mercado)
    if minutos:
        cond.append("minutos = ?"); params.append(minutos)
    with _conexion() as c:
        r = c.execute(f"""SELECT COUNT(*) AS total, AVG(acerto) * 100 AS acierto,
                                 AVG(prob_modelo) * 100 AS prob_media
                          FROM predicciones WHERE {' AND '.join(cond)}""",
                      tuple(params)).fetchone()
    total = int(r["total"] or 0)
    if total < minimo:
        return {"total": total, "acierto": None, "suficiente": False}
    return {"total": total, "acierto": round(float(r["acierto"] or 0), 1),
            "prob_media_anunciada": round(float(r["prob_media"] or 0), 1),
            "suficiente": True}


def historial_aprendizaje(mercado: str = None, minutos: int = None, limite: int = 200) -> list:
    """Mismo formato que database.apuestas_historial_aprendizaje del bot."""
    cond, params = ["resuelta = 1", "acerto IS NOT NULL"], []
    if mercado:
        cond.append("mercado = ?"); params.append(mercado)
    if minutos:
        cond.append("minutos = ?"); params.append(minutos)
    params.append(limite)
    with _conexion() as c:
        r = c.execute(f"""SELECT prob_modelo, acerto, mercado, minutos, resuelta_en
                          FROM predicciones WHERE {' AND '.join(cond)}
                          ORDER BY resuelta_en DESC LIMIT ?""", tuple(params))
        return [dict(x) for x in r.fetchall()]


def resumen(mercado: str = None) -> dict:
    """Panel de fiabilidad: lo que el motor lleva verificado de verdad."""
    filtro, p = ("AND mercado = ?", (mercado,)) if mercado else ("", ())
    with _conexion() as c:
        s = c.execute(f"""SELECT COUNT(*) n, SUM(verificada) v, SUM(acierto) a,
                                 SUM(CASE WHEN tipo_toque='TP' THEN 1 ELSE 0 END) tp,
                                 SUM(CASE WHEN tipo_toque='SL' THEN 1 ELSE 0 END) sl
                          FROM senales WHERE 1=1 {filtro}""", p).fetchone()
        q = c.execute(f"""SELECT COUNT(*) n, SUM(resuelta) v, SUM(acerto) a
                          FROM predicciones WHERE 1=1 {filtro}""", p).fetchone()
    sv, qv = int(s["v"] or 0), int(q["v"] or 0)
    return {
        "senales": {"emitidas": int(s["n"] or 0), "verificadas": sv,
                    "acierto_direccion_pct": round(100 * (s["a"] or 0) / sv, 1) if sv else None,
                    "tocaron_tp": int(s["tp"] or 0), "tocaron_sl": int(s["sl"] or 0)},
        "predicciones": {"emitidas": int(q["n"] or 0), "resueltas": qv,
                         "acierto_pct": round(100 * (q["a"] or 0) / qv, 1) if qv else None},
    }
