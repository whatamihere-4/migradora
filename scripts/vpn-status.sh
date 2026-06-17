#!/bin/sh
# Show VPN egress IP (via gluetun).
set -e
CONTAINER="${GLUETUN_CONTAINER:-migradora-gluetun}"
echo "Gluetun container: $CONTAINER"
echo -n "Public IP: "
docker exec "$CONTAINER" wget -qO- https://api.ipify.org 2>/dev/null || echo "(unavailable)"
echo
echo -n "Gluetun control IP: "
docker exec "$CONTAINER" wget -qO- http://127.0.0.1:8000/v1/publicip/ip 2>/dev/null || echo "(control API unavailable)"
