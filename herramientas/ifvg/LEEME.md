# Patron AMD + IFVG (en estudio, NO integrado al motor)

Acumulacion (rango) -> manipulacion (barrido con FVG) -> inversion (cierre al otro lado del FVG) -> entrada en el retest del FVG invertido.

Solo se integra al analizador si el backtest sobre velas reales supera el 80% de acierto con muestra suficiente (>= 30 senales).

    cd /opt/ordersblock_analisis/herramientas/ifvg
    node aud_ifvg_unit.mjs                     # pruebas con velas sinteticas
    node backtest_ifvg.mjs 180 BTCUSDT,ETHUSDT,SOLUSDT,XAUUSDT   # 6 meses de velas de 15 min de Bybit (publico)

No usa claves, no escribe en la base, no toca el servicio. Necesita node 18+ (fetch nativo).
