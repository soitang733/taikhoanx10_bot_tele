$ErrorActionPreference = 'Stop'
$projectDir = $PSScriptRoot
$runtimePython = Join-Path $projectDir '.venv\Scripts\python.exe'
& $runtimePython (Join-Path $projectDir 'telegram_bot.py')
