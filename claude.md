> Regole comuni a tutti i progetti → `~/.claude/CLAUDE.md`

# CLAUDE.md — Veeam Reporter

Sistema di monitoraggio distribuito per backup: agent su Veeam B&R, Proxmox Backup Server e Proxmox VE raccolgono metriche/risultati e li inviano a **Graylog** via syslog/GELF in JSON strutturato.
**Non è:** una piattaforma con backend proprio (non ha DB/UI; dashboard e alert vivono in Graylog). Slegato da DA-Vul-can/DA-IPAM, nessun modulo condiviso.

## Pointer

- Config Graylog (extractors/pipeline/setup): [graylog/](graylog/) — `extractors.json`, `pipeline-rules.txt`, `README.md`
- Skills (workflow): — (assenti)
- Rules file-scoped: — (assenti)
- ADR: — (assenti)
- Regole comuni a tutti i progetti: `~/.claude/CLAUDE.md`

## Stack vincolante

- **Agent Veeam** ([agents/veeam/VeeamBackupMonitor.ps1](agents/veeam/VeeamBackupMonitor.ps1)): PowerShell 5.1+ · modulo Veeam Backup & Replication · config `config.json`.
- **Agent PBS** ([agents/pbs/pbs_monitor.py](agents/pbs/pbs_monitor.py)): Python 3.6+ · `requests`, `pyyaml` · config `/etc/backup-monitor/pbs-config.yaml`.
- **Agent PVE** ([agents/pve/pve_monitor.py](agents/pve/pve_monitor.py)): Python 3.6+ · solo `pyyaml`, usa `pvesh` CLI nativo (no auth) · config `/etc/backup-monitor/pve-config.yaml`.
- Versione corrente **2.16.3** · SemVer · campo `version` incluso in ogni messaggio.

**Trappole:** syslog UDP non garantisce delivery (scelto per performance/semplicità) · tutti i timestamp in **UTC ISO 8601** · PVE usa `pvesh` locale quindi va eseguito *sul nodo* PVE.

## Comandi essenziali

```powershell
.\VeeamBackupMonitor.ps1 -TestMode          # stampa i messaggi senza inviarli
.\Install-Task.ps1                           # crea Scheduled Task (admin), config interattiva
```

```bash
python3 pbs_monitor.py -c config.yaml --test # stampa i messaggi senza inviarli (PBS/PVE)
sudo ./install.sh                            # crea timer systemd, config interattiva (PBS/PVE)
```

Esecuzione runtime: Scheduled Task (Veeam) / systemd timer (PBS/PVE) ogni 30 min; report giornaliero alle 07:00.

## Architettura

- **3 agent → Graylog.** Veeam (Windows, PS1) · PBS (Linux, Python API porta 8007 via token) · PVE (Linux, Python via `pvesh`).
- **Trasporto:** Syslog TCP/UDP **4514** (RFC 5424, JSON nel campo `message`) · GELF UDP **8514** (usato dall'agent Veeam per status/repository/daily).
- **Priority syslog** = `facility*8 + severity`; facility default `local0`(16).
- **Mapping status→severity:** `success`→6 (Info) · `warning`→4 · `failed`→3.
- **Soglie storage:** 0-90% success · 90-95% warning · >95% failed.
- **Campi comuni** in ogni payload: `message_type`, `version`, `timestamp`, `client{code,name,site}`, `agent_hostname`, `status`.
- **Message types:** Veeam `VEEAM_{SERVER,SERVICE,REPOSITORY}_STATUS|JOB_RESULT|DAILY_REPORT` · PBS `PBS_{SERVER_STATUS,DATASTORE_STATUS,BACKUP_RESULT,DAILY_REPORT}` · PVE `PVE_{NODE_STATUS,STORAGE_STATUS,BACKUP_RESULT,BACKUP_COVERAGE,DAILY_REPORT}`.
- Lookback job completati default 24h (configurabile).

## Regole anti-regressione (CRITICHE — violazione = bug latente)

1. **Versione in ogni messaggio**: bump `version` (SemVer) ad ogni modifica agli agent; il campo è incluso in ogni payload syslog e usato per tracciare la release attiva.
2. **`config.json` mai committato**: solo `config.example.json` nel repo (contiene token PBS in chiaro). Vedi `.gitignore`.
3. **Timestamp UTC ISO 8601** sempre: Graylog assume UTC; orari locali rompono extractors/pipeline.
4. **JSON compresso** (no spazi) nel payload syslog per efficienza — non riformattare.
5. **Test prima dell'invio**: validare con `-TestMode` / `--test` (stampa senza inviare) prima di deployare su client.
6. **Status→severity coerente**: mantenere il mapping (success=6, warning=4, failed=3); gli alert Graylog dipendono da queste severity.
7. **Aggiornamento client = copia file**: nessun riavvio necessario, Scheduled Task/timer usano la nuova versione alla prossima esecuzione.

## Verifica obbligatoria post-modifica

```bash
# Veeam
.\VeeamBackupMonitor.ps1 -TestMode
# PBS/PVE
python3 pbs_monitor.py -c config.yaml --test
# Log runtime
#   Veeam: C:\BackupMonitor\Logs\veeam-monitor-YYYY-MM-DD.log
#   PBS/PVE: journalctl -u pbs-monitor.service | journalctl -u pve-monitor.service
```

## File critici (non rompere senza migration)

| File | Perché |
| --- | --- |
| [agents/veeam/VeeamBackupMonitor.ps1](agents/veeam/VeeamBackupMonitor.ps1) | Agent Windows; `Send-Syslog` RFC 5424 + GELF, raccolta stato/job |
| [agents/pbs/pbs_monitor.py](agents/pbs/pbs_monitor.py) | Agent PBS via API token (porta 8007); classe `SyslogSender` |
| [agents/pve/pve_monitor.py](agents/pve/pve_monitor.py) | Agent PVE via `pvesh`; include backup-coverage VM/CT |
| [graylog/extractors.json](graylog/extractors.json) · [graylog/pipeline-rules.txt](graylog/pipeline-rules.txt) | Parsing lato Graylog; il formato payload deve restare allineato |
| `config.example.json` / `*-config.yaml` | Schema config: `client`, `syslog{server,port,facility}`, `gelf`, sezione `veeam|pbs|pve` (host/token/lookback) |

## Loop di apprendimento (fine sessione)

Prima di chiudere, valuta:

- **Pattern riutilizzabile?** → nuova rule in `.claude/rules/` (scope file) o skill in `.claude/skills/` (workflow)
- **Errore commesso 2+ volte?** → nuova regola anti-regressione qui sopra
- **Decisione architetturale?** → ADR in `docs/adr/`
- **Fatto nuovo sul progetto?** → aggiorna questo file
- **Regola comportamentale trasversale?** → `~/.claude/CLAUDE.md` (vale per tutti)
