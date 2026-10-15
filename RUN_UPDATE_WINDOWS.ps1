param(
  [switch]$Apply,
  [switch]$UpdateExisting,
  [switch]$OnlyDownload,
  [switch]$ForceDownload,
  [switch]$SkipDownload,
  [int]$Limit = 0,
  [int]$VerifyMinActive = 50000,
  [string]$ApprovedReport = ''
)
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root
if ($Limit -lt 0) { throw 'Limit must be >= 0' }
if ($Apply -and [string]::IsNullOrWhiteSpace($ApprovedReport)) {
  throw '-Apply yeu cau -ApprovedReport <path report.json>: chay preview truoc, duyet reports/update-<stamp>.json, roi truyen lai duong dan report de rang buoc apply vao snapshot da duyet.'
}
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
  if (-not (Test-Path $pbfPath) -or -not (Test-Path $sidecarPath)) {
    throw 'SkipDownload: thieu PBF hoac sidecar provenance; chay lai khong -SkipDownload de tai moi truoc.'
  }
  $expected = (Get-Content $sidecarPath -Raw | ConvertFrom-Json).sha256
  $actual = (Get-FileHash -Algorithm SHA256 -Path $pbfPath).Hash.ToLowerInvariant()
  if (-not $expected -or ($actual -ne $expected.ToString().ToLowerInvariant())) {
    throw 'SkipDownload: provenance sidecar thieu SHA-256 hoac khong khop PBF (file bi thay?); chay lai khong -SkipDownload de tai moi va lam moi metadata.'
  }
  Write-Host 'SkipDownload: provenance khop PBF hien co.' -ForegroundColor Yellow
}
if ($OnlyDownload) { exit 0 }
$ts = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')
$rawDir = "reports/_raw/$ts"
New-Item -ItemType Directory -Force -Path $rawDir | Out-Null
$importArgs = @('tools/vn_osm_dataset.py', 'data/vietnam-latest.osm.pbf', '--db', 'data/osm_vietnam.sqlite3')
if ($Limit -gt 0) {
  $importArgs += @('--limit', "$Limit")
} else {
  # Full-scope cycle marks disappeared OSM objects inactive (retire evidence); banned with --limit by the importer.
  $importArgs += '--mark-missing'
}
& $cmd @prefix @importArgs | Tee-Object -FilePath "$rawDir/import.json"
if ($LASTEXITCODE -ne 0) { throw 'OSM import that bai; khong cap nhat Atlas.' }
& $cmd @prefix tools/vn_osm_dataset.py --stats --db data/osm_vietnam.sqlite3 | Tee-Object -FilePath "$rawDir/stats.txt"
if ($LASTEXITCODE -ne 0) { throw 'Stage database khong san sang' }
if ($Limit -gt 0) {
  Write-Warning 'TEST MODE (limit): snapshot khong duoc chung nhan full-scope.'
  & $cmd @prefix tools/verify_osm_snapshot.py --db data/osm_vietnam.sqlite3 --min-active 1 | Tee-Object -FilePath "$rawDir/verify.txt"
} else {
  & $cmd @prefix tools/verify_osm_snapshot.py --db data/osm_vietnam.sqlite3 --require-full --min-active $VerifyMinActive | Tee-Object -FilePath "$rawDir/verify.txt"
}
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
  # Recheck: chay lai dung preview hien tai (cung options) truoc khi ghi; verify_approval
  # doi chieu voi report da duyet => bat ca doi options lan Atlas/staging troi ke tu luc duyet.
  & $cmd @prefix @syncArgs | Tee-Object -FilePath "$rawDir/recheck.json"
  if ($LASTEXITCODE -ne 0) { throw 'Recheck preview that bai; khong ghi database.' }
  & $cmd @prefix tools/verify_approval.py --report $ApprovedReport --pbf data/vietnam-latest.osm.pbf --staging-db data/osm_vietnam.sqlite3 --recheck "$rawDir/recheck.json"
  if ($LASTEXITCODE -ne 0) { throw 'Report duyet khong con khop PBF/snapshot/ket qua preview hien tai; chay lai preview va duyet lai report moi.' }
  Write-Host 'Apply: auto-backup atlas.sqlite3 before write' -ForegroundColor Yellow
  $applyArgs = $syncArgs + @('--apply')
  & $cmd @prefix @applyArgs | Tee-Object -FilePath "$rawDir/apply.json"
  if ($LASTEXITCODE -ne 0) { throw 'Ghi Atlas that bai; kiem tra backup va log.' }
  $reportInput = "$rawDir/apply.json"
  # Idempotency evidence: re-run the same dry-run after apply; leftover would_insert/would_update = warning.
  & $cmd @prefix @syncArgs | Tee-Object -FilePath "$rawDir/postapply.json"
  if ($LASTEXITCODE -ne 0) {
    Write-Warning 'Post-apply dry-run loi; bao cao se thieu bang chung idempotency.'
  } else {
    try {
      $postApply = Get-Content "$rawDir/postapply.json" -Raw | ConvertFrom-Json
      if ($postApply.would_insert -gt 0 -or $postApply.would_update -gt 0) {
        Write-Warning "Sau apply van con thay doi ghi duoc (would_insert=$($postApply.would_insert), would_update=$($postApply.would_update)); kiem tra sync truoc chu ky tiep."
      }
    } catch {
      Write-Warning "Khong doc duoc postapply.json: $($_.Exception.Message)"
    }
  }
} else {
  Write-Host 'DRY RUN only. Chay voi -Apply de ghi vao Atlas.' -ForegroundColor Yellow
}
Write-Host 'Tao bao cap nhat tu cac file da chup...'
$reportArgs = @(
  'tools/update_report.py', '--preview', "$reportInput",
  '--provenance', 'data/vietnam-latest.osm.pbf.provenance.json',
  '--staging-stats', "$rawDir/stats.txt",
  '--verify', "$rawDir/verify.txt",
  '--import-json', "$rawDir/import.json",
  '--post-apply', "$rawDir/postapply.json",
  '--pbf', 'data/vietnam-latest.osm.pbf',
  '--staging-db', 'data/osm_vietnam.sqlite3',
  '--atlas-db', 'data/atlas.sqlite3',
  '--out-dir', 'reports', '--stamp', $ts
)
& $cmd @prefix @reportArgs
if ($LASTEXITCODE -ne 0) { throw 'Tao bao cap nhat that bai; kiem tra file preview va stderr.' }
Write-Host "Bao cap nhat: reports/update-$ts.json"
Write-Host "Bao cap nhat: reports/update-$ts.md"
