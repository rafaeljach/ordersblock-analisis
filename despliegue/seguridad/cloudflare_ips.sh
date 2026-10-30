#!/usr/bin/env bash
# cloudflare_ips.sh — genera las listas de rangos de Cloudflare para nginx desde la fuente oficial.
# Uso: sudo bash cloudflare_ips.sh /etc/nginx   (escribe cloudflare-geo.conf y cloudflare-realip.conf)
set -euo pipefail
DEST=${1:-/etc/nginx}
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
curl -fsS -m 20 https://www.cloudflare.com/ips-v4 -o "$T/v4"
curl -fsS -m 20 https://www.cloudflare.com/ips-v6 -o "$T/v6"
cat "$T/v4" "$T/v6" | grep -E '^[0-9a-fA-F:.]+/[0-9]+$' > "$T/todas"
N=$(wc -l < "$T/todas"); [ "$N" -ge 10 ] || { echo "Lista de Cloudflare sospechosa ($N rangos). No se cambia nada." >&2; exit 1; }
{ echo "# Generado $(date -u +%FT%TZ) desde cloudflare.com/ips-v4 y ips-v6 ($N rangos)"; sed 's/$/ 1;/' "$T/todas"; } > "$T/geo"
{ echo "# Generado $(date -u +%FT%TZ) ($N rangos)"; sed 's/^/set_real_ip_from /; s/$/;/' "$T/todas"; } > "$T/realip"
install -m 644 "$T/geo" "$DEST/cloudflare-geo.conf"
install -m 644 "$T/realip" "$DEST/cloudflare-realip.conf"
echo "OK: $N rangos de Cloudflare en $DEST"
