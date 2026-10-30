#!/usr/bin/env bash
# 3_nginx.sh — nginx endurecido para analisis.ordersblock.com: solo Cloudflare entra por el puerto 80,
# cabeceras de seguridad, sin version de nginx, peticiones pequenas y limite de 10/s por IP real.
#   sudo bash despliegue/seguridad/3_nginx.sh                     # revisar: muestra el cambio, no toca nada
#   sudo bash despliegue/seguridad/3_nginx.sh --aplicar           # respaldo de /etc/nginx, prueba, recarga y verifica
#   sudo bash despliegue/seguridad/3_nginx.sh --revertir <carpeta>
# Solo cambia el sitio de analisis.ordersblock.com y anade un archivo en conf.d. No toca otros sitios.
source "$(dirname "$0")/comun.sh" "$@"
exigir_root
AQUI="$(cd "$(dirname "$0")" && pwd)"
GLOBAL=/etc/nginx/conf.d/00-ordersblock-seguridad.conf

if [ "$MODO" = revertir ]; then
  [ -f "${DIR_REVERTIR:-}/nginx.tar.gz" ] || { falla "Indica la carpeta de respaldo (contiene nginx.tar.gz)"; exit 1; }
  rm -rf /etc/nginx && tar -xzf "$DIR_REVERTIR/nginx.tar.gz" -C /etc
  nginx -t && systemctl reload nginx && ok "nginx restaurado tal como estaba" || falla "Revisa nginx -t"
  sleep 2; salud_publica || true
  exit 0
fi

# Sitio actual del motor
SITIO=$(grep -lR "server_name[^;]*analisis\.ordersblock\.com" /etc/nginx/sites-enabled/ /etc/nginx/conf.d/ 2>/dev/null | head -1 || true)
[ -n "$SITIO" ] || { falla "No encuentro el sitio de $DOMINIO en nginx. Detente y envia el diagnostico."; exit 1; }
REAL=$(readlink -f "$SITIO")
info "Sitio actual: $SITIO → $REAL"
if grep -qE "listen[^;]*443|ssl_certificate" "$REAL"; then
  falla "El sitio actual usa HTTPS en el droplet (443/ssl). Esta plantilla es solo para el puerto 80: revision manual."; exit 1
fi
if grep -qE "^\s*server_name" "$REAL" && [ "$(grep -cE '^\s*server\s*\{' "$REAL")" -gt 1 ]; then
  falla "El archivo $REAL tiene mas de un bloque server (otros sitios). No lo reemplazo: revision manual."; exit 1
fi
# Mismo soporte IPv6 que el sitio actual: si hoy no escucha en [::]:80, el nuevo tampoco
NUEVO=$(mktemp); trap 'rm -f "$NUEVO"' EXIT
if grep -q "listen \[::\]" "$REAL"; then cp "$AQUI/nginx-analisis-seguro.conf" "$NUEVO"; else sed '/listen \[::\]:80;/d' "$AQUI/nginx-analisis-seguro.conf" > "$NUEVO"; fi
info "Cambios en el sitio:"; diff -u "$REAL" "$NUEVO" || true
info "Archivo global nuevo: $GLOBAL (server_tokens off, lista de Cloudflare y zona de limite)"
[ "$MODO" = revisar ] && { echo; info "Modo revisar: no se cambio nada. Para aplicar: --aplicar"; exit 0; }

# 1) Respaldo completo de /etc/nginx
R=$(nueva_carpeta_respaldo nginx)
tar -czf "$R/nginx.tar.gz" -C /etc nginx
ok "Respaldo de /etc/nginx en $R/nginx.tar.gz"
volver(){ falla "$1: restaurando nginx"; rm -rf /etc/nginx && tar -xzf "$R/nginx.tar.gz" -C /etc; nginx -t && systemctl reload nginx && sleep 2 && ok "nginx como antes"; exit 1; }

# 2) Lista oficial de rangos de Cloudflare + archivos nuevos
bash "$AQUI/cloudflare_ips.sh" /etc/nginx || volver "No se pudo bajar la lista de Cloudflare"
install -m 644 "$AQUI/nginx-00-seguridad-global.conf" "$GLOBAL"
install -m 644 "$NUEVO" "$REAL"

# 3) Probar la configuracion antes de recargar
nginx -t 2>&1 | tail -2 || true; nginx -t >/dev/null 2>&1 || volver "nginx -t rechaza la configuracion"
systemctl reload nginx; sleep 2

# 4) Verificar: el motor sigue sano, el dominio responde por Cloudflare y la IP directa se corta
IPPUB=$(ip -4 route get 1.1.1.1 | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1)}')
salud 5 || volver "El motor no responde en local"
salud_publica || volver "El dominio no responde a traves de Cloudflare"
DIRECTO=$(curl -s -o /dev/null -w '%{http_code}' -m 8 -H "Host: $DOMINIO" "http://$IPPUB/salud" || true)
[ "$DIRECTO" = 000 ] && ok "Acceso directo a http://$IPPUB con Host $DOMINIO: cortado (sin respuesta)" || volver "El acceso directo por IP responde $DIRECTO (deberia cortarse)"
CAB=$(curl -s -D - -o /dev/null -m 15 "https://$DOMINIO/salud" || true)
echo "$CAB" | grep -qi "strict-transport-security" && echo "$CAB" | grep -qi "x-content-type-options: nosniff" && ok "Cabeceras de seguridad presentes" || info "Cabeceras no vistas en la respuesta publica (revisa cache de Cloudflare)"
ok "Paso 3 aplicado. Para volver atras: sudo bash $0 --revertir $R"
info "La lista de Cloudflare cambia poco; renovarla cada mes: sudo bash $AQUI/cloudflare_ips.sh /etc/nginx && nginx -t && systemctl reload nginx"
