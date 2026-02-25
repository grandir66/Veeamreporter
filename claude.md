# Veeam Reporter - Documentazione Progetto

## Panoramica

**Veeam Reporter** è un sistema di monitoraggio distribuito per backup che raccoglie metriche e risultati da diverse piattaforme di backup (Veeam Backup & Replication, Proxmox Backup Server, Proxmox VE) e li invia a Graylog tramite syslog UDP in formato JSON strutturato.

### Scopo

Il sistema permette di centralizzare il monitoraggio di:
- Stato dei server di backup (CPU, memoria, uptime, versione)
- Utilizzo dello storage (repository/datastore con soglie di allarme)
- Risultati dei job/task di backup (successo, warning, errori con dettagli)
- Copertura backup (verifica VM/CT senza backup schedulato)
- Report giornalieri aggregati

### Architettura

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│  Veeam B&R      │     │  PBS Server     │     │  Proxmox VE     │
│  (Windows)      │     │  (Linux)        │     │  (Linux)        │
└────────┬────────┘     └────────┬────────┘     └────────┬────────┘
         │                       │                       │
         ▼                       ▼                       ▼
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│  Agent PS1      │     │  Agent Python   │     │  Agent Python   │
│  (PowerShell)   │     │  (pbs_monitor)  │     │  (pve_monitor)  │
└────────┬────────┘     └────────┬────────┘     └────────┬────────┘
         │                       │                       │
         └───────────────────────┴───────────────────────┘
                                 │
                                 ▼
                    ┌────────────────────────┐
                    │ Syslog TCP 4514        │
                    │ GELF UDP 8514 (Veeam)  │
                    │ RFC 5424 + JSON        │
                    └────────────┬───────────┘
                                 │
                                 ▼
                    ┌────────────────────────┐
                    │      Graylog           │
                    │  (Extractors/Pipelines)│
                    └────────────┬───────────┘
                                 │
                                 ▼
                    ┌────────────────────────┐
                    │  Dashboard & Alerts    │
                    └────────────────────────┘
```

## Componenti Principali

### 1. Agent Veeam (Windows)

**File:** `agents/veeam/VeeamBackupMonitor.ps1`

- **Linguaggio:** PowerShell 5.1+
- **Dipendenze:** Veeam Backup & Replication PowerShell Module
- **Esecuzione:** Scheduled Task Windows ogni 30 minuti
- **Configurazione:** `config.json` (creato da `config.example.json`)

**Funzionalità:**
- Raccoglie stato server Veeam (CPU, RAM, uptime, versione, licenza)
- Verifica stato servizi Windows Veeam
- Monitora repository di backup (spazio, utilizzo)
- Raccoglie risultati job di backup completati (lookback 24h)
- Invia report giornaliero alle 07:00

**Messaggi inviati:**
- `VEEAM_SERVER_STATUS` - Stato server e licenza (GELF 8514)
- `VEEAM_SERVICE_STATUS` - Stato servizi Windows (GELF 8514)
- `VEEAM_REPOSITORY_STATUS` - Spazio repository (GELF 8514)
- `VEEAM_JOB_RESULT` - Risultato singolo job (Syslog 4514)
- `VEEAM_DAILY_REPORT` - Riepilogo giornaliero (GELF 8514)

### 2. Agent PBS (Linux)

**File:** `agents/pbs/pbs_monitor.py`

- **Linguaggio:** Python 3.6+
- **Dipendenze:** `requests`, `pyyaml`
- **Esecuzione:** systemd timer ogni 30 minuti
- **Configurazione:** `/etc/backup-monitor/pbs-config.yaml`

**Funzionalità:**
- Connessione API PBS (porta 8007) tramite token
- Raccoglie stato server PBS (CPU, RAM, uptime, versione)
- Monitora datastore (spazio, utilizzo)
- Raccoglie risultati task backup completati (lookback 24h)
- Invia report giornaliero alle 07:00

**Messaggi inviati:**
- `PBS_SERVER_STATUS` - Stato server
- `PBS_DATASTORE_STATUS` - Spazio datastore
- `PBS_BACKUP_RESULT` - Risultato singolo task
- `PBS_DAILY_REPORT` - Riepilogo giornaliero

### 3. Agent PVE (Linux)

**File:** `agents/pve/pve_monitor.py`

- **Linguaggio:** Python 3.6+
- **Dipendenze:** `pyyaml` (solo, usa `pvesh` nativo)
- **Esecuzione:** systemd timer ogni 30 minuti
- **Configurazione:** `/etc/backup-monitor/pve-config.yaml`

**Funzionalità:**
- Usa `pvesh` CLI locale (non richiede autenticazione)
- Raccoglie stato nodo PVE (CPU, RAM, uptime, versione)
- Monitora storage PVE (local, NFS, CIFS, PBS backend)
- Raccoglie risultati task vzdump completati (lookback 24h)
- Verifica copertura backup (VM/CT senza backup schedulato)
- Invia report giornaliero alle 07:00

**Messaggi inviati:**
- `PVE_NODE_STATUS` - Stato nodo
- `PVE_STORAGE_STATUS` - Spazio storage
- `PVE_BACKUP_RESULT` - Risultato singolo task vzdump
- `PVE_BACKUP_COVERAGE` - VM/CT senza backup
- `PVE_DAILY_REPORT` - Riepilogo giornaliero

### 4. Configurazione Graylog

**Directory:** `graylog/`

- **extractors.json** - Extractors JSON per parsing automatico
- **pipeline-rules.txt** - Pipeline rules per parsing avanzato
- **README.md** - Istruzioni setup Graylog

## Formato Messaggi Syslog

### Protocollo

- **Standard:** RFC 5424
- **Trasporto:** UDP
- **Porta:** 4514 (configurabile)
- **Payload:** JSON nel campo `message` del syslog

### Struttura Messaggio

```
<priority>1 timestamp hostname app-name procid msgid - {json_payload}
```

**Priority:** `(facility * 8) + severity`
- Facility: local0-local7 (default: local0 = 16)
- Severity: 6=Info (success), 4=Warning, 3=Error (failed)

### Campi Comuni (tutti i messaggi)

```json
{
  "message_type": "VEEAM_SERVER_STATUS",
  "version": "2.16.3",
  "timestamp": "2026-02-09T10:30:00.000Z",
  "client": {
    "code": "CLI001",
    "name": "Azienda Srl",
    "site": "sede-principale"
  },
  "agent_hostname": "veeam-server-01",
  "status": "success|warning|failed"
}
```

### Mapping Status → Severity

| Status   | Severity | Significato     |
|----------|----------|-----------------|
| success  | 6        | Informational   |
| warning  | 4        | Warning         |
| failed   | 3        | Error           |

### Soglie Storage

| Utilizzo | Status   | Severity |
|----------|----------|----------|
| 0-90%    | success  | 6        |
| 90-95%   | warning  | 4        |
| > 95%    | failed   | 3        |

## Convenzioni di Codice

### PowerShell (Veeam Agent)

- **Error Handling:** `$ErrorActionPreference = "Stop"` per fail-fast
- **Logging:** Funzione `Write-Log` con livelli Info/Warning/Error
- **Configurazione:** JSON caricato all'inizio, validato
- **Syslog:** Funzione `Send-Syslog` con formato RFC 5424
- **Test Mode:** Flag `-TestMode` stampa messaggi senza inviarli

### Python (PBS/PVE Agents)

- **Logging:** Modulo `logging` standard con formato strutturato
- **Configurazione:** YAML caricato con `pyyaml`
- **Error Handling:** Try/except con logging degli errori
- **Syslog:** Classe `SyslogSender` riutilizzabile
- **Test Mode:** Flag `--test` stampa messaggi senza inviarli
- **Type Hints:** Utilizzati per migliorare la leggibilità

### Struttura Configurazione

**Veeam (JSON):**
```json
{
  "client": {
    "code": "CLI001",
    "name": "Azienda Srl",
    "site": "sede-principale"
  },
  "syslog": {
    "server": "graylog.example.com",
    "port": 4514,
    "facility": "local0",
    "protocol": "tcp"
  },
  "gelf": {
    "server": "graylog.example.com",
    "port": 8514,
    "protocol": "udp"
  },
  "veeam": {
    "lookback_hours": 24
  },
  "log_path": "C:\\BackupMonitor\\Logs"
}
```

**PBS/PVE (YAML):**
```yaml
client:
  code: CLI001
  name: Azienda Srl
  site: sede-principale

syslog:
  server: graylog.example.com
  port: 4514
  facility: local0

pbs:  # o pve
  host: pbs.example.com
  port: 8007
  user: monitor@pbs
  token_name: monitor-token
  token_value: "..."
  verify_ssl: false
  lookback_hours: 24
```

## Installazione e Deployment

### Veeam Agent

1. Copia file da `agents/veeam/` in `C:\BackupMonitor\`
2. Esegui `Install-Task.ps1` come amministratore
3. Configurazione interattiva (codice cliente, Graylog server)
4. Task schedulato creato automaticamente

### PBS Agent

1. Scarica repository
2. Esegui `install.sh` come root
3. Configurazione interattiva (codice cliente, credenziali PBS, Graylog)
4. Timer systemd creato e attivato automaticamente

### PVE Agent

1. Scarica repository
2. Esegui `install.sh` come root
3. Configurazione interattiva (codice cliente, Graylog)
4. Timer systemd creato e attivato automaticamente

## Testing e Debug

### Modalità Test

Tutti gli agent supportano una modalità test che stampa i messaggi syslog senza inviarli:

**Veeam:**
```powershell
.\VeeamBackupMonitor.ps1 -TestMode
```

**PBS/PVE:**
```bash
python3 pbs_monitor.py -c config.yaml --test
```

### Verifica Log

**Veeam:** `C:\BackupMonitor\Logs\veeam-monitor-YYYY-MM-DD.log`

**PBS/PVE:** `journalctl -u pbs-monitor.service` o `journalctl -u pve-monitor.service`

## Versioning

- **Versione corrente:** 2.16.3
- **Formato:** SemVer (Major.Minor.Patch)
- **Campo versione:** Incluso in ogni messaggio syslog

## Aggiornamento

### 1. Scarica la versione aggiornata

```bash
cd /path/to/Veeamreporter
git pull origin main
```

### 2. Aggiorna i client

**Veeam (Windows):**
```powershell
# Copia il file aggiornato (da un PC con il repo)
Copy-Item .\agents\veeam\VeeamBackupMonitor.ps1 -Destination \\SERVER\C$\BackupMonitor\

# Oppure download diretto da GitHub
Invoke-WebRequest -Uri "https://raw.githubusercontent.com/grandir66/Veeamreporter/main/agents/veeam/VeeamBackupMonitor.ps1" -OutFile "C:\BackupMonitor\VeeamBackupMonitor.ps1" -UseBasicParsing
```

**PVE (Linux):**
```bash
scp agents/pve/pve_monitor.py root@NODO-PVE:/usr/local/bin/
# oppure dove è installato (es. /opt/backup-monitor/)
```

**PBS (Linux):**
```bash
scp agents/pbs/pbs_monitor.py root@PBS-SERVER:/usr/local/bin/
```

### 3. Nessun riavvio necessario

Il Scheduled Task (Veeam) e i timer systemd (PVE/PBS) useranno automaticamente la nuova versione alla prossima esecuzione.

## Repository Git

- **URL:** https://github.com/grandir66/Veeamreporter.git
- **Branch principale:** `main`
- **File esclusi:** `config.json` (non committato, solo `config.example.json`)

## Estensioni Future

Possibili miglioramenti:
- Supporto per altre piattaforme di backup
- Metriche aggiuntive (throughput, deduplicazione)
- Alerting integrato (non solo via Graylog)
- API REST per query stato
- Dashboard web standalone

## Note Tecniche

- **Syslog UDP:** Non garantisce delivery, ma è performante e semplice
- **Lookback:** Default 24h per job completati, configurabile
- **Timezone:** Tutti i timestamp in UTC ISO 8601
- **JSON:** Compresso (no spazi) per efficienza
- **Facility Syslog:** Configurabile per separare log da altri sistemi
