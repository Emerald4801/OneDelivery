param([string]$Target = (Join-Path $env:LOCALAPPDATA 'VKDiscordPublisher'))
$ErrorActionPreference = 'Stop'
$source = $PSScriptRoot
$Target = [System.IO.Path]::GetFullPath($Target)
if ($source -eq $Target) { throw 'Extract the update to a separate folder, then run UPDATE_STAGE1.cmd there.' }
$configPath = Join-Path $Target 'config.json'
$python = Join-Path $Target '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $configPath) -or -not (Test-Path -LiteralPath $python)) {
    throw 'An installed v7 was not found in LOCALAPPDATA\VKDiscordPublisher. No new installation or publishing was started.'
}
$cfg = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
$taskName = 'VKDiscordPublisher-' + $cfg.guild_id + '-' + $cfg.channel_id
$statusTaskName = 'VKDiscordPublisher-Status-' + $cfg.guild_id + '-' + $cfg.channel_id
$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
$statusTask = Get-ScheduledTask -TaskName $statusTaskName -ErrorAction SilentlyContinue
$wasEnabled = $task -and $task.Settings.Enabled
$wasRunning = $task -and $task.State -eq 'Running'
$oldXml = if ($task) { Export-ScheduledTask -TaskName $taskName } else { $null }
$oldStatusXml = if ($statusTask) { Export-ScheduledTask -TaskName $statusTaskName } else { $null }

# Refuse silently overwriting an unexpected worker or locally edited publication code.
$baseline = Get-Content -LiteralPath (Join-Path $source 'stage1_base_hashes.json') -Raw | ConvertFrom-Json
foreach ($prop in $baseline.PSObject.Properties) {
    $path = Join-Path $Target $prop.Name
    $hash = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
    $newHash = (Get-FileHash -LiteralPath (Join-Path $source $prop.Name) -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($hash -ne $prop.Value -and $hash -ne $newHash) {
        throw ('Installed file differs from v7: ' + $prop.Name + '. Update stopped before changes.')
    }
}

Write-Host 'Preparing the status window. VK and Discord will not be contacted by this check.'
& $python -m pip install --disable-pip-version-check --no-deps -r (Join-Path $source 'requirements-ui.lock')
if ($LASTEXITCODE -ne 0) { throw 'Tray dependencies could not be installed. The running worker was not changed.' }
& $python -c "import tkinter as t,pystray;from PIL import Image;w=t.Tk();w.withdraw();w.destroy()"
if ($LASTEXITCODE -ne 0) { throw 'Tk/tray check failed. The running worker was not changed. Python needs Tcl/Tk support.' }
& $python -m py_compile (Join-Path $source 'ongoing.py') (Join-Path $source 'status_app.py') (Join-Path $source 'worker_status.py') (Join-Path $source 'publication_state.py')
if ($LASTEXITCODE -ne 0) { throw 'Source verification failed before updating.' }

$files = @('ongoing.py','publish_album_v3.py','publisher_runtime.py','publication_state.py','worker_status.py','status_app.py','requirements-ui.lock','status_task.ps1','OPEN_STATUS.cmd','UPDATE_STAGE1.cmd','update_stage1.ps1','stage1_base_hashes.json','README_STAGE1.txt','install.ps1')
$backup = Join-Path $Target ('updates\stage1_' + (Get-Date -Format 'yyyyMMdd_HHmmss_fff'))
New-Item -ItemType Directory -Path $backup -Force | Out-Null
foreach ($name in $files) {
    if (Test-Path -LiteralPath (Join-Path $Target $name)) { Copy-Item -LiteralPath (Join-Path $Target $name) -Destination (Join-Path $backup $name) }
}
if ($oldXml) { $oldXml | Set-Content -LiteralPath (Join-Path $backup 'worker_task.xml') -Encoding Unicode }
if ($oldStatusXml) { $oldStatusXml | Set-Content -LiteralPath (Join-Path $backup 'status_task.xml') -Encoding Unicode }

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
    if ($LASTEXITCODE -ne 0) { throw 'A manually started worker or publisher is still running. Close it and run UPDATE_STAGE1.cmd again.' }
    # No config, secrets, downloads or publication receipts are copied or rewritten.
    foreach ($name in $files) { Copy-Item -LiteralPath (Join-Path $source $name) -Destination (Join-Path $Target $name) -Force }
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Target 'status_task.ps1') -RegisterOnly
    if ($LASTEXITCODE -ne 0) { throw 'Status startup registration failed.' }
    if ($wasEnabled) { Enable-ScheduledTask -TaskName $taskName | Out-Null }
    if ($wasRunning) { Start-ScheduledTask -TaskName $taskName }
    # Show the window now; next Windows sign-in starts it minimized in the tray.
    Start-Process -FilePath (Join-Path $Target '.venv\Scripts\pythonw.exe') -ArgumentList ('"' + (Join-Path $Target 'status_app.py') + '"') -WorkingDirectory $Target
    Write-Host 'Stage 1 installed. The previous automatic publishing state was preserved.'
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
    if ($oldXml) {
        Register-ScheduledTask -TaskName $taskName -Xml $oldXml -Force | Out-Null
        if (-not $wasEnabled) { Disable-ScheduledTask -TaskName $taskName | Out-Null }
        if ($wasRunning) { Start-ScheduledTask -TaskName $taskName }
    }
    if ($oldStatusXml) { Register-ScheduledTask -TaskName $statusTaskName -Xml $oldStatusXml -Force | Out-Null }
    else { Unregister-ScheduledTask -TaskName $statusTaskName -Confirm:$false -ErrorAction SilentlyContinue }
    throw $failure
}
