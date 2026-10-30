#!/usr/bin/env bash
# 0_diagnostico.sh — SOLO LECTURA. No cambia nada. Ejecutar como root en el droplet:
#   sudo bash despliegue/seguridad/0_diagnostico.sh > /tmp/diagnostico.txt 2>&1
# y enviar /tmp/diagnostico.txt. No imprime claves: solo si existen y sus permisos.
set -u
APP=/opt/ordersblock_analisis
SVC=ordersblock-analisis
t(){ echo; echo "=== $* ==="; }

t "Sistema"; . /etc/os-release 2>/dev/null; echo "$PRETTY_NAME"; uname -r; date -u; systemctl --version | head -1
t "IP publica del droplet (ruta de salida)"; ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1)}' 
t "Python del motor"; "$APP/venv/bin/python" -V 2>&1
t "Dependencias instaladas (motor)"; "$APP/venv/bin/pip" freeze 2>/dev/null | grep -iE "^(numpy|requests|starlette|fastapi|uvicorn|anyio|h11|urllib3|idna|certifi)=="
t "Commit desplegado"; git -c safe.directory="$APP" -C "$APP" log --oneline -1 2>&1; git -c safe.directory="$APP" -C "$APP" status --short 2>&1 | head -10
t "Puertos escuchando (quien y donde)"; ss -tlnpH 2>/dev/null | awk '{print $4, $6}' | sort -u
t "Servicio del motor (unidad efectiva)"; systemctl cat "$SVC" 2>&1 | grep -vE "^\s*#" | grep -v "^$"
t "Estado"; systemctl is-active "$SVC"; systemctl show "$SVC" -p NoNewPrivileges -p ProtectSystem -p ProtectHome -p PrivateTmp -p User 2>&1
t "Analisis de exposicion de systemd (menor = mejor)"; systemd-analyze security "$SVC" 2>/dev/null | tail -1
t "Secretos: existencia y permisos (no se muestra el contenido)"
for f in "$APP/.env" /home/icc/.config/icc-futuros/deportes.env; do [ -e "$f" ] && stat -c '%a %U:%G %n' "$f"; done
[ -f "$APP/.env" ] && grep -oE "^[A-Z_]+=" "$APP/.env" | sed 's/=$//' | tr '\n' ' '; echo
[ -f "$APP/.env" ] && { grep -qE "^ORDERSBLOCK_API_KEY=.{20,}" "$APP/.env" && echo "ORDERSBLOCK_API_KEY: definida (>=20 caracteres)" || echo "ORDERSBLOCK_API_KEY: FALTA o es corta"; }
[ -f "$APP/.env" ] && grep -E "^ORDERSBLOCK_CORS=" "$APP/.env"
t "nginx: version, server_tokens y sitio del motor"; nginx -v 2>&1; nginx -T 2>/dev/null | grep -nE "server_tokens|listen |server_name |limit_req|client_max_body_size|add_header|allow |deny |real_ip" | head -40
t "nginx: sitios activos y quien escucha en el puerto 80"; ls -l /etc/nginx/sites-enabled/ 2>&1; grep -lR "server_name .*analisis.ordersblock.com" /etc/nginx/sites-enabled/ /etc/nginx/conf.d/ 2>/dev/null
nginx -T 2>/dev/null | awk '/^# configuration file/{f=$4} /server_name/{print f": "$0}' | sort -u | head -20
t "Rotacion de registros de nginx"; ls /etc/logrotate.d/ 2>/dev/null | tr '\n' ' '; echo
t "Cloudflare Tunnel"; command -v cloudflared && cloudflared --version 2>&1; systemctl is-active cloudflared 2>&1; ls /etc/cloudflared 2>/dev/null
[ -f /etc/cloudflared/config.yml ] && grep -E "hostname|service" /etc/cloudflared/config.yml
t "Cortafuegos"; ufw status verbose 2>&1 | head -25
t "Registros: lineas con posibles claves en las ultimas 2 semanas (solo el numero)"
journalctl -u "$SVC" --since "-14d" --no-pager 2>/dev/null | grep -ciE "apikey=[^*&[:space:]]{6,}|x-api-key: [^*[:space:]]{6,}" 
t "Tamano del journal"; journalctl --disk-usage 2>&1
t "Actualizaciones automaticas de seguridad del sistema"; systemctl is-enabled unattended-upgrades 2>&1; apt list --upgradable 2>/dev/null | grep -ci security
t "Usuarios con servicios (no se tocan billetera ni icc)"; ps -eo user,comm --no-headers | sort -u | grep -E "^(billetera|icc|analisis) " | head -20
echo; echo "FIN DEL DIAGNOSTICO"
