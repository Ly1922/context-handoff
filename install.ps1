# Install context-handoff as a global Antigravity plugin (junction, so edits here take effect).
$ErrorActionPreference = "Stop"
$src = $PSScriptRoot
$dst = Join-Path $env:USERPROFILE ".gemini\config\plugins\context-handoff"
if (-not (Get-Command python -ErrorAction SilentlyContinue)) { throw "python not found on PATH" }
if (Test-Path $dst) {
    $item = Get-Item $dst -Force
    if ($item.LinkType -eq "Junction") { $item.Delete() } else { throw "$dst exists and is not a junction; remove it first" }
}
New-Item -ItemType Junction -Path $dst -Target $src | Out-Null
Write-Host "Linked $dst -> $src"
Push-Location $src
python hooks/handoff.py calibrate
Pop-Location
Write-Host "Done. New conversations pick it up; disable with: config.json enabled=false, or the plugin toggle in Settings."
