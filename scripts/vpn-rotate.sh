#!/bin/sh
# Rotate VPN egress IP (PIA via gluetun). JD2 must route through gluetun.
set -e
CONTAINER="${GLUETUN_CONTAINER:-migradora-gluetun}"

echo "Before:"
./scripts/vpn-status.sh || true

echo "Reconnecting VPN..."
docker exec "$CONTAINER" wget -qO- \
  --method=PUT \
  --header='Content-Type: application/json' \
  --body-data='{"status":"stopped"}' \
  http://127.0.0.1:8000/v1/openvpn/status

echo "Waiting for reconnect..."
sleep 20

for _ in 1 2 3 4 5 6 7 8 9 10; do
  if docker exec "$CONTAINER" wget -qO- https://api.ipify.org >/dev/null 2>&1; then
    break
  fi
  sleep 3
done

echo "After:"
./scripts/vpn-status.sh

echo
echo "Resume pipeline if paused:"
echo "  docker compose exec orchestrator python -m migradora resume"
