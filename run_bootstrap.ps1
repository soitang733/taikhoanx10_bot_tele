$ErrorActionPreference = 'Stop'
$projectDir = $PSScriptRoot
$runtimePython = Join-Path $projectDir '.venv\Scripts\python.exe'

Set-Location -LiteralPath $projectDir
if (-not (Test-Path -LiteralPath $runtimePython)) {
    throw "Khong tim thay Python project: $runtimePython"
}
& $runtimePython (Join-Path $projectDir 'bootstrap_remaining.py') `
    --raw-dir (Join-Path $projectDir 'scraper_output') `
    --analysis-dir (Join-Path $projectDir 'analysis_data') `
    --workers 4
if ($LASTEXITCODE -ne 0) {
    throw "Bootstrap failed with exit code $LASTEXITCODE"
}
