param([switch]$RegisterOnly)
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$cfg = Get-Content -LiteralPath (Join-Path $root 'config.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$name = 'VKDiscordPublisher-Status-' + $cfg.guild_id + '-' + $cfg.channel_id
$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$pythonw = Join-Path $root '.venv\Scripts\pythonw.exe'
$argument = '"' + (Join-Path $root 'status_app.py') + '" --tray'
$action = New-ScheduledTaskAction -Execute $pythonw -Argument $argument -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)
$principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Limited
$task = New-ScheduledTask -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description 'Read-only status window and tray. Does not enable automatic publishing.'
Register-ScheduledTask -TaskName $name -InputObject $task -Force | Out-Null
if (-not $RegisterOnly) { Start-ScheduledTask -TaskName $name }
