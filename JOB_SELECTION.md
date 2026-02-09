# Selezione Job nel Report Ogni 30 Minuti

Questo documento spiega quali job/task di backup vengono inclusi nel report inviato ogni 30 minuti da ciascun agent.

## Panoramica

Ogni agent esegue un ciclo di monitoraggio ogni 30 minuti e invia messaggi syslog con i risultati dei job di backup. La logica di selezione varia leggermente tra i diversi agent.

**Parametro comune:** Tutti gli agent utilizzano un parametro `lookback_hours` (default: **24 ore**) per determinare la finestra temporale di ricerca.

---

## 1. Agent Veeam (Windows)

**File:** `agents/veeam/VeeamBackupMonitor.ps1`  
**Funzione:** `Get-VeeamJobResults`

### Logica di Selezione

**Comportamento:** L'agent Veeam invia **TUTTI i job configurati**, ma con livelli di dettaglio diversi:

#### A) Job con Sessione Completata nelle Ultime 24h

Se un job ha una sessione completata (`EndTime`) nelle ultime `lookback_hours` (default 24h):

✅ **Viene incluso** con **dettagli completi**:
- Nome job, tipo, ID
- Timestamp inizio/fine
- Durata
- Risultato (success/warning/failed)
- Dimensione dati processati e trasferiti
- Bottleneck rilevato
- Dettaglio di ogni VM/oggetto processato (con eventuali errori)
- Se è un retry

**Criteri:**
- `session.EndTime > (now - lookback_hours)`
- `session.EndTime != null` (sessione completata)

#### B) Job SENZA Sessione Recente

Se un job **non ha** sessioni completate nelle ultime 24h:

✅ **Viene comunque incluso** ma con **solo stato base**:
- Nome job, tipo, ID
- Stato corrente: `running`, `idle`, `success`, `warning`, `failed`, `unknown`
- Messaggio: "In esecuzione" o "Ultima esecuzione oltre 24h fa: [risultato]"

**Motivo:** Permette di monitorare anche job che non sono stati eseguiti di recente o che sono attualmente in esecuzione.

### Tipi di Job Inclusi

- ✅ Backup jobs
- ✅ Replica jobs  
- ✅ Qualsiasi tipo di job Veeam (`Get-VBRJob` restituisce tutti i job)

### Esempio

Se hai 10 job configurati:
- 3 job hanno eseguito backup nelle ultime 24h → **3 messaggi con dettagli completi**
- 7 job non hanno eseguito backup nelle ultime 24h → **7 messaggi con solo stato**

**Totale:** 10 messaggi `VEEAM_JOB_RESULT` ogni 30 minuti

---

## 2. Agent PBS (Proxmox Backup Server)

**File:** `agents/pbs/pbs_monitor.py`  
**Funzione:** `collect_backup_tasks`

### Logica di Selezione

**Comportamento:** L'agent PBS invia **SOLO i task di backup completati** nelle ultime `lookback_hours`.

#### Criteri di Inclusione

Un task viene incluso se:

1. ✅ `worker_type` inizia con `"backup"` (es. `"backup"`, `"backup-sync"`)
2. ✅ `endtime` è presente (task completato)
3. ✅ `starttime >= (now - lookback_hours)` (default: ultime 24h)

**Nota:** Solo task **completati** vengono inclusi. Task ancora in esecuzione vengono ignorati.

### Tipi di Task Inclusi

- ✅ Backup VM (`vm/...`)
- ✅ Backup Container (`ct/...`)
- ✅ Backup Host (`host/...`)
- ✅ Qualsiasi task con `worker_type` che inizia con `"backup"`

### Esempio

Se nelle ultime 24h ci sono stati:
- 5 backup completati → **5 messaggi `PBS_BACKUP_RESULT`**
- 2 backup ancora in esecuzione → **0 messaggi** (verranno inclusi quando completano)

**Totale:** N messaggi (dove N = numero di backup completati nelle ultime 24h)

---

## 3. Agent PVE (Proxmox VE)

**File:** `agents/pve/pve_monitor.py`  
**Funzione:** `collect_backup_results`

### Logica di Selezione

**Comportamento:** L'agent PVE invia **SOLO i task vzdump completati** nelle ultime `lookback_hours`.

#### Criteri di Inclusione

Un task viene incluso se:

1. ✅ `typefilter="vzdump"` (solo task di backup vzdump)
2. ✅ `status == "stopped"` (task completato)
3. ✅ `starttime >= (now - lookback_hours)` (default: ultime 24h)

**Nota:** Solo task **completati** vengono inclusi. Task ancora in esecuzione vengono ignorati.

### Tipi di Task Inclusi

- ✅ Backup VM (`qemu`)
- ✅ Backup Container (`lxc`)
- ✅ Qualsiasi task vzdump completato

### Esempio

Se nelle ultime 24h ci sono stati:
- 8 backup vzdump completati → **8 messaggi `PVE_BACKUP_RESULT`**
- 1 backup ancora in esecuzione → **0 messaggi** (verrà incluso quando completa)

**Totale:** N messaggi (dove N = numero di backup vzdump completati nelle ultime 24h)

---

## Confronto Comportamenti

| Agent | Job Inclusi | Criterio Principale | Dettaglio |
|-------|------------|---------------------|-----------|
| **Veeam** | **Tutti i job** | Tutti i job configurati | Dettagli completi se sessione recente, altrimenti solo stato |
| **PBS** | **Solo completati** | Task backup completati nelle ultime 24h | Dettagli completi |
| **PVE** | **Solo completati** | Task vzdump completati nelle ultime 24h | Dettagli completi |

---

## Configurazione Lookback

Il parametro `lookback_hours` può essere configurato in:

### Veeam
```json
{
  "veeam": {
    "lookback_hours": 24
  }
}
```

### PBS
```yaml
pbs:
  lookback_hours: 24
```

### PVE
```yaml
pve:
  lookback_hours: 24
```

**Default:** 24 ore se non specificato.

**Effetto:** Ridurre questo valore (es. a 12h) includerà meno job/task nel report. Aumentarlo (es. a 48h) includerà più job/task.

---

## Messaggi Inviati

### Ogni 30 Minuti (Monitoraggio Standard)

**Veeam:**
- `VEEAM_JOB_RESULT` - Uno per ogni job (con o senza sessione recente)

**PBS:**
- `PBS_SERVER_STATUS` - Stato server
- `PBS_DATASTORE_STATUS` - Stato datastore (uno per datastore)
- `PBS_BACKUP_RESULT` - Uno per ogni backup completato nelle ultime 24h

**PVE:**
- `PVE_NODE_STATUS` - Stato nodo
- `PVE_STORAGE_STATUS` - Stato storage (uno per storage)
- `PVE_BACKUP_RESULT` - Uno per ogni backup vzdump completato nelle ultime 24h
- `PVE_BACKUP_COVERAGE` - VM/CT senza backup schedulato

### Alle 07:00 (Report Giornaliero)

Tutti gli agent inviano anche un report giornaliero (`*_DAILY_REPORT`) con riepilogo aggregato di tutti i job/task nelle ultime 24h.

---

## Note Importanti

1. **Veeam è diverso:** Include sempre tutti i job, anche senza sessioni recenti. Questo permette di monitorare job che non vengono eseguiti frequentemente.

2. **PBS e PVE sono simili:** Includono solo task completati. Se un backup è ancora in esecuzione, verrà incluso nel prossimo ciclo (quando completa).

3. **Duplicati:** Lo stesso job/task può essere incluso in più cicli se completato nelle ultime 24h. Questo è normale e permette di tracciare lo stato nel tempo.

4. **Performance:** Veeam carica tutte le sessioni una sola volta per ottimizzare le performance (evita N query al database).

5. **Limiti:** PBS ha un limite di 500 task per query. Se ci sono più di 500 backup nelle ultime 24h, alcuni potrebbero non essere inclusi.
