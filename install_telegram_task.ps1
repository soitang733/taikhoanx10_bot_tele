$ErrorActionPreference = 'Stop'

$projectDir = $PSScriptRoot
$envPath = Join-Path $projectDir '.env'
if (-not (Test-Path -LiteralPath $envPath)) {
    throw "Chưa có $envPath. Hãy thêm TELEGRAM_BOT_TOKEN và TELEGRAM_ALLOWED_CHAT_IDS trước."
}
$envText = Get-Content -LiteralPath $envPath -Raw
if ($envText -notmatch '(?m)^\s*TELEGRAM_BOT_TOKEN\s*=\s*["'']?[^"''\r\n ]+' -or
    ($envText -notmatch '(?m)^\s*TELEGRAM_PUBLIC_ACCESS\s*=\s*["'']?(?:true|1|yes|on)["'']?\s*$' -and
     $envText -notmatch '(?m)^\s*TELEGRAM_ALLOWED_CHAT_IDS\s*=\s*["'']?-?\d+')) {
    throw 'Thiếu TELEGRAM_BOT_TOKEN hoặc chưa bật TELEGRAM_PUBLIC_ACCESS/chưa khai báo TELEGRAM_ALLOWED_CHAT_IDS.'
}

$taskName = 'VnStockTelegramBot'
$pythonWindowless = Join-Path $projectDir '.venv\Scripts\pythonw.exe'
$botScript = Join-Path $projectDir 'telegram_bot.py'
if (-not (Test-Path -LiteralPath $pythonWindowless)) {
    throw "Không tìm thấy Python trong .venv: $pythonWindowless"
}
$action = New-ScheduledTaskAction `
    -Execute $pythonWindowless `
    -Argument "`"$botScript`"" `
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
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force
Start-ScheduledTask -TaskName $taskName
Write-Output "Đã tạo và khởi động Scheduled Task: $taskName"
