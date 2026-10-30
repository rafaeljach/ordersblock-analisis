#!/usr/bin/env bash
# shellcheck disable=SC2034  # MODO, DIR_REVERTIR... los usan los scripts que cargan este archivo
# comun.sh — funciones compartidas por los pasos de seguridad. No se ejecuta solo.
# Regla del proyecto: procesos solo por PID o por systemctl, nunca por patron (pkill/pgrep con texto).
# No toca los usuarios billetera ni icc, ni sus servicios.
set -euo pipefail

APP=/opt/ordersblock_analisis
SVC=ordersblock-analisis
PUERTO=8091
DOMINIO=analisis.ordersblock.com
RESPALDOS=/root/respaldos-seguridad
FECHA=$(date +%Y%m%d-%H%M%S)

MODO=revisar
case "${1:-}" in
  --aplicar) MODO=aplicar ;;
  --revertir) MODO=revertir; DIR_REVERTIR="${2:-}" ;;
  --cerrar80) MODO=cerrar80 ;;
  ""|--revisar) MODO=revisar ;;
  *) echo "Uso: $0 [--revisar | --aplicar | --revertir <carpeta_de_respaldo>] (4_tunel.sh admite ademas --cerrar80)"; exit 2 ;;
esac

info(){ echo "· $*"; }
ok(){ echo "✓ $*"; }
falla(){ echo "✗ $*" >&2; }

exigir_root(){ [ "$(id -u)" -eq 0 ] || { falla "Ejecuta con sudo."; exit 1; }; }

nueva_carpeta_respaldo(){ # $1 = nombre del paso
  local d="$RESPALDOS/$FECHA-$1"; mkdir -p "$d"; chmod 700 "$RESPALDOS" "$d"; echo "$d"
}

# git como root sobre un repo de otro usuario: se autoriza solo esta carpeta y solo en este comando
G(){ git -c safe.directory="$APP" -c safe.directory="$APP/.git" -C "$APP" "$@"; }

clave_api(){ grep -E "^ORDERSBLOCK_API_KEY=" "$APP/.env" | head -1 | cut -d= -f2- | tr -d '"'"'"' '; }

# Salud del motor: local y, si se pide, a traves de Cloudflare. Devuelve 0 si todo responde.
salud(){
  local intentos=${1:-20} clave; clave=$(clave_api)
  for i in $(seq 1 "$intentos"); do
    if curl -fsS -m 5 "http://127.0.0.1:$PUERTO/salud" >/dev/null 2>&1; then break; fi
    sleep 1
    [ "$i" -eq "$intentos" ] && { falla "/salud local no responde"; return 1; }
  done
  curl -fsS -m 10 "http://127.0.0.1:$PUERTO/precios" >/dev/null || { falla "/precios local falla"; return 1; }
  [ "$(curl -s -o /dev/null -w '%{http_code}' -m 10 -H "X-API-Key: $clave" "http://127.0.0.1:$PUERTO/memoria")" = 200 ] || { falla "/memoria con clave no da 200"; return 1; }
  [ "$(curl -s -o /dev/null -w '%{http_code}' -m 10 "http://127.0.0.1:$PUERTO/memoria")" = 401 ] || { falla "/memoria sin clave no da 401"; return 1; }
  ok "Motor sano en local (salud, precios, clave exigida)"
}
salud_publica(){
  local c; c=$(curl -s -o /dev/null -w '%{http_code}' -m 15 "https://$DOMINIO/salud" || true)
  [ "$c" = 200 ] && ok "https://$DOMINIO/salud responde 200 a traves de Cloudflare" || { falla "https://$DOMINIO/salud responde $c"; return 1; }
}
# Si el paso 5 esta aplicado, el motor escribe en su propio journal (espacio "ordersblock")
J(){ if [ -f "/etc/systemd/system/$SVC.service.d/20-registros.conf" ]; then journalctl --namespace=ordersblock "$@"; else journalctl "$@"; fi; }
sin_errores_desde(){ # $1 = marca de tiempo 'YYYY-MM-DD HH:MM:SS'
  local n; n=$(J -u "$SVC" --since "$1" --no-pager 2>/dev/null | grep -cE "Traceback|CRITICAL|ModuleNotFoundError|ImportError" || true)
  [ "$n" = 0 ] && ok "Sin errores graves en el registro desde el cambio" || { falla "$n errores graves en el registro desde el cambio"; J -u "$SVC" --since "$1" --no-pager | grep -E "Traceback|CRITICAL|Error" | tail -5; return 1; }
}
