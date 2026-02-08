<#
.SYNOPSIS
    Installa task schedulato per Veeam Backup Monitor

.EXAMPLE
    .\Install-Task.ps1 -InstallPath "C:\BackupMonitor"
#>

param(
    [Parameter(Mandatory)]
    [string]$InstallPath
)

#Requires -RunAsAdministrator

$taskName = "VeeamBackupMonitor"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$InstallPath\VeeamBackupMonitor.ps1`" -ConfigPath `"$InstallPath\config.json`""

$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 30)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest

Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal

Write-Host "Task '$taskName' creato (ogni 30 minuti)" -ForegroundColor Green
