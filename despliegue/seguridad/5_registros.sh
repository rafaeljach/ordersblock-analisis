#!/usr/bin/env bash
# 5_registros.sh — registros del motor en un journal propio: 30 dias / 200 MB como maximo.
# El filtro que borra claves de los registros llega con el paso 1 (codigo nuevo). Este paso pone la rotacion.
#   sudo bash despliegue/seguridad/5_registros.sh [--aplicar | --revertir <carpeta>]
# No cambia el journal del sistema ni los de billetera o icc. Los registros viejos del motor quedan en el
# journal del sistema y se borran solos con su rotacion normal (ver informe: rotar la clave de Twelve Data).
source "$(dirname "$0")/comun.sh" "$@"
exigir_root
AQUI="$(cd "$(dirname "$0")" && pwd)"
DROP=/etc/systemd/system/$SVC.service.d

if [ "$MODO" = revertir ]; then
  rm -f "$DROP/20-registros.conf" /etc/systemd/journald@ordersblock.conf
  systemctl daemon-reload; systemctl restart "$SVC"; salud && ok "Revertido: el motor vuelve a escribir en el journal del sistema"; exit 0
fi

info "systemd $(systemctl --version | head -1 | awk '{print $2}') (LogNamespace requiere 245 o mas)"
info "Se instalara $DROP/20-registros.conf y /etc/systemd/journald@ordersblock.conf:"; grep -vE "^#|^$" "$AQUI/journald@ordersblock.conf"
info "Rotacion de nginx: $( [ -f /etc/logrotate.d/nginx ] && grep -oE "daily|weekly|rotate [0-9]+" /etc/logrotate.d/nginx | tr "\n" " " || echo 'NO hay /etc/logrotate.d/nginx')"
[ "$MODO" = revisar ] && { info "Modo revisar: no se cambio nada."; exit 0; }

R=$(nueva_carpeta_respaldo registros); systemctl cat "$SVC" > "$R/unidad-efectiva-antes.txt" || true
MARCA=$(date '+%Y-%m-%d %H:%M:%S')
install -m 644 "$AQUI/journald@ordersblock.conf" /etc/systemd/journald@ordersblock.conf
mkdir -p "$DROP"; install -m 644 "$AQUI/20-registros.conf" "$DROP/20-registros.conf"
systemctl daemon-reload; systemctl restart "$SVC"
if salud && sleep 3 && [ "$(journalctl --namespace=ordersblock -u "$SVC" --since "$MARCA" --no-pager 2>/dev/null | grep -c .)" -gt 1 ] && sin_errores_desde "$MARCA"; then
  ok "El motor escribe en su journal propio: journalctl --namespace=ordersblock -u $SVC"
  ok "Paso 5 aplicado. Para volver atras: sudo bash $0 --revertir $R"
else
  falla "Algo fallo: quitando el journal propio"
  rm -f "$DROP/20-registros.conf" /etc/systemd/journald@ordersblock.conf
  systemctl daemon-reload; systemctl restart "$SVC"; salud && ok "Revertido; produccion como antes"; exit 1
fi
