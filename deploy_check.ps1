$ErrorActionPreference = 'Stop'

$projectDir = $PSScriptRoot
$runtimePython = Join-Path $projectDir '.venv\Scripts\python.exe'
$requiredFiles = @(
    $runtimePython,
    (Join-Path $projectDir 'analysis_data\stocks_analysis.sqlite'),
    (Join-Path $projectDir 'analysis_data\signals.sqlite'),
    (Join-Path $projectDir 'analysis_data\backtest_report.json'),
    (Join-Path $projectDir 'webapp.html'),
    (Join-Path $projectDir 'app_security.py'),
    (Join-Path $projectDir 'supabase_bundle\schema.sql')
)
foreach ($path in $requiredFiles) {
    if (-not (Test-Path -LiteralPath $path)) {
        throw "Missing deployment component: $path"
    }
}

$envPath = Join-Path $projectDir '.env'
if (-not (Test-Path -LiteralPath $envPath)) {
    throw "Missing deployment config: $envPath"
}
$envText = Get-Content -LiteralPath $envPath -Raw
foreach ($name in @('TELEGRAM_BOT_TOKEN')) {
    $pattern = "(?m)^\s*" + [regex]::Escape($name) + "\s*=\s*\S+"
    if ($envText -notmatch $pattern) {
        throw "Missing required .env variable: $name"
    }
}
if ($envText -notmatch '(?m)^\s*TELEGRAM_PUBLIC_ACCESS\s*=\s*["'']?(?:true|1|yes|on)["'']?\s*$' -and
    $envText -notmatch '(?m)^\s*TELEGRAM_ALLOWED_CHAT_IDS\s*=\s*["'']?-?\d+') {
    throw 'Telegram access is not configured: enable public access or provide an allowlist.'
}
if ($envText -notmatch '(?m)^\s*(?:PAPER_DATABASE_URL|SUPABASE_DB_URL)\s*=\s*["'']?postgres(?:ql)?://\S+') {
    throw 'Production Paper Trading requires PAPER_DATABASE_URL or SUPABASE_DB_URL in .env.'
}
$webApp = Get-Content -LiteralPath (Join-Path $projectDir 'webapp.html') -Raw
if ($webApp -notmatch 'X-Telegram-Init-Data' -or $webApp -notmatch 'telegram-web-app\.js') {
    throw 'Web App is missing Telegram initData forwarding.'
}
$schema = Get-Content -LiteralPath (Join-Path $projectDir 'supabase_bundle\schema.sql') -Raw
foreach ($table in @('telegram_users','paper_accounts','paper_trades','rate_limit_buckets')) {
    if ($schema -notmatch ("create table if not exists public\." + $table)) {
        throw "Supabase schema is missing private table: $table"
    }
}

Set-Location -LiteralPath $projectDir
& $runtimePython -c "import sys; assert sys.version_info >= (3, 10), sys.version"
if ($LASTEXITCODE -ne 0) { throw 'Python 3.10 or newer is required.' }
& $runtimePython -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Python dependencies are inconsistent.' }
& $runtimePython -m unittest discover -q
if ($LASTEXITCODE -ne 0) { throw 'Python test suite failed.' }
& node (Join-Path $projectDir 'test_chart_tools.js')
if ($LASTEXITCODE -ne 0) { throw 'Chart test suite failed.' }
& $runtimePython (Join-Path $projectDir 'validate_analysis_data.py')
if ($LASTEXITCODE -ne 0) { throw 'Analysis data or artifacts are not ready.' }

Write-Output 'DEPLOY_CHECK_OK'
