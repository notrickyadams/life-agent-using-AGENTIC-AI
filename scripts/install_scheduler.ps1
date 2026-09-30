<#
.SYNOPSIS
    Set up (or update) the daily run in Windows Task Scheduler.

.DESCRIPTION
    Registers a task that runs the pipeline every morning, so your Notion
    page is current before you look at it.

    Safe to run more than once - it replaces the existing task rather than
    making a second one.

    Choices baked in, and why:

    -no-browser   A scheduled run must never wait at a sign-in prompt. With
                  the browser allowed, an expired Google token would leave
                  the task hanging forever and nothing after it would run.
                  Instead it records the failure, and the Notion page says
                  in red that you need to sign in.

    Two triggers  A daily alarm cannot survive a closed laptop. Windows'
                  "start when available" was supposed to catch up and did
                  not - it recorded a missed run and waited for tomorrow,
                  leaving a day-old page with nothing to say so.

                  So there are two triggers: the daily one, and one at
                  logon. The logon trigger passes --if-stale 6, which does
                  nothing at all if the data is less than six hours old, so
                  logging in repeatedly costs nothing while opening a
                  laptop that has been shut overnight refreshes it.

    Logged on     Runs only while you are logged in, so no password has to
                  be stored anywhere.

.PARAMETER Time
    When to run, 24-hour HH:mm. Default 07:00.

.PARAMETER TaskName
    The name in Task Scheduler. Default "Life Agent daily".

.PARAMETER Remove
    Delete the task instead of creating it.

.EXAMPLE
    .\scripts\install_scheduler.ps1
    .\scripts\install_scheduler.ps1 -Time 06:30
    .\scripts\install_scheduler.ps1 -Remove
#>

param(
    [string]$Time = "07:00",
    [string]$TaskName = "Life Agent daily",
    [switch]$Remove
)

$ErrorActionPreference = "Stop"

# The repo root is one level up from this script.
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"

if ($Remove) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed the scheduled task '$TaskName'."
    } else {
        Write-Host "There is no scheduled task called '$TaskName'."
    }
    return
}

if (-not (Test-Path $Python)) {
    throw "Cannot find $Python. Run this from the project folder, after setup."
}

try {
    $When = [datetime]::ParseExact($Time, "HH:mm", $null)
} catch {
    throw "Time must look like 07:00 or 18:30. Got: $Time"
}

# -Quiet: the plan still goes to Notion and the log; nobody is watching a
# console at 07:00, and an unread console window is just clutter.
# The daily run always syncs. The logon run only syncs if the page is
# already stale, so logging in several times a day costs nothing.
$Action = New-ScheduledTaskAction -Execute $Python `
    -Argument "-m src.run_daily --no-browser --quiet --if-stale 6" `
    -WorkingDirectory $Root

$DailyTrigger = New-ScheduledTaskTrigger -Daily -At $When

# Delayed a couple of minutes so signing in never feels slower, and so the
# network has time to come up.
$LogonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$LogonTrigger.Delay = "PT2M"

$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopIfGoingOnBatteries `
    -AllowStartIfOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -MultipleInstances IgnoreNew

$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Replacing the existing task."
}

Register-ScheduledTask -TaskName $TaskName `
    -Action $Action -Trigger $DailyTrigger, $LogonTrigger -Settings $Settings -Principal $Principal `
    -Description "Reads Google Calendar and Classroom, works out the day, writes it to Notion." | Out-Null

Write-Host ""
Write-Host "  Scheduled '$TaskName' for $Time every day."
Write-Host ""
Write-Host "  It runs:     $Python -m src.run_daily --no-browser --quiet"
Write-Host "  Working dir: $Root"
Write-Host ""
Write-Host "  It also runs when you log in, but only if the page is more than"
Write-Host "  six hours old - so a laptop shut overnight refreshes on opening,"
Write-Host "  and logging in repeatedly costs nothing."
Write-Host "  It will never open a browser - if Google needs you to sign in,"
Write-Host "  your Notion page says so in red."
Write-Host ""
Write-Host "  Check on it:  Get-ScheduledTaskInfo -TaskName '$TaskName'"
Write-Host "  Run it now:   Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "  Remove it:    .\scripts\install_scheduler.ps1 -Remove"
Write-Host ""
