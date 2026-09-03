<#
    Registers Scheduled Tasks so monitoring runs automatically:

      WebsiteMonitor-DailyReport  Mon-Fri 09:30   -> daily_report.py
                                                    (checks every site, then
                                                     posts the Teams group card;
                                                     needs TEAMS_DAILY_WEBHOOK_URL;
                                                     no card on Sat/Sun)
      WebsiteMonitor-FullCrawl    daily at 03:00  -> run_full_crawl.py
                                                    (broken-link crawl; alerts.log only)

    Monitoring runs ONCE A DAY (inside daily_report.py, right before the card).
    There is no hourly check.

    Tasks run as the current user, only while you are logged on, so they
    inherit your user environment (PATH, TEAMS_* webhook vars, etc).

    Usage (normal PowerShell, in this folder):
        .\setup_scheduled_tasks.ps1            # create/replace the tasks
        .\setup_scheduled_tasks.ps1 -Remove    # delete the tasks
#>
param([switch]$Remove)

$ErrorActionPreference = "Stop"
$dir = $PSScriptRoot
$python = (Get-Command python).Source

$full  = "WebsiteMonitor-FullCrawl"
$daily = "WebsiteMonitor-DailyReport"

if ($Remove) {
    foreach ($n in @("WebsiteMonitor-Fast", $full, $daily)) {
        if (Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $n -Confirm:$false
            Write-Host "Removed $n"
        }
    }
    return
}

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
$settings  = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopOnIdleEnd `
                -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

# Old hourly check -- remove it if it's still around from a previous setup.
if (Get-ScheduledTask -TaskName "WebsiteMonitor-Fast" -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName "WebsiteMonitor-Fast" -Confirm:$false
    Write-Host "Removed WebsiteMonitor-Fast (no more hourly checks)"
}

# --- Daily report to the Teams group chat: checks every site, then posts. Mon-Fri 09:30 ---
# (daily_report.py also skips weekends itself, so a catch-up run on Sat/Sun stays quiet.)
$dailyAction  = New-ScheduledTaskAction -Execute $python -Argument "daily_report.py" -WorkingDirectory $dir
$dailyTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 9:30am
Register-ScheduledTask -TaskName $daily -Action $dailyAction -Trigger $dailyTrigger `
    -Principal $principal -Settings $settings -Force | Out-Null
Write-Host "Registered $daily (Mon-Fri 09:30 -- checks then posts the card)"

# --- Full crawl: daily at 03:00 ---
$fullAction  = New-ScheduledTaskAction -Execute $python -Argument "run_full_crawl.py" -WorkingDirectory $dir
$fullTrigger = New-ScheduledTaskTrigger -Daily -At 3am
Register-ScheduledTask -TaskName $full -Action $fullAction -Trigger $fullTrigger `
    -Principal $principal -Settings $settings -Force | Out-Null
Write-Host "Registered $full (daily 03:00)"

Write-Host "`nDone. View them in Task Scheduler, or run:  Get-ScheduledTask WebsiteMonitor-*"
