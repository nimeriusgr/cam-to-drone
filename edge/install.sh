#!/usr/bin/env bash
# Εγκατάσταση του Cam to Drone edge agent σε Raspberry Pi OS / Debian / Ubuntu.
# Χρήση:  sudo ./install.sh
set -euo pipefail

DIR=/opt/cam2drone-edge
DATA=/var/lib/cam2drone

if [ "$(id -u)" -ne 0 ]; then echo "Τρέξτε με sudo"; exit 1; fi

apt-get update -y
apt-get install -y python3 python3-venv chrony ffmpeg   # ffmpeg: κλιπ βίντεο συναγερμών
systemctl enable --now chrony   # σωστή ώρα: απαραίτητη για ONVIF

id -u cam2drone >/dev/null 2>&1 || useradd --system --home "$DATA" --shell /usr/sbin/nologin cam2drone
usermod -aG gpio cam2drone 2>/dev/null || true

mkdir -p "$DIR" "$DATA/spool"
cp agent.py "$DIR/"
[ -f "$DIR/config.json" ] || cp config.example.json "$DIR/config.json"

python3 -m venv "$DIR/venv"
"$DIR/venv/bin/pip" install --quiet --upgrade pip
"$DIR/venv/bin/pip" install --quiet onvif-zeep gpiozero || echo "Προειδοποίηση: δεν εγκαταστάθηκαν όλα τα προαιρετικά πακέτα"

chown -R cam2drone:cam2drone "$DIR" "$DATA"
chmod 600 "$DIR/config.json"

cp cam2drone-edge.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable cam2drone-edge

echo
echo "Έτοιμο. Επόμενα βήματα:"
echo "  1. sudo nano $DIR/config.json   (server, token, κάμερες)"
echo "  2. sudo -u cam2drone $DIR/venv/bin/python $DIR/agent.py --discover   (βρίσκει ONVIF κάμερες)"
echo "  3. sudo systemctl start cam2drone-edge"
echo "  4. journalctl -u cam2drone-edge -f   (καταγραφή)"
