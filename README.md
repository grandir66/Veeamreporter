# Backup Monitor - Syslog to Graylog

Sistema di monitoraggio backup che raccoglie dati da **Veeam Backup & Replication** (Windows) e **Proxmox Backup Server** (Linux), inviandoli a **Graylog** via syslog UDP in formato JSON strutturato.

Ogni agent viene eseguito automaticamente ogni 30 minuti e invia:

- Stato del server (CPU, memoria, uptime, versione)
- Stato dello storage (spazio totale, libero, percentuale uso, soglie allarme)
- Risultati dei job/task di backup (esito, durata, dimensione dati, dettaglio oggetti)

## Come Funziona

```text
Veeam B&R / PBS Server
        │
        ▼
  Agent (PS1 / Python)
        │
        ▼
  JSON serializzato in syslog RFC 5424
        │
        ▼
  Graylog (UDP porta 4514)
        │
        ▼
  Extractors / Pipeline Rules
        │
        ▼
  Dashboard, Allarmi, Report
```

- **Protocollo:** Syslog RFC 5424 su UDP
- **Porta:** 4514 (configurabile)
- **Frequenza:** Ogni 30 minuti (scheduled task Windows / systemd timer Linux)
- **Versione:** 2.0.0

## Struttura Progetto

```text
├── agents/
│   ├── veeam/                        # Agent Windows
│   │   ├── VeeamBackupMonitor.ps1    # Script principale di monitoraggio
│   │   ├── config.json               # Configurazione agent
│   │   └── Install-Task.ps1          # Installer scheduled task
│   └── pbs/                          # Agent Linux
│       ├── pbs_monitor.py            # Script principale di monitoraggio
│       ├── config.yaml               # Configurazione agent
│       ├── requirements.txt          # Dipendenze Python
│       ├── pbs-monitor.service       # Unit systemd
│       └── pbs-monitor.timer         # Timer systemd (30 min)
├── graylog/                          # Configurazione Graylog
│   ├── README.md                     # Istruzioni setup Graylog
│   ├── extractors.json               # Extractor JSON per input
│   └── pipeline-rules.txt            # Pipeline rules per parsing
└── README.md
```

## Requisiti

### Veeam Agent (Windows)

- Windows Server con **Veeam Backup & Replication** installato
- **Veeam PowerShell Module** (incluso con Veeam B&R Console)
- PowerShell 5.1+
- Privilegi amministratore per l'installazione

### PBS Agent (Linux)

- **Proxmox Backup Server** raggiungibile via API (porta 8007)
- Python 3.6+
- Pacchetti: `requests`, `pyyaml`
- API Token PBS con permessi di lettura

### Graylog

- Graylog con input **Syslog UDP** sulla porta 4514

---

## Installazione Veeam Agent (Windows)

### 1. Scarica e copia i file

Da PowerShell (non richiede git):

```powershell
Invoke-WebRequest -Uri "https://github.com/grandir66/Veeamreporter/archive/refs/heads/main.zip" -OutFile "$env:TEMP\Veeamreporter.zip"
Expand-Archive -Path "$env:TEMP\Veeamreporter.zip" -DestinationPath "$env:TEMP\Veeamreporter" -Force
mkdir C:\BackupMonitor -ErrorAction SilentlyContinue
copy "$env:TEMP\Veeamreporter\Veeamreporter-main\agents\veeam\*" C:\BackupMonitor\
Remove-Item "$env:TEMP\Veeamreporter*" -Recurse -Force
```

In alternativa, scarica lo ZIP manualmente da GitHub e copia il contenuto di `agents\veeam\` in `C:\BackupMonitor\`.

### 2. Configura

Modifica `C:\BackupMonitor\config.json`:

```json
{
    "client": {
        "code": "CLI001",
        "name": "Nome Cliente",
        "site": "sede-principale"
    },
    "veeam": {
        "server": "localhost",
        "lookback_hours": 24
    },
    "syslog": {
        "server": "graylog.example.com",
        "port": 4514,
        "facility": "local0"
    },
    "log_path": "C:\\BackupMonitor\\Logs"
}
```

Campi da modificare:

- `client.code` - Codice univoco cliente
- `client.name` - Nome cliente
- `client.site` - Identificativo sede
- `syslog.server` - IP o hostname del server Graylog
- `syslog.port` - Porta UDP syslog (default: 4514)
- `syslog.facility` - Facility syslog (local0-local7)
- `veeam.lookback_hours` - Ore indietro per cercare job completati
- `log_path` - Cartella log locali

### 3. Installa il task schedulato

Esegui come **Amministratore** (il flag `-ExecutionPolicy Bypass` e necessario perche gli script scaricati da internet non sono firmati):

```powershell
powershell -ExecutionPolicy Bypass -File "C:\BackupMonitor\Install-Task.ps1" -InstallPath "C:\BackupMonitor"
```

L'installer automaticamente:

- Sblocca tutti i file `.ps1` e `.json` (rimuove il flag Zone.Identifier di Windows)
- Crea un task schedulato `VeeamBackupMonitor` che gira ogni **30 minuti**
- Usa l'account **SYSTEM** con privilegi elevati
- Si avvia anche se il server e a batteria
- Recupera esecuzioni saltate

### 4. Testa

Verifica il funzionamento in modalita test (stampa i messaggi syslog senza inviarli):

```powershell
powershell -ExecutionPolicy Bypass -File "C:\BackupMonitor\VeeamBackupMonitor.ps1" -TestMode
```

### Log

I log vengono scritti in `C:\BackupMonitor\Logs\` con nome `veeam-monitor-YYYY-MM-DD.log`.

---

## Installazione PBS Agent (Linux)

### 1. Installa dipendenze

```bash
pip3 install -r agents/pbs/requirements.txt
```

### 2. Copia i file

```bash
mkdir -p /opt/backup-monitor
cp agents/pbs/pbs_monitor.py /opt/backup-monitor/

mkdir -p /etc/backup-monitor
cp agents/pbs/config.yaml /etc/backup-monitor/pbs-config.yaml
```

### 3. Configura

Modifica `/etc/backup-monitor/pbs-config.yaml`:

```yaml
client:
  code: "CLI001"              # Codice univoco cliente
  name: "Nome Cliente"        # Nome cliente
  site: "sede-principale"     # Identificativo sede

pbs:
  host: "pbs.example.com"    # Hostname o IP del server PBS
  port: 8007                  # Porta API PBS
  user: "monitor@pbs"         # Utente API
  token_name: "backup-monitor"                        # Nome API token
  token_value: "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"  # Valore API token
  verify_ssl: false           # Verifica certificato SSL
  lookback_hours: 24          # Ore indietro per cercare task completati

syslog:
  server: "graylog.example.com"  # IP o hostname del server Graylog
  port: 4514                      # Porta UDP syslog
  facility: "local0"              # Facility syslog (local0-local7)
```

Per creare l'API token su PBS:

1. Vai in **Configuration > Access Control > API Tokens**
2. Crea un token per l'utente `monitor@pbs`
3. Copia nome e valore nel config

### 4. Testa il funzionamento

```bash
python3 /opt/backup-monitor/pbs_monitor.py -c /etc/backup-monitor/pbs-config.yaml --test
```

### 5. Installa il timer systemd

```bash
cp agents/pbs/pbs-monitor.service /etc/systemd/system/
cp agents/pbs/pbs-monitor.timer /etc/systemd/system/

systemctl daemon-reload
systemctl enable --now pbs-monitor.timer
```

Il timer esegue lo script:

- **5 minuti** dopo il boot
- Poi ogni **30 minuti**
- Recupera le esecuzioni perse (`Persistent=true`)

Verifica stato:

```bash
systemctl status pbs-monitor.timer
systemctl list-timers | grep pbs
```

---

## Configurazione Graylog

### 1. Crea Input Syslog UDP

1. Vai in **System > Inputs > Select Input: Syslog UDP**
2. Configura:
   - **Title:** `Backup Monitor`
   - **Bind address:** `0.0.0.0`
   - **Port:** `4514`
   - **Store full message:** abilitato

### 2. Configura il parsing

Puoi usare **uno** dei due metodi:

**Metodo A - Extractors (piu semplice):**

- Importa `graylog/extractors.json` nell'input creato

**Metodo B - Pipeline Rules (piu flessibile):**

- Crea le regole da `graylog/pipeline-rules.txt`
- Collega la pipeline allo stream dei messaggi
- Vedi `graylog/README.md` per i dettagli

---

## Messaggi Inviati

Ogni messaggio contiene campi comuni + campi specifici per tipo.

### Campi comuni (tutti i messaggi)

| Campo              | Descrizione                        |
| ------------------ | ---------------------------------- |
| `message_type`     | Tipo messaggio (vedi sotto)        |
| `version`          | Versione agent (2.0.0)             |
| `timestamp`        | Timestamp UTC ISO 8601             |
| `client.code`      | Codice cliente                     |
| `client.name`      | Nome cliente                       |
| `client.site`      | Sede                               |
| `agent_hostname`   | Hostname del server agent          |
| `status`           | `success`, `warning` o `failed`    |

### VEEAM_SERVER_STATUS

Stato del server Veeam (CPU, memoria, uptime).

| Campo              | Descrizione                        |
| ------------------ | ---------------------------------- |
| `server_name`      | Nome server                        |
| `veeam_version`    | Versione Veeam installata          |
| `uptime_hours`     | Ore di uptime                      |
| `cpu_percent`      | Utilizzo CPU %                     |
| `memory_free_gb`   | RAM libera (GB)                    |
| `memory_total_gb`  | RAM totale (GB)                    |

### VEEAM_REPOSITORY_STATUS

Spazio dei repository di backup.

| Campo                      | Descrizione                        |
| -------------------------- | ---------------------------------- |
| `repository_name`          | Nome repository                    |
| `repository_type`          | Tipo (WinLocal, LinuxLocal, etc.)  |
| `repository_path`          | Percorso repository                |
| `total_gb` / `free_gb`     | Spazio totale / libero (GB)        |
| `used_percent`             | Percentuale occupata               |

### VEEAM_JOB_RESULT

Risultato di un job di backup completato.

| Campo                              | Descrizione                            |
| ---------------------------------- | -------------------------------------- |
| `job_name` / `job_id`              | Nome e ID del job                      |
| `job_type`                         | Tipo job (Backup, Replica, etc.)       |
| `start_time` / `end_time`          | Inizio e fine (UTC)                    |
| `duration_minutes`                 | Durata in minuti                       |
| `data_size_gb`                     | Dimensione dati processati (GB)        |
| `transferred_gb`                   | Dati effettivamente trasferiti (GB)    |
| `objects_total`                    | Numero oggetti (VM) processati         |
| `objects_success/warning/failed`   | Conteggio per esito                    |
| `objects`                          | Array dettaglio per ogni VM            |
| `result_message`                   | Messaggio di risultato Veeam           |

### PBS_SERVER_STATUS

Stato del server Proxmox Backup Server.

| Campo                              | Descrizione                        |
| ---------------------------------- | ---------------------------------- |
| `server_name`                      | Nome server                        |
| `pbs_version`                      | Versione PBS                       |
| `uptime_hours`                     | Ore di uptime                      |
| `cpu_percent`                      | Utilizzo CPU %                     |
| `memory_used_bytes/total_bytes`    | RAM usata / totale                 |
| `memory_used_percent`              | RAM occupata %                     |
| `load_average`                     | Load average [1m, 5m, 15m]         |

### PBS_DATASTORE_STATUS

Spazio dei datastore PBS.

| Campo                                  | Descrizione              |
| -------------------------------------- | ------------------------ |
| `datastore_name`                       | Nome datastore           |
| `total_gb` / `used_gb` / `free_gb`     | Spazio (GB)              |
| `used_percent`                         | Percentuale occupata     |

### PBS_BACKUP_RESULT

Risultato di un task di backup PBS.

| Campo                        | Descrizione                            |
| ---------------------------- | -------------------------------------- |
| `task_id`                    | UPID del task                          |
| `worker_type`                | Tipo worker (backup, etc.)             |
| `datastore`                  | Datastore di destinazione              |
| `backup_id`                  | ID backup                              |
| `object_type`                | Tipo oggetto (`vm`, `ct`, `host`)      |
| `start_time` / `end_time`    | Inizio e fine (UTC)                    |
| `duration_minutes`           | Durata in minuti                       |
| `result_message`             | Stato task PBS                         |
| `user`                       | Utente che ha eseguito il task         |

---

## Soglie di Allarme Storage

Le soglie di utilizzo disco determinano il campo `status` per repository e datastore:

| Utilizzo | Status      | Severity Syslog     |
| -------- | ----------- | ------------------- |
| 0-90%    | `success`   | 6 (Informational)   |
| 90-95%   | `warning`   | 4 (Warning)         |
| > 95%    | `failed`    | 3 (Error)           |

Queste soglie permettono di configurare allarmi su Graylog in base al livello di severity.

### Mapping Severity Syslog

| Status      | Severity | Significato     |
| ----------- | -------- | --------------- |
| `success`   | 6        | Informational   |
| `warning`   | 4        | Warning         |
| `failed`    | 3        | Error           |

La **priority** nel messaggio syslog e calcolata come: `(facility * 8) + severity`

---

## Troubleshooting

### Windows - Veeam Agent

```powershell
# Verifica che il modulo Veeam sia disponibile
Get-Module -ListAvailable Veeam.Backup.PowerShell

# Esegui manualmente in test
C:\BackupMonitor\VeeamBackupMonitor.ps1 -TestMode

# Controlla i log
Get-Content C:\BackupMonitor\Logs\veeam-monitor-*.log -Tail 50

# Verifica il task schedulato
Get-ScheduledTask -TaskName "VeeamBackupMonitor"

# Esegui manualmente il task
Start-ScheduledTask -TaskName "VeeamBackupMonitor"
```

### Linux - PBS Agent

```bash
# Esegui manualmente in test
python3 /opt/backup-monitor/pbs_monitor.py -c /etc/backup-monitor/pbs-config.yaml --test

# Controlla lo stato del timer
systemctl status pbs-monitor.timer
journalctl -u pbs-monitor.service -n 50

# Esegui manualmente il service
systemctl start pbs-monitor.service
```

### Verifica Ricezione Graylog

- Verifica che l'input UDP 4514 sia attivo in **System > Inputs**
- Controlla che i messaggi arrivino in **Search** filtrando per `source:<hostname-agent>`
- Se i campi JSON non vengono estratti, verifica che l'extractor o le pipeline rules siano configurati correttamente
