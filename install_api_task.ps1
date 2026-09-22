$ErrorActionPreference = 'Stop'

$taskName = 'VnStockDataApi'
$projectDir = $PSScriptRoot
$pythonWindowless = Join-Path $projectDir '.venv\Scripts\pythonw.exe'
$apiScript = Join-Path $projectDir 'stock_data_api.py'
$database = Join-Path $projectDir 'analysis_data\stocks_analysis.sqlite'
if (-not (Test-Path -LiteralPath $pythonWindowless)) {
    throw "Không tìm thấy Python trong .venv: $pythonWindowless"
}

$action = New-ScheduledTaskAction `
    -Execute $pythonWindowless `
    -Argument "`"$apiScript`" --database `"$database`" --host 127.0.0.1 --port 8765" `
    -WorkingDirectory $projectDir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name)
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero)
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
    -Description 'Local read-only stock data API for the analysis bot' `
    -Force

Start-ScheduledTask -TaskName $taskName
Write-Output "Đã tạo và khởi động Scheduled Task: $taskName"
