#!/usr/bin/env bash
# 1_codigo_y_dependencias.sh — actualiza el codigo del motor (rama de seguridad) y sus librerias.
#   sudo bash despliegue/seguridad/1_codigo_y_dependencias.sh            # revisar: muestra lo que haria, no cambia nada
#   sudo bash despliegue/seguridad/1_codigo_y_dependencias.sh --aplicar  # aplica con respaldo y vuelve atras solo si algo falla
#   sudo bash despliegue/seguridad/1_codigo_y_dependencias.sh --revertir /root/respaldos-seguridad/<carpeta>
# REF (opcional): rama o commit a desplegar. Por defecto origin/seguridad-2026-10-30.
source "$(dirname "$0")/comun.sh" "$@"
exigir_root
REF=${REF:-origin/seguridad-2026-10-30}

if [ "$MODO" = revertir ]; then
  [ -d "${DIR_REVERTIR:-}" ] || { falla "Indica la carpeta de respaldo"; exit 1; }
  ANT=$(cat "$DIR_REVERTIR/commit")
  info "Volviendo al commit $ANT y a las librerias anteriores"
  G checkout -q "$ANT"
  rm -rf "$APP/venv" && cp -a "$DIR_REVERTIR/venv" "$APP/venv"
  systemctl restart "$SVC"; salud && ok "Revertido" || falla "Revertido pero el motor no responde: revisa journalctl -u $SVC"
  exit 0
fi

# Comprobaciones previas: la nueva version falla cerrado sin clave; sin clave el Worker se quedaria sin motor
[ -f "$APP/.env" ] || { falla "No existe $APP/.env"; exit 1; }
grep -qE "^ORDERSBLOCK_API_KEY=.{20,}" "$APP/.env" || { falla "ORDERSBLOCK_API_KEY no esta en .env: la version nueva responderia 503. Detente."; exit 1; }
PYV=$("$APP/venv/bin/python" -c 'import sys;print("%d.%d"%sys.version_info[:2])')
REQ=requirements.txt; [ "$(printf '%s\n3.11\n' "$PYV" | sort -V | head -1)" = 3.11 ] || REQ=requirements-py310.txt
G fetch -q origin
info "Python del motor: $PYV  → se usara $REQ"
info "Commit actual: $(G rev-parse --short HEAD)   → nuevo: $(G rev-parse --short "$REF")"
G diff --stat HEAD "$REF" | tail -3
info "Librerias que cambian:"; diff <("$APP/venv/bin/pip" freeze | sort) <(G show "$REF:$REQ" | grep -E "==" | sort) || true
[ "$MODO" = revisar ] && { echo; info "Modo revisar: no se cambio nada. Para aplicar: --aplicar"; exit 0; }

# 1) Probar ANTES de tocar produccion: copia temporal del codigo nuevo + venv nuevo + pruebas del motor
PRUEBA=$(mktemp -d /tmp/obseg.XXXX); trap 'rm -rf "$PRUEBA"' EXIT
G worktree add -q --detach "$PRUEBA/codigo" "$REF"
"$APP/venv/bin/python" -m venv "$PRUEBA/venv"
"$PRUEBA/venv/bin/pip" install -q --upgrade pip
"$PRUEBA/venv/bin/pip" install -q -r "$PRUEBA/codigo/$REQ" httpx
for t in "$PRUEBA"/codigo/tests/test_*.py; do
  ( cd "$PRUEBA/codigo" && ORDERSBLOCK_DB="$PRUEBA/t.db" "$PRUEBA/venv/bin/python" -I "$t" ) | tail -1 | grep -q "TODO OK" \
    || { falla "Prueba $(basename "$t") falla con el codigo nuevo. No se toco produccion."; G worktree remove --force "$PRUEBA/codigo"; exit 1; }
done
G worktree remove --force "$PRUEBA/codigo"
ok "Las pruebas del motor pasan con el codigo y las librerias nuevas"

# 2) Respaldo
R=$(nueva_carpeta_respaldo codigo)
G rev-parse HEAD > "$R/commit"
cp -a "$APP/venv" "$R/venv"
"$APP/venv/bin/pip" freeze > "$R/pip-freeze.txt"
ok "Respaldo en $R (commit, venv completo y lista de librerias)"

# 3) Aplicar
MARCA=$(date '+%Y-%m-%d %H:%M:%S')
G checkout -q "$REF"
"$APP/venv/bin/pip" install -q -r "$APP/$REQ"
systemctl restart "$SVC"

# 4) Verificar; si algo falla, volver atras solo
if salud && sleep 5 && sin_errores_desde "$MARCA"; then
  salud_publica || info "Revisa el acceso publico (puede ser cache de Cloudflare); el motor local esta sano"
  ok "Paso 1 aplicado. Para volver atras: sudo bash $0 --revertir $R"
else
  falla "Algo fallo: volviendo atras automaticamente"
  G checkout -q "$(cat "$R/commit")"; rm -rf "$APP/venv" && cp -a "$R/venv" "$APP/venv"
  systemctl restart "$SVC"; salud && ok "Revertido; produccion como antes" || falla "Revisa journalctl -u $SVC"
  exit 1
fi
