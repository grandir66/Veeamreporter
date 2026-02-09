#!/usr/bin/env bash
#
# Aggiorna PVE Backup Monitor con l'ultima versione da GitHub.
# Non modifica la configurazione in /etc/backup-monitor/pve-config.yaml
# Eseguire come root.
#
# Uso: sudo bash update.sh

set -euo pipefail

INSTALL_DIR="/opt/pve-monitor"
TMP_DIR="/tmp/veeamreporter-update"
ZIP_URL="https://github.com/grandir66/Veeamreporter/archive/refs/heads/main.zip"

if [[ $EUID -ne 0 ]]; then
    echo "Errore: eseguire come root (sudo bash update.sh)"
    exit 1
fi

echo "=== PVE Backup Monitor - Aggiornamento ==="
echo ""
echo "Scarico ultima versione da GitHub..."
curl -sL -o /tmp/veeamreporter-update.zip "$ZIP_URL"
unzip -o /tmp/veeamreporter-update.zip -d "$TMP_DIR"
rm -f /tmp/veeamreporter-update.zip

SRC="$TMP_DIR/Veeamreporter-main/agents/pve"
if [[ ! -d "$SRC" ]]; then
    echo "Errore: struttura archivio non valida (cartella $SRC non trovata)"
    rm -rf "$TMP_DIR"
    exit 1
fi

echo "Copio file in $INSTALL_DIR..."
cp -f "$SRC/pve_monitor.py" "$INSTALL_DIR/pve_monitor.py"
cp -f "$SRC/requirements.txt" "$INSTALL_DIR/requirements.txt"

echo "Aggiorno dipendenze Python..."
"$INSTALL_DIR/venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt" -q

rm -rf "$TMP_DIR"

echo ""
echo "Aggiornamento completato."
echo "File aggiornato: $(ls -la $INSTALL_DIR/pve_monitor.py)"
echo ""
echo "Per testare:"
echo "  $INSTALL_DIR/venv/bin/python $INSTALL_DIR/pve_monitor.py -c /etc/backup-monitor/pve-config.yaml --test"
echo ""
