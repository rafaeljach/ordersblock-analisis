# ordersblock — Análisis CBS sin IA

El motor cuantitativo del CBS Quantum Predictor (el mismo de @CBStradingbot),
empaquetado como una API independiente para `ordersblock.com` / Orders Wallet.
**Cero IA**: no hay `anthropic`, no hay `claude_engine`, no hay coste por consulta.

## Qué incluye

| Archivo | Qué es |
|---|---|
| `real_engine.py` | Motor completo: Montecarlo GBM+bootstrap, Yang-Zhang, Hurst, tendencia sin EMA, 14 señales de consenso, marea BTC, TSMOM, stops ≥1.5 ATR, aseguramiento +0.8R. Copia del bot con un único cambio: la clave de Twelve Data ya no se exige para cripto. |
| `icc_score.py` | Score SMC ICC Sniper V16.2 (13 puntos, ADX Wilder). |
| `bybit_client.py` | Velas de Bybit (gratis, sin clave). Solo se usan los endpoints públicos. |
| `predicciones.py` | Bloques de 5/15/30/60 min con vencimiento de reloj, calibración −1..+1 y memoria correctiva. |
| `fundamental_engine.py` | Noticias RSS + Reddit + Fear&Greed + funding, todo léxico (sin IA). |
| `memoria.py` | **Nuevo.** Memoria propia en SQLite: registra cada análisis y lo verifica contra el precio real. |
| `servicio.py` | **Nuevo.** Orquesta todo igual que la ruta sin IA del bot, sin Telegram ni créditos. |
| `api.py` | **Nuevo.** API HTTP (Starlette) + worker que verifica cada 2 min. |
| `ejemplo_app/PantallaAnalisis.js` | Pantalla de ejemplo para la app (Expo/React Native), tema oscuro con verde. |
| `despliegue/` | Servicio systemd, `.env` de ejemplo, nginx y Cloudflare Tunnel. |
| `tests/test_sintetico.py` | Pruebas sin red con velas sintéticas. |

## Endpoints

Todas las rutas (menos `/salud`) exigen la cabecera `X-API-Key`.
Opcional: `X-Usuario: <id>` para que el límite por minuto sea por usuario.

```
GET /salud
GET /mercados
GET /analisis/BTC?tipo=mercado            entrada a mercado (siempre entrega plan)
GET /analisis/BTC?tipo=precision          orden límite en zona de valor (o explica por qué no)
GET /analisis/EURUSD?fundamental=0        sin noticias (más rápido)
GET /bloques                              vencimientos de reloj vigentes (13:15, 13:30…)
GET /prediccion/BTC?minutos=15            ARRIBA/ABAJO + % al cierre del bloque
GET /prediccion/BTC?minutos=15&strike=64000&cuota=0.55   con referencia y cuota de Polymarket/Kalshi
GET /memoria?mercado=BTC                  lo verificado hasta ahora (fiabilidad real)
```

Respuesta: `{"ok": true, "datos": {...}}` o `{"ok": false, "error": "..."}`.
El análisis trae `texto` ya redactado en español (sin Markdown) y además todos
los campos sueltos (`plan`, `radar`, `smc`, `fundamental`…) por si la app
quiere su propio diseño.

## Cómo aprende (y por qué arranca "prudente")

La memoria de ordersblock **empieza vacía**: no comparte la base de datos del
bot de Telegram (está en Render; unirlas obligaría a exponerla). Cada análisis
y cada predicción se registran y el worker los verifica al vencer:

- Señales de 24 h → `verificar_resultado` del motor (dirección + si tocó TP o SL primero).
- Bloques → precio al vencimiento vs. referencia.

Con los mismos umbrales del bot (no se tocaron): sesgo direccional desde 20
muestras por dirección, calibración propia desde 30 predicciones, inversión
de lectura desde 40 con acierto ≤42%. Antes de eso la probabilidad se encoge
hacia el 50% — es lo honesto cuando todavía no hay historial.

## Instalación en el droplet

> El droplet es el mismo de ICC FUTUROS. El servicio va con tope de 600 MB de
> RAM, 80% de CPU y un solo worker para no quitarle recursos.

```bash
# 1) Comprobar que el droplet llega a Bybit (Bybit bloquea IPs de EE. UU.)
curl -s "https://api.bybit.com/v5/market/kline?category=linear&symbol=BTCUSDT&interval=60&limit=2" | head -c 200
#    Debe responder "retCode":0. Si da 403, el droplet está en una región bloqueada.

# 2) Usuario y carpeta propios (aislado de ICC FUTUROS y de la billetera)
sudo useradd -r -s /usr/sbin/nologin analisis
sudo mkdir -p /opt/ordersblock_analisis
sudo unzip ordersblock_analisis.zip -d /opt/          # o git clone de tu repo
cd /opt/ordersblock_analisis
sudo python3 -m venv venv
sudo ./venv/bin/pip install -r requirements.txt

# 3) Configuración
sudo cp despliegue/env.ejemplo .env
openssl rand -hex 32                                  # pega el resultado en ORDERSBLOCK_API_KEY
sudo nano .env
sudo chown -R analisis:analisis /opt/ordersblock_analisis && sudo chmod 600 .env

# 4) Probar antes de publicar
sudo -u analisis ./venv/bin/python -m tests.test_sintetico      # debe terminar en "TODO OK"

# 5) Servicio
sudo cp despliegue/ordersblock-analisis.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now ordersblock-analisis
curl -s localhost:8091/salud
curl -s -H "X-API-Key: TU_CLAVE" "localhost:8091/analisis/BTC?fundamental=0" | head -c 400
```

### Publicarlo en `analisis.ordersblock.com`

Elige según cómo sirves hoy ordersblock.com:

- **nginx + Cloudflare (nube naranja)**: copia `despliegue/nginx-analisis.conf` a
  `/etc/nginx/sites-available/`, enlázalo en `sites-enabled`, `sudo nginx -t && sudo systemctl reload nginx`,
  y en Cloudflare crea un registro **A** `analisis` → IP del droplet, con proxy activado.
- **Cloudflare Tunnel**: añade la entrada de `despliegue/cloudflared-ingress.yml`
  y reinicia `cloudflared`. No hace falta abrir puertos.

El puerto 8091 escucha solo en `127.0.0.1`: no queda expuesto directamente.

## Seguridad: dónde va la clave

Una clave metida dentro de la APK **se puede extraer**. Dos opciones:

1. **Recomendada**: la app llama al backend de la billetera y este reenvía a
   `127.0.0.1:8091` con la clave. La clave nunca sale del servidor.
2. **Rápida**: la app llama directo con la clave. El límite por minuto y el
   semáforo de 2 cálculos simultáneos protegen el droplet, pero cualquiera que
   extraiga la clave podría usar el servicio. Rótala si pasa.

## Lo que NO hace (a propósito)

- No ejecuta órdenes ni toca claves de Bybit: solo lee precios públicos.
- No usa IA ni cobra créditos: eso queda en el bot de Telegram.
- No toca ICC MOTOR ni sus conectores.
- La sección de predicciones solo cubre las 8 criptos de alta liquidez
  (BTC, ETH, SOL, XRP, ADA, DOGE, LINK, AVAX), igual que el bot.

## Aviso para Play Store

Google Play pide declarar las funciones financieras. Mostrar análisis no es
custodiar ni operar, pero conviene mantener visible el aviso
"Análisis probabilístico. No es asesoría financiera." que ya trae cada respuesta.
