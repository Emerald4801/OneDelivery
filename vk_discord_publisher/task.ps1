param(
    [ValidateSet('Install','Disable','Enable','Status','Remove')][string]$Mode = 'Status',
    [switch]$Preview
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$cfg = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'config.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$taskName = 'VKDiscordPublisher-' + $cfg.guild_id + '-' + $cfg.channel_id
if ($Mode -eq 'Status') {
    Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue | Select-Object TaskName,State
    exit 0
}
if ($Mode -eq 'Disable') {
    Disable-ScheduledTask -TaskName $taskName | Out-Null
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    Write-Host 'Automatic publishing stopped. State preserved.'
    exit 0
}
if ($Mode -eq 'Remove') {
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    exit 0
}
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not $Preview) {
    & $python -u (Join-Path $PSScriptRoot 'ongoing.py') --diagnose
    if ($LASTEXITCODE -ne 0) { throw 'Source/Discord diagnostic failed. Automatic publishing was not enabled. See logs\ongoing.log.' }
}
if ($Mode -eq 'Enable') {
    Enable-ScheduledTask -TaskName $taskName | Out-Null
    Start-ScheduledTask -TaskName $taskName
    exit 0
}
$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute (Join-Path $PSScriptRoot '.venv\Scripts\pythonw.exe') -Argument ('"' + (Join-Path $PSScriptRoot 'ongoing.py') + '"') -WorkingDirectory $PSScriptRoot
$moscow = [System.TimeZoneInfo]::FindSystemTimeZoneById('Russian Standard Time')
$moscowNoon = [System.TimeZoneInfo]::ConvertTimeFromUtc([DateTime]::UtcNow, $moscow).Date.AddHours(12)
$localNoon = [System.TimeZoneInfo]::ConvertTime($moscowNoon, $moscow, [System.TimeZoneInfo]::Local)
$triggers = @((New-ScheduledTaskTrigger -AtLogOn -User $currentUser), (New-ScheduledTaskTrigger -Daily -At $localNoon))
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)
$principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Limited
$task = New-ScheduledTask -Action $action -Trigger $triggers -Settings $settings -Principal $principal -Description 'VK chapters to Discord. Moscow 12-24h; no administrator rights or stored Windows password.'
if ($Preview) {
    [pscustomobject]@{TaskName=$taskName; Execute=$task.Actions.Execute; Arguments=$task.Actions.Arguments; RestartCount=$task.Settings.RestartCount; RestartInterval=$task.Settings.RestartInterval; ExecutionTimeLimit=$task.Settings.ExecutionTimeLimit; LogonType=$task.Principal.LogonType; RunLevel=$task.Principal.RunLevel; MultipleInstances=$task.Settings.MultipleInstances}
    exit 0
}
Register-ScheduledTask -TaskName $taskName -InputObject $task -Force | Out-Null
Start-ScheduledTask -TaskName $taskName
Write-Host 'Automatic publishing enabled. Log: logs\ongoing.log'
