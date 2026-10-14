<#
    Registers Scheduled Tasks so monitoring runs automatically:

      WebsiteMonitor-FullCrawl    daily at 03:00  -> run_full_crawl.py
                                                    (broken-link crawl; alerts.log only)
      WebsiteMonitor-Security     daily at 03:30  -> run_security_checks.py
                                                    (headers, exposed files, malware
                                                     indicators, WP/jQuery versions,
                                                     domain expiry; dashboard + alerts.log)

    Down-site monitoring + Teams alerts run in the cloud every few minutes
    (.github/workflows/monitor.yml); the daily down report too
    (.github/workflows/daily-report.yml).

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
$sec   = "WebsiteMonitor-Security"
$daily = "WebsiteMonitor-DailyReport"

if ($Remove) {
    foreach ($n in @("WebsiteMonitor-Fast", $full, $sec, $daily)) {
        if (Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $n -Confirm:$false
            Write-Host "Removed $n"
        }
    }
    return
}

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
$settings  = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopOnIdleEnd `
                -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
                -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

# Old hourly check -- remove it if it's still around from a previous setup.
if (Get-ScheduledTask -TaskName "WebsiteMonitor-Fast" -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName "WebsiteMonitor-Fast" -Confirm:$false
    Write-Host "Removed WebsiteMonitor-Fast (no more hourly checks)"
}

# The daily report now runs in the cloud (.github/workflows/daily-report.yml,
# every day 09:30 IST, only when a site is down). Remove the old local task so
# the card isn't sent twice.
if (Get-ScheduledTask -TaskName $daily -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $daily -Confirm:$false
    Write-Host "Removed $daily (daily report runs on GitHub Actions now)"
}

# --- Full crawl: daily at 03:00 ---
$fullAction  = New-ScheduledTaskAction -Execute $python -Argument "run_full_crawl.py" -WorkingDirectory $dir
$fullTrigger = New-ScheduledTaskTrigger -Daily -At 3am
Register-ScheduledTask -TaskName $full -Action $fullAction -Trigger $fullTrigger `
    -Principal $principal -Settings $settings -Force | Out-Null
Write-Host "Registered $full (daily 03:00)"

# --- Security scan: daily at 03:30 (after the link crawl) ---
$secAction  = New-ScheduledTaskAction -Execute $python -Argument "run_security_checks.py" -WorkingDirectory $dir
$secTrigger = New-ScheduledTaskTrigger -Daily -At 3:30am
Register-ScheduledTask -TaskName $sec -Action $secAction -Trigger $secTrigger `
    -Principal $principal -Settings $settings -Force | Out-Null
Write-Host "Registered $sec (daily 03:30)"

Write-Host "`nDone. View them in Task Scheduler, or run:  Get-ScheduledTask WebsiteMonitor-*"
