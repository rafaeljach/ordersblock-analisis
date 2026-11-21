"""
Velas del perpetuo USDT de criptos que el motor no tiene en su lista (hoy: XMR).

Por que existe (21 nov 2026): Rafael opera XMR en el perpetuo de Binance. El Worker de ordersblock.com no llega ni a
Binance ni a Bybit (le responden 403 por pais desde su centro de datos), asi que XMR se verificaba solo con Kraken spot.
El droplet si llega a Bybit; aqui se prueba primero el perpetuo de Binance (USDT-M) y, si no responde, el de Bybit.
La respuesta dice siempre de donde salieron las velas y por que se descarto la otra fuente.
"""
import logging
import time

import requests

logger = logging.getLogger(__name__)

# base -> simbolo del perpetuo USDT (el mismo nombre en Binance y en Bybit)
PERPETUOS = {"XMR": "XMRUSDT"}

BINANCE_FAPI = "https://fapi.binance.com/fapi/v1/klines"
BYBIT_KLINE = "https://api.bybit.com/v5/market/kline"
_IV_BINANCE = {"15": "15m", "60": "1h", "240": "4h", "1440": "1d"}
_IV_BYBIT = {"15": "15", "60": "60", "240": "240", "1440": "D"}


def base_de(mercado: str) -> str:
    """XMRUSD, XMRUSDT, XMR -> XMR si es un perpetuo conocido; si no, cadena vacia."""
    m = str(mercado or "").upper().strip()
    for suf in ("USDT", "USD", ""):
        if suf and m.endswith(suf):
            b = m[: -len(suf)]
        elif not suf:
            b = m
        else:
            continue
        if b in PERPETUOS:
            return b
    return ""


def _desde_binance(simbolo: str, tf: str, n: int, get) -> list:
    r = get(BINANCE_FAPI, params={"symbol": simbolo, "interval": _IV_BINANCE[tf], "limit": n}, timeout=12)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    filas = r.json()
    if not isinstance(filas, list) or not filas:
        raise RuntimeError("0 velas")
    # [openTime, open, high, low, close, volume, ...] de la mas vieja a la mas nueva
    return [{"t": int(f[0]), "o": float(f[1]), "h": float(f[2]), "l": float(f[3]), "c": float(f[4]), "v": float(f[5])} for f in filas]


def _desde_bybit(simbolo: str, tf: str, n: int, get) -> list:
    r = get(BYBIT_KLINE, params={"category": "linear", "symbol": simbolo, "interval": _IV_BYBIT[tf], "limit": n}, timeout=12)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    d = r.json()
    if d.get("retCode") != 0:
        raise RuntimeError(f"retCode {d.get('retCode')}: {d.get('retMsg')}")
    filas = (d.get("result") or {}).get("list") or []
    if not filas:
        raise RuntimeError("0 velas")
    # Bybit las da de la mas nueva a la mas vieja
    return [{"t": int(f[0]), "o": float(f[1]), "h": float(f[2]), "l": float(f[3]), "c": float(f[4]), "v": float(f[5])} for f in reversed(filas)]


def velas(base: str, tf: str, n: int, get=None) -> dict:
    """Velas del perpetuo: Binance primero, Bybit si Binance no responde. Lanza ValueError si ninguna sirve."""
    get = get or requests.get
    simbolo = PERPETUOS[base]
    intentos = []
    for nombre, fn in (("binance", _desde_binance), ("bybit", _desde_bybit)):
        try:
            v = fn(simbolo, tf, n, get)
            return {"mercado": base, "simbolo": simbolo, "tf": int(tf), "velas": v,
                    "precio_actual": v[-1]["c"], "fuente": nombre + "-perp", "intentos": intentos,
                    "actualizado_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        except Exception as e:  # se anota y se prueba la siguiente
            motivo = f"{nombre}: {type(e).__name__}: {e}"[:160]
            intentos.append(motivo)
            logger.warning(f"perpetuo {simbolo} {motivo}")
    raise ValueError(" | ".join(intentos))
