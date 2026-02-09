#!/usr/bin/env bash
#
# Installer PVE Backup Monitor
# Crea venv, installa dipendenze, configura e attiva il timer systemd.
# Eseguire direttamente sul server Proxmox VE come root.
#
# Uso:
#   sudo bash install.sh
#

set -euo pipefail

INSTALL_DIR="/opt/pve-monitor"
CONFIG_DIR="/etc/backup-monitor"
CONFIG_FILE="$CONFIG_DIR/pve-config.yaml"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# --- Verifica root ---
if [[ $EUID -ne 0 ]]; then
    echo "Errore: eseguire come root (sudo bash install.sh)"
    exit 1
fi

# --- Verifica Proxmox VE ---
if ! command -v pvesh &>/dev/null; then
    echo "Errore: pvesh non trovato. Questo script deve girare su un server Proxmox VE."
    exit 1
fi

echo ""
echo "=== PVE Backup Monitor - Installazione ==="
echo ""
pveversion 2>/dev/null || true
echo ""

# --- Installa unzip se mancante ---
if ! command -v unzip &>/dev/null; then
    echo "Installazione unzip..."
    apt-get update -qq && apt-get install -y -qq unzip
fi

# --- Installa python3-venv se mancante ---
if ! python3 -m venv --help &>/dev/null; then
    echo "Installazione python3-venv..."
    # Rileva versione Python installata (es. 3.13)
    PYTHON_VERSION=$(python3 --version 2>&1 | sed -E 's/.*Python ([0-9]+\.[0-9]+).*/\1/')
    if [[ -n "$PYTHON_VERSION" ]] && apt-cache show "python${PYTHON_VERSION}-venv" &>/dev/null; then
        echo "Installazione python${PYTHON_VERSION}-venv..."
        apt-get update -qq && apt-get install -y -qq "python${PYTHON_VERSION}-venv"
    else
        echo "Installazione python3-venv..."
        apt-get update -qq && apt-get install -y -qq python3-venv
    fi
fi

# --- Copia file ---
echo "Copia file in $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR"
cp "$SCRIPT_DIR/pve_monitor.py" "$INSTALL_DIR/"
cp "$SCRIPT_DIR/requirements.txt" "$INSTALL_DIR/"

# --- Crea virtual environment ---
echo "Creazione virtual environment..."
python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install --upgrade pip --quiet
"$INSTALL_DIR/venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt" --quiet
echo "Dipendenze installate."

# --- Configurazione ---
mkdir -p "$CONFIG_DIR"

if [[ -f "$CONFIG_FILE" ]] && ! grep -q "graylog.example.com" "$CONFIG_FILE"; then
    echo "Configurazione esistente trovata in $CONFIG_FILE"
    grep -E "^\s+(code|name|server|port):" "$CONFIG_FILE" | head -4
    echo "  (per riconfigurare, eliminare $CONFIG_FILE prima di reinstallare)"
else
    echo ""
    echo "=== Configurazione ==="
    echo ""

    read -rp "Codice cliente (es. CLI001): " client_code
    read -rp "Nome cliente (es. Azienda Srl): " client_name
    read -rp "Sede [Invio = sede-principale]: " client_site
    client_site="${client_site:-sede-principale}"

    echo ""
    read -rp "Server Graylog - IP o hostname: " syslog_server
    read -rp "Porta syslog [Invio = 4514]: " syslog_port
    syslog_port="${syslog_port:-4514}"

    cat > "$CONFIG_FILE" <<EOF
# PVE Backup Monitor - Configurazione

client:
  code: "$client_code"
  name: "$client_name"
  site: "$client_site"

pve:
  lookback_hours: 24

syslog:
  server: "$syslog_server"
  port: $syslog_port
  facility: "local0"
EOF

    echo ""
    echo "Configurazione salvata in $CONFIG_FILE"
    echo "  Cliente: $client_code - $client_name"
    echo "  Syslog:  $syslog_server:$syslog_port"
fi

# --- Installa systemd ---
echo ""
echo "=== Installazione Timer Systemd ==="
echo ""

cat > /etc/systemd/system/pve-monitor.service <<EOF
[Unit]
Description=PVE Backup Monitor
After=network.target pveproxy.service

[Service]
Type=oneshot
ExecStart=$INSTALL_DIR/venv/bin/python $INSTALL_DIR/pve_monitor.py -c $CONFIG_FILE

[Install]
WantedBy=multi-user.target
EOF

cp "$SCRIPT_DIR/pve-monitor.timer" /etc/systemd/system/

# Timer report giornaliero
cat > /etc/systemd/system/pve-daily-report.service <<EOF
[Unit]
Description=PVE Backup Monitor - Report Giornaliero
After=network.target pveproxy.service

[Service]
Type=oneshot
ExecStart=$INSTALL_DIR/venv/bin/python $INSTALL_DIR/pve_monitor.py -c $CONFIG_FILE --daily-report

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/pve-daily-report.timer <<EOF
[Unit]
Description=PVE Backup Monitor - Report giornaliero alle 07:00

[Timer]
OnCalendar=*-*-* 07:00:00
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now pve-monitor.timer
systemctl enable --now pve-daily-report.timer

echo "Timer pve-monitor attivato (ogni 30 minuti)."
echo "Timer pve-daily-report attivato (ogni giorno alle 07:00)."
echo ""
echo "=== Installazione completata ==="
echo ""
echo "Per testare:"
echo "  $INSTALL_DIR/venv/bin/python $INSTALL_DIR/pve_monitor.py -c $CONFIG_FILE --test"
echo ""
echo "Stato timer:"
echo "  systemctl status pve-monitor.timer"
echo ""
