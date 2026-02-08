# Configurazione Graylog per Backup Monitor

## Setup Input

1. **System → Inputs → Select Input: Syslog UDP**
2. **Launch new input:**
   - Title: `Backup Monitor`
   - Bind address: `0.0.0.0`
   - Port: `4514`
   - Store full message: ✓

## Tipi di Messaggio

Gli agent inviano questi tipi di messaggio (campo `message_type`):

### Veeam Agent
- `VEEAM_SERVER_STATUS` - Stato server Veeam
- `VEEAM_REPOSITORY_STATUS` - Stato repository
- `VEEAM_JOB_RESULT` - Risultato job backup

### PBS Agent
- `PBS_SERVER_STATUS` - Stato server PBS
- `PBS_DATASTORE_STATUS` - Stato datastore
- `PBS_BACKUP_RESULT` - Risultato task backup

## Struttura JSON

Tutti i messaggi contengono:
```json
{
  "message_type": "VEEAM_JOB_RESULT",
  "version": "2.0.0",
  "timestamp": "2024-01-15T08:30:00.000Z",
  "client": {
    "code": "CLI001",
    "name": "Nome Cliente",
    "site": "sede"
  },
  "agent_hostname": "backup-server",
  "status": "success|warning|failed",
  // ... campi specifici per tipo messaggio
}
```

## Configurazione Extractors/Pipeline

Vedi i file in questa directory:
- `extractors.json` - Extractors da importare
- `pipeline-rules.txt` - Pipeline rules per parsing avanzato
