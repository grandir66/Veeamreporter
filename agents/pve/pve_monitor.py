#!/usr/bin/env python3
"""
Proxmox VE Backup Monitor - Invia stato backup e server a Graylog via Syslog

Raccoglie dati direttamente dal nodo PVE tramite pvesh (no autenticazione necessaria):
- Stato del nodo (CPU, memoria, uptime)
- Stato degli storage
- Risultati task di backup (vzdump)
- Copertura backup (VM/CT senza backup schedulato)
"""

import argparse
import json
import logging
import os
import re
import socket
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import yaml

VERSION = "2.0.0"
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


class SyslogSender:
    """Invia messaggi syslog RFC 5424 via TCP o UDP"""

    FACILITY_MAP = {
        "local0": 16, "local1": 17, "local2": 18, "local3": 19,
        "local4": 20, "local5": 21, "local6": 22, "local7": 23
    }

    def __init__(self, server: str, port: int, facility: str = "local0", protocol: str = "tcp"):
        self.server = server
        self.port = port
        self.facility = self.FACILITY_MAP.get(facility, 16)
        self.protocol = protocol.lower()

    def send(self, message_type: str, data: Dict, client: Dict, test_mode: bool = False):
        """Invia messaggio syslog con payload JSON"""

        status = data.get("status", "success")
        severity = {"success": 6, "warning": 4, "failed": 3}.get(status, 6)
        priority = (self.facility * 8) + severity

        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        hostname = socket.gethostname()

        payload = {
            "message_type": message_type,
            "version": VERSION,
            "timestamp": timestamp,
            "client": client,
            "agent_hostname": hostname,
            **data
        }

        json_payload = json.dumps(payload, separators=(",", ":"), default=str)
        syslog_msg = f"<{priority}>1 {timestamp} {hostname} pve-backup-monitor {os.getpid()} {message_type} - {json_payload}"

        if test_mode:
            print(f"\n=== SYSLOG MESSAGE ({len(syslog_msg)} bytes) ===\n{syslog_msg}\n======================\n")
            return

        try:
            if self.protocol == "tcp":
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(10)
                sock.connect((self.server, self.port))
                sock.sendall(syslog_msg.encode("utf-8") + b"\n")
                sock.close()
            else:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.sendto(syslog_msg.encode("utf-8"), (self.server, self.port))
                sock.close()
            logger.info(f"Syslog inviato ({self.protocol.upper()}): {message_type} ({len(syslog_msg)} bytes)")
        except Exception as e:
            logger.error(f"Errore invio syslog: {e}")


def pvesh_get(path: str, **params) -> Any:
    """Esegue pvesh get e ritorna il risultato JSON"""
    cmd = ["pvesh", "get", path, "--output-format", "json"]
    for k, v in params.items():
        cmd.extend([f"--{k.replace('_', '-')}", str(v)])

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"pvesh error: {result.stderr.strip()}")
    return json.loads(result.stdout)


def get_node_name() -> str:
    """Rileva il nome del nodo PVE locale"""
    return socket.gethostname()


def get_pve_version() -> str:
    """Ottiene la versione di Proxmox VE"""
    try:
        result = subprocess.run(
            ["pveversion"], capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            # Output: "pve-manager/8.1.3/abc123 (running kernel: 6.5.11-8-pve)"
            match = re.match(r"pve-manager/([\d.]+)", result.stdout.strip())
            if match:
                return match.group(1)
        return result.stdout.strip()
    except Exception:
        return "unknown"


def read_proc_uptime() -> float:
    """Legge uptime in ore da /proc/uptime"""
    with open("/proc/uptime") as f:
        return float(f.read().split()[0]) / 3600


def read_proc_loadavg() -> List[float]:
    """Legge load average da /proc/loadavg"""
    with open("/proc/loadavg") as f:
        parts = f.read().split()
        return [float(parts[0]), float(parts[1]), float(parts[2])]


def read_proc_meminfo() -> Dict[str, int]:
    """Legge informazioni memoria da /proc/meminfo (valori in bytes)"""
    mem = {}
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith(("MemTotal:", "MemAvailable:")):
                key, value = line.split(":")
                # Valore in kB, converti in bytes
                mem[key.strip()] = int(value.strip().split()[0]) * 1024
    return mem


def read_proc_cpu() -> float:
    """Legge utilizzo CPU da /proc/stat (media su 1 secondo)"""
    def read_stat():
        with open("/proc/stat") as f:
            parts = f.readline().split()
            # user, nice, system, idle, iowait, irq, softirq, steal
            idle = int(parts[4]) + int(parts[5])
            total = sum(int(p) for p in parts[1:])
            return idle, total

    idle1, total1 = read_stat()
    import time
    time.sleep(1)
    idle2, total2 = read_stat()

    idle_delta = idle2 - idle1
    total_delta = total2 - total1
    if total_delta == 0:
        return 0.0
    return round((1 - idle_delta / total_delta) * 100, 1)


def collect_node_status(node: str, syslog: SyslogSender, client: Dict, test_mode: bool):
    """Raccoglie e invia stato del nodo PVE"""
    logger.info("Raccolta stato nodo PVE...")

    try:
        meminfo = read_proc_meminfo()
        mem_total = meminfo.get("MemTotal", 0)
        mem_available = meminfo.get("MemAvailable", 0)
        mem_used = mem_total - mem_available
        mem_used_pct = round((mem_used / max(mem_total, 1)) * 100, 1)

        data = {
            "status": "success",
            "server_name": node,
            "pve_version": get_pve_version(),
            "uptime_hours": round(read_proc_uptime(), 1),
            "cpu_percent": read_proc_cpu(),
            "memory_total_bytes": mem_total,
            "memory_used_bytes": mem_used,
            "memory_total_gb": round(mem_total / (1024 ** 3), 2),
            "memory_used_percent": mem_used_pct,
            "load_average": read_proc_loadavg(),
        }

        syslog.send("PVE_NODE_STATUS", data, client, test_mode)
    except Exception as e:
        logger.error(f"Errore raccolta stato nodo: {e}")


def collect_storage_status(node: str, syslog: SyslogSender, client: Dict, test_mode: bool):
    """Raccoglie e invia stato degli storage di backup (uno per cluster, non per host)"""
    logger.info("Raccolta stato storage backup...")

    try:
        # Usa cluster/resources per avere lista deduplicata (non per singolo nodo)
        resources = pvesh_get("/cluster/resources", type="storage")
        
        # Filtra solo storage che contengono backup e deduplica per nome
        seen = set()
        backup_storages = []
        
        for res in resources:
            storage_name = res.get("storage", "")
            if storage_name in seen:
                continue
            
            # Verifica se lo storage supporta backup
            content = res.get("content", "")
            if "backup" not in content:
                continue
            
            seen.add(storage_name)
            
            total = res.get("maxdisk", 0)
            used = res.get("disk", 0)
            avail = total - used if total > 0 else 0
            used_percent = round((used / total * 100), 1) if total > 0 else 0
            
            status = "success"
            if used_percent > 95:
                status = "failed"
            elif used_percent > 90:
                status = "warning"
            
            backup_storages.append({
                "name": storage_name,
                "type": res.get("plugintype", "unknown"),
                "total_gb": round(total / (1024 ** 3), 2) if total > 0 else None,
                "used_gb": round(used / (1024 ** 3), 2) if used > 0 else None,
                "free_gb": round(avail / (1024 ** 3), 2) if avail > 0 else None,
                "used_percent": used_percent,
                "status": status
            })
        
        if backup_storages:
            # Status globale: peggiore tra tutti gli storage
            overall_status = "success"
            for s in backup_storages:
                if s["status"] == "failed":
                    overall_status = "failed"
                    break
                elif s["status"] == "warning":
                    overall_status = "warning"
            
            data = {
                "status": overall_status,
                "storage_count": len(backup_storages),
                "storages": backup_storages
            }
            syslog.send("PVE_STORAGE_STATUS", data, client, test_mode)
            
    except Exception as e:
        logger.error(f"Errore raccolta storage: {e}")


def collect_backup_results(node: str, syslog: SyslogSender, client: Dict,
                           lookback_hours: int, test_mode: bool):
    """Raccoglie e invia risultati task vzdump, raggruppati per job con dettagli per ogni VM"""
    logger.info("Raccolta risultati backup vzdump...")

    try:
        since = int((datetime.now(timezone.utc) - timedelta(hours=lookback_hours)).timestamp())
        tasks = pvesh_get(f"/nodes/{node}/tasks", typefilter="vzdump", since=str(since),
                          limit="500", source="all")

        # Filtra task completati (status può essere "stopped", "job errors", o altri stati di completamento)
        # Escludi solo task ancora in esecuzione ("running")
        completed = [t for t in tasks if t.get("status") != "running"]
        logger.info(f"Trovati {len(completed)} task vzdump completati (su {len(tasks)} totali)")

        # Raggruppa task per job (task che iniziano nello stesso momento con lo stesso utente sono probabilmente dello stesso job)
        jobs_dict = {}
        
        for task in completed:
            starttime = task.get("starttime", 0)
            endtime = task.get("endtime", 0)
            duration = endtime - starttime if endtime and starttime else 0
            vmid = task.get("id", "")
            upid = task.get("upid", "")
            user = task.get("user", "")
            exitstatus = task.get("exitstatus", "")
            
            # Determina status
            if exitstatus == "OK":
                status = "success"
            elif "error" in exitstatus.lower():
                status = "failed"
            else:
                status = "warning"
            
            # Prova a ottenere informazioni sulla VM/CT
            vm_name = f"VM-{vmid}"
            vm_type = "unknown"
            try:
                vm_info = pvesh_get(f"/nodes/{node}/qemu/{vmid}")
                vm_name = vm_info.get("name", f"VM-{vmid}")
                vm_type = "qemu"
            except:
                try:
                    ct_info = pvesh_get(f"/nodes/{node}/lxc/{vmid}")
                    vm_name = ct_info.get("name", f"CT-{vmid}")
                    vm_type = "lxc"
                except:
                    pass
            
            # Prova a ottenere dimensione backup
            backup_size_bytes = 0
            try:
                # Prova a ottenere informazioni dettagliate dal task
                task_details = pvesh_get(f"/nodes/{node}/tasks/{upid}")
                
                # Cerca informazioni sulla dimensione nei dettagli del task
                # Potrebbe essere nel campo "size" o simile
                if "size" in task_details:
                    backup_size_bytes = int(task_details["size"])
                
                # Alternativa: cerca nei log del task
                if backup_size_bytes == 0:
                    try:
                        log_result = subprocess.run(
                            ["pvesh", "get", f"/nodes/{node}/tasks/{upid}/log"],
                            capture_output=True,
                            text=True,
                            timeout=10
                        )
                        if log_result.returncode == 0:
                            log_lines = log_result.stdout.split('\n')
                            # Cerca righe che contengono informazioni sulla dimensione
                            for line in log_lines:
                                # Cerca pattern come "total bytes read: 1234567890" o "backup size: 1234 GB"
                                size_match = re.search(r'(\d+)\s*(bytes?|KB|MB|GB)', line, re.IGNORECASE)
                                if size_match:
                                    size_val = int(size_match.group(1))
                                    unit = size_match.group(2).upper()
                                    if 'GB' in unit:
                                        backup_size_bytes = size_val * (1024**3)
                                    elif 'MB' in unit:
                                        backup_size_bytes = size_val * (1024**2)
                                    elif 'KB' in unit:
                                        backup_size_bytes = size_val * 1024
                                    else:
                                        backup_size_bytes = size_val
                                    break
                    except:
                        pass
            except:
                pass
            
            # Crea chiave per raggruppare: user + timestamp arrotondato a 5 minuti
            # Task dello stesso job iniziano quasi simultaneamente
            time_key = int(starttime / 300) * 300  # Arrotonda a 5 minuti
            job_key = f"{user}_{time_key}"
            
            if job_key not in jobs_dict:
                jobs_dict[job_key] = {
                    "user": user,
                    "start_time": starttime,
                    "end_time": endtime,
                    "vms": [],
                    "task_ids": []
                }
            
            # Aggiorna end_time se questo task è finito dopo
            if endtime > jobs_dict[job_key]["end_time"]:
                jobs_dict[job_key]["end_time"] = endtime
            
            # Aggiungi VM al job
            vm_data = {
                "vmid": vmid,
                "name": vm_name,
                "type": vm_type,
                "status": status,
                "exit_status": exitstatus,
                "start_time": datetime.fromtimestamp(starttime, tz=timezone.utc).isoformat() if starttime else None,
                "end_time": datetime.fromtimestamp(endtime, tz=timezone.utc).isoformat() if endtime else None,
                "duration_seconds": duration,
                "duration_minutes": round(duration / 60, 1),
                "task_id": upid,
                "size_bytes": backup_size_bytes,
                "size_gb": round(backup_size_bytes / (1024**3), 2) if backup_size_bytes > 0 else None
            }
            
            jobs_dict[job_key]["vms"].append(vm_data)
            jobs_dict[job_key]["task_ids"].append(upid)
        
        # Invia un messaggio per ogni job con tutte le VM
        for job_key, job_data in jobs_dict.items():
            vms = job_data["vms"]
            start_time = job_data["start_time"]
            end_time = job_data["end_time"]
            job_duration = end_time - start_time if end_time and start_time else 0
            
            # Calcola statistiche del job
            vms_success = sum(1 for v in vms if v["status"] == "success")
            vms_warning = sum(1 for v in vms if v["status"] == "warning")
            vms_failed = sum(1 for v in vms if v["status"] == "failed")
            total_size_bytes = sum(v.get("size_bytes", 0) for v in vms)
            
            # Status complessivo del job
            if vms_failed > 0:
                job_status = "failed"
            elif vms_warning > 0:
                job_status = "warning"
            else:
                job_status = "success"
            
            data = {
                "status": job_status,
                "job_start_time": datetime.fromtimestamp(start_time, tz=timezone.utc).isoformat() if start_time else None,
                "job_end_time": datetime.fromtimestamp(end_time, tz=timezone.utc).isoformat() if end_time else None,
                "job_duration_seconds": job_duration,
                "job_duration_minutes": round(job_duration / 60, 1),
                "user": job_data["user"],
                "vm_count": len(vms),
                "vms_success": vms_success,
                "vms_warning": vms_warning,
                "vms_failed": vms_failed,
                "total_size_bytes": total_size_bytes if total_size_bytes > 0 else None,
                "total_size_gb": round(total_size_bytes / (1024**3), 2) if total_size_bytes > 0 else None,
                "vms": vms,
                "task_ids": job_data["task_ids"]
            }
            
            syslog.send("PVE_BACKUP_RESULT", data, client, test_mode)
        
        logger.info(f"Processati {len(jobs_dict)} job di backup con {sum(len(j['vms']) for j in jobs_dict.values())} VM totali")

    except Exception as e:
        logger.error(f"Errore raccolta task backup: {e}")


def get_latest_backup_info(storage: str, vmid: str, vm_type: str) -> Dict:
    """Ottiene informazioni sull'ultimo backup di una VM/CT dal repository"""
    try:
        # Cerca nel repository PBS
        content = pvesh_get(f"/nodes/{node}/storage/{storage}/content")
        
        # Filtra backup per questa VM/CT
        vm_backups = []
        for backup in content:
            if backup.get("vmid") == int(vmid) and backup.get("content") == "backup":
                # Verifica che il tipo corrisponda (qemu -> vm, lxc -> ct)
                if vm_type == "qemu" and backup.get("subtype") == "qemu":
                    vm_backups.append(backup)
                elif vm_type == "lxc" and backup.get("subtype") == "lxc":
                    vm_backups.append(backup)
        
        if vm_backups:
            # Ordina per data (più recente prima)
            latest = sorted(vm_backups, key=lambda x: x.get("ctime", 0), reverse=True)[0]
            return {
                "backup_date": datetime.fromtimestamp(latest.get("ctime", 0), tz=timezone.utc).isoformat() if latest.get("ctime") else None,
                "backup_size_bytes": latest.get("size", 0),
                "backup_size_gb": round(latest.get("size", 0) / (1024**3), 2) if latest.get("size", 0) > 0 else None,
                "backup_volid": latest.get("volid", ""),
                "verification_state": latest.get("verification", {}).get("state", "unknown") if latest.get("verification") else None
            }
    except Exception as e:
        logger.debug(f"Errore ottenimento info backup per VM {vmid}: {e}")
    
    return {}


def get_backup_task_info(node: str, vmid: str, lookback_days: int = 7) -> Dict:
    """Ottiene informazioni sul task di backup più recente per una VM"""
    try:
        since = int((datetime.now(timezone.utc) - timedelta(days=lookback_days)).timestamp())
        tasks = pvesh_get(f"/nodes/{node}/tasks", typefilter="vzdump", since=str(since),
                          limit="500", source="all")
        
        # Cerca task per questa VM nei log (il campo "id" nei task è spesso vuoto, dobbiamo cercare nei log)
        # Alternativamente, cerca task che corrispondono per tempo approssimativo
        # Per ora, restituiamo None se non troviamo direttamente
        # Il matching preciso richiederebbe di parsare i log di ogni task
        
        # Cerca nei task completati
        completed = [t for t in tasks if t.get("status") != "running"]
        
        # Prova a ottenere informazioni dal log del task più recente
        # Nota: questo è un approccio semplificato, potrebbe non essere preciso
        for task in sorted(completed, key=lambda x: x.get("endtime", 0), reverse=True)[:10]:
            try:
                upid = task.get("upid", "")
                if not upid:
                    continue
                
                # Prova a ottenere il log per vedere se contiene questa VM
                log_result = subprocess.run(
                    ["pvesh", "get", f"/nodes/{node}/tasks/{upid}/log", "--output-format", "json"],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                if log_result.returncode == 0:
                    import json as json_lib
                    log_data = json_lib.loads(log_result.stdout)
                    log_text = "\n".join([line.get("t", "") for line in log_data if isinstance(log_data, list)])
                    
                    # Cerca riferimenti alla VM nel log
                    if f"VM {vmid}" in log_text or f"CT {vmid}" in log_text or f"vmid={vmid}" in log_text:
                        starttime = task.get("starttime", 0)
                        endtime = task.get("endtime", 0)
                        duration = endtime - starttime if endtime and starttime else 0
                        exitstatus = task.get("exitstatus", "")
                        
                        # Determina status
                        if exitstatus == "OK":
                            status = "success"
                        elif "error" in str(exitstatus).lower() or "failed" in str(task.get("status", "")).lower():
                            status = "failed"
                        else:
                            status = "warning"
                        
                        return {
                            "backup_status": status,
                            "backup_start_time": datetime.fromtimestamp(starttime, tz=timezone.utc).isoformat() if starttime else None,
                            "backup_end_time": datetime.fromtimestamp(endtime, tz=timezone.utc).isoformat() if endtime else None,
                            "backup_duration_seconds": duration,
                            "backup_duration_minutes": round(duration / 60, 1) if duration > 0 else None,
                            "task_upid": upid
                        }
            except:
                continue
    except Exception as e:
        logger.debug(f"Errore ottenimento task backup per VM {vmid}: {e}")
    
    return {}


# Cache globali per evitare chiamate API ripetute
_storage_content_cache: Dict[str, List] = {}
_vzdump_tasks_cache: Dict[str, List] = {}
_cluster_resources_cache: List = None


def clear_caches():
    """Resetta tutte le cache globali"""
    global _storage_content_cache, _vzdump_tasks_cache, _cluster_resources_cache
    _storage_content_cache = {}
    _vzdump_tasks_cache = {}
    _cluster_resources_cache = None
    logger.debug("Cache globali resettate")


def get_storage_content_cached(node: str, storage: str) -> List:
    """Ottiene il contenuto dello storage con cache"""
    cache_key = f"{node}:{storage}"
    if cache_key not in _storage_content_cache:
        try:
            _storage_content_cache[cache_key] = pvesh_get(f"/nodes/{node}/storage/{storage}/content")
            logger.debug(f"Cache storage {cache_key}: {len(_storage_content_cache[cache_key])} elementi")
        except Exception as e:
            logger.debug(f"Errore lettura storage {storage}: {e}")
            _storage_content_cache[cache_key] = []
    return _storage_content_cache[cache_key]


def get_vzdump_tasks_cached(node: str, lookback_days: int = 7) -> List:
    """Ottiene i task vzdump con cache"""
    cache_key = f"{node}:{lookback_days}"
    if cache_key not in _vzdump_tasks_cache:
        try:
            since = int((datetime.now(timezone.utc) - timedelta(days=lookback_days)).timestamp())
            tasks = pvesh_get(f"/nodes/{node}/tasks", typefilter="vzdump", since=str(since),
                              limit="500", source="all")
            # Ordina per data più recente e filtra completati
            _vzdump_tasks_cache[cache_key] = sorted(
                [t for t in tasks if t.get("status") != "running"],
                key=lambda x: x.get("endtime", 0), reverse=True
            )
            logger.debug(f"Cache tasks {cache_key}: {len(_vzdump_tasks_cache[cache_key])} task")
        except Exception as e:
            logger.debug(f"Errore lettura task vzdump: {e}")
            _vzdump_tasks_cache[cache_key] = []
    return _vzdump_tasks_cache[cache_key]


def get_cluster_resources_cached() -> List:
    """Ottiene le risorse del cluster con cache"""
    global _cluster_resources_cache
    if _cluster_resources_cache is None:
        try:
            _cluster_resources_cache = pvesh_get("/cluster/resources")
            logger.debug(f"Cache cluster/resources: {len(_cluster_resources_cache)} risorse")
        except Exception as e:
            logger.debug(f"Errore lettura cluster/resources: {e}")
            _cluster_resources_cache = []
    return _cluster_resources_cache


def get_latest_backup_info(node: str, storage: str, vmid: str, vm_type: str) -> Dict:
    """Ottiene informazioni sull'ultimo backup di una VM/CT dal repository PBS (con cache)"""
    try:
        # Usa cache invece di chiamata API diretta
        content = get_storage_content_cached(node, storage)
        
        # Filtra backup per questa VM/CT
        vm_backups = []
        for backup in content:
            if backup.get("vmid") == int(vmid) and backup.get("content") == "backup":
                # Verifica che il tipo corrisponda (qemu -> vm, lxc -> ct)
                if vm_type == "qemu" and backup.get("subtype") == "qemu":
                    vm_backups.append(backup)
                elif vm_type == "lxc" and backup.get("subtype") == "lxc":
                    vm_backups.append(backup)
        
        if vm_backups:
            # Ordina per data (più recente prima)
            latest = sorted(vm_backups, key=lambda x: x.get("ctime", 0), reverse=True)[0]
            backup_time = latest.get("ctime", 0)
            
            # Determina lo status basandosi sulla verifica e sulla data
            verification = latest.get("verification", {})
            if isinstance(verification, dict):
                verify_state = verification.get("state", "")
            else:
                verify_state = ""
            
            # Se verificato ok o se il backup esiste, consideriamo success
            if verify_state == "ok" or backup_time > 0:
                backup_status = "success"
            elif verify_state == "failed":
                backup_status = "failed"
            else:
                backup_status = "unknown"
            
            return {
                "backup_status": backup_status,
                "backup_date": datetime.fromtimestamp(backup_time, tz=timezone.utc).isoformat() if backup_time else None,
                "backup_size_bytes": latest.get("size", 0),
                "backup_size_gb": round(latest.get("size", 0) / (1024**3), 2) if latest.get("size", 0) > 0 else None,
                "backup_volid": latest.get("volid", ""),
                "verification_state": verify_state if verify_state else None
            }
    except Exception as e:
        logger.debug(f"Errore ottenimento info backup per VM {vmid} da storage {storage}: {e}")
    
    return {}


def get_backup_task_info(node: str, vmid: str, storage: str, lookback_days: int = 7) -> Dict:
    """Ottiene informazioni sul task di backup più recente per una VM.
    
    NOTA: Per performance, non leggiamo i log dei task individualmente 
    (sarebbe troppo lento con molte VM). Le informazioni principali 
    (data, size) vengono già dal repository PBS via get_latest_backup_info.
    """
    # Ritorna dizionario vuoto - le info principali vengono da get_latest_backup_info
    # che usa il contenuto dello storage PBS che è già cached
    return {}


def collect_backup_jobs(node: str, syslog: SyslogSender, client: Dict, test_mode: bool, lookback_hours: int = 24):
    """Raccoglie informazioni sui job di backup che hanno eseguito backup nel periodo di lookback.
    
    NOTA: Invia solo job con VM che hanno backup_date nel periodo di lookback.
    Non invia job che non hanno eseguito backup recentemente.
    """
    logger.info("Raccolta job di backup eseguiti...")
    
    # Resetta le cache per avere dati freschi
    clear_caches()

    try:
        backup_jobs = []
        
        # Ottieni i job vzdump schedulati dal cluster (endpoint corretto: /cluster/backup)
        # Se l'API non restituisce le VM, prova a leggere direttamente il file di configurazione
        cluster_jobs = []
        
        try:
            cluster_jobs = pvesh_get("/cluster/backup")
            logger.info(f"Trovati {len(cluster_jobs)} job di backup nel cluster via API")
        except Exception as e:
            logger.warning(f"Errore lettura API /cluster/backup: {e}")
        
        # Se l'API non ha restituito job o le VM sono vuote, prova a leggere il file di configurazione
        if not cluster_jobs or all(not job.get("vms") and not job.get("all") for job in cluster_jobs):
            logger.info("Tentativo lettura diretta file di configurazione /etc/pve/jobs.cfg")
            try:
                with open("/etc/pve/jobs.cfg", "r") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#"):
                            continue
                        # Parse formato jobs.cfg: backup:backup-xxx:vms=100,101,102:storage=xxx:schedule=xxx
                        if line.startswith("backup:"):
                            parts = line.split(":")
                            if len(parts) >= 2:
                                job_id = parts[1]
                                job_data = {"id": job_id}
                                for part in parts[2:]:
                                    if "=" in part:
                                        key, value = part.split("=", 1)
                                        job_data[key] = value
                                cluster_jobs.append(job_data)
                logger.info(f"Letti {len(cluster_jobs)} job da /etc/pve/jobs.cfg")
            except Exception as e:
                logger.warning(f"Errore lettura /etc/pve/jobs.cfg: {e}")
        
        for job in cluster_jobs:
            job_id = job.get("id", "")
            if not job_id:
                continue
            
            try:
                # Prova a ottenere dettagli completi del job tramite API
                try:
                    job_details = pvesh_get(f"/cluster/backup/{job_id}")
                    # Merge dettagli con dati base
                    job.update(job_details)
                    logger.debug(f"Job {job_id} dettagli completi: {json.dumps(job, indent=2, default=str)}")
                except Exception as e:
                    logger.debug(f"Impossibile ottenere dettagli job {job_id}: {e}")
                
                # Log struttura completa del job per debug
                logger.debug(f"Job {job_id} completo: {json.dumps(job, indent=2, default=str)}")
                
                # Estrai VM/CT incluse nel backup
                # Proxmox può usare "vms", "vmid", o "all" con "exclude"
                vms_value = job.get("vms") or job.get("vmid", "")
                all_flag = job.get("all", False) or job.get("all") == 1
                exclude_value = job.get("exclude", "")
                
                logger.info(f"Job {job_id}: vms/vmid={repr(vms_value)}, all={all_flag}, exclude={repr(exclude_value)}, nodes={job.get('nodes', '')}")
                
                # Gestisci diversi formati: stringa, lista, o None
                vm_list = []
                vm_ids = []
                
                # Se all=True, ottieni tutte le VM del cluster e filtra quelle escluse
                if all_flag:
                    logger.info(f"Job {job_id}: configurazione 'all' (backuppa tutte le VM)")
                    try:
                        # Ottieni tutte le VM/CT dal cluster usando /cluster/resources (più efficiente)
                        all_vms = []
                        nodes_str = job.get("nodes", "")
                        nodes_list = []
                        if nodes_str:
                            nodes_list = [n.strip() for n in nodes_str.split(",") if n.strip()]
                        
                        # Se nodes è vuoto, usa tutte le VM del cluster
                        if not nodes_list:
                            try:
                                # Usa /cluster/resources con cache per ottenere tutte le VM/CT del cluster
                                resources = get_cluster_resources_cached()
                                for res in resources:
                                    if res.get("type") in ["qemu", "lxc"] and res.get("template", 0) == 0:
                                        all_vms.append({
                                            "vmid": str(res.get("vmid", "")),
                                            "node": res.get("node", ""),
                                            "type": res.get("type", "qemu")
                                        })
                                logger.info(f"Job {job_id}: trovate {len(all_vms)} VM/CT nel cluster totale")
                            except Exception as e:
                                logger.warning(f"Job {job_id}: errore ottenimento VM da cluster/resources: {e}")
                                # Fallback: cerca su tutti i nodi conosciuti
                                try:
                                    cluster_nodes = pvesh_get("/nodes")
                                    nodes_list = [n.get("node", "") for n in cluster_nodes if n.get("node")]
                                except:
                                    nodes_list = [node]  # Ultimo fallback: solo nodo locale
                        
                        # Se nodes è specificato, cerca solo su quei nodi
                        if nodes_list:
                            for n in nodes_list:
                                try:
                                    qemu_list = pvesh_get(f"/nodes/{n}/qemu")
                                    for vm in qemu_list:
                                        if vm.get("template", 0) == 0:  # Escludi template
                                            all_vms.append({"vmid": str(vm.get("vmid", "")), "node": n, "type": "qemu"})
                                except:
                                    pass
                                try:
                                    lxc_list = pvesh_get(f"/nodes/{n}/lxc")
                                    for ct in lxc_list:
                                        if ct.get("template", 0) == 0:  # Escludi template
                                            all_vms.append({"vmid": str(ct.get("vmid", "")), "node": n, "type": "lxc"})
                                except:
                                    pass
                        
                        # Rimuovi duplicati (nel caso si usino entrambi i metodi)
                        seen = set()
                        unique_vms = []
                        for vm in all_vms:
                            vm_id = vm["vmid"]
                            if vm_id and vm_id not in seen:
                                seen.add(vm_id)
                                unique_vms.append(vm)
                        all_vms = unique_vms
                        
                        # Filtra le VM escluse
                        exclude_ids = []
                        if exclude_value:
                            exclude_ids = [v.strip() for v in re.split(r'[,;\s]+', str(exclude_value)) if v.strip() and v.strip().isdigit()]
                        
                        for vm in all_vms:
                            if vm["vmid"] not in exclude_ids:
                                vm_ids.append(vm["vmid"])
                        
                        logger.info(f"Job {job_id}: trovati {len(vm_ids)} VM (tutte tranne {len(exclude_ids)} escluse su {len(all_vms)} totali nel cluster)")
                    except Exception as e:
                        logger.warning(f"Job {job_id}: errore ottenimento VM per job 'all': {e}")
                        # Fallback: segna come job "all" senza elencare le VM
                        backup_jobs.append({
                            "job_id": job_id,
                            "nodes": job.get("nodes", node),
                            "storage": job.get("storage", "unknown"),
                            "schedule": job.get("schedule", ""),
                            "enabled": job.get("enabled", True) if "enabled" in job else True,
                            "mode": job.get("mode", "snapshot"),
                            "compress": job.get("compress", ""),
                            "all": True,
                            "vms": [],
                            "vm_count": 0
                        })
                        continue
                
                # Se vms/vmid è una lista, usa direttamente
                elif isinstance(vms_value, list):
                    vm_ids = [str(v) for v in vms_value if v]
                # Se vms/vmid è una stringa, parsala
                elif isinstance(vms_value, str) and vms_value.strip():
                    # Parse VM list (può essere una stringa con VMID separati da spazio, virgola o punto e virgola)
                    # Accetta sia numeri che pattern come "vm/100" o "lxc/200"
                    vm_ids_raw = re.split(r'[,;\s]+', vms_value.strip())
                    for v in vm_ids_raw:
                        v = v.strip()
                        if not v:
                            continue
                        # Se è nel formato "vm/100" o "lxc/200", estrai solo il numero
                        match = re.match(r'(?:vm|lxc|qemu|ct)[/:]?(\d+)', v, re.IGNORECASE)
                        if match:
                            vm_ids.append(match.group(1))
                        # Altrimenti, se è solo un numero, usalo direttamente
                        elif v.isdigit():
                            vm_ids.append(v)
                        else:
                            logger.debug(f"Formato VMID non riconosciuto: {v}")
                
                # Se non ci sono VM da processare, salta questo job
                if not vm_ids:
                    logger.warning(f"Job {job_id}: nessuna VM trovata nei campi 'vms'/'vmid' (valore: {vms_value})")
                    continue
                    
                # Determina i nodi interessati dal job
                nodes_str = job.get("nodes", "")
                nodes_list = []
                if nodes_str:
                    nodes_list = [n.strip() for n in nodes_str.split(",") if n.strip()]
                if not nodes_list:
                    nodes_list = [node]  # Default al nodo locale
                
                logger.info(f"Job {job_id}: trovati {len(vm_ids)} VM IDs: {vm_ids[:10]}{'...' if len(vm_ids) > 10 else ''}")
                
                # Ottieni informazioni su tutte le VM/CT del cluster per avere nomi e dettagli
                cluster_vms = {}
                cluster_cts = {}
                try:
                    # Cerca su tutti i nodi del cluster
                    all_nodes = nodes_list if nodes_list else [node]
                    for n in all_nodes:
                        try:
                            qemu_list = pvesh_get(f"/nodes/{n}/qemu")
                            for vm in qemu_list:
                                vm_id = str(vm.get("vmid", ""))
                                if vm_id:
                                    cluster_vms[vm_id] = {
                                        "name": vm.get("name", f"VM-{vm_id}"),
                                        "type": "qemu",
                                        "node": n,
                                        "status": vm.get("status", "unknown"),
                                        "maxdisk": vm.get("maxdisk", 0),
                                        "maxmem": vm.get("maxmem", 0)
                                    }
                        except:
                            pass
                        try:
                            lxc_list = pvesh_get(f"/nodes/{n}/lxc")
                            for ct in lxc_list:
                                ct_id = str(ct.get("vmid", ""))
                                if ct_id:
                                    cluster_cts[ct_id] = {
                                        "name": ct.get("name", f"CT-{ct_id}"),
                                        "type": "lxc",
                                        "node": n,
                                        "status": ct.get("status", "unknown"),
                                        "maxdisk": ct.get("maxdisk", 0),
                                        "maxmem": ct.get("maxmem", 0)
                                    }
                        except:
                            pass
                except Exception as e:
                    logger.debug(f"Errore ottenimento lista VM/CT cluster: {e}")
                
                # Ottieni storage del job per cercare i backup
                job_storage = job.get("storage", "")
                
                for vmid in vm_ids:
                    # Cerca nella cache delle VM/CT del cluster
                    vm_info = cluster_vms.get(vmid) or cluster_cts.get(vmid)
                    
                    vm_data = {}
                    if vm_info:
                        vm_data = {
                            "vmid": vmid,
                            "name": vm_info["name"],
                            "type": vm_info["type"],
                            "node": vm_info["node"],
                            "status": vm_info.get("status", "unknown"),
                            "disk_size_bytes": vm_info.get("maxdisk", 0),
                            "disk_size_gb": round(vm_info.get("maxdisk", 0) / (1024**3), 2) if vm_info.get("maxdisk", 0) > 0 else None,
                            "memory_bytes": vm_info.get("maxmem", 0),
                            "memory_gb": round(vm_info.get("maxmem", 0) / (1024**3), 2) if vm_info.get("maxmem", 0) > 0 else None
                        }
                    else:
                        # Se non trovata nella cache locale, cerca nella lista delle risorse del cluster
                        vm_name = f"VM-{vmid}"
                        vm_type = "unknown"
                        vm_node = nodes_list[0] if nodes_list else node
                        vm_status = "unknown"
                        vm_disk = 0
                        vm_mem = 0
                        
                        try:
                            # Cerca nella lista delle risorse del cluster con cache
                            resources = get_cluster_resources_cached()
                            for res in resources:
                                if str(res.get("vmid", "")) == vmid:
                                    vm_name = res.get("name", f"VM-{vmid}")
                                    vm_type = "qemu" if res.get("type") == "qemu" else ("lxc" if res.get("type") == "lxc" else "unknown")
                                    vm_node = res.get("node", nodes_list[0] if nodes_list else node)
                                    vm_status = res.get("status", "unknown")
                                    vm_disk = res.get("maxdisk", 0)
                                    vm_mem = res.get("maxmem", 0)
                                    break
                        except Exception as e:
                            logger.debug(f"Errore ricerca VM {vmid} in cluster/resources: {e}")
                        
                        vm_data = {
                            "vmid": vmid,
                            "name": vm_name,
                            "type": vm_type,
                            "node": vm_node,
                            "status": vm_status,
                            "disk_size_bytes": vm_disk if vm_disk > 0 else None,
                            "disk_size_gb": round(vm_disk / (1024**3), 2) if vm_disk > 0 else None,
                            "memory_bytes": vm_mem if vm_mem > 0 else None,
                            "memory_gb": round(vm_mem / (1024**3), 2) if vm_mem > 0 else None
                        }
                    
                    # Aggiungi informazioni sul backup dal repository PBS
                    if job_storage and vm_data.get("type") != "unknown":
                        backup_info = get_latest_backup_info(node, job_storage, vmid, vm_data.get("type", "qemu"))
                        if backup_info:
                            vm_data.update(backup_info)
                        
                        # Aggiungi informazioni sul task di backup
                        task_info = get_backup_task_info(node, vmid, job_storage)
                        if task_info:
                            vm_data.update(task_info)
                    
                    vm_list.append(vm_data)
                
                # Filtra solo VM che hanno backup_date nel periodo di lookback
                cutoff_time = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
                vms_with_recent_backup = []
                for vm in vm_list:
                    backup_date_str = vm.get("backup_date")
                    if backup_date_str:
                        try:
                            # Parse ISO 8601 date
                            backup_dt = datetime.fromisoformat(backup_date_str.replace("Z", "+00:00"))
                            if backup_dt >= cutoff_time:
                                vms_with_recent_backup.append(vm)
                        except:
                            pass
                
                # Invia solo se ci sono VM con backup recente
                if vms_with_recent_backup:
                    backup_jobs.append({
                        "job_id": job_id,
                        "nodes": job.get("nodes", node),
                        "storage": job.get("storage", "unknown"),
                        "schedule": job.get("schedule", ""),
                        "enabled": job.get("enabled", True) if "enabled" in job else True,
                        "mode": job.get("mode", "snapshot"),
                        "compress": job.get("compress", ""),
                        "all": job.get("all", False),
                        "vms": vms_with_recent_backup,
                        "vm_count": len(vms_with_recent_backup)
                    })
            except Exception as e:
                logger.debug(f"Errore elaborazione job {job_id}: {e}")
                continue
        
    except Exception as e:
        logger.warning(f"Errore lettura job backup dal cluster: {e}")
        backup_jobs = []  # In caso di errore, usa lista vuota
    
    # Invia un messaggio per ogni job di backup (TCP supporta messaggi grandi)
    for job in backup_jobs:
        if job.get("enabled", True):
            status = "success"
        else:
            status = "warning"
        
        data = {
            "status": status,
            "job_id": job.get("job_id", ""),
            "nodes": job.get("nodes", ""),
            "storage": job.get("storage", ""),
            "schedule": job.get("schedule", ""),
            "enabled": job.get("enabled", True),
            "mode": job.get("mode", ""),
            "compress": job.get("compress", ""),
            "all": job.get("all", False),
            "vm_count": job.get("vm_count", 0),
            "vms": job.get("vms", [])
        }
        
        syslog.send("PVE_BACKUP_JOB", data, client, test_mode)
    
    if backup_jobs:
        logger.info(f"Trovati {len(backup_jobs)} job di backup schedulati con {sum(j.get('vm_count', 0) for j in backup_jobs)} VM/CT totali")
    else:
        logger.info("Nessun job di backup schedulato trovato")


def collect_backup_coverage(syslog: SyslogSender, client: Dict, test_mode: bool):
    """Verifica VM/CT senza copertura backup schedulato"""
    logger.info("Verifica copertura backup...")

    try:
        not_backed_up = pvesh_get("/cluster/backup-info/not-backed-up")

        guests = []
        for item in not_backed_up:
            guests.append({
                "vmid": item.get("vmid", ""),
                "name": item.get("name", "unknown"),
                "type": item.get("type", "unknown"),
            })

        count = len(guests)
        status = "warning" if count > 0 else "success"

        data = {
            "status": status,
            "not_backed_up_count": count,
            "guests": guests,
        }

        syslog.send("PVE_BACKUP_COVERAGE", data, client, test_mode)
    except Exception as e:
        logger.error(f"Errore verifica copertura backup: {e}")


def collect_service_status(syslog: SyslogSender, client: Dict, test_mode: bool):
    """Verifica stato servizi systemd importanti di Proxmox VE (equivalente a VEEAM_SERVICE_STATUS)"""
    logger.info("Raccolta stato servizi Proxmox VE...")

    try:
        import subprocess
        
        # Servizi importanti di Proxmox VE
        important_services = [
            "pve-cluster",
            "pve-daemon",
            "pveproxy",
            "pvestatd",
            "pve-firewall",
            "corosync",
            "pve-ha-crm",
            "pve-ha-lrm",
        ]

        services = []
        services_running = 0
        services_stopped = 0
        services_failed = 0

        for service_name in important_services:
            try:
                # Verifica stato servizio
                result = subprocess.run(
                    ["systemctl", "is-active", service_name],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                state = result.stdout.strip() if result.returncode == 0 else "inactive"

                # Verifica tipo avvio
                enabled_result = subprocess.run(
                    ["systemctl", "is-enabled", service_name],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                startup_type = enabled_result.stdout.strip() if enabled_result.returncode == 0 else "unknown"

                service_info = {
                    "name": service_name,
                    "state": state,
                    "startup_type": startup_type,
                }

                services.append(service_info)

                if state == "active":
                    services_running += 1
                elif state == "failed":
                    services_failed += 1
                else:
                    services_stopped += 1

            except Exception as e:
                logger.warning(f"Errore verifica servizio {service_name}: {e}")
                services.append({
                    "name": service_name,
                    "state": "unknown",
                    "startup_type": "unknown",
                })

        # Determina status complessivo: failed se almeno un servizio importante è failed o stopped quando dovrebbe essere enabled
        status = "success"
        for svc in services:
            if svc["state"] == "failed":
                status = "failed"
                break
            elif svc["startup_type"] == "enabled" and svc["state"] != "active":
                status = "failed"
                break
            elif svc["state"] != "active" and svc["startup_type"] == "enabled":
                status = "warning"

        data = {
            "status": status,
            "services_total": len(services),
            "services_running": services_running,
            "services_stopped": services_stopped,
            "services_failed": services_failed,
            "services": services,
        }

        syslog.send("PVE_SERVICE_STATUS", data, client, test_mode)
        logger.info(f"Servizi: {services_running} running, {services_stopped} stopped, {services_failed} failed")

    except Exception as e:
        logger.error(f"Errore raccolta stato servizi: {e}")


def collect_daily_report(node: str, syslog: SyslogSender, client: Dict,
                         lookback_hours: int, test_mode: bool):
    """Genera report giornaliero completo: stato nodo, storage e riepilogo task vzdump"""
    logger.info("Generazione report giornaliero...")

    # Invia stato nodo (come Veeam invia VEEAM_SERVER_STATUS)
    try:
        collect_node_status(node, syslog, client, test_mode)
    except Exception as e:
        logger.error(f"Errore raccolta stato nodo nel report giornaliero: {e}")

    # Invia stato storage (come Veeam invia VEEAM_REPOSITORY_STATUS)
    try:
        collect_storage_status(node, syslog, client, test_mode)
    except Exception as e:
        logger.error(f"Errore raccolta stato storage nel report giornaliero: {e}")

    # Invia stato servizi (equivalente a VEEAM_SERVICE_STATUS)
    try:
        collect_service_status(syslog, client, test_mode)
    except Exception as e:
        logger.error(f"Errore raccolta stato servizi nel report giornaliero: {e}")

    # Invia copertura backup (informazioni aggiuntive)
    try:
        collect_backup_coverage(syslog, client, test_mode)
    except Exception as e:
        logger.error(f"Errore raccolta copertura backup nel report giornaliero: {e}")

    # Genera riepilogo task vzdump (equivalente a VEEAM_DAILY_REPORT)
    try:
        since = int((datetime.now(timezone.utc) - timedelta(hours=lookback_hours)).timestamp())
        tasks = pvesh_get(f"/nodes/{node}/tasks", typefilter="vzdump", since=str(since),
                          limit="500", source="all")

        completed = [t for t in tasks if t.get("status") == "stopped"]

        # Raggruppa per job (come nella funzione collect_backup_results)
        jobs_dict = {}
        
        for task in completed:
            starttime = task.get("starttime", 0)
            endtime = task.get("endtime", 0)
            duration = endtime - starttime if endtime and starttime else 0
            vmid = task.get("id", "")
            upid = task.get("upid", "")
            user = task.get("user", "")
            exitstatus = task.get("exitstatus", "")
            
            if exitstatus == "OK":
                status = "success"
            elif "error" in exitstatus.lower():
                status = "failed"
            else:
                status = "warning"
            
            # Prova a ottenere nome VM
            vm_name = f"VM-{vmid}"
            try:
                vm_info = pvesh_get(f"/nodes/{node}/qemu/{vmid}")
                vm_name = vm_info.get("name", f"VM-{vmid}")
            except:
                try:
                    ct_info = pvesh_get(f"/nodes/{node}/lxc/{vmid}")
                    vm_name = ct_info.get("name", f"CT-{vmid}")
                except:
                    pass
            
            time_key = int(starttime / 300) * 300
            job_key = f"{user}_{time_key}"
            
            if job_key not in jobs_dict:
                jobs_dict[job_key] = {
                    "start_time": starttime,
                    "end_time": endtime,
                    "vms": []
                }
            
            if endtime > jobs_dict[job_key]["end_time"]:
                jobs_dict[job_key]["end_time"] = endtime
            
            jobs_dict[job_key]["vms"].append({
                "vmid": vmid,
                "name": vm_name,
                "status": status,
                "start_time": datetime.fromtimestamp(starttime, tz=timezone.utc).isoformat() if starttime else None,
                "end_time": datetime.fromtimestamp(endtime, tz=timezone.utc).isoformat() if endtime else None,
                "duration_minutes": round(duration / 60, 1),
                "exit_status": exitstatus,
            })
        
        # Converti in formato jobs per il report
        jobs = []
        for job_key, job_data in jobs_dict.items():
            vms = job_data["vms"]
            start_time = job_data["start_time"]
            end_time = job_data["end_time"]
            job_duration = end_time - start_time if end_time and start_time else 0
            
            vms_success = sum(1 for v in vms if v["status"] == "success")
            vms_warning = sum(1 for v in vms if v["status"] == "warning")
            vms_failed = sum(1 for v in vms if v["status"] == "failed")
            
            if vms_failed > 0:
                job_status = "failed"
            elif vms_warning > 0:
                job_status = "warning"
            else:
                job_status = "success"
            
            jobs.append({
                "job_start_time": datetime.fromtimestamp(start_time, tz=timezone.utc).isoformat() if start_time else None,
                "job_end_time": datetime.fromtimestamp(end_time, tz=timezone.utc).isoformat() if end_time else None,
                "job_duration_minutes": round(job_duration / 60, 1),
                "status": job_status,
                "vm_count": len(vms),
                "vms_success": vms_success,
                "vms_warning": vms_warning,
                "vms_failed": vms_failed,
                "vms": vms
            })

        success_count = sum(1 for j in jobs if j["status"] == "success")
        warning_count = sum(1 for j in jobs if j["status"] == "warning")
        failed_count = sum(1 for j in jobs if j["status"] == "failed")

        overall = "failed" if failed_count > 0 else "warning" if warning_count > 0 else "success"

        data = {
            "status": overall,
            "report_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "lookback_hours": lookback_hours,
            "jobs_total": len(jobs),
            "jobs_success": success_count,
            "jobs_warning": warning_count,
            "jobs_failed": failed_count,
            "jobs": jobs,
        }

        syslog.send("PVE_DAILY_REPORT", data, client, test_mode)
        logger.info(f"Report giornaliero: {len(jobs)} task ({success_count} ok, {warning_count} warning, {failed_count} failed)")

    except Exception as e:
        logger.error(f"Errore generazione report giornaliero: {e}")


def main():
    parser = argparse.ArgumentParser(description="PVE Backup Monitor - Syslog sender")
    parser.add_argument("-c", "--config", default="/etc/backup-monitor/pve-config.yaml",
                        help="Path al file di configurazione")
    parser.add_argument("--test", action="store_true", help="Modalita test (stampa syslog)")
    parser.add_argument("--daily-report", action="store_true", help="Invia report giornaliero")
    args = parser.parse_args()

    # Carica configurazione
    with open(args.config, "r") as f:
        config = yaml.safe_load(f)

    logger.info(f"=== PVE Backup Monitor v{VERSION} ===")

    node = get_node_name()
    logger.info(f"Nodo: {node}")

    syslog_cfg = config["syslog"]
    syslog = SyslogSender(
        server=syslog_cfg["server"],
        port=syslog_cfg["port"],
        facility=syslog_cfg.get("facility", "local0"),
        protocol=syslog_cfg.get("protocol", "tcp")
    )

    client = config["client"]
    lookback = config.get("pve", {}).get("lookback_hours", 24)

    # Raccogli e invia dati
    if args.daily_report:
        collect_daily_report(node, syslog, client, lookback, args.test)
    else:
        collect_node_status(node, syslog, client, args.test)
        collect_storage_status(node, syslog, client, args.test)
        collect_backup_results(node, syslog, client, lookback, args.test)
        collect_backup_jobs(node, syslog, client, args.test, lookback)
        collect_backup_coverage(syslog, client, args.test)

    logger.info("=== Completato ===")


if __name__ == "__main__":
    main()
