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
    """Raccoglie e invia risultati task vzdump"""
    logger.info("Raccolta risultati backup vzdump...")

    try:
        since = int((datetime.now(timezone.utc) - timedelta(hours=lookback_hours)).timestamp())
        tasks = pvesh_get(f"/nodes/{node}/tasks", typefilter="vzdump", since=str(since),
                          limit="500", source="all")

        # Filtra solo task completati (status = "stopped")
        completed = [t for t in tasks if t.get("status") == "stopped"]
        logger.info(f"Trovati {len(completed)} task vzdump completati")

        for task in completed:
            starttime = task.get("starttime", 0)
            endtime = task.get("endtime", 0)
            duration = endtime - starttime if endtime and starttime else 0

            exitstatus = task.get("exitstatus", "")
            if exitstatus == "OK":
                status = "success"
            elif "error" in exitstatus.lower():
                status = "failed"
            else:
                status = "warning"

            vmid = task.get("id", "")

            data = {
                "status": status,
                "task_id": task.get("upid", ""),
                "vmid": vmid,
                "start_time": datetime.fromtimestamp(starttime, tz=timezone.utc).isoformat() if starttime else None,
                "end_time": datetime.fromtimestamp(endtime, tz=timezone.utc).isoformat() if endtime else None,
                "duration_seconds": duration,
                "duration_minutes": round(duration / 60, 1),
                "exit_status": exitstatus,
                "result_message": exitstatus,
                "user": task.get("user", ""),
            }

            syslog.send("PVE_BACKUP_RESULT", data, client, test_mode)

    except Exception as e:
        logger.error(f"Errore raccolta task backup: {e}")


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


def collect_daily_report(node: str, syslog: SyslogSender, client: Dict,
                         lookback_hours: int, test_mode: bool):
    """Genera report giornaliero con riepilogo di tutti i task vzdump"""
    logger.info("Generazione report giornaliero...")

    try:
        since = int((datetime.now(timezone.utc) - timedelta(hours=lookback_hours)).timestamp())
        tasks = pvesh_get(f"/nodes/{node}/tasks", typefilter="vzdump", since=str(since),
                          limit="500", source="all")

        completed = [t for t in tasks if t.get("status") == "stopped"]

        jobs = []
        for task in completed:
            starttime = task.get("starttime", 0)
            endtime = task.get("endtime", 0)
            duration = endtime - starttime if endtime and starttime else 0

            exitstatus = task.get("exitstatus", "")
            if exitstatus == "OK":
                status = "success"
            elif "error" in exitstatus.lower():
                status = "failed"
            else:
                status = "warning"

            jobs.append({
                "vmid": task.get("id", ""),
                "status": status,
                "start_time": datetime.fromtimestamp(starttime, tz=timezone.utc).isoformat() if starttime else None,
                "end_time": datetime.fromtimestamp(endtime, tz=timezone.utc).isoformat() if endtime else None,
                "duration_minutes": round(duration / 60, 1),
                "exit_status": exitstatus,
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
        collect_backup_coverage(syslog, client, args.test)

    logger.info("=== Completato ===")


if __name__ == "__main__":
    main()
