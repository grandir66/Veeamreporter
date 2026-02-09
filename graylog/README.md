# Configurazione Graylog per Backup Monitor

## Setup Input

1. **System > Inputs > Select Input: Syslog UDP**
2. **Launch new input:**
   - Title: `Backup Monitor`
   - Bind address: `0.0.0.0`
   - Port: `4514`
   - Store full message: abilitato

## Come arrivano i messaggi

Il campo `message` in Graylog contiene il formato:

```text
VEEAM_JOB_RESULT - {"message_type":"VEEAM_JOB_RESULT","status":"success",...}
```

Il JSON NON e l'intero campo `message` ma e preceduto dal MSGID e ` - `.
Per questo servono due step di estrazione: prima isolare il JSON con regex, poi parsarlo.

## Metodo A - Extractors (consigliato)

Importa gli extractors sull'input creato:

1. Vai su **System > Inputs**
2. Clicca **Manage extractors** sull'input `Backup Monitor`
3. **Actions > Import extractors**
4. Incolla il contenuto di `extractors.json`

Gli extractors funzionano in sequenza:

1. **Estrai JSON da syslog** (regex) - Estrae `{...}` dal campo `message` e lo salva in `backup_json`
2. **Parsa campi JSON** (json) - Parsa `backup_json` e crea i campi individuali con flattening

Dopo l'import, verifica che i campi vengano estratti in **Search** cercando un messaggio recente.

## Metodo B - Pipeline Rules (alternativo, piu flessibile)

Se preferisci le pipeline rules:

1. **System > Pipelines > Manage rules**
2. Crea ogni regola da `pipeline-rules.txt` (8 regole totali)
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
- `PVE_BACKUP_RESULT` - Risultato task vzdump
- `PVE_BACKUP_COVERAGE` - VM/CT senza backup schedulato
- `PVE_DAILY_REPORT` - Report giornaliero (07:00)

## Struttura JSON

Tutti i messaggi contengono:

```json
{
  "message_type": "VEEAM_JOB_RESULT",
  "version": "2.0.0",
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
