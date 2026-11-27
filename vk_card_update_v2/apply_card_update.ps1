param([string]$Target = (Join-Path $env:LOCALAPPDATA 'VKDiscordPublisher'))
$ErrorActionPreference = 'Stop'
$source = $PSScriptRoot
$mutex = $null
$locked = $false
$workerTouched = $false
$viewerClosed = $false
$exitCode = 0
try {
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
    try { $locked = $mutex.WaitOne(0) }
    catch [System.Threading.AbandonedMutexException] { $locked = $true }
    if (-not $locked) { throw 'Another card update is already running.' }
    $wasEnabled = $task.Settings.Enabled
    $wasRunning = $task.State -eq 'Running'
    $backup = Join-Path $Target ('updates\card_status_v2_' + (Get-Date -Format 'yyyyMMdd_HHmmss_fff'))
    & $python $helper prepare --root $Target --backup $backup
    if ($LASTEXITCODE -ne 0) { throw 'Source verification failed before changing installed files.' }
    & $python (Join-Path $Target 'status_app.py') --root $Target --close-viewer
    if ($LASTEXITCODE -ne 0) { throw 'The status window could not be closed. No files were changed.' }
    $viewerClosed = $true
    # Restore task settings even if disabling/stopping fails partway through.
    $workerTouched = $true
    Disable-ScheduledTask -TaskName $taskName | Out-Null
    Stop-ScheduledTask -TaskName $taskName -ErrorAction Stop
    Write-Host 'Waiting up to 30 seconds for the publisher to release its locks...'
    & $python (Join-Path $source 'worker_shutdown.py') --root $Target --timeout 30
    $probeCode = $LASTEXITCODE
    if ($probeCode -eq 1) {
        throw 'The publisher is still busy. Update cancelled before file replacement. Close the utility normally before retrying; no process was forcibly terminated.'
    }
    if ($probeCode -ne 0) { throw 'Cannot verify publisher shutdown. Update cancelled before file replacement.' }
    # apply acquires the same locks again and HOLDS them during the transaction.
    & $python $helper apply --root $Target --backup $backup
    if ($LASTEXITCODE -ne 0) { throw ('Card update was not completed. See the message above. Backup: ' + $backup) }
    # Failure to save a convenience report must not roll back a successful update.
    try {
        Copy-Item -LiteralPath (Join-Path $backup 'manifest.json') -Destination (Join-Path $source 'card_update_result.json') -Force
    } catch { Write-Warning 'Update installed, but its optional report could not be saved.' }
    Write-Host 'Card update installed: loading/published thread mentions; normal card, silent pages.'
    Write-Host ('Backup: ' + $backup)
} catch {
    $exitCode = 1
    Write-Host ('Update stopped: ' + $_.Exception.Message) -ForegroundColor Red
} finally {
    try {
        if ($workerTouched) {
            try {
                if ($wasEnabled -or $wasRunning) { Enable-ScheduledTask -TaskName $taskName | Out-Null }
                else { Disable-ScheduledTask -TaskName $taskName | Out-Null }
                if ($wasRunning) { Start-ScheduledTask -TaskName $taskName }
            } finally {
                if (-not $wasEnabled) { Disable-ScheduledTask -TaskName $taskName | Out-Null }
            }
        }
    } catch {
        $exitCode = 1
        Write-Host ('Could not restore the previous worker mode: ' + $_.Exception.Message) -ForegroundColor Red
    }
    try {
        if ($viewerClosed) {
            # The user needs this visible interactive window after the update.
            Start-Process -FilePath $pythonw -ArgumentList ('"' + (Join-Path $Target 'status_app.py') + '"') -WorkingDirectory $Target
        }
    } catch {
        $exitCode = 1
        Write-Host ('Could not reopen the status window: ' + $_.Exception.Message) -ForegroundColor Red
    } finally {
        if ($locked) { $mutex.ReleaseMutex() }
        if ($null -ne $mutex) { $mutex.Dispose() }
    }
}
exit $exitCode
