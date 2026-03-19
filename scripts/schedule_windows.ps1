# Registra el bot en Windows Task Scheduler para correr 4x por dia
# Uso: ejecuta este script UNA VEZ como Administrador en PowerShell
# powershell -ExecutionPolicy Bypass -File scripts\schedule_windows.ps1

$ProjectPath = Split-Path -Parent $PSScriptRoot
$PythonExe   = (Get-Command python).Source
$BotScript   = Join-Path $ProjectPath "scripts\run_bot.py"
$LogFile     = Join-Path $ProjectPath "logs\scheduler.log"
$TaskName    = "PolymarketClaudeBot"

$Action = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument "$BotScript --mode paper --once >> `"$LogFile`" 2>&1" `
    -WorkingDirectory $ProjectPath

# Corre a las 6am, 12pm, 6pm, 12am
$Triggers = @(
    $(New-ScheduledTaskTrigger -Daily -At "06:00"),
    $(New-ScheduledTaskTrigger -Daily -At "12:00"),
    $(New-ScheduledTaskTrigger -Daily -At "18:00"),
    $(New-ScheduledTaskTrigger -Daily -At "00:00")
)

$Settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Triggers `
    -Settings $Settings `
    -Description "Polymarket Claude Bot — paper trading, 4x daily" `
    -Force

Write-Host ""
Write-Host "  [OK] Task '$TaskName' registrada en Task Scheduler"
Write-Host "  Corre a las: 06:00, 12:00, 18:00, 00:00"
Write-Host "  Logs en: $LogFile"
Write-Host ""
Write-Host "  Para ver la tarea: taskschd.msc"
Write-Host "  Para eliminarla:   Unregister-ScheduledTask -TaskName $TaskName"
