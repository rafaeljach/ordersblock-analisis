#!/usr/bin/env bash
# 4_tunel.sh — pasa analisis.ordersblock.com a Cloudflare Tunnel y cierra la entrada por el puerto 80.
# Requiere el paso 3 aplicado (usa su zona de limite) y cloudflared instalado con un tunel activo.
#   sudo bash despliegue/seguridad/4_tunel.sh              # revisar: comprueba requisitos, no toca nada
#   sudo bash despliegue/seguridad/4_tunel.sh --aplicar    # FASE A: anade la entrada local 127.0.0.1:8092 (aditivo)
#        → en Cloudflare Zero Trust > Networks > Tunnels > tu tunel > Public hostname:
#          analisis.ordersblock.com  →  HTTP  127.0.0.1:8092
#   sudo bash despliegue/seguridad/4_tunel.sh --cerrar80   # FASE B: confirma que el trafico llega por el tunel y
#                                                          #         quita el sitio del puerto 80 de analisis
#   sudo bash despliegue/seguridad/4_tunel.sh --revertir <carpeta>
# No cambia el cortafuegos: si nada mas usa el puerto 80, el script lo indica y da la orden de ufw por separado.
source "$(dirname "$0")/comun.sh" "$@"
exigir_root
AQUI="$(cd "$(dirname "$0")" && pwd)"
TUN=/etc/nginx/sites-available/analisis-tunel

if [ "$MODO" = revertir ]; then
  [ -f "${DIR_REVERTIR:-}/nginx.tar.gz" ] || { falla "Indica la carpeta de respaldo (contiene nginx.tar.gz)"; exit 1; }
  rm -rf /etc/nginx && tar -xzf "$DIR_REVERTIR/nginx.tar.gz" -C /etc
  nginx -t && systemctl reload nginx && ok "nginx restaurado" || falla "Revisa nginx -t"
  sleep 2; salud_publica || info "Si ya habias movido $DOMINIO al tunel, en Zero Trust quita el Public hostname y vuelve a crear el registro A (proxy naranja) con la IP del droplet."
  exit 0
fi

# Requisitos
[ -f /etc/nginx/conf.d/00-ordersblock-seguridad.conf ] || { falla "Primero aplica el paso 3 (3_nginx.sh)"; exit 1; }
command -v cloudflared >/dev/null || { falla "cloudflared no esta instalado. Ver informe: instalacion desde el repositorio oficial de Cloudflare."; exit 1; }
V=$(cloudflared --version 2>&1 | grep -oE "[0-9]{4}\.[0-9]+\.[0-9]+" | head -1)
info "cloudflared $V"; [ "$(printf '%s\n2026.10.2\n' "$V" | sort -V | head -1)" = 2026.10.2 ] || falla "Actualiza cloudflared (minimo 2026.10.2): apt update && apt install --only-upgrade cloudflared"
systemctl is-active --quiet cloudflared || { falla "El servicio cloudflared no esta activo"; exit 1; }
ok "cloudflared activo"

marcar(){ # pide el dominio publico con una marca unica y mira por que puerta entro
  local m; m="t$(date +%s%N)"; curl -s -o /dev/null -m 15 "https://$DOMINIO/salud?via=$m" || true; sleep 2
  grep -q "via=$m" /var/log/nginx/analisis-tunel.access.log 2>/dev/null
}

if [ "$MODO" = revisar ]; then
  info "Fase A instalara $TUN (escucha solo en 127.0.0.1:8092):"; sed -n '/server {/,/^}/p' "$AQUI/nginx-analisis-tunel.conf" | grep -vE "^\s*(#|$)" | head -12
  info "Otros sitios que escuchan en el puerto 80 (si hay, el puerto 80 seguira abierto para ellos):"
  nginx -T 2>/dev/null | awk '/^# configuration file/{f=$4} /listen[^;]*80[; ]/{print "   " f": "$0}' | grep -v "analisis" || echo "   ninguno aparte de analisis"
  info "Modo revisar: no se cambio nada."; exit 0
fi

[ "$MODO" = cerrar80 ] && [ ! -f "$TUN" ] && { falla "Primero la fase A (--aplicar)"; exit 1; }
R=$(nueva_carpeta_respaldo "tunel-$MODO")
tar -czf "$R/nginx.tar.gz" -C /etc nginx; ok "Respaldo de /etc/nginx en $R"
volver(){ falla "$1: restaurando nginx"; rm -rf /etc/nginx && tar -xzf "$R/nginx.tar.gz" -C /etc; nginx -t && systemctl reload nginx && sleep 2 && ok "nginx como antes"; exit 1; }

if [ "$MODO" = aplicar ]; then
  install -m 644 "$AQUI/nginx-analisis-tunel.conf" "$TUN"; ln -sf "$TUN" /etc/nginx/sites-enabled/analisis-tunel
  nginx -t >/dev/null 2>&1 || volver "nginx -t rechaza la configuracion"; systemctl reload nginx; sleep 1
  C=$(curl -s -o /dev/null -w '%{http_code}' -m 8 -H "Host: $DOMINIO" -H "CF-Connecting-IP: 203.0.113.9" http://127.0.0.1:8092/salud || true)
  [ "$C" = 200 ] || volver "La entrada local 127.0.0.1:8092 responde $C"
  ss -tlnH | awk '{print $4}' | grep -q "^127.0.0.1:8092$" || volver "8092 no quedo solo en 127.0.0.1"
  ok "Fase A lista: 127.0.0.1:8092 responde y no es visible desde Internet. El puerto 80 sigue igual."
  info "Ahora en Cloudflare Zero Trust: Networks > Tunnels > (tu tunel) > Public hostname > Add:"
  info "   Subdomain: analisis   Domain: ordersblock.com   Type: HTTP   URL: 127.0.0.1:8092"
  info "   (si avisa que ya existe un registro DNS, borra antes el registro A de analisis: 1-2 minutos sin servicio)"
  info "Despues: sudo bash $0 --cerrar80      Para deshacer la fase A: sudo bash $0 --revertir $R"
  exit 0
fi

# FASE B (--cerrar80)
marcar || { falla "El dominio todavia NO entra por el tunel. Revisa el Public hostname en Zero Trust. No se cambio nada."; exit 1; }
ok "Confirmado: https://$DOMINIO entra por el tunel"
P80=$(grep -lR "listen[^;]*\b80\b" /etc/nginx/sites-enabled/ 2>/dev/null | xargs -r grep -l "server_name[^;]*analisis\.ordersblock\.com" || true)
for f in $P80; do info "Desactivando $f (sitio de analisis en el puerto 80)"; rm -f "$f"; done
nginx -t >/dev/null 2>&1 || volver "nginx -t rechaza la configuracion"; systemctl reload nginx; sleep 2
salud 5 || volver "El motor no responde en local"
salud_publica || volver "El dominio dejo de responder"
marcar || volver "El dominio ya no entra por el tunel"
ok "Paso 4 aplicado: analisis solo entra por Cloudflare Tunnel. Para volver atras: sudo bash $0 --revertir $R"
if ss -tlnH | awk '{print $4}' | grep -qE "(^|:)80$"; then
  info "nginx sigue escuchando en el puerto 80 para otros sitios: no se cierra en el cortafuegos."
else
  info "Ya nada escucha en el puerto 80. Opcional, cortafuegos (primero SSH para no quedarte fuera):"
  info "   ufw allow OpenSSH && ufw deny 80/tcp && ufw status    (ufw enable solo si estaba inactivo y tras allow OpenSSH)"
fi
info "Con el tunel ya puedes poner SSL/TLS en Full (strict) en Cloudflare (ver informe: revisa antes los demas subdominios)."
