$ErrorActionPreference = 'Stop'

$taskName = 'VnStockDailyData'
$projectDir = $PSScriptRoot
$scriptPath = Join-Path $projectDir 'run_daily_pipeline.ps1'
$powerShell = (Get-Command powershell.exe).Source

if (-not (Test-Path -LiteralPath $scriptPath)) {
    throw "Không tìm thấy pipeline: $scriptPath"
}

$action = New-ScheduledTaskAction `
    -Execute $powerShell `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$scriptPath`"" `
    -WorkingDirectory $projectDir
$trigger = New-ScheduledTaskTrigger -Daily -At '18:00'
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 3)
$principal = New-ScheduledTaskPrincipal `
    -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description 'Cập nhật dữ liệu chứng khoán và kho phân tích mỗi ngày lúc 18:00' `
    -Force

Write-Output "Đã tạo Scheduled Task: $taskName"
