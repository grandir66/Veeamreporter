#!/usr/bin/env python3
"""
Proxmox Backup Server Monitor - Invia stato backup e server a Graylog via Syslog

Raccoglie:
- Risultati task di backup
- Stato del server PBS
- Stato dei datastore
"""

import argparse
import json
import logging
import socket
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import requests
import yaml
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

VERSION = "2.0.0"
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


class SyslogSender:
    """Invia messaggi syslog UDP"""

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

        # Severity basata su status
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
        syslog_msg = f"<{priority}>1 {timestamp} {hostname} pbs-backup-monitor {sys.argv[0]} {message_type} - {json_payload}"

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


class PBSClient:
    """Client API Proxmox Backup Server"""

    def __init__(self, host: str, port: int, user: str, token_name: str,
                 token_value: str, verify_ssl: bool = False):
        self.base_url = f"https://{host}:{port}/api2/json"
        self.auth_header = f"PBSAPIToken={user}!{token_name}:{token_value}"
        self.verify_ssl = verify_ssl
        self.session = requests.Session()

    def _get(self, endpoint: str, params: Dict = None) -> Any:
        """GET request all'API PBS"""
        url = f"{self.base_url}/{endpoint}"
        response = self.session.get(
            url,
            params=params,
            headers={"Authorization": self.auth_header},
            verify=self.verify_ssl
        )
        response.raise_for_status()
        return response.json().get("data", {})

    def get_server_status(self) -> Dict:
        """Stato del server PBS"""
        return self._get("nodes/localhost/status")

    def get_datastores(self) -> List[Dict]:
        """Lista datastore"""
        return self._get("admin/datastore")

    def get_datastore_status(self, name: str) -> Dict:
        """Stato di un datastore"""
        return self._get(f"admin/datastore/{name}/status")

    def get_tasks(self, since: datetime = None, limit: int = 500) -> List[Dict]:
        """Lista task recenti"""
        params = {"limit": limit}
        if since:
            params["since"] = int(since.timestamp())
        return self._get("nodes/localhost/tasks", params)


def collect_server_status(pbs: PBSClient, syslog: SyslogSender, client: Dict, test_mode: bool):
    """Raccoglie e invia stato server PBS"""
    logger.info("Raccolta stato server PBS...")

    try:
        status = pbs.get_server_status()

        data = {
            "status": "success",
            "server_name": socket.gethostname(),
            "pbs_version": status.get("info", {}).get("version", "unknown"),
            "uptime_seconds": status.get("uptime", 0),
            "uptime_hours": round(status.get("uptime", 0) / 3600, 1),
            "cpu_percent": round(status.get("cpu", 0) * 100, 1),
            "memory_used_bytes": status.get("memory", {}).get("used", 0),
            "memory_total_bytes": status.get("memory", {}).get("total", 0),
            "memory_used_percent": round(
                status.get("memory", {}).get("used", 0) /
                max(status.get("memory", {}).get("total", 1), 1) * 100, 1
            ),
            "load_average": status.get("loadavg", [0, 0, 0]),
        }

        syslog.send("PBS_SERVER_STATUS", data, client, test_mode)
    except Exception as e:
        logger.error(f"Errore raccolta stato server: {e}")


def collect_datastore_status(pbs: PBSClient, syslog: SyslogSender, client: Dict, test_mode: bool):
    """Raccoglie e invia stato datastore"""
    logger.info("Raccolta stato datastore...")

    try:
        datastores = pbs.get_datastores()

        for ds in datastores:
            ds_name = ds.get("store", ds.get("name", "unknown"))
            try:
                ds_status = pbs.get_datastore_status(ds_name)

                total = ds_status.get("total", 0)
                used = ds_status.get("used", 0)
                avail = ds_status.get("avail", total - used)
                used_percent = round((used / max(total, 1)) * 100, 1)

                status = "success"
                if used_percent > 90:
                    status = "warning"
                if used_percent > 95:
                    status = "failed"

                data = {
                    "status": status,
                    "datastore_name": ds_name,
                    "total_bytes": total,
                    "used_bytes": used,
                    "free_bytes": avail,
                    "used_percent": used_percent,
                    "total_gb": round(total / (1024**3), 2),
                    "used_gb": round(used / (1024**3), 2),
                    "free_gb": round(avail / (1024**3), 2),
                }

                syslog.send("PBS_DATASTORE_STATUS", data, client, test_mode)
            except Exception as e:
                logger.warning(f"Errore lettura datastore {ds_name}: {e}")
    except Exception as e:
        logger.error(f"Errore raccolta datastore: {e}")


def collect_backup_tasks(pbs: PBSClient, syslog: SyslogSender, client: Dict,
                         lookback_hours: int, test_mode: bool):
    """Raccoglie e invia risultati task backup"""
    logger.info("Raccolta risultati task backup...")

    try:
        since = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
        tasks = pbs.get_tasks(since=since)

        # Filtra solo task di backup completati
        backup_tasks = [
            t for t in tasks
            if t.get("worker_type", "").startswith("backup")
            and t.get("endtime")
        ]

        logger.info(f"Trovati {len(backup_tasks)} task backup")

        for task in backup_tasks:
            starttime = task.get("starttime", 0)
            endtime = task.get("endtime", 0)
            duration = endtime - starttime if endtime and starttime else 0

            task_status = task.get("status", "")
            status = "success" if task_status == "OK" else "failed" if "error" in task_status.lower() else "warning"

            # Parse worker_id per estrarre datastore e backup-id
            worker_id = task.get("worker_id", "")
            parts = worker_id.split(":") if ":" in worker_id else [worker_id]
            datastore = parts[0] if parts else ""
            backup_id = parts[1] if len(parts) > 1 else ""

            # Determina tipo oggetto (vm, ct, host)
            object_type = "host"
            if "vm/" in worker_id.lower():
                object_type = "vm"
            elif "ct/" in worker_id.lower():
                object_type = "ct"

            data = {
                "status": status,
                "task_id": task.get("upid", ""),
                "worker_type": task.get("worker_type", ""),
                "datastore": datastore,
                "backup_id": backup_id,
                "object_type": object_type,
                "start_time": datetime.fromtimestamp(starttime, tz=timezone.utc).isoformat() if starttime else None,
                "end_time": datetime.fromtimestamp(endtime, tz=timezone.utc).isoformat() if endtime else None,
                "duration_seconds": duration,
                "duration_minutes": round(duration / 60, 1),
                "result_message": task_status,
                "user": task.get("user", ""),
            }

            syslog.send("PBS_BACKUP_RESULT", data, client, test_mode)

    except Exception as e:
        logger.error(f"Errore raccolta task: {e}")


def collect_daily_report(pbs: PBSClient, syslog: SyslogSender, client: Dict,
                         lookback_hours: int, test_mode: bool):
    """Genera report giornaliero con riepilogo di tutti i task"""
    logger.info("Generazione report giornaliero...")

    try:
        since = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
        tasks = pbs.get_tasks(since=since)

        backup_tasks = [
            t for t in tasks
            if t.get("worker_type", "").startswith("backup")
            and t.get("endtime")
        ]

        jobs = []
        for task in backup_tasks:
            starttime = task.get("starttime", 0)
            endtime = task.get("endtime", 0)
            duration = endtime - starttime if endtime and starttime else 0

            task_status = task.get("status", "")
            status = "success" if task_status == "OK" else "failed" if "error" in task_status.lower() else "warning"

            worker_id = task.get("worker_id", "")
            parts = worker_id.split(":") if ":" in worker_id else [worker_id]

            jobs.append({
                "backup_id": parts[1] if len(parts) > 1 else worker_id,
                "datastore": parts[0] if parts else "",
                "status": status,
                "start_time": datetime.fromtimestamp(starttime, tz=timezone.utc).isoformat() if starttime else None,
                "end_time": datetime.fromtimestamp(endtime, tz=timezone.utc).isoformat() if endtime else None,
                "duration_minutes": round(duration / 60, 1),
                "result_message": task_status,
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

        syslog.send("PBS_DAILY_REPORT", data, client, test_mode)
        logger.info(f"Report giornaliero: {len(jobs)} task ({success_count} ok, {warning_count} warning, {failed_count} failed)")

    except Exception as e:
        logger.error(f"Errore generazione report giornaliero: {e}")


def main():
    parser = argparse.ArgumentParser(description="PBS Backup Monitor - Syslog sender")
    parser.add_argument("-c", "--config", default="/etc/backup-monitor/pbs-config.yaml",
                        help="Path al file di configurazione")
    parser.add_argument("--test", action="store_true", help="Modalita test (stampa syslog)")
    parser.add_argument("--daily-report", action="store_true", help="Invia report giornaliero")
    args = parser.parse_args()

    # Carica configurazione
    with open(args.config, "r") as f:
        config = yaml.safe_load(f)

    logger.info(f"=== PBS Backup Monitor v{VERSION} ===")

    # Inizializza client
    pbs_cfg = config["pbs"]
    pbs = PBSClient(
        host=pbs_cfg["host"],
        port=pbs_cfg.get("port", 8007),
        user=pbs_cfg["user"],
        token_name=pbs_cfg["token_name"],
        token_value=pbs_cfg["token_value"],
        verify_ssl=pbs_cfg.get("verify_ssl", False)
    )

    syslog_cfg = config["syslog"]
    syslog = SyslogSender(
        server=syslog_cfg["server"],
        port=syslog_cfg["port"],
        facility=syslog_cfg.get("facility", "local0")
    )

    client = config["client"]
    lookback = pbs_cfg.get("lookback_hours", 24)

    # Raccogli e invia dati
    if args.daily_report:
        collect_daily_report(pbs, syslog, client, lookback, args.test)
    else:
        collect_server_status(pbs, syslog, client, args.test)
        collect_datastore_status(pbs, syslog, client, args.test)
        collect_backup_tasks(pbs, syslog, client, lookback, args.test)

    logger.info("=== Completato ===")


if __name__ == "__main__":
    main()
