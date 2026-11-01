#!/bin/bash
# Install or update the PiDP-11 status panel on the Raspberry Pi.
# Run from the repository directory:   sudo ./install.sh
# An existing /opt/statuspanel/config.json is never overwritten.
set -euo pipefail

DEST=/opt/statuspanel
RUN_USER=${SUDO_USER:-pi}
[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
cd "$(dirname "$0")"

echo "== packages"
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-paho-mqtt gcc >/dev/null

echo "== stop the PiDP-11 emulator if present (it drives the same GPIO pins)"
if [ -x /etc/init.d/pidp11 ]; then
    /etc/init.d/pidp11 stop || true
    update-rc.d pidp11 disable >/dev/null 2>&1 || true
fi
pkill -x server11 || true

echo "== build ledmux"
gcc -O2 -Wall -o ledmux/ledmux ledmux/ledmux.c

echo "== install to $DEST (service user: $RUN_USER)"
usermod -aG gpio "$RUN_USER" || true
mkdir -p "$DEST/web"
install -m 755 ledmux/ledmux statuspanel/statuspanel.py "$DEST/"
install -m 644 statuspanel/web/index.html "$DEST/web/"
install -m 644 statuspanel/config.example.json "$DEST/"
if [ ! -f "$DEST/config.json" ]; then
    install -m 600 statuspanel/config.example.json "$DEST/config.json"
    echo "   created $DEST/config.json from the example - edit it in the web page"
fi
chown "$RUN_USER" "$DEST" "$DEST/config.json"

for unit in ledmux statuspanel; do
    sed "s/@USER@/$RUN_USER/" "systemd/$unit.service" > "/etc/systemd/system/$unit.service"
done
systemctl daemon-reload
systemctl enable ledmux.service statuspanel.service
systemctl restart ledmux.service statuspanel.service
sleep 2
systemctl --no-pager --lines=3 status ledmux.service statuspanel.service || true
echo
echo "Done. Open http://$(hostname).local/"
