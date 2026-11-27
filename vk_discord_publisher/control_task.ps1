param([ValidateSet('Manual','Resume')][string]$Mode = 'Manual')
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$cfg = Get-Content -LiteralPath (Join-Path $root 'config.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$name = 'VKDiscordPublisher-' + $cfg.guild_id + '-' + $cfg.channel_id
$task = Get-ScheduledTask -TaskName $name -ErrorAction Stop
$pythonw = Join-Path $root '.venv\Scripts\pythonw.exe'
$worker = '"' + (Join-Path $root 'ongoing.py') + '"'
if ($task.Actions.Count -ne 1 -or $task.Actions[0].Execute -ne $pythonw -or $task.Actions[0].Arguments -ne $worker) {
    throw 'The registered task does not match this installation. No task was started.'
}
if ($Mode -eq 'Manual' -and ($task.State -ne 'Running' -or -not $task.Settings.Enabled)) {
    & (Join-Path $root '.venv\Scripts\python.exe') (Join-Path $root 'publish_control.py') --root $root --paused true --set-paused
    if ($LASTEXITCODE -ne 0) { throw 'The paused state could not be saved. No worker was started.' }
}
Enable-ScheduledTask -TaskName $name | Out-Null
if ($task.State -ne 'Running') { Start-ScheduledTask -TaskName $name }
