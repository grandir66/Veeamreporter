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
    .\VeeamBackupMonitor.ps1 -VerboseLog
    .\VeeamBackupMonitor.ps1 -ConfigPath .\config.json -VerboseLog -DailyReport

.NOTES
    Richiede: Veeam Backup & Replication PowerShell Module
#>

[CmdletBinding()]
param(
    [string]$ConfigPath,
    [switch]$TestMode,
    [switch]$DailyReport,
    [switch]$VerboseLog
)

$ErrorActionPreference = "Stop"

# Risolvi ConfigPath: $PSScriptRoot puo essere vuoto in certi contesti di esecuzione
if (-not $ConfigPath) {
    $scriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
    $ConfigPath = Join-Path $scriptDir "config.json"
}
$Script:Version = "2.16.3"

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

# Log verbose: dati completi e dettagli errori per debug/test
function Write-VerboseLog {
    param([string]$Message, [string]$Level = "Verbose")
    if (-not $Script:VerboseLogEnabled) { return }
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss.fff"
    $log = "[$ts] [$Level] $Message"
    if ($Script:Config.log_path) {
        $verboseFile = Join-Path $Script:Config.log_path "veeam-monitor-verbose-$(Get-Date -Format 'yyyy-MM-dd').log"
        Add-Content -Path $verboseFile -Value $log -ErrorAction SilentlyContinue
    }
}

function Write-ErrorVerbose {
    param([string]$Context, $ErrorRecord)
    if (-not $Script:VerboseLogEnabled) { return }
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss.fff"
    $msg = if ($ErrorRecord.Exception) { $ErrorRecord.Exception.Message } else { $ErrorRecord.ToString() }
    $stack = if ($ErrorRecord.ScriptStackTrace) { $ErrorRecord.ScriptStackTrace } else { $ErrorRecord.Exception.StackTrace }
    $log = @"
[$ts] [ERROR] $Context
  Message: $msg
  StackTrace: $stack
"@
    if ($Script:Config.log_path) {
        $verboseFile = Join-Path $Script:Config.log_path "veeam-monitor-verbose-$(Get-Date -Format 'yyyy-MM-dd').log"
        Add-Content -Path $verboseFile -Value $log -ErrorAction SilentlyContinue
    }
}
#endregion

#region Syslog / GELF
# Messaggi heartbeat -> GELF 8514; VEEAM_JOB_RESULT -> Syslog 4514
$Script:HeartbeatTypes = @("VEEAM_SERVER_STATUS", "VEEAM_SERVICE_STATUS", "VEEAM_REPOSITORY_STATUS", "VEEAM_DAILY_REPORT")

function ConvertTo-GelfFields {
    param($InputObject, [string]$Prefix = "")
    $result = @{}
    $keys = if ($InputObject -is [hashtable]) { $InputObject.Keys } else { $InputObject.PSObject.Properties.Name }
    foreach ($key in $keys) {
        $val = if ($InputObject -is [hashtable]) { $InputObject[$key] } else { $InputObject.$key }
        $gelfKey = if ($Prefix) { "_${Prefix}_$key" } else { "_$key" }
        if ($val -is [hashtable] -or $val -is [PSCustomObject]) {
            $nested = if ($val -is [hashtable]) { $val } else {
                $ht = @{}; $val.PSObject.Properties | ForEach-Object { $ht[$_.Name] = $_.Value }; $ht
            }
            $flattened = ConvertTo-GelfFields -InputObject $nested -Prefix $key
            foreach ($k in $flattened.Keys) { $result[$k] = $flattened[$k] }
        } elseif ($null -ne $val) {
            $result[$gelfKey] = $val
        }
    }
    return $result
}

function Send-Gelf {
    param([string]$MessageType, [hashtable]$Data)
    $gelf = $Script:Config.gelf
    if (-not $gelf -or -not $gelf.server) { return }
    $level = switch ($Data.status) { "success" { 6 } "warning" { 4 } "failed" { 3 } default { 6 } }
    $ts = (Get-Date).ToUniversalTime()
    $tsIso = $ts.ToString("yyyy-MM-ddTHH:mm:ss.fffZ")
    $tsUnix = [math]::Round(($ts - [datetime]'1970-01-01Z').TotalSeconds, 3)
    $hostname = $env:COMPUTERNAME
    $payload = @{ message_type = $MessageType; version = $Script:Version; timestamp = $tsIso; client = $Script:Config.client; agent_hostname = $hostname } + $Data
    $gelfBase = @{ version = "1.1"; host = $hostname; short_message = "$MessageType : $($Data.status)"; full_message = ($payload | ConvertTo-Json -Depth 10 -Compress); timestamp = $tsUnix; level = $level }
    $custom = ConvertTo-GelfFields -InputObject $payload
    $gelfMsg = ($gelfBase + $custom) | ConvertTo-Json -Depth 10 -Compress
    Write-VerboseLog "=== GELF $MessageType ($($gelfMsg.Length) bytes) ==="
    Write-VerboseLog $gelfMsg
    if ($TestMode) { Write-Host "`n=== GELF ($($gelfMsg.Length) bytes) ===`n$gelfMsg`n======================`n" -ForegroundColor Cyan; return }
    $port = if ($gelf.port) { $gelf.port } else { 8514 }
    $proto = if ($gelf.protocol) { $gelf.protocol.ToLower() } else { "udp" }
    try {
        if ($proto -eq "tcp") {
            $tcp = New-Object System.Net.Sockets.TcpClient
            $tcp.Connect($gelf.server, $port)
            $tcp.GetStream().Write([System.Text.Encoding]::UTF8.GetBytes($gelfMsg + "`0"), 0, $gelfMsg.Length + 1)
            $tcp.Close()
        } else {
            $udp = New-Object System.Net.Sockets.UdpClient
            $udp.Send([System.Text.Encoding]::UTF8.GetBytes($gelfMsg), $gelfMsg.Length, $gelf.server, $port) | Out-Null
            $udp.Close()
        }
        Write-Log "GELF inviato ($proto): $MessageType -> $($gelf.server):$port"
    } catch { Write-Log "Errore GELF: $_" -Level Error; Write-ErrorVerbose -Context "Send-Gelf $MessageType" -ErrorRecord $_ }
}

function Send-Syslog {
    param([string]$MessageType, [hashtable]$Data)
    $syslog = $Script:Config.syslog
    $severity = switch ($Data.status) { "success" { 6 } "warning" { 4 } "failed" { 3 } default { 6 } }
    $facilityMap = @{ "local0"=16; "local1"=17; "local2"=18; "local3"=19; "local4"=20; "local5"=21; "local6"=22; "local7"=23 }
    $priority = ($facilityMap[$syslog.facility] * 8) + $severity
    $timestamp = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ss.fffZ")
    $hostname = $env:COMPUTERNAME
    $payload = @{ message_type = $MessageType; version = $Script:Version; timestamp = $timestamp; client = $Script:Config.client; agent_hostname = $hostname } + $Data
    $jsonPayload = $payload | ConvertTo-Json -Depth 10 -Compress
    $syslogMsg = "<$priority>1 $timestamp $hostname veeam-backup-monitor $PID $MessageType - $jsonPayload"
    Write-VerboseLog "=== SYSLOG $MessageType ($($syslogMsg.Length) bytes) ==="
    Write-VerboseLog $syslogMsg
    if ($TestMode) { Write-Host "`n=== SYSLOG ($($syslogMsg.Length) bytes) ===`n$syslogMsg`n======================`n" -ForegroundColor Cyan; return }
    $protocol = if ($syslog.protocol) { $syslog.protocol.ToLower() } else { "tcp" }
    try {
        if ($protocol -eq "tcp") {
            $tcp = New-Object System.Net.Sockets.TcpClient
            $tcp.Connect($syslog.server, $syslog.port)
            $tcp.GetStream().Write([System.Text.Encoding]::UTF8.GetBytes($syslogMsg + "`n"), 0, $syslogMsg.Length + 1)
            $tcp.Close()
            Write-Log "Syslog inviato (TCP): $MessageType ($($syslogMsg.Length) bytes)"
        } else {
            $udp = New-Object System.Net.Sockets.UdpClient
            $udp.Send([System.Text.Encoding]::UTF8.GetBytes($syslogMsg), $syslogMsg.Length, $syslog.server, $syslog.port) | Out-Null
            $udp.Close()
            Write-Log "Syslog inviato (UDP): $MessageType ($($syslogMsg.Length) bytes)"
        }
    } catch { Write-Log "Errore syslog: $_" -Level Error; Write-ErrorVerbose -Context "Send-Syslog $MessageType" -ErrorRecord $_ }
}

function Send-Message {
    param([string]$MessageType, [hashtable]$Data)
    if ($Script:HeartbeatTypes -contains $MessageType -and $Script:Config.gelf -and $Script:Config.gelf.server) {
        Send-Gelf -MessageType $MessageType -Data $Data
    } else {
        Send-Syslog -MessageType $MessageType -Data $Data
    }
}
#endregion

#region Veeam Functions
function Initialize-Veeam {
    Write-Log "Caricamento modulo Veeam..."
    if (-not (Get-Module -Name Veeam.Backup.PowerShell -ErrorAction SilentlyContinue)) {
        $prevVerbose = $VerbosePreference
        $prevWarning = $WarningPreference
        $VerbosePreference = 'SilentlyContinue'
        $WarningPreference = 'SilentlyContinue'
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
        finally {
            $VerbosePreference = $prevVerbose
            $WarningPreference = $prevWarning
        }
    }
    Write-Log "Modulo Veeam caricato"

    # Connessione esplicita al server locale (necessario con MFA abilitata in Veeam v12+)
    try {
        Disconnect-VBRServer -ErrorAction SilentlyContinue
        Connect-VBRServer -Server localhost -ErrorAction Stop
        Write-Log "Connesso al server Veeam locale"
    }
    catch {
        Write-Log "Connessione al server Veeam: $_" -Level Warning
    }
}

function Get-VeeamServerStatus {
    Write-Log "Raccolta stato server Veeam..."

    try {
        # Versione Veeam (registry con fallback su DLL)
        $version = $null
        try {
            $version = (Get-ItemProperty "HKLM:\SOFTWARE\Veeam\Veeam Backup and Replication" -ErrorAction SilentlyContinue).CoreVersion
            if (-not $version) {
                $dll = "C:\Program Files\Veeam\Backup and Replication\Backup\Veeam.Backup.Core.dll"
                if (Test-Path $dll) {
                    $version = (Get-Item $dll).VersionInfo.ProductVersion
                }
            }
        }
        catch {
            Write-Log "Errore lettura versione Veeam: $_" -Level Warning
        }
        Write-Log "Versione Veeam: $version"

        # Licenza (solo Get-VBRInstalledLicense, no InstanceLicenseSummary che puo bloccarsi)
        $licenseData = @{}
        try {
            Write-Log "Lettura licenza..."
            $license = Get-VBRInstalledLicense
            $licenseData = @{
                license_status = $license.Status.ToString()
                license_type = $license.Type.ToString()
                license_edition = $license.Edition.ToString()
                license_expiration = if ($license.ExpirationDate) { $license.ExpirationDate.ToString("yyyy-MM-dd") } else { $null }
                support_expiration = if ($license.SupportExpirationDate) { $license.SupportExpirationDate.ToString("yyyy-MM-dd") } else { $null }
                support_id = $license.SupportId
                licensed_instances = $license.InstanceLicenseSummary.LicensedInstancesNumber
                used_instances = $license.InstanceLicenseSummary.UsedInstancesNumber
            }
            Write-Log "Licenza: $($license.Edition) - $($license.Status)"
        }
        catch {
            Write-Log "Errore lettura licenza: $_" -Level Warning
        }

        # Sistema operativo (uptime, memoria) - con timeout 10s sulle chiamate CIM
        Write-Log "Lettura info sistema..."
        $uptimeHours = 0
        $memFreeGb = 0
        $memTotalGb = 0
        try {
            $os = Get-CimInstance Win32_OperatingSystem -OperationTimeoutSec 10
            $uptimeHours = [math]::Round($os.LastBootUpTime.Subtract((Get-Date)).TotalHours * -1, 1)
            $memFreeGb = [math]::Round($os.FreePhysicalMemory / 1MB, 2)
            $memTotalGb = [math]::Round((Get-CimInstance Win32_ComputerSystem -OperationTimeoutSec 10).TotalPhysicalMemory / 1GB, 2)
            Write-Log "Sistema: uptime ${uptimeHours}h, RAM ${memFreeGb}/${memTotalGb} GB"
        }
        catch {
            Write-Log "Errore lettura info sistema: $_" -Level Warning
        }

        # CPU - uso contatore performance (piu affidabile e veloce di Win32_Processor)
        # Get-Counter puo bloccarsi indefinitamente su alcuni sistemi (contatori corrotti, primo avvio)
        Write-Log "Lettura CPU..."
        $cpuPercent = 0
        $cpuJob = $null
        try {
            $cpuJob = Start-Job -ScriptBlock {
                (Get-Counter '\Processor(_Total)\% Processor Time' -ErrorAction Stop).CounterSamples[0].CookedValue
            }
            $completed = Wait-Job $cpuJob -Timeout 15
            if ($completed) {
                $cpuPercent = [math]::Round((Receive-Job $cpuJob), 1)
            } else {
                Stop-Job $cpuJob -ErrorAction SilentlyContinue
                Write-Log "Get-Counter timeout (15s), skip CPU" -Level Warning
            }
        }
        catch {
            Write-Log "Get-Counter fallito, skip CPU: $_" -Level Warning
        }
        finally {
            if ($cpuJob) { Remove-Job $cpuJob -Force -ErrorAction SilentlyContinue }
        }

        $status = @{
            status = "success"
            server_name = $env:COMPUTERNAME
            veeam_version = $version
            server_time = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
            uptime_hours = $uptimeHours
            cpu_percent = $cpuPercent
            memory_free_gb = $memFreeGb
            memory_total_gb = $memTotalGb
        } + $licenseData

        Send-Message -MessageType "VEEAM_SERVER_STATUS" -Data $status
    }
    catch {
        Write-Log "Errore raccolta stato server: $_" -Level Error
        Write-ErrorVerbose -Context "Get-VeeamServerStatus" -ErrorRecord $_
    }
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

        Send-Message -MessageType "VEEAM_SERVICE_STATUS" -Data $serviceData
    }
    catch {
        Write-Log "Errore raccolta stato servizi: $_" -Level Warning
        Write-ErrorVerbose -Context "Get-VeeamServiceStatus" -ErrorRecord $_
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

            Send-Message -MessageType "VEEAM_REPOSITORY_STATUS" -Data $repoData
        }
        catch {
            Write-Log "Errore lettura repository $($repo.Name): $_" -Level Warning
            Write-ErrorVerbose -Context "Get-VeeamRepositoryStatus $($repo.Name)" -ErrorRecord $_
        }
    }
}

function Get-VeeamJobResults {
    Write-Log "Raccolta risultati job backup..."

    try {
        $lookbackHours = $Script:Config.veeam.lookback_hours
        $cutoffTime = (Get-Date).AddHours(-$lookbackHours)
        Write-Log "Finestra: ultime ${lookbackHours}h (dal $($cutoffTime.ToString('yyyy-MM-dd HH:mm')))"

        # Carica tutti i job (backup + replica + qualsiasi tipo)
        $jobs = Get-VBRJob -WarningAction SilentlyContinue
        Write-Log "Trovati $($jobs.Count) job: $(($jobs | ForEach-Object { "$($_.Name)[$($_.JobType)]" }) -join ', ')"

        # Carica TUTTE le sessioni recenti UNA SOLA volta (evita N query al DB)
        Write-Log "Caricamento sessioni recenti..."
        $allSessions = @(Get-VBRBackupSession | Where-Object {
            $_.EndTime -gt $cutoffTime -and $_.EndTime -ne $null
        })

        # Includi anche sessioni di replica se disponibili
        try {
            $replicaSessions = @(Get-VBRReplicaSession -ErrorAction Stop | Where-Object {
                $_.EndTime -gt $cutoffTime -and $_.EndTime -ne $null
            })
            if ($replicaSessions.Count -gt 0) {
                $allSessions += $replicaSessions
                Write-Log "Aggiunte $($replicaSessions.Count) sessioni replica"
            }
        }
        catch {
            # Get-VBRReplicaSession non disponibile o sessioni gia incluse in Get-VBRBackupSession
        }

        Write-Log "Trovate $($allSessions.Count) sessioni nelle ultime ${lookbackHours}h"
        foreach ($s in $allSessions) {
            Write-Log "  - $($s.JobName): End=$($s.EndTime.ToString('dd/MM HH:mm')) Result=$($s.Result)"
        }

        foreach ($job in $jobs) {
            # Filtra in memoria (veloce)
            $session = $allSessions | Where-Object { $_.JobId -eq $job.Id } |
                Sort-Object EndTime -Descending | Select-Object -First 1

            if ($session) {
                # Sessione completata nel periodo di lookback - report completo
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
                    # Aggiungi informazioni di errore dettagliate per oggetti falliti/warning
                    if ($task.Status -ne 'Success') {
                        if ($task.Info -and $task.Info.Reason) {
                            $obj.error_message = $task.Info.Reason
                        }
                        # Prova a ottenere log dettagliati dell'errore dal task
                        try {
                            if ($task.Logger) {
                                $taskLog = $task.Logger.GetLog()
                                if ($taskLog -and $taskLog.UpdatedRecords) {
                                $errorRecords = $taskLog.UpdatedRecords | Where-Object { 
                                    $_.Title -match "error|failed|warning|exception" -or 
                                    $_.Status -eq "Error" -or 
                                    $_.Status -eq "Warning"
                                } | Select-Object -First 5
                                if ($errorRecords) {
                                    $obj.error_details = @($errorRecords | ForEach-Object { 
                                        @{
                                            title = $_.Title
                                            message = $_.Message
                                            status = $_.Status.ToString()
                                            time = $_.Time.ToString("yyyy-MM-ddTHH:mm:ssZ")
                                        }
                                    })
                                }
                            }
                            }
                        }
                        catch {
                            # Ignora errori nel recupero log dettagliati
                        }
                    }
                    $objects += $obj
                }

                # Gestisci bottleneck - estrai valore invece del tipo
                $bottleneckStr = "None"
                try {
                    if ($session.Progress -and $session.Progress.BottleneckInfo) {
                        $bottleneck = $session.Progress.BottleneckInfo
                        # Prova a ottenere il valore effettivo invece del tipo
                        if ($bottleneck -is [string]) {
                            $bottleneckStr = $bottleneck
                        } elseif ($bottleneck.ToString() -ne $bottleneck.GetType().FullName) {
                            $bottleneckStr = $bottleneck.ToString()
                        } else {
                            # Se ToString() restituisce il tipo, prova altre proprietà
                            $bottleneckStr = if ($bottleneck.Name) { $bottleneck.Name } else { "Unknown" }
                        }
                    }
                }
                catch {
                    $bottleneckStr = "None"
                }

                # Raccogli informazioni dettagliate sugli errori a livello di sessione
                $errorDetails = $null
                $errorLogs = @()
                if ($status -eq "failed" -or $status -eq "warning") {
                    try {
                        # Verifica che Logger esista prima di chiamare GetLog()
                        if ($session.Logger) {
                            $sessionLog = $session.Logger.GetLog()
                            if ($sessionLog -and $sessionLog.UpdatedRecords) {
                            # Filtra record di errore/warning
                            $errorRecords = $sessionLog.UpdatedRecords | Where-Object { 
                                $_.Status -eq "Error" -or 
                                $_.Status -eq "Warning" -or
                                $_.Title -match "error|failed|exception|warning"
                            } | Select-Object -First 10 | Sort-Object Time -Descending
                            
                            if ($errorRecords) {
                                $errorLogs = @($errorRecords | ForEach-Object {
                                    @{
                                        title = $_.Title
                                        message = $_.Message
                                        status = $_.Status.ToString()
                                        time = $_.Time.ToString("yyyy-MM-ddTHH:mm:ssZ")
                                    }
                                })
                            }
                        }
                        }
                    }
                    catch {
                        Write-Log "Errore recupero log dettagliati sessione: $_" -Level Warning
                    }
                    
                    # Se ci sono errori dettagliati, crea un riepilogo
                    if ($errorLogs.Count -gt 0) {
                        $summaryMsg = "Errore durante l'esecuzione del job"
                        if ($session.Info -and $session.Info.Reason) {
                            $summaryMsg = $session.Info.Reason
                        }
                        $errorDetails = @{
                            error_count = $errorLogs.Count
                            errors = $errorLogs
                            summary = $summaryMsg
                        }
                    }
                }

                $jobData = @{
                    status = $status
                    job_id = $job.Id.ToString()
                    job_name = $job.Name
                    job_type = $job.JobType.ToString()
                    start_time = $session.CreationTime.ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
                    end_time = $session.EndTime.ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
                    duration_seconds = $duration
                    duration_minutes = [math]::Round($duration / 60, 1)
                    result_message = if ($session.Info -and $session.Info.Reason) { $session.Info.Reason } else { "" }
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
                
                # Aggiungi dettagli errori solo se presenti
                if ($errorDetails) {
                    $jobData.error_details = $errorDetails
                }

                Send-Message -MessageType "VEEAM_JOB_RESULT" -Data $jobData
                Write-Log "Job '$($job.Name)': $status"
            }
            # Se non c'è sessione recente, NON inviare nulla (evita di sporcare la storia)
        }
    }
    catch {
        Write-Log "Errore raccolta risultati job: $_" -Level Error
        Write-ErrorVerbose -Context "Get-VeeamJobResults" -ErrorRecord $_
    }
}

function Get-VeeamDailyReport {
    Write-Log "Generazione report giornaliero..."

    $lookbackHours = $Script:Config.veeam.lookback_hours
    $cutoffTime = (Get-Date).AddHours(-$lookbackHours)

    $jobs = Get-VBRJob -WarningAction SilentlyContinue

    # Carica TUTTE le sessioni recenti UNA SOLA volta
    Write-Log "Caricamento sessioni per report..."
    $allSessions = @(Get-VBRBackupSession | Where-Object {
        $_.EndTime -gt $cutoffTime -and $_.EndTime -ne $null
    })
    try {
        $replicaSessions = @(Get-VBRReplicaSession -ErrorAction Stop | Where-Object {
            $_.EndTime -gt $cutoffTime -and $_.EndTime -ne $null
        })
        if ($replicaSessions.Count -gt 0) { $allSessions += $replicaSessions }
    } catch {}
    Write-Log "Sessioni trovate: $($allSessions.Count)"

    $allJobResults = @()

    foreach ($job in $jobs) {
        # Filtra in memoria per job
        $sessions = $allSessions | Where-Object { $_.JobId -eq $job.Id } |
            Sort-Object EndTime -Descending

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

            $allJobResults += @{
                job_name = $job.Name
                job_type = $job.JobType.ToString()
                status = $status
                start_time = $session.CreationTime.ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
                end_time = $session.EndTime.ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
                duration_minutes = [math]::Round($duration / 60, 1)
                data_size_gb = [math]::Round($session.Progress.ProcessedSize / 1GB, 2)
                transferred_gb = [math]::Round($session.Progress.TransferedSize / 1GB, 2)
                result_message = $session.Info.Reason
                is_retry = $session.IsRetryMode
            }
        }
    }

    $successCount = ($allJobResults | Where-Object { $_.status -eq "success" }).Count
    $warningCount = ($allJobResults | Where-Object { $_.status -eq "warning" }).Count
    $failedCount = ($allJobResults | Where-Object { $_.status -eq "failed" }).Count

    # Status complessivo: failed se almeno un fallimento, warning se almeno un warning
    $overallStatus = if ($failedCount -gt 0) { "failed" } elseif ($warningCount -gt 0) { "warning" } else { "success" }

    $reportData = @{
        status = $overallStatus
        report_date = (Get-Date).ToString("yyyy-MM-dd")
        lookback_hours = $lookbackHours
        jobs_total = $allJobResults.Count
        jobs_success = $successCount
        jobs_warning = $warningCount
        jobs_failed = $failedCount
        jobs = $allJobResults
    }

    Send-Message -MessageType "VEEAM_DAILY_REPORT" -Data $reportData
    Write-Log "Report giornaliero inviato: $($allJobResults.Count) job ($successCount ok, $warningCount warning, $failedCount failed)"
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

    # GELF abilitato di default: stesso server di syslog, porta 8514
    if (-not $Script:Config.gelf -or -not $Script:Config.gelf.server) {
        $gelfServer = if ($Script:Config.syslog -and $Script:Config.syslog.server) { $Script:Config.syslog.server } else { "localhost" }
        $Script:Config | Add-Member -MemberType NoteProperty -Name "gelf" -Value ([PSCustomObject]@{ server = $gelfServer; port = 8514; protocol = "udp" }) -Force
    }

    # Crea directory log
    if ($Script:Config.log_path -and -not (Test-Path $Script:Config.log_path)) {
        New-Item -ItemType Directory -Path $Script:Config.log_path -Force | Out-Null
    }

    # Log verbose: attivo con -VerboseLog o config.verbose_log (per test/debug)
    $Script:VerboseLogEnabled = ($VerboseLog -or $Script:Config.verbose_log) -and $Script:Config.log_path
    if ($VerboseLog -or $Script:Config.verbose_log) {
        if (-not $Script:Config.log_path) {
            Write-Log "verbose_log attivo ma log_path non configurato - log verbose disabilitato" -Level Warning
        } else {
            Write-Log "Log verbose attivo -> $($Script:Config.log_path)\veeam-monitor-verbose-$(Get-Date -Format 'yyyy-MM-dd').log"
            Write-VerboseLog "========== AVVIO ESECUZIONE (DailyReport=$DailyReport, TestMode=$TestMode) =========="
        }
    }

    # Inizializza Veeam
    Initialize-Veeam

    $logErr = { param($ctx, $err) Write-Log "ERRORE $ctx : $err" -Level Error; Write-ErrorVerbose -Context $ctx -ErrorRecord $err }
    if ($DailyReport) {
        # Report giornaliero (07:00): stato completo + riepilogo 24h
        try { Get-VeeamServerStatus } catch { & $logErr "Get-VeeamServerStatus" $_ }
        try { Get-VeeamServiceStatus } catch { & $logErr "Get-VeeamServiceStatus" $_ }
        try { Get-VeeamRepositoryStatus } catch { & $logErr "Get-VeeamRepositoryStatus" $_ }
        try { Get-VeeamDailyReport } catch { & $logErr "Get-VeeamDailyReport" $_ }
    } else {
        # Monitoraggio standard (ogni 30 min): stato sistema, repository e risultati job
        try { Get-VeeamServerStatus } catch { & $logErr "Get-VeeamServerStatus" $_ }
        try { Get-VeeamServiceStatus } catch { & $logErr "Get-VeeamServiceStatus" $_ }
        try { Get-VeeamRepositoryStatus } catch { & $logErr "Get-VeeamRepositoryStatus" $_ }
        try { Get-VeeamJobResults } catch { & $logErr "Get-VeeamJobResults" $_ }
    }

    Write-Log "=== Completato ==="
    if ($Script:VerboseLogEnabled) {
        Write-VerboseLog "========== FINE ESECUZIONE =========="
    }
}
catch {
    Write-Log "ERRORE: $_" -Level Error
    Write-ErrorVerbose -Context "Main" -ErrorRecord $_
    exit 1
}
finally {
    Disconnect-VBRServer -ErrorAction SilentlyContinue
}
#endregion
