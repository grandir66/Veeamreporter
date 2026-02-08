<#
.SYNOPSIS
    Veeam Backup Monitor - Invia stato backup e server a Graylog via Syslog

.DESCRIPTION
    Raccoglie dati da Veeam B&R e li invia a Graylog in formato JSON strutturato.
    Invia:
    - Risultati job di backup
    - Stato del server Veeam
    - Stato dei repository

.EXAMPLE
    .\VeeamBackupMonitor.ps1 -ConfigPath .\config.json
    .\VeeamBackupMonitor.ps1 -ConfigPath .\config.json -TestMode

.NOTES
    Richiede: Veeam Backup & Replication PowerShell Module
#>

[CmdletBinding()]
param(
    [string]$ConfigPath = "$PSScriptRoot\config.json",
    [switch]$TestMode
)

$ErrorActionPreference = "Stop"
$Script:Version = "2.0.0"

#region Logging
function Write-Log {
    param([string]$Message, [string]$Level = "Info")
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $log = "[$ts] [$Level] $Message"
    Write-Host $log -ForegroundColor $(switch($Level) { "Error" {"Red"} "Warning" {"Yellow"} default {"White"} })
    if ($Script:Config.log_path) {
        $logFile = Join-Path $Script:Config.log_path "veeam-monitor-$(Get-Date -Format 'yyyy-MM-dd').log"
        Add-Content -Path $logFile -Value $log -ErrorAction SilentlyContinue
    }
}
#endregion

#region Syslog
function Send-Syslog {
    param(
        [string]$MessageType,
        [hashtable]$Data
    )

    $syslog = $Script:Config.syslog

    # Severity: 6=Info, 4=Warning, 3=Error
    $severity = switch ($Data.status) {
        "success" { 6 }
        "warning" { 4 }
        "failed"  { 3 }
        default   { 6 }
    }

    $facilityMap = @{ "local0"=16; "local1"=17; "local2"=18; "local3"=19; "local4"=20; "local5"=21; "local6"=22; "local7"=23 }
    $facility = $facilityMap[$syslog.facility]
    $priority = ($facility * 8) + $severity

    $timestamp = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ss.fffZ")
    $hostname = $env:COMPUTERNAME

    # Costruisci JSON payload
    $payload = @{
        message_type = $MessageType
        version = $Script:Version
        timestamp = $timestamp
        client = $Script:Config.client
        agent_hostname = $hostname
    } + $Data

    $jsonPayload = $payload | ConvertTo-Json -Depth 10 -Compress

    # RFC 5424 format
    $syslogMsg = "<$priority>1 $timestamp $hostname veeam-backup-monitor $PID $MessageType - $jsonPayload"

    if ($TestMode) {
        Write-Host "`n=== SYSLOG MESSAGE ===" -ForegroundColor Cyan
        Write-Host $syslogMsg
        Write-Host "======================`n" -ForegroundColor Cyan
        return
    }

    try {
        $udpClient = New-Object System.Net.Sockets.UdpClient
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($syslogMsg)
        $udpClient.Send($bytes, $bytes.Length, $syslog.server, $syslog.port) | Out-Null
        $udpClient.Close()
        Write-Log "Syslog inviato: $MessageType"
    }
    catch {
        Write-Log "Errore invio syslog: $_" -Level Error
    }
}
#endregion

#region Veeam Functions
function Initialize-Veeam {
    Write-Log "Caricamento modulo Veeam..."
    if (-not (Get-Module -Name Veeam.Backup.PowerShell -ErrorAction SilentlyContinue)) {
        try {
            Import-Module Veeam.Backup.PowerShell -ErrorAction Stop
        }
        catch {
            $altPath = "C:\Program Files\Veeam\Backup and Replication\Console\Veeam.Backup.PowerShell\Veeam.Backup.PowerShell.psd1"
            if (Test-Path $altPath) {
                Import-Module $altPath -ErrorAction Stop
            } else {
                throw "Modulo Veeam PowerShell non trovato"
            }
        }
    }
    Write-Log "Modulo Veeam caricato"
}

function Get-VeeamServerStatus {
    Write-Log "Raccolta stato server Veeam..."

    $serverInfo = Get-VBRServerSession
    $version = (Get-ItemProperty "HKLM:\SOFTWARE\Veeam\Veeam Backup and Replication" -ErrorAction SilentlyContinue).CoreVersion

    $status = @{
        status = "success"
        server_name = $env:COMPUTERNAME
        veeam_version = $version
        server_time = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
        uptime_hours = [math]::Round((Get-CimInstance Win32_OperatingSystem).LastBootUpTime.Subtract((Get-Date)).TotalHours * -1, 1)
        cpu_percent = [math]::Round((Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average, 1)
        memory_free_gb = [math]::Round((Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory / 1MB, 2)
        memory_total_gb = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB, 2)
    }

    Send-Syslog -MessageType "VEEAM_SERVER_STATUS" -Data $status
}

function Get-VeeamServiceStatus {
    Write-Log "Raccolta stato servizi Veeam..."

    try {
        $veeamServices = Get-Service -Name "Veeam*" -ErrorAction SilentlyContinue
        if (-not $veeamServices) {
            Write-Log "Nessun servizio Veeam trovato" -Level Warning
            return
        }

        $services = @()
        foreach ($svc in $veeamServices) {
            $services += @{
                name = $svc.Name
                display_name = $svc.DisplayName
                state = $svc.Status.ToString()
                startup_type = $svc.StartType.ToString()
            }
        }

        # Servizi Automatic che non sono Running = problema
        $autoStopped = $veeamServices | Where-Object { $_.StartType -eq 'Automatic' -and $_.Status -ne 'Running' }
        $status = if ($autoStopped) { "failed" } else { "success" }

        $serviceData = @{
            status = $status
            services_total = $veeamServices.Count
            services_running = ($veeamServices | Where-Object { $_.Status -eq 'Running' }).Count
            services_stopped = ($veeamServices | Where-Object { $_.Status -ne 'Running' }).Count
            services = $services
        }

        Send-Syslog -MessageType "VEEAM_SERVICE_STATUS" -Data $serviceData
    }
    catch {
        Write-Log "Errore raccolta stato servizi: $_" -Level Warning
    }
}

function Get-VeeamRepositoryStatus {
    Write-Log "Raccolta stato repository..."

    $repos = Get-VBRBackupRepository
    foreach ($repo in $repos) {
        try {
            $container = $repo.GetContainer()
            $freeBytes = $container.CachedFreeSpace.InBytes
            $totalBytes = $container.CachedTotalSpace.InBytes
            $usedPercent = if ($totalBytes -gt 0) { [math]::Round((1 - $freeBytes/$totalBytes) * 100, 1) } else { 0 }

            $status = if ($usedPercent -gt 95) { "failed" } elseif ($usedPercent -gt 90) { "warning" } else { "success" }

            $repoData = @{
                status = $status
                repository_name = $repo.Name
                repository_type = $repo.Type.ToString()
                repository_path = $repo.FriendlyPath
                total_bytes = $totalBytes
                free_bytes = $freeBytes
                used_percent = $usedPercent
                total_gb = [math]::Round($totalBytes / 1GB, 2)
                free_gb = [math]::Round($freeBytes / 1GB, 2)
            }

            Send-Syslog -MessageType "VEEAM_REPOSITORY_STATUS" -Data $repoData
        }
        catch {
            Write-Log "Errore lettura repository $($repo.Name): $_" -Level Warning
        }
    }
}

function Get-VeeamJobResults {
    Write-Log "Raccolta risultati job backup..."

    $lookbackHours = $Script:Config.veeam.lookback_hours
    $cutoffTime = (Get-Date).AddHours(-$lookbackHours)

    # Backup Jobs
    $jobs = Get-VBRJob -WarningAction SilentlyContinue
    foreach ($job in $jobs) {
        $sessions = Get-VBRBackupSession | Where-Object {
            $_.JobId -eq $job.Id -and $_.EndTime -gt $cutoffTime -and $_.EndTime -ne $null
        } | Sort-Object EndTime -Descending | Select-Object -First 1

        foreach ($session in $sessions) {
            $status = switch ($session.Result.ToString()) {
                "Success" { "success" }
                "Warning" { "warning" }
                "Failed"  { "failed" }
                default   { "unknown" }
            }

            $duration = if ($session.EndTime -and $session.CreationTime) {
                [int]($session.EndTime - $session.CreationTime).TotalSeconds
            } else { 0 }

            # Dettaglio VM/oggetti processati
            $taskSessions = Get-VBRTaskSession -Session $session -ErrorAction SilentlyContinue
            $objects = @()
            foreach ($task in $taskSessions) {
                $obj = @{
                    name = $task.Name
                    status = $task.Status.ToString().ToLower()
                    size_bytes = $task.Progress.ProcessedSize
                    duration_seconds = if ($task.Progress.Duration) { [int]$task.Progress.Duration.TotalSeconds } else { 0 }
                }
                # Aggiungi motivo errore/warning per oggetti non success
                if ($task.Status -ne 'Success' -and $task.Info.Reason) {
                    $obj.error_message = $task.Info.Reason
                }
                $objects += $obj
            }

            # Bottleneck rilevato da Veeam
            $bottleneck = $session.Progress.BottleneckInfo
            $bottleneckStr = if ($bottleneck) { $bottleneck.ToString() } else { "None" }

            $jobData = @{
                status = $status
                job_id = $job.Id.ToString()
                job_name = $job.Name
                job_type = $job.JobType.ToString()
                start_time = $session.CreationTime.ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
                end_time = $session.EndTime.ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
                duration_seconds = $duration
                duration_minutes = [math]::Round($duration / 60, 1)
                result_message = $session.Info.Reason
                bottleneck = $bottleneckStr
                is_retry = $session.IsRetryMode
                data_size_bytes = $session.Progress.ProcessedSize
                data_size_gb = [math]::Round($session.Progress.ProcessedSize / 1GB, 2)
                transferred_bytes = $session.Progress.TransferedSize
                transferred_gb = [math]::Round($session.Progress.TransferedSize / 1GB, 2)
                objects_total = $objects.Count
                objects_success = ($objects | Where-Object { $_.status -eq "success" }).Count
                objects_warning = ($objects | Where-Object { $_.status -eq "warning" }).Count
                objects_failed = ($objects | Where-Object { $_.status -eq "failed" }).Count
                objects = $objects
            }

            Send-Syslog -MessageType "VEEAM_JOB_RESULT" -Data $jobData
        }
    }
}
#endregion

#region Main
try {
    Write-Log "=== Avvio Veeam Backup Monitor v$Script:Version ==="

    # Carica configurazione
    if (-not (Test-Path $ConfigPath)) {
        throw "File configurazione non trovato: $ConfigPath"
    }
    $Script:Config = Get-Content $ConfigPath -Raw | ConvertFrom-Json

    # Crea directory log
    if ($Script:Config.log_path -and -not (Test-Path $Script:Config.log_path)) {
        New-Item -ItemType Directory -Path $Script:Config.log_path -Force | Out-Null
    }

    # Inizializza Veeam
    Initialize-Veeam

    # Raccogli e invia dati
    Get-VeeamServerStatus
    Get-VeeamServiceStatus
    Get-VeeamRepositoryStatus
    Get-VeeamJobResults

    Write-Log "=== Completato ==="
}
catch {
    Write-Log "ERRORE: $_" -Level Error
    exit 1
}
#endregion
