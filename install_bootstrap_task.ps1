$ErrorActionPreference = 'Stop'

$taskName = 'VnStockBootstrap'
$projectDir = $PSScriptRoot
$scriptPath = Join-Path $projectDir 'run_bootstrap.ps1'
$powerShell = (Get-Command powershell.exe).Source

$action = New-ScheduledTaskAction `
    -Execute $powerShell `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$scriptPath`"" `
    -WorkingDirectory $projectDir
$trigger = New-ScheduledTaskTrigger -Daily -At '00:30'
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 4)
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
    -Description 'Tiếp tục bootstrap lịch sử cho các mã chưa hoàn tất' `
    -Force

Write-Output "Đã tạo Scheduled Task: $taskName"
