param(
  [switch]$Apply,
  [switch]$UpdateExisting,
  [switch]$OnlyDownload,
  [switch]$ForceDownload,
  [switch]$SkipDownload,
  [int]$Limit = 0
)
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root
if ($Limit -lt 0) { throw 'Limit must be >= 0' }
$py = Get-Command py -ErrorAction SilentlyContinue
if ($py) { $cmd = 'py'; $prefix = @('-3') } else { $cmd = 'python'; $prefix = @() }
& $cmd @prefix --version
if ($LASTEXITCODE -ne 0) { throw 'Cai Python 3.10+ truoc khi chay.' }
Write-Host 'Kiem tra pyosmium (PBF country-wide reader)...'
& $cmd @prefix -c 'import osmium'
if ($LASTEXITCODE -ne 0) {
  Write-Host 'Chua co pyosmium. Chay: py -m pip install osmium' -ForegroundColor Yellow
  throw 'Thieu pyosmium'
}
if (-not $SkipDownload) {
  $downloadArgs = @('tools/fetch_vn_geofabrik.py', '--out', 'data/vietnam-latest.osm.pbf')
  if ($ForceDownload) { $downloadArgs += '--force' }
  & $cmd @prefix @downloadArgs
  if ($LASTEXITCODE -ne 0) { throw 'Download that bai; khong nhap vao Atlas.' }
} else {
  Write-Host 'SkipDownload: bo qua buoc tai PBF, dung snapshot hien co.' -ForegroundColor Yellow
  $pbfPath = 'data/vietnam-latest.osm.pbf'
  $sidecarPath = "$pbfPath.provenance.json"
  if ((Test-Path $pbfPath) -and (Test-Path $sidecarPath)) {
    $expected = (Get-Content $sidecarPath -Raw | ConvertFrom-Json).sha256
    $actual = (Get-FileHash -Algorithm SHA256 -Path $pbfPath).Hash.ToLowerInvariant()
    if ($expected -and ($actual -ne $expected.ToString().ToLowerInvariant())) {
      throw 'SkipDownload: SHA-256 PBF khong khop provenance sidecar (file bi thay?); chay lai khong -SkipDownload de tai moi va lam moi metadata.'
    }
    Write-Host 'SkipDownload: provenance khop PBF hien co.' -ForegroundColor Yellow
  } else {
    Write-Host 'SkipDownload: thieu PBF hoac sidecar provenance; bao cao se thieu metadata nguon.' -ForegroundColor Yellow
  }
}
if ($OnlyDownload) { exit 0 }
$importArgs = @('tools/vn_osm_dataset.py', 'data/vietnam-latest.osm.pbf', '--db', 'data/osm_vietnam.sqlite3')
if ($Limit -gt 0) { $importArgs += @('--limit', "$Limit") }
& $cmd @prefix @importArgs
if ($LASTEXITCODE -ne 0) { throw 'OSM import that bai; khong cap nhat Atlas.' }
$ts = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')
$rawDir = "reports/_raw/$ts"
New-Item -ItemType Directory -Force -Path $rawDir | Out-Null
& $cmd @prefix tools/vn_osm_dataset.py --stats --db data/osm_vietnam.sqlite3 | Tee-Object -FilePath "$rawDir/stats.txt"
if ($LASTEXITCODE -ne 0) { throw 'Stage database khong san sang' }
& $cmd @prefix tools/verify_osm_snapshot.py --db data/osm_vietnam.sqlite3 --min-active 1 | Tee-Object -FilePath "$rawDir/verify.txt"
if ($LASTEXITCODE -ne 0) { throw 'Snapshot validation FAIL' }
# Preview before actual live DB changes.
$syncArgs = @('tools/sync_osm_into_atlas.py', '--osm-db', 'data/osm_vietnam.sqlite3', '--atlas-db', 'data/atlas.sqlite3')
if ($Limit -gt 0) { $syncArgs += @('--limit', "$Limit") }
if ($UpdateExisting) { $syncArgs += '--update-existing' }
Write-Host 'Preview changes to Atlas:'
& $cmd @prefix @syncArgs | Tee-Object -FilePath "$rawDir/preview.json"
if ($LASTEXITCODE -ne 0) { throw 'Dry-run that bai; khong ghi database.' }
$reportInput = "$rawDir/preview.json"
if ($Apply) {
  Write-Host 'Apply: auto-backup atlas.sqlite3 before write' -ForegroundColor Yellow
  $applyArgs = $syncArgs + @('--apply')
  & $cmd @prefix @applyArgs | Tee-Object -FilePath "$rawDir/apply.json"
  if ($LASTEXITCODE -ne 0) { throw 'Ghi Atlas that bai; kiem tra backup va log.' }
  $reportInput = "$rawDir/apply.json"
} else {
  Write-Host 'DRY RUN only. Chay voi -Apply de ghi vao Atlas.' -ForegroundColor Yellow
}
Write-Host 'Tao bao cap nhat tu cac file da chup...'
& $cmd @prefix tools/update_report.py --preview "$reportInput" --provenance 'data/vietnam-latest.osm.pbf.provenance.json' --staging-stats "$rawDir/stats.txt" --verify "$rawDir/verify.txt" --out-dir reports --stamp $ts
if ($LASTEXITCODE -ne 0) { throw 'Tao bao cap nhat that bai; kiem tra file preview va stderr.' }
Write-Host "Bao cap nhat: reports/update-$ts.json"
Write-Host "Bao cap nhat: reports/update-$ts.md"
