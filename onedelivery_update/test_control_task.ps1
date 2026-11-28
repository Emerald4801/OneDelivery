param([string]$Root, [ValidateSet('Manual','Resume')][string]$Mode,
      [string]$State, [int]$Enabled, [switch]$Foreign, [switch]$Preview)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$global:testTaskCalls = @()
function Enable-ScheduledTask { param($TaskName) $global:testTaskCalls += 'enable' }
function Start-ScheduledTask { param($TaskName) $global:testTaskCalls += 'start' }
function Stop-ScheduledTask { throw 'Stopping a worker is forbidden in these tests.' }
function Register-ScheduledTask { throw 'Registering a task is forbidden in these tests.' }
function Unregister-ScheduledTask { throw 'Removing a task is forbidden in these tests.' }
function Disable-ScheduledTask { throw 'Disabling a task is forbidden in these tests.' }
function Get-ScheduledTask {
    param($TaskName,$ErrorAction)
    $executable = Join-Path $Root '.venv\Scripts\pythonw.exe'
    if ($Foreign) { $executable = 'C:\Foreign\pythonw.exe' }
    return [pscustomobject]@{
        State=$State; Settings=[pscustomobject]@{Enabled=[bool]$Enabled}
        Actions=@([pscustomobject]@{
            Execute=$executable; Arguments=('"' + (Join-Path $Root 'ongoing.py') + '"')
        })
    }
}
try {
    if ($Preview) {
        # Real New-ScheduledTask* cmdlets create an in-memory definition only.
        # Registration/start operations remain blocked by the stubs above.
        $definition = & (Join-Path $Root 'task.ps1') -Mode Install -Preview
        @{ok=$true; definition=$definition; calls=$global:testTaskCalls} | ConvertTo-Json -Depth 6 -Compress
    } else {
        $null = & (Join-Path $Root 'control_task.ps1') -Mode $Mode
        @{ok=$true; calls=$global:testTaskCalls} | ConvertTo-Json -Depth 6 -Compress
    }
} catch {
    @{ok=$false; error=$_.Exception.Message; calls=$global:testTaskCalls} | ConvertTo-Json -Depth 6 -Compress
    exit 1
}
