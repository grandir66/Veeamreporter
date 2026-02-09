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
$config = Get-Content $configPath -Raw | ConvertFrom-Json

# Se il config e gia stato compilato (server syslog diverso dal placeholder), salta la configurazione
$isConfigured = $config.syslog.server -and $config.syslog.server -ne "graylog.example.com"

if ($isConfigured) {
    Write-Host "`nConfigurazione esistente trovata in '$configPath'" -ForegroundColor Green
    Write-Host "  Cliente: $($config.client.code) - $($config.client.name)" -ForegroundColor Cyan
    Write-Host "  Syslog:  $($config.syslog.server):$($config.syslog.port)" -ForegroundColor Cyan
    Write-Host "  (per riconfigurare, modificare config.json o eliminarlo prima di reinstallare)" -ForegroundColor DarkGray
} else {
    Write-Host "`n=== Configurazione Backup Monitor ===" -ForegroundColor Yellow

    $clientCode = Read-Host "Codice cliente (es. CLI001)"
    $clientName = Read-Host "Nome cliente (es. Azienda Srl)"
    $clientSite = Read-Host "Sede (es. sede-principale) [Invio = sede-principale]"
    $syslogServer = Read-Host "Server Graylog - IP o hostname (es. 192.168.1.100)"
    $syslogPort = Read-Host "Porta syslog [Invio = 4514]"

    # Applica valori (con default per campi opzionali)
    $config.client.code = $clientCode
    $config.client.name = $clientName
    $config.client.site = if ($clientSite) { $clientSite } else { "sede-principale" }
    $config.syslog.server = $syslogServer
    $config.syslog.port = if ($syslogPort) { [int]$syslogPort } else { 4514 }

    # Salva config.json
    $config | ConvertTo-Json -Depth 5 | Set-Content -Path $configPath -Encoding UTF8
    Write-Host "`nConfigurazione salvata in '$configPath'" -ForegroundColor Green
    Write-Host "  Cliente: $($config.client.code) - $($config.client.name)" -ForegroundColor Cyan
    Write-Host "  Syslog:  $($config.syslog.server):$($config.syslog.port)" -ForegroundColor Cyan
}

# --- Creazione task schedulato ---
Write-Host "`n=== Installazione Task Schedulato ===" -ForegroundColor Yellow

$taskName = "VeeamBackupMonitor"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$InstallPath\VeeamBackupMonitor.ps1`" -ConfigPath `"$InstallPath\config.json`""

$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 30)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest

Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal

Write-Host "`nTask '$taskName' creato (ogni 30 minuti)" -ForegroundColor Green
Write-Host "`nInstallazione completata. Per testare:" -ForegroundColor Yellow
Write-Host "  powershell -ExecutionPolicy Bypass -File `"$InstallPath\VeeamBackupMonitor.ps1`" -TestMode" -ForegroundColor White
