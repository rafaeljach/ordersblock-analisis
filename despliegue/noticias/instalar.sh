#!/usr/bin/env bash
# instalar.sh — servicio de noticias de ordersblock (lectura de fuentes + traduccion local con Argos).
# Requiere el codigo nuevo del motor (noticias.py y traductor.py) ya aplicado con el paso 1 del kit:
#   REF=origin/main bash despliegue/seguridad/1_codigo_y_dependencias.sh --aplicar
#
#   sudo bash despliegue/noticias/instalar.sh             # revisar: comprueba requisitos, no cambia nada
#   sudo bash despliegue/noticias/instalar.sh --probar    # baja los modelos y traduce titulares de prueba; lee cada
#                                                         # fuente una vez (no arranca nada, no toca servicios)
#   sudo bash despliegue/noticias/instalar.sh --aplicar   # instala y arranca el servicio; si no funciona, lo quita solo
#   sudo bash despliegue/noticias/instalar.sh --revertir  # para y quita el servicio (la base y los modelos se quedan)
source "$(dirname "$0")/../seguridad/comun.sh" "$@"
exigir_root
AQUI="$(cd "$(dirname "$0")" && pwd)"
UNIDAD=/etc/systemd/system/ordersblock-noticias.service
DUENO=$(stat -c %U "$APP")
COMO(){ runuser -u "$DUENO" -- env HOME=/tmp "$@"; }   # todo lo del motor se ejecuta como su usuario, nunca como root

quitar(){ systemctl disable --now ordersblock-noticias 2>/dev/null || true; rm -f "$UNIDAD"; systemctl daemon-reload; }

if [ "$MODO" = revertir ]; then
  quitar; ok "Servicio de noticias quitado. El motor sigue igual (/noticias responde una lista vacia)."
  exit 0
fi

[ -f "$APP/noticias.py" ] && [ -f "$APP/traductor.py" ] || { falla "Falta el codigo nuevo en $APP. Primero: REF=origin/main bash $AQUI/../seguridad/1_codigo_y_dependencias.sh --aplicar"; exit 1; }
COMO "$APP/venv/bin/python" -c "import ctranslate2, sentencepiece" 2>/dev/null || { falla "Faltan ctranslate2/sentencepiece en el venv (paso 1 sin aplicar)"; exit 1; }
ok "Codigo y librerias del traductor presentes"
info "Memoria libre del droplet: $(free -m | awk '/^Mem:/{print $7}') MB disponibles"

if [ "$MODO" = revisar ]; then
  info "Se instalara $UNIDAD (tope 700 MB de memoria, 50% de CPU, prioridad baja):"; grep -E "^(ExecStart|Memory|CPUQuota|Nice)" "$AQUI/ordersblock-noticias.service"
  info "Modo revisar: no se cambio nada. Siguiente: --probar"
  exit 0
fi

if [ "$MODO" = probar ]; then
  info "Bajando modelos de traduccion (indice oficial de Argos) a $APP/modelos ..."
  COMO "$APP/venv/bin/python" "$APP/traductor.py" instalar || { falla "No se pudieron bajar los modelos"; exit 1; }
  info "Traduciendo titulares de ejemplo:"
  ( cd "$APP" && COMO env OMP_NUM_THREADS=1 "$APP/venv/bin/python" traductor.py probar ) || { falla "La traduccion de prueba fallo"; exit 1; }
  info "Leyendo cada fuente una vez (no se guarda nada):"
  ( cd "$APP" && COMO "$APP/venv/bin/python" noticias.py probar-fuentes ) || true
  ok "Prueba terminada. Si las traducciones se ven bien: --aplicar"
  exit 0
fi

# --aplicar
for p in en_es en_pt es_en es_pt; do [ -d "$APP/modelos/$p/model" ] || { falla "Falta el modelo $p: corre primero --probar"; exit 1; }; done
R=$(nueva_carpeta_respaldo noticias); [ -f "$UNIDAD" ] && cp -a "$UNIDAD" "$R/"
MARCA=$(date '+%Y-%m-%d %H:%M:%S')
install -m 644 "$AQUI/ordersblock-noticias.service" "$UNIDAD"
systemctl daemon-reload; systemctl enable --now ordersblock-noticias
info "Esperando los primeros titulares (hasta 3 minutos)..."
N=0
for _ in $(seq 1 36); do
  N=$(curl -s -m 5 "http://127.0.0.1:$PUERTO/noticias?n=150" | python3 -c "import sys,json;print(len(json.load(sys.stdin)['datos']['noticias']))" 2>/dev/null || echo 0)
  [ "${N:-0}" -ge 5 ] && break; sleep 5
done
VIVO=$(systemctl is-active ordersblock-noticias || true)
MEM=$(( $(systemctl show -p MemoryCurrent --value ordersblock-noticias 2>/dev/null | grep -E '^[0-9]+$' || echo 0) / 1048576 ))
if [ "$VIVO" = active ] && [ "${N:-0}" -ge 5 ] && salud 5; then
  ok "Servicio de noticias activo: $N titulares ya disponibles en /noticias, usando $MEM MB de memoria"
  ok "Para quitarlo: sudo bash $0 --revertir   ·   Registros: journalctl --namespace=ordersblock -u ordersblock-noticias"
else
  falla "No arranco bien (estado $VIVO, $N titulares): se quita el servicio"
  journalctl --namespace=ordersblock -u ordersblock-noticias --since "$MARCA" --no-pager 2>/dev/null | tail -15 || journalctl -u ordersblock-noticias --since "$MARCA" --no-pager | tail -15
  quitar; salud 5 && ok "El motor sigue sano"; exit 1
fi
