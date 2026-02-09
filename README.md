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
- **Frequenza:** Ogni 30 minuti (monitoraggio) + report giornaliero alle 07:00
- **Versione:** 2.0.0

## Struttura Progetto

```text
├── agents/
│   ├── veeam/                        # Agent Windows
│   │   ├── VeeamBackupMonitor.ps1    # Script principale di monitoraggio
│   │   ├── config.example.json        # Template configurazione
│   │   └── Install-Task.ps1          # Installer scheduled task
│   ├── pbs/                          # Agent PBS (Linux)
│   │   ├── pbs_monitor.py            # Script principale di monitoraggio
│   │   ├── config.example.yaml       # Template configurazione
│   │   ├── requirements.txt          # Dipendenze Python
│   │   ├── install.sh                # Installer interattivo
│   │   ├── pbs-monitor.service       # Unit systemd
│   │   └── pbs-monitor.timer         # Timer systemd (30 min)
│   └── pve/                          # Agent Proxmox VE (Linux)
│       ├── pve_monitor.py            # Script principale di monitoraggio
│       ├── config.example.yaml       # Template configurazione
│       ├── requirements.txt          # Dipendenze Python
│       ├── install.sh                # Installer interattivo
│       ├── pve-monitor.service       # Unit systemd
│       └── pve-monitor.timer         # Timer systemd (30 min)
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

### PVE Agent (Proxmox VE)

- **Proxmox VE** 7.0+ installato sul server
- Python 3.6+
- Accesso root (usa `pvesh` locale, nessun token necessario)

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

> **Nota:** Il file `config.json` non e incluso nel repository. Alla prima installazione, l'installer lo crea automaticamente dal template `config.example.json`. Gli aggiornamenti successivi non sovrascrivono la configurazione esistente.

### 2. Installa

Esegui come **Amministratore** (il flag `-ExecutionPolicy Bypass` e necessario perche gli script scaricati da internet non sono firmati):

```powershell
powershell -ExecutionPolicy Bypass -File "C:\BackupMonitor\Install-Task.ps1" -InstallPath "C:\BackupMonitor"
```

L'installer chiede interattivamente:

- **Codice cliente** (es. CLI001)
- **Nome cliente** (es. Azienda Srl)
- **Sede** (default: sede-principale)
- **Server Graylog** - IP o hostname
- **Porta syslog** (default: 4514)

I dati inseriti vengono salvati in `config.json` (creato dal template `config.example.json` se non presente). Poi l'installer:

- Sblocca tutti i file `.ps1` e `.json` (rimuove il flag Zone.Identifier di Windows)
- Crea un task schedulato `VeeamBackupMonitor` che gira ogni **30 minuti**
- Usa l'account **SYSTEM** con privilegi elevati
- Si avvia anche se il server e a batteria
- Recupera esecuzioni saltate

### Configurazione avanzata (opzionale)

Per modificare parametri aggiuntivi, edita manualmente `C:\BackupMonitor\config.json`:

| Campo | Descrizione | Default |
| ----- | ----------- | ------- |
| `syslog.facility` | Facility syslog (local0-local7) | local0 |
| `veeam.lookback_hours` | Ore indietro per cercare job completati | 24 |
| `log_path` | Cartella log locali | C:\BackupMonitor\Logs |

### 3. Testa

Verifica il funzionamento in modalita test (stampa i messaggi syslog senza inviarli):

```powershell
powershell -ExecutionPolicy Bypass -File "C:\BackupMonitor\VeeamBackupMonitor.ps1" -TestMode
```

### Log

I log vengono scritti in `C:\BackupMonitor\Logs\` con nome `veeam-monitor-YYYY-MM-DD.log`.

---

## Installazione PBS Agent (Linux)

### Prerequisiti

Prima di installare, crea un API token su PBS:

1. Vai in **Configuration > Access Control > API Tokens**
2. Crea un token per l'utente `monitor@pbs`
3. Annota nome e valore del token (serviranno durante l'installazione)

### 1. Scarica i file

```bash
curl -L -o /tmp/veeamreporter.zip https://github.com/grandir66/Veeamreporter/archive/refs/heads/main.zip
unzip -o /tmp/veeamreporter.zip -d /tmp/veeamreporter
rm -f /tmp/veeamreporter.zip
```

### 2. Esegui l'installer

```bash
bash /tmp/veeamreporter/Veeamreporter-main/agents/pbs/install.sh
```

> **Nota:** Lo script deve essere eseguito come root. Se non sei già root, usa `sudo`. Lo script installerà automaticamente `unzip` e `python3-venv` se mancanti.

L'installer chiede interattivamente:

- **Codice cliente** e **nome cliente**
- **Sede** (default: sede-principale)
- **Server PBS** - hostname/IP, porta, utente e API token
- **Server Graylog** - IP o hostname e porta syslog

L'installer automaticamente:

- Crea un virtual environment Python in `/opt/backup-monitor/venv/`
- Installa le dipendenze (`requests`, `pyyaml`)
- Salva la configurazione in `/etc/backup-monitor/pbs-config.yaml`
- Installa e attiva il timer systemd (ogni **30 minuti**)

### 3. Verifica

```bash
/opt/backup-monitor/venv/bin/python /opt/backup-monitor/pbs_monitor.py -c /etc/backup-monitor/pbs-config.yaml --test
```

### Parametri aggiuntivi (opzionale)

Per modificare parametri aggiuntivi, edita `/etc/backup-monitor/pbs-config.yaml`:

| Campo | Descrizione | Default |
| ----- | ----------- | ------- |
| `pbs.verify_ssl` | Verifica certificato SSL PBS | false |
| `pbs.lookback_hours` | Ore indietro per cercare task completati | 24 |
| `syslog.facility` | Facility syslog (local0-local7) | local0 |

### Verifica stato timer

```bash
systemctl status pbs-monitor.timer
systemctl list-timers | grep pbs
```

### Pulizia file temporanei

```bash
rm -rf /tmp/veeamreporter
```

---

## Installazione PVE Agent (Proxmox VE)

Agent per monitorare backup vzdump direttamente sul server Proxmox VE, senza necessita di PBS. Usa `pvesh` (CLI nativo PVE) e non richiede autenticazione perche gira come root sul nodo.

### Prerequisiti

- **Proxmox VE** 7.0+ installato
- Accesso root (lo script verifica automaticamente)
- Connessione internet per scaricare dipendenze

**Nota su Proxmox VE:** Su alcune installazioni Proxmox VE potrebbero essere necessari i repository Debian standard per installare `python3-venv`. Se l'installazione fallisce, verificare che i repository siano abilitati.

### Scarica i file

```bash
curl -L -o /tmp/veeamreporter.zip https://github.com/grandir66/Veeamreporter/archive/refs/heads/main.zip
unzip -o /tmp/veeamreporter.zip -d /tmp/veeamreporter
rm -f /tmp/veeamreporter.zip
```

> **Nota:** Se `unzip` non è installato, lo script lo installerà automaticamente.

### Esegui l'installer PVE

```bash
bash /tmp/veeamreporter/Veeamreporter-main/agents/pve/install.sh
```

> **Nota:** Lo script deve essere eseguito come root. Su Proxmox VE di solito si è già root, quindi `sudo` non è necessario. Lo script installerà automaticamente `unzip` e `python3-venv` (o `python3.X-venv` per la versione specifica) se mancanti.

L'installer chiede interattivamente:

- **Codice cliente** e **nome cliente**
- **Sede** (default: sede-principale)
- **Server Graylog** - IP o hostname e porta syslog

Non vengono chieste credenziali PVE perche lo script usa `pvesh` localmente come root.

L'installer automaticamente:

- Verifica che `pvesh` sia presente (conferma che e un server Proxmox VE)
- Crea un virtual environment Python in `/opt/pve-monitor/venv/`
- Installa la dipendenza (`pyyaml`)
- Salva la configurazione in `/etc/backup-monitor/pve-config.yaml`
- Installa e attiva il timer systemd (ogni **30 minuti**)

### 3. Verifica PVE

```bash
/opt/pve-monitor/venv/bin/python /opt/pve-monitor/pve_monitor.py -c /etc/backup-monitor/pve-config.yaml --test
```

### Parametri aggiuntivi PVE (opzionale)

Per modificare parametri aggiuntivi, edita `/etc/backup-monitor/pve-config.yaml`:

| Campo | Descrizione | Default |
| ----- | ----------- | ------- |
| `pve.lookback_hours` | Ore indietro per cercare task completati | 24 |
| `syslog.facility` | Facility syslog (local0-local7) | local0 |

### Verifica stato timer PVE

```bash
systemctl status pve-monitor.timer
systemctl list-timers | grep pve
```

### Pulizia file temporanei PVE

```bash
rm -rf /tmp/veeamreporter
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

| Campo                    | Descrizione                              |
| ------------------------ | ---------------------------------------- |
| `server_name`            | Nome server                              |
| `veeam_version`          | Versione Veeam installata                |
| `uptime_hours`           | Ore di uptime                            |
| `cpu_percent`            | Utilizzo CPU %                           |
| `memory_free_gb`         | RAM libera (GB)                          |
| `memory_total_gb`        | RAM totale (GB)                          |
| `license_status`         | Stato licenza (Valid, Expired, etc.)     |
| `license_type`           | Tipo licenza (Perpetual, Rental, etc.)   |
| `license_edition`        | Edizione (Standard, Enterprise, etc.)    |
| `license_expiration`     | Scadenza licenza (YYYY-MM-DD)            |
| `support_expiration`     | Scadenza supporto (YYYY-MM-DD)           |
| `support_id`             | ID contratto di supporto                 |
| `licensed_instances`     | Istanze licenziate totali                |
| `used_instances`         | Istanze attualmente in uso               |

### VEEAM_SERVICE_STATUS

Stato dei servizi Windows di Veeam. Segnala `failed` se un servizio con avvio Automatic non e Running.

| Campo                       | Descrizione                                  |
| --------------------------- | -------------------------------------------- |
| `services_total`            | Numero totale servizi Veeam trovati          |
| `services_running`          | Servizi in esecuzione                        |
| `services_stopped`          | Servizi non in esecuzione                    |
| `services`                  | Array dettaglio per ogni servizio            |
| `services[].name`           | Nome interno servizio (es. VeeamBackupSvc)   |
| `services[].display_name`   | Nome visualizzato                            |
| `services[].state`          | Stato attuale (Running, Stopped, etc.)       |
| `services[].startup_type`   | Tipo avvio (Automatic, Manual, Disabled)     |

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
| `result_message`                   | Messaggio di risultato Veeam           |
| `bottleneck`                       | Collo di bottiglia rilevato da Veeam   |
| `is_retry`                         | Se la sessione e un retry              |
| `data_size_gb`                     | Dimensione dati processati (GB)        |
| `transferred_gb`                   | Dati effettivamente trasferiti (GB)    |
| `objects_total`                    | Numero oggetti (VM) processati         |
| `objects_success/warning/failed`   | Conteggio per esito                    |
| `objects`                          | Array dettaglio per ogni VM            |
| `objects[].error_message`          | Motivo errore (solo se failed/warning) |
| `objects[].error_details`          | Array log dettagliati errore (solo se failed/warning) |
| `error_details`                   | Dettagli errori sessione (solo se failed/warning) |
| `error_details.error_count`       | Numero errori trovati                  |
| `error_details.summary`            | Riepilogo errore principale            |
| `error_details.errors[]`           | Array log errori dettagliati           |
| `error_details.errors[].title`     | Titolo record errore                   |
| `error_details.errors[].message`   | Messaggio errore completo              |
| `error_details.errors[].status`   | Status (Error/Warning)                 |
| `error_details.errors[].time`      | Timestamp errore (UTC)                 |

**Esempio messaggio con errori dettagliati:**

```json
{
  "message_type": "VEEAM_JOB_RESULT",
  "status": "failed",
  "job_name": "Backup VM Production",
  "result_message": "Job failed",
  "error_details": {
    "error_count": 3,
    "summary": "Failed to process VM: VM-Prod-01",
    "errors": [
      {
        "title": "Error",
        "message": "Failed to create snapshot: The operation timed out",
        "status": "Error",
        "time": "2026-02-09T10:15:23Z"
      },
      {
        "title": "Error",
        "message": "VM VM-Prod-01: Connection timeout",
        "status": "Error",
        "time": "2026-02-09T10:15:45Z"
      }
    ]
  },
  "objects": [
    {
      "name": "VM-Prod-01",
      "status": "failed",
      "error_message": "Failed to create snapshot",
      "error_details": [
        {
          "title": "Error",
          "message": "Snapshot creation failed: The operation timed out",
          "status": "Error",
          "time": "2026-02-09T10:15:23Z"
        }
      ]
    }
  ]
}
```

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

### PVE_NODE_STATUS

Stato del nodo Proxmox VE (CPU, memoria, uptime).

| Campo                              | Descrizione                        |
| ---------------------------------- | ---------------------------------- |
| `server_name`                      | Nome nodo PVE                      |
| `pve_version`                      | Versione Proxmox VE                |
| `uptime_hours`                     | Ore di uptime                      |
| `cpu_percent`                      | Utilizzo CPU %                     |
| `memory_used_percent`              | RAM occupata %                     |
| `memory_total_gb`                  | RAM totale (GB)                    |
| `load_average`                     | Load average [1m, 5m, 15m]         |

### PVE_STORAGE_STATUS

Spazio degli storage Proxmox VE (local, NFS, CIFS, PBS backend, etc.).

| Campo                              | Descrizione                        |
| ---------------------------------- | ---------------------------------- |
| `storage_name`                     | Nome storage                       |
| `storage_type`                     | Tipo (dir, nfs, cifs, pbs, etc.)   |
| `content`                          | Contenuto (backup, images, etc.)   |
| `total_gb` / `used_gb` / `free_gb` | Spazio (GB)                        |
| `used_percent`                     | Percentuale occupata               |

### PVE_BACKUP_RESULT

Risultato di un task vzdump completato.

| Campo                        | Descrizione                            |
| ---------------------------- | -------------------------------------- |
| `task_id`                    | UPID del task                          |
| `vmid`                       | ID della VM/CT                         |
| `start_time` / `end_time`    | Inizio e fine (UTC)                    |
| `duration_minutes`           | Durata in minuti                       |
| `exit_status`                | Esito task (OK, errore, etc.)          |
| `result_message`             | Messaggio di risultato                 |
| `user`                       | Utente che ha eseguito il task         |

### PVE_BACKUP_COVERAGE

Verifica copertura backup: VM/CT non coperte da alcun job di backup schedulato.

| Campo                        | Descrizione                            |
| ---------------------------- | -------------------------------------- |
| `not_backed_up_count`        | Numero VM/CT senza backup              |
| `guests`                     | Array dettaglio VM/CT non coperte      |
| `guests[].vmid`              | ID della VM/CT                         |
| `guests[].name`              | Nome della VM/CT                       |
| `guests[].type`              | Tipo (`qemu` o `lxc`)                  |

### PVE_SERVICE_STATUS

Stato dei servizi systemd importanti di Proxmox VE (equivalente a VEEAM_SERVICE_STATUS). Segnala `failed` se un servizio importante è fermo o fallito.

| Campo                       | Descrizione                                  |
| --------------------------- | -------------------------------------------- |
| `services_total`            | Numero totale servizi verificati             |
| `services_running`          | Servizi in esecuzione                       |
| `services_stopped`          | Servizi non in esecuzione                    |
| `services_failed`           | Servizi falliti                              |
| `services`                  | Array dettaglio per ogni servizio            |
| `services[].name`           | Nome servizio (es. pve-cluster, pveproxy)   |
| `services[].state`          | Stato attuale (active, inactive, failed)     |
| `services[].startup_type`   | Tipo avvio (enabled, disabled, unknown)      |

Servizi monitorati: `pve-cluster`, `pve-daemon`, `pveproxy`, `pvestatd`, `pve-firewall`, `corosync`, `pve-ha-crm`, `pve-ha-lrm`.

### VEEAM_DAILY_REPORT

Riepilogo giornaliero di tutti i job Veeam eseguiti nelle ultime 24 ore. Inviato una volta al giorno alle 07:00.

| Campo                              | Descrizione                            |
| ---------------------------------- | -------------------------------------- |
| `report_date`                      | Data del report (YYYY-MM-DD)           |
| `lookback_hours`                   | Ore di lookback (default 24)           |
| `jobs_total`                       | Numero totale job eseguiti             |
| `jobs_success`                     | Job completati con successo            |
| `jobs_warning`                     | Job con warning                        |
| `jobs_failed`                      | Job falliti                            |
| `jobs`                             | Array dettaglio per ogni job           |
| `jobs[].job_name`                  | Nome del job                           |
| `jobs[].job_type`                  | Tipo job (Backup, Replica, etc.)       |
| `jobs[].status`                    | Esito (success/warning/failed)         |
| `jobs[].start_time` / `end_time`   | Inizio e fine (UTC)                    |
| `jobs[].duration_minutes`          | Durata in minuti                       |
| `jobs[].data_size_gb`              | Dimensione dati processati (GB)        |
| `jobs[].transferred_gb`            | Dati trasferiti (GB)                   |

### PBS_DAILY_REPORT

Riepilogo giornaliero di tutti i task backup PBS nelle ultime 24 ore. Inviato una volta al giorno alle 07:00.

| Campo                              | Descrizione                            |
| ---------------------------------- | -------------------------------------- |
| `report_date`                      | Data del report (YYYY-MM-DD)           |
| `lookback_hours`                   | Ore di lookback (default 24)           |
| `jobs_total`                       | Numero totale task eseguiti            |
| `jobs_success`                     | Task completati con successo           |
| `jobs_warning`                     | Task con warning                       |
| `jobs_failed`                      | Task falliti                           |
| `jobs`                             | Array dettaglio per ogni task          |
| `jobs[].backup_id`                 | ID backup                              |
| `jobs[].datastore`                 | Datastore di destinazione              |
| `jobs[].status`                    | Esito (success/warning/failed)         |
| `jobs[].start_time` / `end_time`   | Inizio e fine (UTC)                    |
| `jobs[].duration_minutes`          | Durata in minuti                       |

### PVE_DAILY_REPORT

Report giornaliero completo di Proxmox VE. Inviato una volta al giorno alle 07:00 e include:

- **PVE_NODE_STATUS** - Stato del nodo (CPU, memoria, uptime, versione)
- **PVE_STORAGE_STATUS** - Stato di tutti gli storage
- **PVE_SERVICE_STATUS** - Stato dei servizi systemd importanti
- **PVE_BACKUP_COVERAGE** - VM/CT senza backup schedulato
- **PVE_DAILY_REPORT** - Riepilogo di tutti i task vzdump nelle ultime 24 ore

Riepilogo giornaliero di tutti i task vzdump PVE nelle ultime 24 ore:

| Campo                              | Descrizione                            |
| ---------------------------------- | -------------------------------------- |
| `report_date`                      | Data del report (YYYY-MM-DD)           |
| `lookback_hours`                   | Ore di lookback (default 24)           |
| `jobs_total`                       | Numero totale task eseguiti            |
| `jobs_success`                     | Task completati con successo           |
| `jobs_warning`                     | Task con warning                       |
| `jobs_failed`                      | Task falliti                           |
| `jobs`                             | Array dettaglio per ogni task          |
| `jobs[].vmid`                      | ID della VM/CT                         |
| `jobs[].status`                    | Esito (success/warning/failed)         |
| `jobs[].start_time` / `end_time`   | Inizio e fine (UTC)                    |
| `jobs[].duration_minutes`          | Durata in minuti                       |
| `jobs[].exit_status`               | Esito task (OK, errore, etc.)          |

| Campo                              | Descrizione                            |
| ---------------------------------- | -------------------------------------- |
| `report_date`                      | Data del report (YYYY-MM-DD)           |
| `lookback_hours`                   | Ore di lookback (default 24)           |
| `jobs_total`                       | Numero totale task eseguiti            |
| `jobs_success`                     | Task completati con successo           |
| `jobs_warning`                     | Task con warning                       |
| `jobs_failed`                      | Task falliti                           |
| `jobs`                             | Array dettaglio per ogni task          |
| `jobs[].vmid`                      | ID della VM/CT                         |
| `jobs[].status`                    | Esito (success/warning/failed)         |
| `jobs[].start_time` / `end_time`   | Inizio e fine (UTC)                    |
| `jobs[].duration_minutes`          | Durata in minuti                       |
| `jobs[].exit_status`               | Esito task (OK, errore, etc.)          |

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

### Proxmox VE - PVE Agent

```bash
# Esegui manualmente in test
/opt/pve-monitor/venv/bin/python /opt/pve-monitor/pve_monitor.py -c /etc/backup-monitor/pve-config.yaml --test

# Controlla lo stato del timer
systemctl status pve-monitor.timer
journalctl -u pve-monitor.service -n 50

# Esegui manualmente il service
systemctl start pve-monitor.service

# Verifica che pvesh funzioni
pvesh get /nodes/$(hostname)/status --output-format json
```

### Verifica Ricezione Graylog

- Verifica che l'input UDP 4514 sia attivo in **System > Inputs**
- Controlla che i messaggi arrivino in **Search** filtrando per `source:<hostname-agent>`
- Se i campi JSON non vengono estratti, verifica che l'extractor o le pipeline rules siano configurati correttamente
