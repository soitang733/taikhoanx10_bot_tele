$ErrorActionPreference = 'Stop'
$projectDir = $PSScriptRoot
$runtimePython = Join-Path $projectDir '.venv\Scripts\python.exe'

Set-Location -LiteralPath $projectDir
& $runtimePython (Join-Path $projectDir 'daily_data_pipeline.py') `
    --raw-dir (Join-Path $projectDir 'scraper_output') `
    --analysis-dir (Join-Path $projectDir 'analysis_data') `
    --workers 4 `
    --overlap-days 2 `
    --history-audit-size 10 `
    --action-audit-size 20
if ($LASTEXITCODE -ne 0) {
    throw "Daily data pipeline failed with exit code $LASTEXITCODE"
}
