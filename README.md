# Backup Monitor - Syslog to Graylog

Sistema per inviare lo stato dei backup (Veeam e PBS) a Graylog via syslog.

## Struttura

```
├── agents/
│   ├── veeam/              # Agent Windows per Veeam B&R
│   │   ├── VeeamBackupMonitor.ps1
│   │   ├── config.json
│   │   └── Install-Task.ps1
│   └── pbs/                # Agent Linux per Proxmox Backup Server
│       ├── pbs_monitor.py
│       ├── config.yaml
│       ├── requirements.txt
│       └── pbs-monitor.service/timer
└── graylog/                # Configurazione Graylog
    ├── README.md
    ├── extractors.json
    └── pipeline-rules.txt
```

## Installazione Veeam Agent (Windows)

1. Copia `agents/veeam/` in `C:\BackupMonitor\`
2. Modifica `config.json` con i tuoi dati
3. Esegui `Install-Task.ps1` come amministratore
4. Test: `.\VeeamBackupMonitor.ps1 -TestMode`

## Installazione PBS Agent (Linux)

```bash
pip3 install -r requirements.txt
cp pbs_monitor.py /opt/backup-monitor/
cp config.yaml /etc/backup-monitor/pbs-config.yaml
# Modifica config con i tuoi dati

# Test
python3 /opt/backup-monitor/pbs_monitor.py -c /etc/backup-monitor/pbs-config.yaml --test

# Installa timer systemd
cp pbs-monitor.service pbs-monitor.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now pbs-monitor.timer
```

## Configurazione Graylog

1. Crea Input Syslog UDP porta 4514
2. Importa extractors o configura pipeline rules
3. Vedi `graylog/README.md` per dettagli

## Messaggi Inviati

| Tipo | Descrizione |
|------|-------------|
| `VEEAM_SERVER_STATUS` | Stato server Veeam |
| `VEEAM_REPOSITORY_STATUS` | Spazio repository |
| `VEEAM_JOB_RESULT` | Risultato job backup |
| `PBS_SERVER_STATUS` | Stato server PBS |
| `PBS_DATASTORE_STATUS` | Spazio datastore |
| `PBS_BACKUP_RESULT` | Risultato task backup |
