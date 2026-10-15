# Dang ky / truy van / huy tap-tu Windows cho chu ky tien xem-truoc (preview-only) cua Vietnam Atlas.
# Tap-tu chi chay RUN_UPDATE_WINDOWS.ps1 KHONG -Apply: khong co gi tu dong ghi du lieu.
param(
  [ValidateSet('Register', 'Unregister', 'Query')]
  [string]$Action = 'Query',
  [string]$TaskName = 'VietnamAtlas-Update-Preview',
  [string]$Time = '21:30',
  [string]$Day = 'SUN'
)
$ErrorActionPreference = 'Stop'
if ($Action -eq 'Register') {
  if ($Time -notmatch '^\d{2}:\d{2}$') { throw "Time phai dinh dang HH:MM (24h), nhan: '$Time'" }
  if ($Day -notmatch '^(MON|TUE|WED|THU|FRI|SAT|SUN)$') { throw "Day phai la MON..SUN, nhan: '$Day'" }
}
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Runner = Join-Path $RepoRoot 'RUN_UPDATE_WINDOWS.ps1'
if (-not (Test-Path $Runner)) { throw "Khong tim thay RUN_UPDATE_WINDOWS.ps1 tai: $Runner" }
$TaskRun = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$Runner`""
switch ($Action) {
  'Register' {
    schtasks /Create /F /TN $TaskName /SC WEEKLY /D $Day /ST $Time /TR $TaskRun
    if ($LASTEXITCODE -ne 0) { throw "Dang ky task '$TaskName' that bai (schtasks exit $LASTEXITCODE)." }
    Write-Host "Da dang ky '$TaskName': chay hang tuan vao $Day luc $Time."
  }
  'Unregister' {
    schtasks /Delete /F /TN $TaskName
    if ($LASTEXITCODE -ne 0) { throw "Huy task '$TaskName' that bai (schtasks exit $LASTEXITCODE)." }
    Write-Host "Da huy '$TaskName'."
  }
  'Query' {
    schtasks /Query /TN $TaskName /V /FO LIST
    if ($LASTEXITCODE -ne 0) {
      Write-Host "Task '$TaskName' khong ton tai hoac khong truy van duoc (schtasks exit $LASTEXITCODE)." -ForegroundColor Yellow
    }
  }
}
