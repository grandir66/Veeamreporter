#!/usr/bin/env bash
#
# Aggiorna PBS Backup Monitor con l'ultima versione da GitHub.
# Non modifica la configurazione in /etc/backup-monitor/pbs-config.yaml
# Eseguire come root.
#
# Uso: bash update.sh   (come root su server PBS o dove è installato)

set -euo pipefail

INSTALL_DIR="/opt/backup-monitor"
TMP_DIR="/tmp/veeamreporter-update"
ZIP_URL="https://github.com/grandir66/Veeamreporter/archive/refs/heads/main.zip"

if [[ $EUID -ne 0 ]]; then
    echo "Errore: eseguire come root"
    exit 1
fi

echo "=== PBS Backup Monitor - Aggiornamento ==="
echo ""
echo "Scarico ultima versione da GitHub..."
curl -sL -o /tmp/veeamreporter-update.zip "$ZIP_URL"
unzip -o /tmp/veeamreporter-update.zip -d "$TMP_DIR"
rm -f /tmp/veeamreporter-update.zip

SRC="$TMP_DIR/Veeamreporter-main/agents/pbs"
if [[ ! -d "$SRC" ]]; then
    echo "Errore: struttura archivio non valida (cartella $SRC non trovata)"
    rm -rf "$TMP_DIR"
    exit 1
fi

echo "Copio file in $INSTALL_DIR..."
cp -f "$SRC/pbs_monitor.py" "$INSTALL_DIR/pbs_monitor.py"
cp -f "$SRC/requirements.txt" "$INSTALL_DIR/requirements.txt"

echo "Aggiorno dipendenze Python..."
"$INSTALL_DIR/venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt" -q

rm -rf "$TMP_DIR"

echo ""
echo "Aggiornamento completato."
echo "File aggiornato: $(ls -la $INSTALL_DIR/pbs_monitor.py)"
echo ""
echo "Per testare:"
echo "  $INSTALL_DIR/venv/bin/python $INSTALL_DIR/pbs_monitor.py -c /etc/backup-monitor/pbs-config.yaml --test"
echo ""
