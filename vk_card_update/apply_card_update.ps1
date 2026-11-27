param([string]$Target = (Join-Path $env:LOCALAPPDATA 'VKDiscordPublisher'))
$ErrorActionPreference = 'Stop'
$source = $PSScriptRoot
$Target = [System.IO.Path]::GetFullPath($Target)
$python = Join-Path $Target '.venv\Scripts\python.exe'
$pythonw = Join-Path $Target '.venv\Scripts\pythonw.exe'
$helper = Join-Path $source 'publisher_patch.py'
if (-not (Test-Path -LiteralPath $python) -or -not (Test-Path -LiteralPath (Join-Path $Target 'status_app.py'))) {
    throw 'Installed v9 was not found. No files were changed.'
}
$cfg = Get-Content -LiteralPath (Join-Path $Target 'config.json') -Raw -Encoding UTF8 | ConvertFrom-Json
if ([string]$cfg.guild_id -notmatch '^\d+$' -or [string]$cfg.channel_id -notmatch '^\d+$') {
    throw 'Invalid destination identifiers. No files were changed.'
}
$taskName = 'VKDiscordPublisher-' + $cfg.guild_id + '-' + $cfg.channel_id
$task = Get-ScheduledTask -TaskName $taskName -ErrorAction Stop
$expectedArguments = '"' + (Join-Path $Target 'ongoing.py') + '"'
if ($task.Actions.Count -ne 1 -or $task.Actions[0].Execute -ne $pythonw -or $task.Actions[0].Arguments -ne $expectedArguments) {
    throw 'The worker task does not match this installation. No task or file was changed.'
}
$mutex = [System.Threading.Mutex]::new($false, ('Local\VKDiscordCardUpdate_' + $cfg.guild_id + '_' + $cfg.channel_id))
$locked = $false
$workerTouched = $false
$viewerClosed = $false
$applied = $false
$wasEnabled = $task.Settings.Enabled
$wasRunning = $task.State -eq 'Running'
$backup = Join-Path $Target ('updates\card_status_' + (Get-Date -Format 'yyyyMMdd_HHmmss_fff'))
try {
    try { $locked = $mutex.WaitOne(0) }
    catch [System.Threading.AbandonedMutexException] { $locked = $true }
    if (-not $locked) { throw 'Another card update is already running.' }
    & $python $helper prepare --root $Target --backup $backup
    if ($LASTEXITCODE -ne 0) { throw 'Source verification failed before changing installed files.' }
    & $python (Join-Path $Target 'status_app.py') --root $Target --close-viewer
    if ($LASTEXITCODE -ne 0) { throw 'The status window could not be closed. No files were changed.' }
    $viewerClosed = $true
    Disable-ScheduledTask -TaskName $taskName | Out-Null
    $workerTouched = $true
    Stop-ScheduledTask -TaskName $taskName -ErrorAction Stop
    $limit = (Get-Date).AddSeconds(20)
    while ((Get-ScheduledTask -TaskName $taskName).State -eq 'Running') {
        if ((Get-Date) -gt $limit) { throw 'The worker did not stop. No files were changed.' }
        Start-Sleep -Milliseconds 250
    }
    & $python -c "import sys,json,pathlib,contextlib;sys.path.insert(0,sys.argv[1]);import ongoing,publisher_runtime as r;p=pathlib.Path(sys.argv[1]);c=json.loads((p/'config.json').read_text(encoding='utf-8'));s=contextlib.ExitStack();s.enter_context(ongoing.channel_lock(c));s.enter_context(r.exclusive(p));s.close()" $Target
    if ($LASTEXITCODE -ne 0) { throw 'A manually started publisher is still running. No files were changed.' }
    & $python $helper apply --root $Target --backup $backup
    if ($LASTEXITCODE -ne 0) { throw 'Card update was not applied. See the message above.' }
    $applied = $true
    Copy-Item -LiteralPath (Join-Path $backup 'manifest.json') -Destination (Join-Path $source 'card_update_result.json') -Force
    Write-Host 'Card update installed: loading/published thread mentions; normal card, silent pages.'
    Write-Host ('Backup: ' + $backup)
} catch {
    if ($applied) {
        & $python $helper restore --root $Target --backup $backup
        if ($LASTEXITCODE -ne 0) { Write-Warning ('Automatic restoration failed. Backup: ' + $backup) }
    }
    throw
} finally {
    try {
        try {
            if ($workerTouched) {
                if ($wasEnabled -or $wasRunning) { Enable-ScheduledTask -TaskName $taskName | Out-Null }
                else { Disable-ScheduledTask -TaskName $taskName | Out-Null }
                if ($wasRunning) {
                    Start-ScheduledTask -TaskName $taskName
                    if (-not $wasEnabled) { Disable-ScheduledTask -TaskName $taskName | Out-Null }
                }
            }
        } finally {
            if ($viewerClosed) {
                # This is the interactive window the user needs to review after the update.
                Start-Process -FilePath $pythonw -ArgumentList ('"' + (Join-Path $Target 'status_app.py') + '"') -WorkingDirectory $Target
            }
        }
    } finally {
        if ($locked) { $mutex.ReleaseMutex() }
        $mutex.Dispose()
    }
}
