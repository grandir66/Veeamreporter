# Configurazione Graylog per Backup Monitor

Gli agent inviano messaggi su due canali:

- **Syslog TCP 4514:** Job backup e risultati (formato RFC 5424 + JSON)
- **GELF UDP 8514:** Heartbeat e stato server (solo agent Veeam)

## Setup Input

### Input Syslog TCP (porta 4514)

1. **System > Inputs > Select Input: Syslog TCP**
2. **Launch new input:**
   - Title: `Backup Monitor`
   - Bind address: `0.0.0.0`
   - Port: `4514`
   - Store full message: abilitato

### Input GELF UDP (porta 8514 – solo Veeam)

1. **System > Inputs > Select Input: GELF UDP**
2. **Launch new input:**
   - Title: `Backup Monitor GELF`
   - Bind address: `0.0.0.0`
   - Port: `8514`
   - Store full message: abilitato

L'agent Veeam invia su GELF i messaggi di heartbeat: `VEEAM_SERVER_STATUS`, `VEEAM_SERVICE_STATUS`, `VEEAM_REPOSITORY_STATUS`, `VEEAM_DAILY_REPORT`. I risultati job (`VEEAM_JOB_RESULT`) arrivano su Syslog TCP.

### Come arrivano i messaggi

**Syslog (porta 4514):** Il campo `message` contiene:

```text
PVE_BACKUP_JOB - {"message_type":"PVE_BACKUP_JOB","status":"success",...}
```

Il JSON è preceduto dal MSGID e ` - `. Servono extractors o pipeline rules per estrarre i campi.

**GELF (porta 8514):** I messaggi arrivano già in formato GELF nativo. Il campo `short_message` contiene il tipo (es. `VEEAM_SERVER_STATUS : success`), i campi custom sono prefissati con `_`.

## Metodo A - Extractors

Importa gli extractors sull'input creato:

1. Vai su **System > Inputs**
2. Clicca **Manage extractors** sull'input `Backup Monitor`
3. **Actions > Import extractors**
4. Incolla il contenuto di `extractors.json`

Gli extractors funzionano in sequenza:

1. **Estrai JSON da syslog** (regex) - Estrae `{...}` dal campo `message` e lo salva in `backup_json`
2. **Parsa campi JSON** (json) - Parsa `backup_json` e crea i campi individuali con flattening

Dopo l'import, verifica che i campi vengano estratti in **Search** cercando un messaggio recente.

## Metodo B - Pipeline Rules (alternativo, più flessibile)

Se preferisci le pipeline rules:

1. **System > Pipelines > Manage rules**
2. Crea ogni regola da `pipeline-rules.txt` (9 regole totali)
3. **System > Pipelines > Add new pipeline**: `Backup Monitor Processing`
4. Aggiungi le regole alla pipeline:
   - **Stage 0**: `backup_monitor_parse_json` (estrae JSON e campi base)
   - **Stage 1**: tutte le altre regole (parse campi specifici per tipo messaggio)
5. **Collega la pipeline** allo stream `All messages` o a uno stream dedicato

**Importante**: Se usi le pipeline rules, **non** usare anche gli extractors sullo stesso input per evitare conflitti.

## Tipi di Messaggio

Gli agent inviano questi tipi di messaggio (campo `message_type`):

### Veeam Agent

- `VEEAM_SERVER_STATUS` - Stato server Veeam
- `VEEAM_SERVICE_STATUS` - Stato servizi Windows Veeam
- `VEEAM_REPOSITORY_STATUS` - Stato repository
- `VEEAM_JOB_RESULT` - Risultato job backup
- `VEEAM_DAILY_REPORT` - Report giornaliero (07:00)

### PBS Agent

- `PBS_SERVER_STATUS` - Stato server PBS
- `PBS_DATASTORE_STATUS` - Stato datastore
- `PBS_BACKUP_RESULT` - Risultato task backup
- `PBS_DAILY_REPORT` - Report giornaliero (07:00)

### PVE Agent

- `PVE_NODE_STATUS` - Stato nodo Proxmox VE
- `PVE_STORAGE_STATUS` - Stato storage
- `PVE_BACKUP_JOB` - Configurazione job backup con elenco VM/CT
- `PVE_BACKUP_RESULT` - Risultato task vzdump
- `PVE_BACKUP_COVERAGE` - VM/CT senza backup schedulato
- `PVE_DAILY_REPORT` - Report giornaliero (07:00)

## Struttura JSON

Tutti i messaggi contengono:

```json
{
  "message_type": "VEEAM_JOB_RESULT",
  "version": "2.16.3",
  "timestamp": "2026-02-08T08:30:00.000Z",
  "client": {
    "code": "CLI001",
    "name": "Nome Cliente",
    "site": "sede"
  },
  "agent_hostname": "backup-server",
  "status": "success"
}
```

Ogni tipo di messaggio aggiunge campi specifici (vedi README principale per il dettaglio completo).
