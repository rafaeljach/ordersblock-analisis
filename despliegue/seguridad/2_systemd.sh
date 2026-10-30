#!/usr/bin/env bash
# 2_systemd.sh — protecciones de systemd para el motor (drop-in, no edita la unidad original)
#   sudo bash despliegue/seguridad/2_systemd.sh [--aplicar | --revertir <carpeta>]
source "$(dirname "$0")/comun.sh" "$@"
exigir_root
DROP=/etc/systemd/system/$SVC.service.d
NUEVO="$(dirname "$0")/10-seguridad.conf"

if [ "$MODO" = revertir ]; then
  rm -f "$DROP/10-seguridad.conf"; [ -f "${DIR_REVERTIR:-}/10-seguridad.conf.antes" ] && cp -a "$DIR_REVERTIR/10-seguridad.conf.antes" "$DROP/10-seguridad.conf"
  [ -f "${DIR_REVERTIR:-}/env.permisos" ] && chmod "$(cat "$DIR_REVERTIR/env.permisos")" "$APP/.env"
  systemctl daemon-reload; systemctl restart "$SVC"; salud && ok "Revertido" ; exit 0
fi

info "Exposicion actual (menor es mejor): $(systemd-analyze security "$SVC" 2>/dev/null | tail -1)"
info "Permisos actuales de .env: $(stat -c '%a %U:%G' "$APP/.env")  → se dejara en 600 analisis:analisis"
info "Se instalara: $DROP/10-seguridad.conf"; sed -n '/^\[Service\]/,$p' "$NUEVO" | grep -v "^#"
[ "$MODO" = revisar ] && { info "Modo revisar: no se cambio nada."; exit 0; }

R=$(nueva_carpeta_respaldo systemd)
[ -f "$DROP/10-seguridad.conf" ] && cp -a "$DROP/10-seguridad.conf" "$R/10-seguridad.conf.antes"
stat -c '%a' "$APP/.env" > "$R/env.permisos"
systemctl cat "$SVC" > "$R/unidad-efectiva-antes.txt" || true
MARCA=$(date '+%Y-%m-%d %H:%M:%S')
mkdir -p "$DROP"; install -m 644 "$NUEVO" "$DROP/10-seguridad.conf"
chown analisis:analisis "$APP/.env"; chmod 600 "$APP/.env"
systemd-analyze verify "/etc/systemd/system/$SVC.service" 2>&1 | grep -v "^$" | head -5 || true
systemctl daemon-reload; systemctl restart "$SVC"

if salud && sleep 5 && sin_errores_desde "$MARCA" && \
   [ "$(curl -s -D - -o /dev/null http://127.0.0.1:$PUERTO/salud | grep -ci '^server: uvicorn')" = 0 ]; then
  ok "Exposicion ahora: $(systemd-analyze security "$SVC" 2>/dev/null | tail -1)"
  ok "Paso 2 aplicado. Para volver atras: sudo bash $0 --revertir $R"
else
  falla "Algo fallo: quitando el drop-in"
  rm -f "$DROP/10-seguridad.conf"; [ -f "$R/10-seguridad.conf.antes" ] && cp -a "$R/10-seguridad.conf.antes" "$DROP/10-seguridad.conf"
  chmod "$(cat "$R/env.permisos")" "$APP/.env"
  systemctl daemon-reload; systemctl restart "$SVC"; salud && ok "Revertido; produccion como antes"; exit 1
fi
