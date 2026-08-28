# Scheduler entrypoint: build one video and publish it to the configured platforms.
# Called by the Windows Scheduled Task created by register_task.ps1.

$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $PSScriptRoot
Set-Location $Repo

# Prefer the project venv if it exists.
$VenvPy = Join-Path $Repo ".venv\Scripts\python.exe"
$Py = if (Test-Path $VenvPy) { $VenvPy } else { "python" }

$stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Write-Output "[$stamp] social-peace run  (python: $Py)"

& $Py -m social_peace run --count 1
exit $LASTEXITCODE
