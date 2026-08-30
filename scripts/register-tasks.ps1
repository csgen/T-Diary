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
    [string] $WeeklyOn = "Sunday",

    # The interpreter is FROZEN into the task definition, so whichever python
    # is on PATH when you run this is the one every scheduled run uses forever.
    # Activate a conda env first and you will silently pin that env. Pass this
    # explicitly to choose deliberately.
    [string] $Python
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$py   = if ($Python) { $Python } else { (Get-Command python).Source }

if (-not (Test-Path $py)) { throw "no interpreter at $py" }

# tokenDiary is stdlib-only, so any 3.11+ works (tomllib). What matters is that
# the interpreter still exists in a year -- a task pinned to a conda env breaks
# silently the day that env is renamed or removed, and Task Scheduler reports it
# only as a non-zero Last Run Result.
$envRoot = if ($env:CONDA_PREFIX) { $env:CONDA_PREFIX } elseif ($env:VIRTUAL_ENV) { $env:VIRTUAL_ENV } else { $null }
# Split on both separators by character code: a literal backslash in a regex here
# is one escaping layer away from an invalid pattern, and comparing path COMPONENTS
# is stricter than a substring match anyway.
$parts = $py.Split([char]92, [char]47)
$inEnv = ($envRoot -and $py.StartsWith($envRoot)) -or ($parts -contains 'envs') -or ($parts -contains '.venv')
if ($inEnv) {
    Write-Warning "That interpreter belongs to a virtual environment. Scheduled runs will"
    Write-Warning "keep using it after you deactivate, and will fail if the env is removed."
    Write-Warning "Re-run with -Python <path to a permanent interpreter> to pin a stable one."
}

if (-not (Test-Path (Join-Path $root "src/cli.py"))) {
    throw "cannot find src/cli.py under $root -- run this from the repo's scripts/ directory"
}

Write-Host "project root : $root"
Write-Host "interpreter  : $py"
Write-Host ""

$settings  = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive

function Register-TdTask {
    # NOT $Args -- that is a reserved automatic variable (the array of unbound
    # arguments), so a param named $Args silently never binds and -Argument
    # arrives empty. Cost one failed registration to find.
    param([string] $Name, [string] $Arguments, $Trigger, [string] $Why)

    $action = New-ScheduledTaskAction -Execute $py -Argument $Arguments -WorkingDirectory $root
    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $Trigger `
        -Settings $settings -Principal $principal -Description $Why -Force | Out-Null
    Write-Host ("registered  {0,-24} {1}" -f $Name, $Arguments)
}

Register-TdTask -Name "tokenDiary daily" -Arguments "-m src run" `
    -Trigger (New-ScheduledTaskTrigger -Daily -At $DailyAt) `
    -Why "Incremental ingest + dashboard export."

Register-TdTask -Name "tokenDiary logon" -Arguments "-m src run" `
    -Trigger (New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME") `
    -Why "Catch-up run after a day the machine was off or signed out."

Register-TdTask -Name "tokenDiary weekly full" -Arguments "-m src run --full" `
    -Trigger (New-ScheduledTaskTrigger -Weekly -DaysOfWeek $WeeklyOn -At $WeeklyAt) `
    -Why "Full reparse. Backstops section 6.2's blind spot: a file rewritten to a byte-identical length with its mtime preserved is never selected incrementally."

Write-Host ""
Write-Host "Verify with:  Get-ScheduledTask -TaskName 'tokenDiary *' | Format-Table TaskName, State"
Write-Host "Run one now:  Start-ScheduledTask -TaskName 'tokenDiary daily'"
Write-Host "Then read:    data/logs/$(Get-Date -Format yyyy-MM).log"
