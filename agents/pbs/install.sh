#!/usr/bin/env bash
#
# Installer PBS Backup Monitor
# Crea venv, installa dipendenze, configura e attiva il timer systemd.
#
# Uso:
#   sudo bash install.sh
#

set -euo pipefail

INSTALL_DIR="/opt/backup-monitor"
CONFIG_DIR="/etc/backup-monitor"
CONFIG_FILE="$CONFIG_DIR/pbs-config.yaml"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# --- Verifica root ---
if [[ $EUID -ne 0 ]]; then
    echo "Errore: eseguire come root (sudo bash install.sh)"
    exit 1
fi

echo ""
echo "=== PBS Backup Monitor - Installazione ==="
echo ""

# --- Installa unzip se mancante ---
if ! command -v unzip &>/dev/null; then
    echo "Installazione unzip..."
    if command -v apt-get &>/dev/null; then
        apt-get update -qq && apt-get install -y -qq unzip
    elif command -v dnf &>/dev/null; then
        dnf install -y unzip
    elif command -v yum &>/dev/null; then
        yum install -y unzip
    else
        echo "Errore: impossibile installare unzip. Installarlo manualmente."
        exit 1
    fi
fi

# --- Installa python3-venv se mancante ---
if ! python3 -m venv --help &>/dev/null; then
    echo "Installazione python3-venv..."
    if command -v apt-get &>/dev/null; then
        # Rileva versione Python installata (es. 3.13)
        PYTHON_VERSION=$(python3 --version 2>&1 | sed -E 's/.*Python ([0-9]+\.[0-9]+).*/\1/')
        if [[ -n "$PYTHON_VERSION" ]] && apt-cache show "python${PYTHON_VERSION}-venv" &>/dev/null; then
            echo "Installazione python${PYTHON_VERSION}-venv..."
            apt-get update -qq && apt-get install -y -qq "python${PYTHON_VERSION}-venv"
        else
            echo "Installazione python3-venv..."
            apt-get update -qq && apt-get install -y -qq python3-venv
        fi
    elif command -v dnf &>/dev/null; then
        dnf install -y python3-virtualenv
    elif command -v yum &>/dev/null; then
        yum install -y python3-virtualenv
    else
        echo "Errore: impossibile installare python3-venv. Installarlo manualmente."
        exit 1
    fi
fi

# --- Copia file ---
echo "Copia file in $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR"
cp "$SCRIPT_DIR/pbs_monitor.py" "$INSTALL_DIR/"
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
    grep -E "^\s+(code|name|server|port):" "$CONFIG_FILE" | head -6
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
    read -rp "Hostname/IP server PBS [Invio = localhost]: " pbs_host
    pbs_host="${pbs_host:-localhost}"
    read -rp "Porta API PBS [Invio = 8007]: " pbs_port
    pbs_port="${pbs_port:-8007}"
    read -rp "Utente API PBS [Invio = monitor@pbs]: " pbs_user
    pbs_user="${pbs_user:-monitor@pbs}"
    read -rp "Nome API token: " pbs_token_name
    read -rp "Valore API token: " pbs_token_value

    echo ""
    read -rp "Server Graylog - IP o hostname: " syslog_server
    read -rp "Porta syslog [Invio = 4514]: " syslog_port
    syslog_port="${syslog_port:-4514}"

    cat > "$CONFIG_FILE" <<EOF
# PBS Backup Monitor - Configurazione

client:
  code: "$client_code"
  name: "$client_name"
  site: "$client_site"

pbs:
  host: "$pbs_host"
  port: $pbs_port
  user: "$pbs_user"
  token_name: "$pbs_token_name"
  token_value: "$pbs_token_value"
  verify_ssl: false
  lookback_hours: 24

syslog:
  server: "$syslog_server"
  port: $syslog_port
  facility: "local0"
EOF

    echo ""
    echo "Configurazione salvata in $CONFIG_FILE"
    echo "  Cliente: $client_code - $client_name"
    echo "  PBS:     $pbs_host:$pbs_port"
    echo "  Syslog:  $syslog_server:$syslog_port"
fi

# --- Installa systemd ---
echo ""
echo "=== Installazione Timer Systemd ==="
echo ""

cat > /etc/systemd/system/pbs-monitor.service <<EOF
[Unit]
Description=PBS Backup Monitor
After=network.target

[Service]
Type=oneshot
ExecStart=$INSTALL_DIR/venv/bin/python $INSTALL_DIR/pbs_monitor.py -c $CONFIG_FILE

[Install]
WantedBy=multi-user.target
EOF

cp "$SCRIPT_DIR/pbs-monitor.timer" /etc/systemd/system/

# Timer report giornaliero
cat > /etc/systemd/system/pbs-daily-report.service <<EOF
[Unit]
Description=PBS Backup Monitor - Report Giornaliero
After=network.target

[Service]
Type=oneshot
ExecStart=$INSTALL_DIR/venv/bin/python $INSTALL_DIR/pbs_monitor.py -c $CONFIG_FILE --daily-report

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/pbs-daily-report.timer <<EOF
[Unit]
Description=PBS Backup Monitor - Report giornaliero alle 07:00

[Timer]
OnCalendar=*-*-* 07:00:00
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now pbs-monitor.timer
systemctl enable --now pbs-daily-report.timer

echo "Timer pbs-monitor attivato (ogni 30 minuti)."
echo "Timer pbs-daily-report attivato (ogni giorno alle 07:00)."
echo ""
echo "=== Installazione completata ==="
echo ""
echo "Per testare:"
echo "  $INSTALL_DIR/venv/bin/python $INSTALL_DIR/pbs_monitor.py -c $CONFIG_FILE --test"
echo ""
echo "Stato timer:"
echo "  systemctl status pbs-monitor.timer"
echo ""
