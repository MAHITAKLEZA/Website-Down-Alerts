<#
    Registers Scheduled Tasks so monitoring runs automatically:

      WebsiteMonitor-Fast         every 1 hour     -> run_fast_checks.py
      WebsiteMonitor-FullCrawl    daily at 03:00   -> run_full_crawl.py
      WebsiteMonitor-DailyReport  daily at 09:30   -> daily_report.py
                                                     (Teams group card; needs
                                                      TEAMS_DAILY_WEBHOOK_URL)

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

$fast  = "WebsiteMonitor-Fast"
$full  = "WebsiteMonitor-FullCrawl"
$daily = "WebsiteMonitor-DailyReport"

if ($Remove) {
    foreach ($n in @($fast, $full, $daily)) {
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

# --- Fast checks: every 1 hour ---
# RepetitionDuration must be a finite value on current Windows builds
# ([TimeSpan]::MaxValue is rejected as out-of-range); 10 years is effectively
# "forever" for this.
$fastAction  = New-ScheduledTaskAction -Execute $python -Argument "run_fast_checks.py" -WorkingDirectory $dir
$fastTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
                -RepetitionInterval (New-TimeSpan -Hours 1) -RepetitionDuration (New-TimeSpan -Days 3650)
Register-ScheduledTask -TaskName $fast -Action $fastAction -Trigger $fastTrigger `
    -Principal $principal -Settings $settings -Force | Out-Null
Write-Host "Registered $fast (every 1 hour)"

# --- Full crawl: daily at 03:00 ---
$fullAction  = New-ScheduledTaskAction -Execute $python -Argument "run_full_crawl.py" -WorkingDirectory $dir
$fullTrigger = New-ScheduledTaskTrigger -Daily -At 3am
Register-ScheduledTask -TaskName $full -Action $fullAction -Trigger $fullTrigger `
    -Principal $principal -Settings $settings -Force | Out-Null
Write-Host "Registered $full (daily 03:00)"

# --- Daily report to the Teams group chat: daily at 09:30 ---
$dailyAction  = New-ScheduledTaskAction -Execute $python -Argument "daily_report.py" -WorkingDirectory $dir
$dailyTrigger = New-ScheduledTaskTrigger -Daily -At 9:30am
Register-ScheduledTask -TaskName $daily -Action $dailyAction -Trigger $dailyTrigger `
    -Principal $principal -Settings $settings -Force | Out-Null
Write-Host "Registered $daily (daily 09:30)"

Write-Host "`nDone. View them in Task Scheduler, or run:  Get-ScheduledTask WebsiteMonitor-*"
