$ErrorActionPreference = 'Stop'
$projectDir = $PSScriptRoot
$python = Join-Path $projectDir '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $python)) {
    throw "Không tìm thấy môi trường Python: $python"
}
if (Test-Path -LiteralPath (Join-Path $projectDir '.env')) {
    Write-Output 'SECURITY_OK: .env tồn tại cục bộ và phải tiếp tục được loại khỏi Git/Vercel.'
}

Set-Location -LiteralPath $projectDir
& (Join-Path $projectDir 'deploy_check.ps1')
if ($LASTEXITCODE -ne 0) { throw 'Kiểm tra dự án thất bại.' }

& $python (Join-Path $projectDir 'build_supabase_bundle.py')
if ($LASTEXITCODE -ne 0) { throw 'Không tạo được bundle Supabase.' }

& $python (Join-Path $projectDir 'supabase_bundle\validate_bundle.py')
if ($LASTEXITCODE -ne 0) { throw 'Bundle Supabase không khớp database hiện tại.' }

$forbidden = Get-ChildItem -LiteralPath (Join-Path $projectDir 'supabase_bundle') -File |
    Select-String -Pattern 'AIza[0-9A-Za-z_-]{20,}|sk-[0-9A-Za-z_-]{20,}|bot[0-9]{6,}:[0-9A-Za-z_-]{20,}' -ErrorAction SilentlyContinue
if ($forbidden) { throw 'Phát hiện chuỗi có hình dạng khóa bí mật trong bundle Supabase.' }

$manifest = Get-Content -LiteralPath (Join-Path $projectDir 'supabase_bundle\manifest.json') -Raw | ConvertFrom-Json
$compressedBytes = 0
foreach ($dataset in $manifest.datasets) {
    foreach ($file in $dataset.files) { $compressedBytes += [long]$file.bytes }
}
Write-Output ("SUPABASE_BUNDLE_OK: {0:N2} MB nén, tạo lúc {1}" -f ($compressedBytes / 1MB), $manifest.generated_at_utc)
Write-Output 'VERCEL_STATIC_OK: .vercelignore chỉ cho phép 6 tệp frontend/config được tải lên.'
Write-Output 'Lưu ý: cần API cloud tương thích /v1 trước khi Web App Vercel hoạt động đầy đủ.'
