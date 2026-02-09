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

# --- Aggiorna repository (necessario su Proxmox VE) ---
echo "Aggiornamento repository..."
apt-get update -qq || {
    echo "Errore: impossibile aggiornare repository. Verificare connessione di rete e configurazione apt."
    exit 1
}

# --- Installa unzip se mancante ---
if ! command -v unzip &>/dev/null; then
    echo "Installazione unzip..."
    apt-get install -y -qq unzip || {
        echo "Errore: impossibile installare unzip."
        exit 1
    }
fi

# --- Installa python3-venv se mancante ---
# Rileva versione Python installata (es. 3.13)
PYTHON_VERSION=$(python3 --version 2>&1 | sed -E 's/.*Python ([0-9]+\.[0-9]+).*/\1/')
echo "Python versione rilevata: ${PYTHON_VERSION}"

VENV_INSTALLED=false
VENV_PACKAGE=""

# Verifica se un pacchetto venv è già installato
if dpkg -l | grep -qE "^ii.*python[0-9.]+-venv "; then
    VENV_INSTALLED=true
    echo "python3-venv già installato."
else
    # Prova prima con la versione specifica (es. python3.13-venv)
    if [[ -n "$PYTHON_VERSION" ]]; then
        SPECIFIC_PACKAGE="python${PYTHON_VERSION}-venv"
        if apt-cache show "$SPECIFIC_PACKAGE" &>/dev/null 2>&1; then
            VENV_PACKAGE="$SPECIFIC_PACKAGE"
            echo "Trovato pacchetto specifico: ${VENV_PACKAGE}"
        fi
    fi
    
    # Se non trovato, prova con python3-venv generico
    if [[ -z "$VENV_PACKAGE" ]] && apt-cache show python3-venv &>/dev/null 2>&1; then
        VENV_PACKAGE="python3-venv"
        echo "Usando pacchetto generico: ${VENV_PACKAGE}"
    fi
    
    # Installa il pacchetto trovato
    if [[ -n "$VENV_PACKAGE" ]]; then
        echo "Installazione ${VENV_PACKAGE}..."
        apt-get install -y -qq "$VENV_PACKAGE" || {
            echo ""
            echo "Errore: impossibile installare ${VENV_PACKAGE}."
            echo ""
            echo "Su Proxmox VE potrebbe essere necessario:"
            echo "  1. Verificare che i repository Debian siano abilitati"
            echo "  2. Installare manualmente con:"
            echo "     apt-get install ${VENV_PACKAGE}"
            echo ""
            echo "Se il pacchetto non esiste, provare:"
            echo "     apt-get install python3-venv"
            exit 1
        }
        VENV_INSTALLED=true
    else
        echo ""
        echo "Errore: pacchetto python3-venv non trovato nei repository."
        echo ""
        echo "Su Proxmox VE potrebbe essere necessario abilitare i repository Debian standard."
        echo "Installare manualmente con:"
        if [[ -n "$PYTHON_VERSION" ]]; then
            echo "  apt-get install python${PYTHON_VERSION}-venv"
        fi
        echo "  oppure"
        echo "  apt-get install python3-venv"
        exit 1
    fi
fi

# --- Copia file ---
echo "Copia file in $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR"
cp "$SCRIPT_DIR/pve_monitor.py" "$INSTALL_DIR/"
cp "$SCRIPT_DIR/requirements.txt" "$INSTALL_DIR/"

# --- Crea virtual environment ---
echo "Creazione virtual environment..."
if ! python3 -m venv "$INSTALL_DIR/venv"; then
    echo ""
    echo "Errore: impossibile creare virtual environment."
    echo "Verificare che python3-venv sia installato correttamente:"
    echo "  dpkg -l | grep python.*venv"
    echo ""
    echo "Se non installato, eseguire:"
    if [[ -n "$PYTHON_VERSION" ]]; then
        echo "  apt-get install python${PYTHON_VERSION}-venv"
    else
        echo "  apt-get install python3-venv"
    fi
    exit 1
fi

# Verifica che pip sia disponibile nel venv
if [[ ! -f "$INSTALL_DIR/venv/bin/pip" ]]; then
    echo ""
    echo "Errore: pip non disponibile nel virtual environment."
    echo "Il pacchetto python3-venv potrebbe non essere installato correttamente."
    exit 1
fi

echo "Aggiornamento pip..."
"$INSTALL_DIR/venv/bin/pip" install --upgrade pip --quiet || {
    echo "Avviso: impossibile aggiornare pip, continuo con versione installata..."
}

echo "Installazione dipendenze..."
"$INSTALL_DIR/venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt" --quiet || {
    echo "Errore: impossibile installare dipendenze."
    echo "Verificare il file requirements.txt e la connessione di rete."
    exit 1
}
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
    read -rp "Server Graylog - IP o hostname [Invio = syslog.domarc.it]: " syslog_server
    syslog_server="${syslog_server:-syslog.domarc.it}"
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
