"""
CBS Quantum Predictor - Cliente Bybit API v5
==============================================
Ejecuta órdenes reales en futuros USDT-perpetual (category="linear").

LÍMITE HONESTO DE ALCANCE: Bybit no ofrece forex real (EURUSD, GBPUSD,
USDCOP) en su API estándar de derivados — eso solo existe en un producto
aparte (Bybit TradFi, vía MetaTrader 5), que es una integración
completamente distinta y no está cubierta aquí. Este cliente cubre
BTC, ETH y XAUUSD (como proxy vía PAXGUSDT, token respaldado por oro
físico — no es el spot XAUUSD exacto de un bróker forex).

Firma: HMAC-SHA256 sobre (timestamp + api_key + recv_window + body/query),
según la especificación oficial: https://bybit-exchange.github.io/docs/v5/guide
"""

import time
import json
import hmac
import hashlib
import logging
import requests
from datetime import datetime, timezone

logger = logging.getLogger("bybit_client")

BASE_URL_MAINNET = "https://api.bybit.com"
BASE_URL_TESTNET = "https://api-testnet.bybit.com"

RECV_WINDOW = "5000"

# Mercados de este bot que SÍ tienen equivalente real en Bybit futuros.
# EURUSD/GBPUSD/USDCOP quedan fuera a propósito (ver límite arriba).
# Perpetuos lineales USDT de Bybit. Solo cripto de alta liquidez: los
# mercados finos tienen spread ancho y la orden límite se ejecuta peor,
# lo que se come la ventaja antes de empezar.
#
# Forex (EURUSD, GBPUSD, USDCOP) y acciones NO están aquí a propósito:
# Bybit no los lista. El bot da señales de esos mercados, pero no puede
# ejecutarlas — y es mejor que la ejecución falle por símbolo ausente
# que enviar una orden a un mercado equivocado.
MERCADO_A_BYBIT_SYMBOL = {
    "BTC": "BTCUSDT",
    "BTCUSD": "BTCUSDT",
    "ETH": "ETHUSDT",
    "ETHUSD": "ETHUSDT",
    "SOL": "SOLUSDT",
    "XRP": "XRPUSDT",
    "ADA": "ADAUSDT",
    "DOGE": "DOGEUSDT",
    "LINK": "LINKUSDT",
    "AVAX": "AVAXUSDT",
    "BNB": "BNBUSDT",
    "LTC": "LTCUSDT",
    "DOT": "DOTUSDT",
    "ATOM": "ATOMUSDT",
    "XAUUSD": "PAXGUSDT",
}


class BybitAPIError(Exception):
    pass


def _generar_firma(api_secret: str, timestamp: str, api_key: str, payload_str: str) -> str:
    """
    Especificación oficial de Bybit v5:
      rawData = timestamp + api_key + recv_window + payload
      signature = HMAC_SHA256(api_secret, rawData) -> hex minúsculas
    """
    raw_data = f"{timestamp}{api_key}{RECV_WINDOW}{payload_str}"
    return hmac.new(
        api_secret.encode("utf-8"), raw_data.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _headers(api_key: str, api_secret: str, payload_str: str) -> dict:
    timestamp = str(int(time.time() * 1000))
    firma = _generar_firma(api_secret, timestamp, api_key, payload_str)
    return {
        "X-BAPI-API-KEY": api_key,
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-RECV-WINDOW": RECV_WINDOW,
        "X-BAPI-SIGN": firma,
        "Content-Type": "application/json",
    }


def _base_url(testnet: bool) -> str:
    return BASE_URL_TESTNET if testnet else BASE_URL_MAINNET


def _get(api_key: str, api_secret: str, testnet: bool, path: str, params: dict) -> dict:
    # Se construye el query string a mano y se usa EXACTAMENTE el mismo
    # string tanto para firmar como para la URL real — si se le pasara un
    # dict aparte a requests, podría reordenar o recodificar los
    # parámetros de forma distinta a como se firmaron, invalidando la firma.
    query_string = "&".join(f"{k}={v}" for k, v in sorted(params.items())) if params else ""
    headers = _headers(api_key, api_secret, query_string)
    url = f"{_base_url(testnet)}{path}"
    if query_string:
        url = f"{url}?{query_string}"
    resp = requests.get(url, headers=headers, timeout=15)
    return _procesar_respuesta(resp)


def _post(api_key: str, api_secret: str, testnet: bool, path: str, body: dict) -> dict:
    body_str = json.dumps(body)
    headers = _headers(api_key, api_secret, body_str)
    url = f"{_base_url(testnet)}{path}"
    resp = requests.post(url, headers=headers, data=body_str, timeout=15)
    return _procesar_respuesta(resp)


def _procesar_respuesta(resp: requests.Response) -> dict:
    try:
        data = resp.json()
    except Exception:
        raise BybitAPIError(f"Respuesta no válida de Bybit (HTTP {resp.status_code}): {resp.text[:200]}")

    if data.get("retCode") != 0:
        raise BybitAPIError(f"Bybit rechazó la solicitud: {data.get('retMsg', 'error desconocido')} "
                             f"(retCode {data.get('retCode')})")
    return data.get("result", {})


# ---------------------------------------------------------------------------
# Endpoints usados
# ---------------------------------------------------------------------------

def verificar_conexion(api_key: str, api_secret: str, testnet: bool) -> dict:
    """Prueba que las credenciales funcionan, trayendo el balance de la cuenta unificada."""
    return _get(api_key, api_secret, testnet, "/v5/account/wallet-balance",
                {"accountType": "UNIFIED"})


def obtener_info_instrumento(symbol: str, testnet: bool) -> dict:
    """Trae el paso mínimo de cantidad (qtyStep) y tamaño mínimo, para redondear la orden correctamente."""
    url = f"{_base_url(testnet)}/v5/market/instruments-info"
    resp = requests.get(url, params={"category": "linear", "symbol": symbol}, timeout=15)
    data = _procesar_respuesta(resp)
    lista = data.get("list", [])
    if not lista:
        raise BybitAPIError(f"No se encontró información del instrumento {symbol} en Bybit.")
    return lista[0]


def colocar_orden(api_key: str, api_secret: str, testnet: bool,
                   symbol: str, direccion: str, qty: str,
                   stop_loss: float = None, take_profit: float = None,
                   apalancamiento: int = 5) -> dict:
    """
    Coloca una orden de MERCADO en futuros lineales (USDT-perpetual) con
    SL/TP adjuntos desde la creación. direccion: "ALCISTA" o "BAJISTA".
    """
    side = "Buy" if direccion == "ALCISTA" else "Sell"

    # Fijar apalancamiento antes de la orden (Bybit lo requiere como paso separado)
    try:
        _post(api_key, api_secret, testnet, "/v5/position/set-leverage", {
            "category": "linear", "symbol": symbol,
            "buyLeverage": str(apalancamiento), "sellLeverage": str(apalancamiento),
        })
    except BybitAPIError as e:
        # Si el apalancamiento ya estaba en ese valor, Bybit devuelve error
        # "leverage not modified" — no es un fallo real, se ignora ese caso.
        if "not modified" not in str(e).lower():
            raise

    body = {
        "category": "linear",
        "symbol": symbol,
        "side": side,
        "orderType": "Market",
        "qty": qty,
    }
    if stop_loss:
        body["stopLoss"] = str(stop_loss)
    if take_profit:
        body["takeProfit"] = str(take_profit)

    return _post(api_key, api_secret, testnet, "/v5/order/create", body)


def crear_orden_limite(api_key: str, api_secret: str, testnet: bool,
                       symbol: str, direccion: str, qty: str, precio_entrada: float,
                       stop_loss: float = None, take_profit: float = None,
                       apalancamiento: int = 5) -> dict:
    """
    Coloca una orden LÍMITE pendiente: no entra al precio actual, espera
    en el nivel indicado. Es la ejecución que corresponde al análisis de
    precisión — entrar a mercado destruiría todo el sentido de haber
    calculado una zona de valor.

    `timeInForce=GTC` (Good Till Cancel): la orden vive hasta que se
    ejecuta o el usuario la cancela. Deliberadamente NO se pone
    caducidad automática en el exchange: si el bot se cayera, una orden
    con vencimiento podría desaparecer sin que el usuario se entere.
    Prefiero que quede visible en su cuenta y que él decida.

    SL y TP van adjuntos desde la creación: si la orden se ejecuta
    mientras el bot está caído, la posición NUNCA queda sin protección.
    """
    side = "Buy" if direccion == "ALCISTA" else "Sell"

    try:
        _post(api_key, api_secret, testnet, "/v5/position/set-leverage", {
            "category": "linear", "symbol": symbol,
            "buyLeverage": str(apalancamiento), "sellLeverage": str(apalancamiento),
        })
    except BybitAPIError as e:
        if "not modified" not in str(e).lower():
            raise

    body = {
        "category": "linear",
        "symbol": symbol,
        "side": side,
        "orderType": "Limit",
        "qty": qty,
        "price": str(precio_entrada),
        "timeInForce": "GTC",
    }
    if stop_loss:
        body["stopLoss"] = str(stop_loss)
    if take_profit:
        body["takeProfit"] = str(take_profit)

    return _post(api_key, api_secret, testnet, "/v5/order/create", body)


def cancelar_orden(api_key: str, api_secret: str, testnet: bool,
                    symbol: str, order_id: str) -> dict:
    """Cancela una orden pendiente (por si el usuario cambia de idea)."""
    return _post(api_key, api_secret, testnet, "/v5/order/cancel", {
        "category": "linear", "symbol": symbol, "orderId": order_id,
    })


# Intervalos de Bybit: la API los expresa en minutos (o D/W/M)
INTERVALO_A_BYBIT = {
    "1min": "1", "5min": "5", "15min": "15", "30min": "30",
    "1h": "60", "2h": "120", "4h": "240", "6h": "360", "12h": "720",
    "1day": "D", "1week": "W",
}


# Candidatos de símbolo por mercado. Bybit nombra distinto según el
# producto: los perpetuos cripto son XXXUSDT, los TradFi Perpetuals de
# acciones y materias primas pueden llevar sufijos, y algunos pares
# existen en spot pero no en derivados. En vez de adivinar cuál es el
# correcto, se prueban todos y se usa el que exista de verdad.
CANDIDATOS_SIMBOLO = {
    "EURUSD": ["EURUSDT", "EURUSD", "EURUSDT-PERP"],
    "GBPUSD": ["GBPUSDT", "GBPUSD", "GBPUSDT-PERP"],
    "USDJPY": ["USDJPYUSDT", "USDJPY"],
    "XAUUSD": ["XAUUSDT", "XAUUSD", "PAXGUSDT", "GOLDUSDT"],
    "XAGUSD": ["XAGUSDT", "XAGUSD", "SILVERUSDT"],
    "USDCOP": ["USDCOPUSDT", "USDCOP"],
    "AAPL": ["AAPLUSDT", "AAPL"],
    "TSLA": ["TSLAUSDT", "TSLA"],
    "NVDA": ["NVDAUSDT", "NVDA"],
    "MSFT": ["MSFTUSDT", "MSFT"],
    "AMZN": ["AMZNUSDT", "AMZN"],
    "GOOGL": ["GOOGLUSDT", "GOOGL"],
    "META": ["METAUSDT", "META"],
    "NFLX": ["NFLXUSDT", "NFLX"],
    "AMD": ["AMDUSDT", "AMD"],
    "COIN": ["COINUSDT", "COIN"],
    "MSTR": ["MSTRUSDT", "MSTR"],
    "SPY": ["SPYUSDT", "SPY"],
    "QQQ": ["QQQUSDT", "QQQ"],
}

# Caché del descubrimiento: se rellena una vez al arrancar
_simbolos_descubiertos: dict = {}


def descubrir_simbolos(mercados: list = None, testnet: bool = False) -> dict:
    """
    Pregunta a Bybit qué símbolos existen REALMENTE y construye el mapeo.

    Se hizo así porque adivinar nombres no funciona: Bybit fue añadiendo
    TradFi Perpetuals con convenciones distintas y algunos pares están en
    spot pero no en derivados. Una sola consulta al catálogo resuelve la
    duda para todos los mercados a la vez, y el bot se adapta solo cuando
    Bybit añade instrumentos nuevos.

    Devuelve {mercado: (symbol, categoria)}.
    """
    encontrados = {}
    disponibles = {}
    for categoria in ("linear", "spot"):
        try:
            base = BASE_URL_TESTNET if testnet else BASE_URL_MAINNET
            r = requests.get(f"{base}/v5/market/instruments-info",
                              params={"category": categoria, "limit": 1000}, timeout=20)
            r.raise_for_status()
            data = r.json()
            if data.get("retCode") != 0:
                continue
            for i in data.get("result", {}).get("list", []) or []:
                if i.get("status") == "Trading":
                    disponibles.setdefault(i["symbol"], categoria)
        except Exception as e:
            logger.warning(f"No se pudo listar instrumentos {categoria}: {e}")

    objetivo = mercados or list(CANDIDATOS_SIMBOLO.keys())
    for mercado in objetivo:
        for cand in CANDIDATOS_SIMBOLO.get(mercado, [f"{mercado}USDT"]):
            if cand in disponibles:
                encontrados[mercado] = (cand, disponibles[cand])
                break
    return encontrados


def refrescar_mapeo_simbolos(testnet: bool = False) -> dict:
    """
    Descubre los símbolos y los añade al mapeo global. Se llama una vez
    al arrancar; si falla, el bot sigue con el mapeo fijo de siempre.
    """
    global _simbolos_descubiertos
    try:
        nuevos = descubrir_simbolos(testnet=testnet)
    except Exception as e:
        logger.warning(f"Descubrimiento de símbolos falló: {e}")
        return {}
    for mercado, (symbol, categoria) in nuevos.items():
        if mercado not in MERCADO_A_BYBIT_SYMBOL:
            MERCADO_A_BYBIT_SYMBOL[mercado] = symbol
            _simbolos_descubiertos[mercado] = (symbol, categoria)
            logger.info(f"Bybit sirve {mercado} como {symbol} ({categoria})")
    return _simbolos_descubiertos


def listar_instrumentos(filtro: str = "", testnet: bool = False) -> list:
    """
    Lista los perpetuos disponibles en Bybit, opcionalmente filtrados.

    Sirve para descubrir qué mercados TradFi Perpetual existen: desde
    abril de 2026 Bybit lista perpetuos USDT que replican acciones, ETFs
    y materias primas, y operan en el MISMO libro que los cripto — es
    decir, salen por esta misma API. El forex, en cambio, vive en el
    lado CFD/MetaTrader 5 y no es accesible por aquí.

    Con esto se comprueba qué hay realmente en vez de suponerlo.
    """
    base = BASE_URL_TESTNET if testnet else BASE_URL_MAINNET
    r = requests.get(f"{base}/v5/market/instruments-info",
                      params={"category": "linear", "limit": 1000}, timeout=20)
    r.raise_for_status()
    data = r.json()
    if data.get("retCode") != 0:
        raise BybitAPIError(f"{data.get('retCode')}: {data.get('retMsg')}")
    lista = data.get("result", {}).get("list", []) or []
    f = (filtro or "").upper()
    return [
        {"symbol": i.get("symbol"), "base": i.get("baseCoin"),
         "estado": i.get("status"),
         "min_qty": i.get("lotSizeFilter", {}).get("minOrderQty"),
         "paso": i.get("lotSizeFilter", {}).get("qtyStep")}
        for i in lista
        if i.get("status") == "Trading" and (not f or f in (i.get("symbol") or ""))
    ]


def obtener_velas(symbol: str, interval: str = "1h", limit: int = 200,
                   testnet: bool = False) -> list:
    """
    Velas (klines) desde Bybit. Gratis, sin API key y sin cuota práctica.

    Por qué esto importa más que ahorrar peticiones: hasta ahora el bot
    ANALIZABA con datos de Twelve Data y EJECUTABA en Bybit. Dos fuentes
    distintas, con desfase entre ellas. Eso costó una operación real: el
    análisis de XRP vio 1.0344 y la orden entró con el mercado ya en
    1.045, con el stop a cuatro segundos de distancia.

    Con velas de Bybit, el precio que se analiza es el precio donde se
    opera. El desfase desaparece.

    Bybit devuelve las velas de MÁS RECIENTE a más antigua; aquí se
    invierten para dejarlas en orden cronológico, que es como las espera
    el motor.
    """
    base = BASE_URL_TESTNET if testnet else BASE_URL_MAINNET
    iv = INTERVALO_A_BYBIT.get(interval, interval)
    # Algunos pares (sobre todo forex y acciones) existen en spot pero no
    # en derivados. Se usa la categoría donde se encontró el símbolo.
    categoria = "linear"
    for m, (s, cat) in _simbolos_descubiertos.items():
        if s == symbol:
            categoria = cat
            break
    r = requests.get(f"{base}/v5/market/kline",
                      params={"category": categoria, "symbol": symbol,
                              "interval": iv, "limit": min(limit, 1000)},
                      timeout=15)
    # Si la categoría no era la correcta, se prueba la otra antes de
    # rendirse: es preferible una petición extra a caer en Twelve Data.
    if r.status_code == 200 and r.json().get("retCode") != 0 and categoria == "linear":
        r = requests.get(f"{base}/v5/market/kline",
                          params={"category": "spot", "symbol": symbol,
                                  "interval": iv, "limit": min(limit, 1000)},
                          timeout=15)
    r.raise_for_status()
    data = r.json()
    if data.get("retCode") != 0:
        raise BybitAPIError(f"{data.get('retCode')}: {data.get('retMsg')}")

    filas = data.get("result", {}).get("list", []) or []
    if not filas:
        raise BybitAPIError(f"Bybit no devolvió velas para {symbol}")

    # [startTime, open, high, low, close, volume, turnover], más nueva primero
    velas = []
    for f in reversed(filas):
        try:
            ts = int(f[0]) / 1000
            velas.append({
                "datetime": datetime.fromtimestamp(ts, tz=timezone.utc)
                            .strftime("%Y-%m-%d %H:%M:%S"),
                "open": float(f[1]), "high": float(f[2]),
                "low": float(f[3]), "close": float(f[4]),
                "volume": float(f[5]),
            })
        except (ValueError, IndexError, TypeError):
            continue
    return velas


def precio_actual(symbol: str, testnet: bool = False) -> float:
    """
    Precio de mercado REAL en Bybit, ahora mismo.

    Existe porque el análisis usa velas de Twelve Data —que llegan con
    minutos de retraso y desde otra fuente— mientras la orden se ejecuta
    en Bybit. Ese desfase provocó una pérdida real: el motor calculó una
    corta en XRP a 1.0344 con stop en 1.0449, pero cuando la orden llegó
    al mercado el precio ya estaba en 1.045. El stop saltó a los cuatro
    segundos. La operación estaba perdida antes de empezar.

    Comprobar el precio en la MISMA fuente donde se va a ejecutar, justo
    antes de enviar la orden, es lo que evita repetirlo.
    """
    base = BASE_URL_TESTNET if testnet else BASE_URL_MAINNET
    r = requests.get(f"{base}/v5/market/tickers",
                      params={"category": "linear", "symbol": symbol}, timeout=10)
    r.raise_for_status()
    data = r.json()
    if data.get("retCode") != 0:
        raise BybitAPIError(f"{data.get('retCode')}: {data.get('retMsg')}")
    lista = data.get("result", {}).get("list", [])
    if not lista:
        raise BybitAPIError(f"sin datos de precio para {symbol}")
    return float(lista[0]["lastPrice"])


def calcular_qty(margen_usd: float, apalancamiento: int, precio_entrada: float, qty_step: float) -> str:
    """Calcula la cantidad del contrato, redondeada al paso mínimo real del instrumento."""
    qty_bruta = (margen_usd * apalancamiento) / precio_entrada
    pasos = int(qty_bruta / qty_step)
    qty_final = pasos * qty_step
    decimales = max(0, str(qty_step)[::-1].find("."))
    return f"{qty_final:.{decimales}f}"
