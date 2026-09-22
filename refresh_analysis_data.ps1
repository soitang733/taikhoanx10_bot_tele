$ErrorActionPreference = 'Stop'

$projectDir = $PSScriptRoot
$projectPython = Join-Path $projectDir '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $projectPython)) {
    throw "Không tìm thấy Python project: $projectPython"
}

& $projectPython `
    (Join-Path $projectDir 'prepare_analysis_data.py') `
    --input (Join-Path $projectDir 'scraper_output') `
    --output (Join-Path $projectDir 'analysis_data')
if ($LASTEXITCODE -ne 0) {
    throw "Chuẩn bị dữ liệu thất bại với mã lỗi $LASTEXITCODE"
}
