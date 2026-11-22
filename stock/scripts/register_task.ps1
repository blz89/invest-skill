<#
(Re-)register the Windows scheduled task that fetches tonight's data in one go.

    DSH-DailyPatrol  ->  powershell.exe -NoProfile -ExecutionPolicy Bypass `
                           -File "%USERPROFILE%\.dsh\skills\invest\stock\scripts\daily_patrol.ps1" -Ledger 50k

daily_patrol.ps1 already runs everything in one shot:
    prefetch (python -m stock prefetch)  ->  invest_data  ->  prune
so the task definition never changes when the steps change; only this script or the
timeout in the task settings needs touching.

Usage (an elevated prompt is required when registering with -RunLevel Highest):
    powershell -NoProfile -ExecutionPolicy Bypass -File %USERPROFILE%\.dsh\skills\invest\stock\scripts\register_task.ps1 -DryRun
    powershell -NoProfile -ExecutionPolicy Bypass -File %USERPROFILE%\.dsh\skills\invest\stock\scripts\register_task.ps1 -Time 19:00 -ExecutionHours 3
    powershell -NoProfile -ExecutionPolicy Bypass -File %USERPROFILE%\.dsh\skills\invest\stock\scripts\register_task.ps1 -Remove

Defaults match the current production task: Mon-Fri 19:00, ExecutionTimeLimit PT3H,
StartWhenAvailable, MultipleInstances=IgnoreNew, run as the current user (highest).
Why 3h: daily_patrol.ps1 worst-case step budget is prefetch 2400s + invest 1800s
+ prune 1800s = 6000s (100min); a PT2H limit could kill the run mid-invest (2026-11-16 incident).
Keep this file ASCII-only (Windows PowerShell 5.1 reads BOM-less UTF-8 as ANSI).
#>
param(
    [string]$TaskName = 'DSH-DailyPatrol',
    [string]$Time = '19:00',
    [int]$ExecutionHours = 3,
    [string]$Ledger = '50k',
    [string]$PatrolScript = '',
    [string]$PowerShell = 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe',
    [switch]$DryRun,
    [switch]$Remove,
    [switch]$NoHighest
)

$ErrorActionPreference = 'Stop'

if (-not $PatrolScript) { $PatrolScript = Join-Path $PSScriptRoot 'daily_patrol.ps1' }

if ($Remove) {
    if ($DryRun) { Write-Host ("[dry] would delete task: {0}" -f $TaskName); exit 0 }
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($task) { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false; Write-Host ("deleted task: {0}" -f $TaskName) }
    else { Write-Host ("task not found: {0}" -f $TaskName) }
    exit 0
}

if (-not (Test-Path -LiteralPath $PatrolScript)) { throw ("patrol script not found: {0}" -f $PatrolScript) }

$argLine = '-NoProfile -ExecutionPolicy Bypass -File "{0}" -Ledger {1}' -f $PatrolScript, $Ledger
$action = New-ScheduledTaskAction -Execute $PowerShell -Argument $argLine -WorkingDirectory (Split-Path $PatrolScript -Parent)

# Weekly Mon-Fri trigger at $Time. Register-ScheduledTask has no "weekly on weekdays" helper,
# so use a daily trigger restricted to the five workdays.
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $Time

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours $ExecutionHours) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

$runLevel = if ($NoHighest) { 'Limited' } else { 'Highest' }
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel $runLevel

$old = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($old) {
    Write-Host ("task exists, will replace: {0} (state={1})" -f $TaskName, $old.State)
}
Write-Host ("action   : {0} {1}" -f $PowerShell, $argLine)
Write-Host ("trigger  : Mon-Fri {0}" -f $Time)
Write-Host ("limit    : {0}h   run-as: {1} ({2})" -f $ExecutionHours, $env:USERNAME, $runLevel)
Write-Host ("start-in : {0}" -f (Split-Path $PatrolScript -Parent))

if ($DryRun) { Write-Host '[dry] nothing registered'; exit 0 }

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin -and -not $NoHighest) {
    throw 'registering with RunLevel Highest needs an elevated PowerShell. Re-run as Administrator, or add -NoHighest.'
}

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
$t = Get-ScheduledTask -TaskName $TaskName
$info = Get-ScheduledTaskInfo -TaskName $TaskName
Write-Host ''
Write-Host ("registered: {0}  state={1}" -f $t.TaskName, $t.State)
Write-Host ("next run : {0}" -f $info.NextRunTime)
Write-Host ("last run : {0}  last result={1}" -f $info.LastRunTime, $info.LastTaskResult)
