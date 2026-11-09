<#
  Удаление задач Планировщика Windows, созданных install-task.ps1
#>
$ErrorActionPreference = 'Continue'

$names = @("tg-ref-bot", "tg-ref-bot-watchdog", "tg-ref-daily")
foreach ($name in $names) {
    if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $name -Confirm:$false
        Write-Host "✅ Задача $name удалена"
    } else {
        Write-Host "— Задача $name не найдена"
    }
}

Write-Host ""
Write-Host "Если бот запущен вручную — остановите его:  deploy\botctl.cmd stop"
