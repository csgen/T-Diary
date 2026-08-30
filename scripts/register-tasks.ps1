<#
.SYNOPSIS
  Register the three tokenDiary scheduled tasks.

.DESCRIPTION
  One-time setup helper. This is NOT part of the nightly path -- the nightly
  path is `python -m src run`, which owns its own exit-code rules and logging.
  This script only creates Task Scheduler entries.

  Three settings here are load-bearing:

  -WorkingDirectory   `python -m src` needs the project root as cwd or it
                      fails with "No module named src", which reads like a
                      Python install problem rather than a path one.
                      schtasks.exe cannot set this at all, which is why this
                      uses the ScheduledTasks module.

  -LogonType Interactive
                      Runs in your own logged-on session. Locking the screen
                      is still "logged on"; signing out or rebooting is not.

  -MultipleInstances IgnoreNew
                      A task never overlaps itself. It cannot see across the
                      three tasks -- exit code 6 covers that.

  -StartWhenAvailable Runs as soon as possible after a missed start, which is
                      what covers a machine that was asleep at the trigger.
#>
[CmdletBinding()]
param(
    [string] $DailyAt  = "21:00",
    [string] $WeeklyAt = "20:00",
    [string] $WeeklyOn = "Sunday"
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$py   = (Get-Command python).Source

if (-not (Test-Path (Join-Path $root "src/cli.py"))) {
    throw "cannot find src/cli.py under $root -- run this from the repo's scripts/ directory"
}

Write-Host "project root : $root"
Write-Host "interpreter  : $py"
Write-Host ""

$settings  = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive

function Register-TdTask {
    param([string] $Name, [string] $Args, $Trigger, [string] $Why)

    $action = New-ScheduledTaskAction -Execute $py -Argument $Args -WorkingDirectory $root
    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $Trigger `
        -Settings $settings -Principal $principal -Description $Why -Force | Out-Null
    Write-Host ("registered  {0,-24} {1}" -f $Name, $Args)
}

Register-TdTask -Name "tokenDiary daily" -Args "-m src run" `
    -Trigger (New-ScheduledTaskTrigger -Daily -At $DailyAt) `
    -Why "Incremental ingest + dashboard export."

Register-TdTask -Name "tokenDiary logon" -Args "-m src run" `
    -Trigger (New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME") `
    -Why "Catch-up run after a day the machine was off or signed out."

Register-TdTask -Name "tokenDiary weekly full" -Args "-m src run --full" `
    -Trigger (New-ScheduledTaskTrigger -Weekly -DaysOfWeek $WeeklyOn -At $WeeklyAt) `
    -Why "Full reparse. Backstops section 6.2's blind spot: a file rewritten to a byte-identical length with its mtime preserved is never selected incrementally."

Write-Host ""
Write-Host "Verify with:  Get-ScheduledTask -TaskName 'tokenDiary *' | Format-Table TaskName, State"
Write-Host "Run one now:  Start-ScheduledTask -TaskName 'tokenDiary daily'"
Write-Host "Then read:    data/logs/$(Get-Date -Format yyyy-MM).log"
