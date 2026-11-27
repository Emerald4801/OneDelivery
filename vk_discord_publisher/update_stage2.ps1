param([string]$Target = (Join-Path $env:LOCALAPPDATA 'VKDiscordPublisher'))
$ErrorActionPreference = 'Stop'
$source = $PSScriptRoot
$Target = [System.IO.Path]::GetFullPath($Target)
if ($source -eq $Target) { throw 'Extract the update to a separate folder, then run UPDATE_STAGE2.cmd there.' }
$configPath = Join-Path $Target 'config.json'
$python = Join-Path $Target '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $configPath) -or -not (Test-Path -LiteralPath $python)) {
    throw 'An installed v8/v9 was not found in LOCALAPPDATA\VKDiscordPublisher. No new installation or publishing was started.'
}
$cfg = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
$taskName = 'VKDiscordPublisher-' + $cfg.guild_id + '-' + $cfg.channel_id
$statusTaskName = 'VKDiscordPublisher-Status-' + $cfg.guild_id + '-' + $cfg.channel_id
$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if (-not $task) { throw 'The worker task was not found. Update stopped before changes.' }
$statusTask = Get-ScheduledTask -TaskName $statusTaskName -ErrorAction SilentlyContinue
$wasEnabled = $task -and $task.Settings.Enabled
$wasRunning = $task -and $task.State -eq 'Running'
$oldXml = if ($task) { Export-ScheduledTask -TaskName $taskName } else { $null }
$oldStatusXml = if ($statusTask) { Export-ScheduledTask -TaskName $statusTaskName } else { $null }

# Refuse silently overwriting an unexpected worker or locally edited publication code.
$baseline = Get-Content -LiteralPath (Join-Path $source 'stage2_base_hashes.json') -Raw | ConvertFrom-Json
foreach ($prop in $baseline.PSObject.Properties) {
    $path = Join-Path $Target $prop.Name
    $hash = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
    $newHash = (Get-FileHash -LiteralPath (Join-Path $source $prop.Name) -Algorithm SHA256).Hash.ToLowerInvariant()
    if ((@($prop.Value) + @($newHash)) -notcontains $hash) {
        throw ('Installed file differs from supported v8/v9 versions: ' + $prop.Name + '. Update stopped before changes.')
    }
}

Write-Host 'Preparing the status window. VK and Discord will not be contacted by this check.'
& $python -c "import tkinter as t,pystray;from PIL import Image,ImageTk;w=t.Tk();w.withdraw();i=ImageTk.PhotoImage(Image.new('RGBA',(32,32)),master=w);w.iconphoto(True,i);w.destroy()"
if ($LASTEXITCODE -ne 0) { throw 'Tk/tray check failed. The running worker was not changed. Python needs Tcl/Tk support.' }
& $python -m py_compile (Join-Path $source 'ongoing.py') (Join-Path $source 'status_app.py') (Join-Path $source 'worker_status.py') (Join-Path $source 'publication_state.py') (Join-Path $source 'publish_control.py') (Join-Path $source 'publisher_controller.py') (Join-Path $source 'control_client.py') (Join-Path $source 'app_icon.py')
if ($LASTEXITCODE -ne 0) { throw 'Source verification failed before updating.' }

$files = @('copy_update_file.py','README_AUTO.txt','ongoing.py','publish_album_v3.py','publisher_runtime.py','publication_state.py','worker_status.py','status_app.py','publish_control.py','publisher_controller.py','control_client.py','control_task.ps1','app_icon.py','status_task.ps1','OPEN_STATUS.cmd','UPDATE_STAGE2.cmd','update_stage2.ps1','stage2_base_hashes.json','README_STAGE2.txt','install.ps1','START_AUTO.cmd','STOP_AUTO.cmd')
$backup = Join-Path $Target ('updates\stage2_' + (Get-Date -Format 'yyyyMMdd_HHmmss_fff'))
New-Item -ItemType Directory -Path $backup -Force | Out-Null
foreach ($name in $files) {
    if (Test-Path -LiteralPath (Join-Path $Target $name)) { Copy-Item -LiteralPath (Join-Path $Target $name) -Destination (Join-Path $backup $name) }
}
if ($oldXml) { $oldXml | Set-Content -LiteralPath (Join-Path $backup 'worker_task.xml') -Encoding Unicode }
if ($oldStatusXml) { $oldStatusXml | Set-Content -LiteralPath (Join-Path $backup 'status_task.xml') -Encoding Unicode }
$controlPath = Join-Path $Target ('state\' + $cfg.guild_id + '\' + $cfg.channel_id + '\control.json')
$hadControl = $false
$controlBackedUp = $false


try {
    if ($task) {
        Disable-ScheduledTask -TaskName $taskName | Out-Null
        Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        $limit = (Get-Date).AddSeconds(20)
        while ((Get-ScheduledTask -TaskName $taskName).State -eq 'Running') {
            if ((Get-Date) -gt $limit) { throw 'The worker did not stop in time. No files were replaced.' }
            Start-Sleep -Milliseconds 250
        }
    }
    if ($statusTask) { Stop-ScheduledTask -TaskName $statusTaskName -ErrorAction SilentlyContinue }
    & $python (Join-Path $source 'status_app.py') --root $Target --close-viewer
    if ($LASTEXITCODE -ne 0) { throw 'The previous status viewer could not be closed.' }
    & $python -c "import sys,json,contextlib,pathlib;sys.path.insert(0,sys.argv[1]);import ongoing,publisher_runtime as r;p=pathlib.Path(sys.argv[1]);c=json.loads((p/'config.json').read_text(encoding='utf-8'));s=contextlib.ExitStack();s.enter_context(ongoing.channel_lock(c));s.enter_context(r.exclusive(p));s.close()" $Target
    if ($LASTEXITCODE -ne 0) { throw 'A manually started worker or publisher is still running. Close it and run UPDATE_STAGE2.cmd again.' }
    $hadControl = Test-Path -LiteralPath $controlPath
    if ($hadControl) { Copy-Item -LiteralPath $controlPath -Destination (Join-Path $backup 'control.json') }
    $controlBackedUp = $true
    # No config, secrets, downloads or publication receipts are copied or rewritten.
    foreach ($name in $files) {
        & $python (Join-Path $source 'copy_update_file.py') (Join-Path $source $name) (Join-Path $Target $name)
        if ($LASTEXITCODE -ne 0) { throw ('File replacement failed: ' + $name) }
    }
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Target 'status_task.ps1') -RegisterOnly
    if ($LASTEXITCODE -ne 0) { throw 'Status startup registration failed.' }
    $initialPause = if ($wasRunning -and $wasEnabled) { 'false' } else { 'true' }
    & $python (Join-Path $Target 'publish_control.py') --root $Target --paused $initialPause
    if ($LASTEXITCODE -ne 0) { throw 'Control state initialization failed.' }
    if (-not ($wasRunning -and $wasEnabled)) {
        & $python (Join-Path $Target 'publish_control.py') --root $Target --paused true --set-paused
        if ($LASTEXITCODE -ne 0) { throw 'The paused publication state could not be preserved.' }
    }
    # The listener stays alive for manual requests; the durable pause gates all auto work.
    Enable-ScheduledTask -TaskName $taskName | Out-Null
    $startedAt = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    Start-ScheduledTask -TaskName $taskName
    $statusPath = Join-Path $Target ('state\' + $cfg.guild_id + '\' + $cfg.channel_id + '\worker_status.json')
    $ready = $false
    $limit = (Get-Date).AddSeconds(15)
    while ((Get-Date) -lt $limit) {
        if (Test-Path -LiteralPath $statusPath) {
            try {
                $fresh = Get-Content -LiteralPath $statusPath -Raw -Encoding UTF8 | ConvertFrom-Json
                if ($fresh.controller_version -eq 2 -and $fresh.started_at -ge $startedAt -and $fresh.running) {
                    $ready = $true
                    break
                }
            } catch { }
        }
        Start-Sleep -Milliseconds 250
    }
    if (-not $ready) { throw 'The new controller did not report startup. The previous code and task state will be restored.' }
    # Show the window now; next Windows sign-in starts it minimized in the tray.
    Start-Process -FilePath (Join-Path $Target '.venv\Scripts\pythonw.exe') -ArgumentList ('"' + (Join-Path $Target 'status_app.py') + '"') -WorkingDirectory $Target
    Write-Host 'v9.2 installed: taskbar identity and 10-minute album observation. Automatic publishing mode was preserved.'
    Write-Host ('Working folder: ' + $Target)
    Write-Host ('Backup: ' + $backup)
} catch {
    $failure = $_
    if ($task) { Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue }
    foreach ($name in $files) {
        $saved = Join-Path $backup $name
        if (Test-Path -LiteralPath $saved) { Copy-Item -LiteralPath $saved -Destination (Join-Path $Target $name) -Force }
        else { Remove-Item -LiteralPath (Join-Path $Target $name) -Force -ErrorAction SilentlyContinue }
    }
    if ($controlBackedUp) {
        if ($hadControl) { Copy-Item -LiteralPath (Join-Path $backup 'control.json') -Destination $controlPath -Force }
        else { Remove-Item -LiteralPath $controlPath -Force -ErrorAction SilentlyContinue }
    }
    if ($oldXml) {
        Register-ScheduledTask -TaskName $taskName -Xml $oldXml -Force | Out-Null
        if (-not $wasEnabled) { Disable-ScheduledTask -TaskName $taskName | Out-Null }
        if ($wasRunning) { Start-ScheduledTask -TaskName $taskName }
    }
    if ($oldStatusXml) { Register-ScheduledTask -TaskName $statusTaskName -Xml $oldStatusXml -Force | Out-Null }
    else { Unregister-ScheduledTask -TaskName $statusTaskName -Confirm:$false -ErrorAction SilentlyContinue }
    throw $failure
}
