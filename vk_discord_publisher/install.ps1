param(
    [string]$Target = (Join-Path $env:LOCALAPPDATA 'VKDiscordPublisher'),
    [string]$ExistingRoot = (Join-Path $env:USERPROFILE 'Downloads\vk_discord_publisher_windows_v1\vk_discord_publisher')
)
$ErrorActionPreference = 'Stop'
$source = $PSScriptRoot
$Target = [System.IO.Path]::GetFullPath($Target)
New-Item -ItemType Directory -Path $Target -Force | Out-Null
$existingConfig = Join-Path $Target 'config.json'
if (Test-Path -LiteralPath $existingConfig) {
    $cfg = Get-Content -LiteralPath $existingConfig -Raw -Encoding UTF8 | ConvertFrom-Json
    $taskName = 'VKDiscordPublisher-' + $cfg.guild_id + '-' + $cfg.channel_id
    $oldTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($oldTask) {
        Disable-ScheduledTask -TaskName $taskName | Out-Null
        Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    }
}
$files = @('copy_update_file.py','publish_control.py','publisher_controller.py','control_client.py','control_task.ps1','app_icon.py','UPDATE_STAGE2.cmd','update_stage2.ps1','stage2_base_hashes.json','README_STAGE2.txt','publication_state.py','worker_status.py','status_app.py','requirements-ui.lock','status_task.ps1','OPEN_STATUS.cmd','README_STAGE1.txt','ongoing.py','vk_source.py','vk_api.py','probe_vk_api.py','PROBE_VK_API.cmd','vk_session.py','snapshot_worker.py','download_album.py','publisher_runtime.py','publish_album_v3.py','album_info.py','requirements.lock','prepare_env.ps1','task.ps1','install.ps1','INSTALL_AUTO.cmd','START_AUTO.cmd','STOP_AUTO.cmd','CHECK_AUTO.cmd','IMPORT_VK_SESSION.cmd','_run.cmd','SETUP.cmd','PUBLISH_ALBUM.cmd','PUBLISH_LOCAL.cmd','CHECK_CONNECTION.cmd','README_AUTO.txt','README_V3.txt','README.txt')
if ($source -ne $Target) {
    foreach ($name in $files) { Copy-Item -LiteralPath (Join-Path $source $name) -Destination (Join-Path $Target $name) -Force }
}
foreach ($name in @('config.json','token.dpapi','vk_api_token.dpapi','vk_cookies.dpapi','cookies.txt','state','.venv')) {
    $destination = Join-Path $Target $name
    if (Test-Path -LiteralPath $destination) { continue }
    $origin = Join-Path $source $name
    if (-not (Test-Path -LiteralPath $origin)) { $origin = Join-Path $ExistingRoot $name }
    if (Test-Path -LiteralPath $origin) { Copy-Item -LiteralPath $origin -Destination $destination -Recurse }
}
Write-Host ('Working folder: ' + $Target)
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Target 'prepare_env.ps1')
if ($LASTEXITCODE -ne 0) { throw 'Dependencies are not ready.' }
& (Join-Path $Target '.venv\Scripts\python.exe') -u (Join-Path $Target 'probe_vk_api.py') --root $Target --activate
if ($LASTEXITCODE -ne 0) { throw 'The API acceptance check is missing or incomplete. Automatic publishing was not enabled.' }
& (Join-Path $Target '.venv\Scripts\python.exe') -m pip install --disable-pip-version-check --no-deps -r (Join-Path $Target 'requirements-ui.lock')
if ($LASTEXITCODE -ne 0) { throw 'Tray dependencies are not ready.' }
& (Join-Path $Target '.venv\Scripts\python.exe') -c "import tkinter,pystray;from PIL import Image,ImageTk;w=tkinter.Tk();w.withdraw();i=ImageTk.PhotoImage(Image.new('RGBA',(32,32)),master=w);w.iconphoto(True,i);w.destroy()"
if ($LASTEXITCODE -ne 0) { throw 'Tk/tray is unavailable. Automatic publishing was not enabled.' }
& (Join-Path $Target '.venv\Scripts\python.exe') (Join-Path $Target 'publish_control.py') --root $Target --paused false --set-paused
if ($LASTEXITCODE -ne 0) { throw 'Automatic mode could not be saved. Automatic publishing was not enabled.' }
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Target 'task.ps1') -Mode Install
if ($LASTEXITCODE -ne 0) { throw 'Diagnostic failed or task registration failed. Automatic publishing was not enabled.' }

& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Target 'status_task.ps1')
if ($LASTEXITCODE -ne 0) { throw 'Status startup registration failed. The worker state can be inspected through CHECK_AUTO.cmd.' }
