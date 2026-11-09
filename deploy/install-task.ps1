<#
  Установка задач Планировщика Windows для локального режима:
    tg-ref-bot         — бот запускается при входе в систему и сам перезапускается при падении
    tg-ref-bot-watchdog — каждые 5 минут поднимает бота, если он не работает
    tg-ref-daily       — ежедневная задача в заданное время (по умолчанию 21:00)

  Запуск (обычная PowerShell, права администратора не нужны):
    powershell -ExecutionPolicy Bypass -File deploy\install-task.ps1
#>
param(
    [string]$ProjectDir = (Split-Path -Parent $PSScriptRoot),
    [string]$ScheduleTime = "21:00",
    [string]$PythonExe = "",
    [int]$WatchdogMinutes = 5
)

$ErrorActionPreference = 'Stop'

if (-not $PythonExe) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if (-not $cmd) {
        $cmd = Get-Command py -ErrorAction SilentlyContinue
    }
    if (-not $cmd) { throw "Python не найден. Укажите путь: -PythonExe C:\Python312\python.exe" }
    $PythonExe = $cmd.Source
}

$ProjectDir = (Resolve-Path $ProjectDir).Path
$botScript = Join-Path $ProjectDir "tg_ref.py"
if (-not (Test-Path $botScript)) { throw "Не найден $botScript" }

try {
    $parts = $ScheduleTime -split '[:\s]+'
    $hour = [int]$parts[0]
    $minute = [int]$parts[1]
    $at = (Get-Date).Date.AddHours($hour).AddMinutes($minute)
} catch {
    throw "Неверное время: $ScheduleTime (нужно ЧЧ:ММ)"
}

Write-Host "Проект:    $ProjectDir"
Write-Host "Python:    $PythonExe"
Write-Host "Запуск:    $ScheduleTime"
Write-Host ""

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew

# 1. Бот: старт при входе + автоперезапуск при сбое
$botAction = New-ScheduledTaskAction -Execute $PythonExe -Argument "tg_ref.py bot" -WorkingDirectory $ProjectDir
$botTrigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
Register-ScheduledTask -TaskName "tg-ref-bot" -Action $botAction -Trigger $botTrigger `
    -Settings $settings -Principal $principal -Description "Бот трекера реферальных ссылок (Telegram)" -Force | Out-Null
Write-Host "✅ Задача tg-ref-bot — старт при входе в систему"

# 2. Watchdog: каждые N минут поднимает бота, если он упал
$wdScript = Join-Path $ProjectDir "deploy\botctl.cmd"
$wdAction = New-ScheduledTaskAction -Execute $wdScript -Argument "start" -WorkingDirectory $ProjectDir
$wdTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $WatchdogMinutes) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
Register-ScheduledTask -TaskName "tg-ref-bot-watchdog" -Action $wdAction -Trigger $wdTrigger `
    -Settings $settings -Principal $principal -Description "Watchdog: поднимает бота, если он не работает" -Force | Out-Null
Write-Host "✅ Задача tg-ref-bot-watchdog — каждые $WatchdogMinutes мин"

# 3. Ежедневная задача
$dailyAction = New-ScheduledTaskAction -Execute $PythonExe -Argument "tg_ref.py daily" -WorkingDirectory $ProjectDir
$dailyTrigger = New-ScheduledTaskTrigger -Daily -At $at
Register-ScheduledTask -TaskName "tg-ref-daily" -Action $dailyAction -Trigger $dailyTrigger `
    -Settings $settings -Principal $principal -Description "Ежедневные реферальные ссылки и отчёт" -Force | Out-Null
Write-Host "✅ Задача tg-ref-daily — ежедневно в $ScheduleTime"

Write-Host ""
Write-Host "Готово. Проверить:"
Write-Host "  Get-ScheduledTask -TaskName tg-ref-* | Select TaskName, State"
Write-Host ""
Write-Host "Запустить бота прямо сейчас:"
Write-Host "  Start-ScheduledTask -TaskName tg-ref-bot"
Write-Host ""
Write-Host "Удалить задачи:  powershell -ExecutionPolicy Bypass -File deploy\uninstall-task.ps1"
