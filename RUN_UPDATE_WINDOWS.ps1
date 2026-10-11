param(
  [switch]$Apply,
  [switch]$UpdateExisting,
  [switch]$OnlyDownload,
  [switch]$ForceDownload,
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
$downloadArgs = @('tools/fetch_vn_geofabrik.py', '--out', 'data/vietnam-latest.osm.pbf')
if ($ForceDownload) { $downloadArgs += '--force' }
& $cmd @prefix @downloadArgs
if ($LASTEXITCODE -ne 0) { throw 'Download that bai; khong nhap vao Atlas.' }
if ($OnlyDownload) { exit 0 }
$importArgs = @('tools/vn_osm_dataset.py', 'data/vietnam-latest.osm.pbf', '--db', 'data/osm_vietnam.sqlite3')
if ($Limit -gt 0) { $importArgs += @('--limit', "$Limit") }
& $cmd @prefix @importArgs
if ($LASTEXITCODE -ne 0) { throw 'OSM import that bai; khong cap nhat Atlas.' }
& $cmd @prefix tools/vn_osm_dataset.py --stats --db data/osm_vietnam.sqlite3
if ($LASTEXITCODE -ne 0) { throw 'Stage database khong san sang' }
& $cmd @prefix tools/verify_osm_snapshot.py --db data/osm_vietnam.sqlite3 --min-active 1
if ($LASTEXITCODE -ne 0) { throw 'Snapshot validation FAIL' }
# Preview before actual live DB changes.
$syncArgs = @('tools/sync_osm_into_atlas.py', '--osm-db', 'data/osm_vietnam.sqlite3', '--atlas-db', 'data/atlas.sqlite3')
if ($Limit -gt 0) { $syncArgs += @('--limit', "$Limit") }
if ($UpdateExisting) { $syncArgs += '--update-existing' }
Write-Host 'Preview changes to Atlas:'
& $cmd @prefix @syncArgs
if ($LASTEXITCODE -ne 0) { throw 'Dry-run that bai; khong ghi database.' }
if ($Apply) {
  Write-Host 'Apply: auto-backup atlas.sqlite3 before write' -ForegroundColor Yellow
  $applyArgs = $syncArgs + @('--apply')
  & $cmd @prefix @applyArgs
  if ($LASTEXITCODE -ne 0) { throw 'Ghi Atlas that bai; kiem tra backup va log.' }
} else {
  Write-Host 'DRY RUN only. Chay voi -Apply de ghi vao Atlas.' -ForegroundColor Yellow
}
