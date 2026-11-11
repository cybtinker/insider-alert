# One-time setup on a Windows PC: installs what senate.py needs, runs it once
# to load about 13 months of Senate trades, and schedules it daily at 7 PM.
# Run from the insider-alert folder:  powershell -ExecutionPolicy Bypass -File setup-senate.ps1
param([string]$Contact = "InsiderAlert cybertinker-sub@pm.me", [string]$At = "7:00PM")
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $repo

$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) { $py = (Get-Command py -ErrorAction SilentlyContinue).Source }
if (-not $py) { throw "Python isn't installed. Install it from https://www.python.org/downloads/ and run this again." }
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw "Git isn't installed. Install it from https://git-scm.com/download/win and run this again." }

& $py -m pip install --quiet --upgrade requests
$env:SEC_USER_AGENT = $Contact
Write-Host "Downloading about 13 months of Senate filings (takes a few minutes)..."
& $py senate.py --days 400 --push
if ($LASTEXITCODE -ne 0) { throw "senate.py failed. See the message above." }

$cmd = "/c set SEC_USER_AGENT=$Contact&& cd /d `"$repo`" && `"$py`" senate.py --push >> senate-log.txt 2>&1"
$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $cmd
$trigger = New-ScheduledTaskTrigger -Daily -At $At
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RunOnlyIfNetworkAvailable
Register-ScheduledTask -TaskName "Insider Alert - Senate trades" -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Write-Host "Done. Senate trades will update every day at $At (or when the PC next wakes up)."
