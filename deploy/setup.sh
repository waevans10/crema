#!/usr/bin/env bash
# One-shot: bind crema to the LAN, secure it, install + start it via systemd,
# run the connectivity check, and print the URL. Run from the project root:
#   bash deploy/setup.sh
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
ROOT="$(pwd)"
USER_NAME="$(id -un)"
BIN="$ROOT/.venv/bin/crema"

[ -x "$BIN" ] || { echo "No .venv found — run 'uv sync' first."; exit 1; }
[ -f .env ]   || { echo "No .env found — 'cp .env.example .env' and set your keys first."; exit 1; }

# --- 1. bind to the LAN ---
if grep -q '^CREMA_HOST=' .env; then
  sed -i 's/^CREMA_HOST=.*/CREMA_HOST=0.0.0.0/' .env
else
  echo 'CREMA_HOST=0.0.0.0' >> .env
fi

# --- 2. ensure a web password (secures the LAN-exposed UI) ---
GENERATED_PW=""
if ! grep -qE '^CREMA_WEB_PASSWORD=.+' .env; then
  PW="$(openssl rand -hex 12 2>/dev/null || head -c 12 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  if grep -q '^CREMA_WEB_PASSWORD=' .env; then
    sed -i "s/^CREMA_WEB_PASSWORD=.*/CREMA_WEB_PASSWORD=$PW/" .env
  else
    echo "CREMA_WEB_PASSWORD=$PW" >> .env
  fi
  GENERATED_PW="$PW"
fi
grep -q '^CREMA_WEB_USER=' .env || echo 'CREMA_WEB_USER=crema' >> .env

# --- lock down .env (holds the API key + web password) ---
chmod 600 .env

PORT="$(grep -E '^CREMA_PORT=' .env | cut -d= -f2)"; PORT="${PORT:-8765}"
WEB_USER="$(grep -E '^CREMA_WEB_USER=' .env | cut -d= -f2)"; WEB_USER="${WEB_USER:-crema}"

# --- 3. write systemd units with the real user + paths ---
sudo tee /etc/systemd/system/crema-web.service >/dev/null <<EOF
[Unit]
Description=crema web report
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER_NAME
WorkingDirectory=$ROOT
ExecStartPre=-$BIN doctor
ExecStart=$BIN serve
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/crema-review.service >/dev/null <<EOF
[Unit]
Description=crema shot review
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=$USER_NAME
WorkingDirectory=$ROOT
ExecStart=$BIN review
EOF

sudo tee /etc/systemd/system/crema-review.timer >/dev/null <<EOF
[Unit]
Description=Run crema review periodically

[Timer]
OnBootSec=2min
OnUnitActiveSec=15min
Persistent=true

[Install]
WantedBy=timers.target
EOF

# --- 4. enable + start ---
sudo systemctl daemon-reload
sudo systemctl enable --now crema-web.service crema-review.timer

# --- 5. check + report ---
echo
echo "=== connectivity check ==="
"$BIN" doctor || true

IP="$(hostname -I | awk '{print $1}')"
echo
echo "crema is running and will auto-start on boot."
echo "  Open:     http://$IP:$PORT"
echo "  Login:    $WEB_USER"
if [ -n "$GENERATED_PW" ]; then
  echo "  Password: $GENERATED_PW   (generated; also saved in .env)"
else
  echo "  Password: (your existing CREMA_WEB_PASSWORD in .env)"
fi
echo
echo "Logs:  journalctl -u crema-web.service -f"
