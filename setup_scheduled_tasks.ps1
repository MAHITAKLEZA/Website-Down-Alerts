<#
    Registers Scheduled Tasks so monitoring runs automatically:

      WebsiteMonitor-FullCrawl    daily at 03:00  -> run_full_crawl.py
                                                    (broken-link crawl; alerts.log only)
      WebsiteMonitor-Security     daily at 03:30  -> run_security_checks.py
                                                    (headers, exposed files, malware
                                                     indicators, WP/jQuery versions,
                                                     domain expiry; dashboard + alerts.log)
      WebsiteMonitor-Fast         at logon, then every 5 minutes while this
                                  machine is on -> run_fast_checks.py
                                  (scans every site, sends the Teams "down" card
                                   for newly-down sites / changed issues)

    This machine (the Kleza server) is not on all the time: monitoring runs
    whenever it is on, and a scan is made as soon as it is back on (missed runs
    are started when it wakes). The cloud schedule in
    .github/workflows/monitor.yml is switched off so the two don't both post.
    Needs TEAMS_WEBHOOK_URL set here (setx, or the dashboard's Settings tab).

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
$fast  = "WebsiteMonitor-Fast"
$daily = "WebsiteMonitor-DailyReport"  # retired -- removed if still registered

if ($Remove) {
    foreach ($n in @($fast, $full, $sec, $daily)) {
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

# Retired daily-report task.
if (Get-ScheduledTask -TaskName $daily -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $daily -Confirm:$false
    Write-Host "Removed $daily (no more daily report)"
}

# --- Fast check: at logon, then every 5 minutes while the machine is on ---
# pythonw = no console window. IgnoreNew skips a tick if the last scan is still running.
$pythonw = Join-Path (Split-Path $python) "pythonw.exe"
$fastAction  = New-ScheduledTaskAction -Execute $pythonw -Argument "run_fast_checks.py" -WorkingDirectory $dir
$fastTrigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$fastTrigger.Repetition = (New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 9999)).Repetition
Register-ScheduledTask -TaskName $fast -Action $fastAction -Trigger $fastTrigger `
    -Principal $principal -Settings $settings -Force | Out-Null
Write-Host "Registered $fast (at logon, then every 5 minutes)"

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
