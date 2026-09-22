$ErrorActionPreference = 'Stop'
$projectDir = $PSScriptRoot
$runtimePython = Join-Path $projectDir '.venv\Scripts\python.exe'
& $runtimePython `
    (Join-Path $projectDir 'stock_data_api.py') `
    --database (Join-Path $projectDir 'analysis_data\stocks_analysis.sqlite') `
    --host '127.0.0.1' `
    --port 8765
