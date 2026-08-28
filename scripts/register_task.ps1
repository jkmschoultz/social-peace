# Register (or replace) a Windows Scheduled Task that posts on a cadence.
# Usage (from an elevated PowerShell):
#   .\scripts\register_task.ps1 -Times "09:00","18:00"
#   .\scripts\register_task.ps1 -Times "12:30" -TaskName "social-peace-noon"

param(
    [string[]] $Times = @("09:00"),
    [string]   $TaskName = "social-peace-daily"
)

$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $PSScriptRoot
$Script = Join-Path $Repo "scripts\run_daily.ps1"

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$Script`""

$triggers = foreach ($t in $Times) { New-ScheduledTaskTrigger -Daily -At $t }

$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -DontStopOnIdleEnd -MultipleInstances IgnoreNew

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers `
    -Settings $settings -Description "social-peace: build + post calming short-form video" `
    -RunLevel Limited

Write-Output "Registered '$TaskName' at: $($Times -join ', ')"
Write-Output "Test now with:  Start-ScheduledTask -TaskName $TaskName"
