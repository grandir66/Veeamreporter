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
    """Invia messaggi syslog UDP RFC 5424"""

    FACILITY_MAP = {
        "local0": 16, "local1": 17, "local2": 18, "local3": 19,
        "local4": 20, "local5": 21, "local6": 22, "local7": 23
    }

    def __init__(self, server: str, port: int, facility: str = "local0"):
        self.server = server
        self.port = port
        self.facility = self.FACILITY_MAP.get(facility, 16)

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
            print(f"\n=== SYSLOG MESSAGE ===\n{syslog_msg}\n======================\n")
            return

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.sendto(syslog_msg.encode("utf-8"), (self.server, self.port))
            sock.close()
            logger.info(f"Syslog inviato: {message_type}")
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
    """Raccoglie e invia stato degli storage"""
    logger.info("Raccolta stato storage...")

    try:
        storages = pvesh_get(f"/nodes/{node}/storage", content="backup")

        for st in storages:
            if not st.get("active", 0):
                continue

            total = st.get("total", 0)
            used = st.get("used", 0)
            avail = st.get("avail", total - used)
            used_percent = round(st.get("used_fraction", 0) * 100, 1)

            status = "success"
            if used_percent > 95:
                status = "failed"
            elif used_percent > 90:
                status = "warning"

            data = {
                "status": status,
                "storage_name": st.get("storage", "unknown"),
                "storage_type": st.get("type", "unknown"),
                "content": st.get("content", ""),
                "total_bytes": total,
                "used_bytes": used,
                "free_bytes": avail,
                "used_percent": used_percent,
                "total_gb": round(total / (1024 ** 3), 2),
                "used_gb": round(used / (1024 ** 3), 2),
                "free_gb": round(avail / (1024 ** 3), 2),
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

        # Filtra solo task completati (status = "stopped")
        completed = [t for t in tasks if t.get("status") == "stopped"]
        logger.info(f"Trovati {len(completed)} task vzdump completati")

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


def collect_backup_jobs(node: str, syslog: SyslogSender, client: Dict, test_mode: bool):
    """Raccoglie informazioni sui job di backup schedulati e le VM/CT che vengono backuppate"""
    logger.info("Raccolta job di backup schedulati...")

    try:
        backup_jobs = []
        
        # Ottieni i job vzdump schedulati dal cluster (endpoint corretto: /cluster/backup)
        try:
            cluster_jobs = pvesh_get("/cluster/backup")
            
            for job in cluster_jobs:
                job_id = job.get("id", "")
                if not job_id:
                    continue
                
                try:
                    # Estrai VM/CT incluse nel backup
                    vms_str = job.get("vms", "")
                    if not vms_str:
                        # Se vms è vuoto ma "all" è True, significa che il job backuppa tutte le VM
                        if job.get("all", False):
                            # Per job con "all", non possiamo elencare tutte le VM facilmente
                            # Invia comunque il job senza lista VM
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
                    
                    # Parse VM list (può essere una stringa con VMID separati da spazio, virgola o punto e virgola)
                    vm_list = []
                    if isinstance(vms_str, str):
                        # Rimuovi spazi e split su vari separatori
                        vm_ids = [v.strip() for v in re.split(r'[,;\s]+', vms_str) if v.strip() and v.strip().isdigit()]
                        
                        # Determina i nodi interessati dal job
                        nodes_str = job.get("nodes", "")
                        nodes_list = []
                        if nodes_str:
                            nodes_list = [n.strip() for n in nodes_str.split(",") if n.strip()]
                        if not nodes_list:
                            nodes_list = [node]  # Default al nodo locale
                        
                        for vmid in vm_ids:
                            vm_name = f"VM-{vmid}"
                            vm_type = "unknown"
                            vm_node = nodes_list[0]  # Prova prima con il primo nodo
                            
                            # Prova a ottenere informazioni sulla VM/CT su tutti i nodi
                            for try_node in nodes_list:
                                try:
                                    vm_info = pvesh_get(f"/nodes/{try_node}/qemu/{vmid}")
                                    vm_name = vm_info.get("name", f"VM-{vmid}")
                                    vm_type = "qemu"
                                    vm_node = try_node
                                    break
                                except:
                                    try:
                                        ct_info = pvesh_get(f"/nodes/{try_node}/lxc/{vmid}")
                                        vm_name = ct_info.get("name", f"CT-{vmid}")
                                        vm_type = "lxc"
                                        vm_node = try_node
                                        break
                                    except:
                                        continue
                            
                            vm_list.append({
                                "vmid": vmid,
                                "name": vm_name,
                                "type": vm_type,
                                "node": vm_node
                            })
                    
                    if vm_list or job.get("all", False):
                        backup_jobs.append({
                            "job_id": job_id,
                            "nodes": job.get("nodes", node),
                            "storage": job.get("storage", "unknown"),
                            "schedule": job.get("schedule", ""),
                            "enabled": job.get("enabled", True) if "enabled" in job else True,
                            "mode": job.get("mode", "snapshot"),
                            "compress": job.get("compress", ""),
                            "all": job.get("all", False),
                            "vms": vm_list,
                            "vm_count": len(vm_list)
                        })
                except Exception as e:
                    logger.debug(f"Errore elaborazione job {job_id}: {e}")
                    continue
        
        except Exception as e:
            logger.warning(f"Errore lettura job backup dal cluster: {e}")
        
        # Invia un messaggio per ogni job di backup trovato
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
        
    except Exception as e:
        logger.error(f"Errore raccolta job di backup schedulati: {e}")


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
        facility=syslog_cfg.get("facility", "local0")
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
        collect_backup_jobs(node, syslog, client, args.test)
        collect_backup_coverage(syslog, client, args.test)

    logger.info("=== Completato ===")


if __name__ == "__main__":
    main()
