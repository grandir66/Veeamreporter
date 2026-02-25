<#
.SYNOPSIS
    Installa task schedulato per Veeam Backup Monitor

.DESCRIPTION
    Chiede i dati del cliente, compila config.json e crea il task schedulato.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\Install-Task.ps1 -InstallPath "C:\BackupMonitor"
#>

param(
    [Parameter(Mandatory)]
    [string]$InstallPath
)

#Requires -RunAsAdministrator

# Sblocca script scaricati da internet (rimuove Zone.Identifier)
Get-ChildItem -Path $InstallPath -Filter "*.ps1" | Unblock-File -ErrorAction SilentlyContinue
Get-ChildItem -Path $InstallPath -Filter "*.json" | Unblock-File -ErrorAction SilentlyContinue
Write-Host "File sbloccati in '$InstallPath'" -ForegroundColor Cyan

# --- Configurazione ---
$configPath = Join-Path $InstallPath "config.json"
$templatePath = Join-Path $InstallPath "config.example.json"

# Se config.json non esiste, crealo dal template
if (-not (Test-Path $configPath)) {
    if (Test-Path $templatePath) {
        Copy-Item $templatePath $configPath
        Write-Host "Creato config.json dal template" -ForegroundColor Cyan
    } else {
        throw "Nessun file di configurazione trovato ($configPath o $templatePath)"
    }
}

$config = Get-Content $configPath -Raw | ConvertFrom-Json

# Se il config e gia stato compilato (server syslog diverso dal placeholder), salta la configurazione
$isConfigured = $config.syslog.server -and $config.syslog.server -ne "graylog.example.com"

if ($isConfigured) {
    Write-Host "`nConfigurazione esistente trovata in '$configPath'" -ForegroundColor Green
    Write-Host "  Cliente: $($config.client.code) - $($config.client.name)" -ForegroundColor Cyan
    Write-Host "  Syslog:  $($config.syslog.server):$($config.syslog.port)" -ForegroundColor Cyan
    # Aggiornamento: abilita GELF se mancante (stesso server syslog, porta 8514)
    $needsSave = $false
    if (-not $config.gelf -or -not $config.gelf.server) {
        if (-not $config.gelf) { $config | Add-Member -MemberType NoteProperty -Name "gelf" -Value ([PSCustomObject]@{}) -Force }
        $config.gelf.server = $config.syslog.server
        $config.gelf.port = 8514
        $config.gelf.protocol = "udp"
        $needsSave = $true
    }
    if ($needsSave) {
        $config | ConvertTo-Json -Depth 5 | Set-Content -Path $configPath -Encoding UTF8
        Write-Host "  GELF:    $($config.gelf.server):$($config.gelf.port) (abilitato)" -ForegroundColor Cyan
    }
    Write-Host "  (per riconfigurare, modificare config.json o eliminarlo prima di reinstallare)" -ForegroundColor DarkGray
} else {
    Write-Host "`n=== Configurazione Backup Monitor ===" -ForegroundColor Yellow

    $clientCode = Read-Host "Codice cliente (es. CLI001)"
    $clientName = Read-Host "Nome cliente (es. Azienda Srl)"
    $clientSite = Read-Host "Sede (es. sede-principale) [Invio = sede-principale]"
    $syslogServerDefault = "syslog.domarc.it"
    $syslogServerInput = Read-Host "Server Graylog - IP o hostname [Invio = $syslogServerDefault]"
    $syslogServer = if ($syslogServerInput) { $syslogServerInput } else { $syslogServerDefault }
    $syslogPort = Read-Host "Porta syslog [Invio = 4514]"

    # Applica valori (con default per campi opzionali)
    $config.client.code = $clientCode
    $config.client.name = $clientName
    $config.client.site = if ($clientSite) { $clientSite } else { "sede-principale" }
    $config.syslog.server = $syslogServer
    $config.syslog.port = if ($syslogPort) { [int]$syslogPort } else { 4514 }
    # GELF: stesso server di syslog, porta 8514 (abilitato di default)
    if (-not $config.gelf) { $config | Add-Member -MemberType NoteProperty -Name "gelf" -Value ([PSCustomObject]@{}) -Force }
    $config.gelf.server = $syslogServer
    $config.gelf.port = 8514
    $config.gelf.protocol = "udp"

    # Salva config.json
    $config | ConvertTo-Json -Depth 5 | Set-Content -Path $configPath -Encoding UTF8
    Write-Host "`nConfigurazione salvata in '$configPath'" -ForegroundColor Green
    Write-Host "  Cliente: $($config.client.code) - $($config.client.name)" -ForegroundColor Cyan
    Write-Host "  Syslog:  $($config.syslog.server):$($config.syslog.port)" -ForegroundColor Cyan
    Write-Host "  GELF:    $($config.gelf.server):$($config.gelf.port) (heartbeat)" -ForegroundColor Cyan
}

# --- Creazione task schedulato ---
Write-Host "`n=== Installazione Task Schedulato ===" -ForegroundColor Yellow

$taskName = "VeeamBackupMonitor"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$InstallPath\VeeamBackupMonitor.ps1`" -ConfigPath `"$InstallPath\config.json`""

$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 30) -RepetitionDuration (New-TimeSpan -Days 9999)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest

Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal

Write-Host "`nTask '$taskName' creato (ogni 30 minuti)" -ForegroundColor Green

# --- Task report giornaliero ---
$dailyTaskName = "VeeamBackupMonitor-DailyReport"
$dailyAction = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$InstallPath\VeeamBackupMonitor.ps1`" -ConfigPath `"$InstallPath\config.json`" -DailyReport"

$dailyTrigger = New-ScheduledTaskTrigger -Daily -At "07:00"
$dailySettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable

Unregister-ScheduledTask -TaskName $dailyTaskName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $dailyTaskName -Action $dailyAction -Trigger $dailyTrigger -Settings $dailySettings -Principal $principal

Write-Host "Task '$dailyTaskName' creato (ogni giorno alle 07:00)" -ForegroundColor Green
Write-Host "`nInstallazione completata. Per testare:" -ForegroundColor Yellow
Write-Host "  powershell -ExecutionPolicy Bypass -File `"$InstallPath\VeeamBackupMonitor.ps1`" -TestMode" -ForegroundColor White
Write-Host "  powershell -ExecutionPolicy Bypass -File `"$InstallPath\VeeamBackupMonitor.ps1`" -TestMode -DailyReport" -ForegroundColor White
