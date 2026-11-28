param([string]$Bridge, [string]$Root, [string]$Operation, [switch]$Foreign)
$ErrorActionPreference = 'Stop'
$kind = 'ongoing.py'
$global:bridgeCalls = @()
$global:bridgeTask = [pscustomobject]@{
    State='Ready'; Settings=[pscustomobject]@{Enabled=$true}
    Actions=@([pscustomobject]@{
        Execute=(Join-Path $Root '.venv\Scripts\pythonw.exe')
        Arguments=('"' + (Join-Path $Root $kind) + '"')
        WorkingDirectory=$Root
    })
}
if ($Foreign) { $global:bridgeTask.Actions[0].Execute = 'C:\Foreign\pythonw.exe' }
function Get-ScheduledTask { param($TaskName,$TaskPath,$ErrorAction) return $global:bridgeTask }
function Export-ScheduledTask {
    param($TaskName,$TaskPath)
    return '<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"><RegistrationInfo><URI>\Old</URI></RegistrationInfo></Task>'
}
function Register-ScheduledTask {
    param($TaskName,$TaskPath,$Xml,[switch]$Force)
    [xml]$definition = $Xml
    if ($definition.Task.RegistrationInfo.URI -ne ('\' + $TaskName)) { throw 'Task URI was not updated.' }
    $global:bridgeCalls += 'restore'
}
function Disable-ScheduledTask { param($TaskName,$TaskPath) $global:bridgeCalls += 'disable' }
function Enable-ScheduledTask { param($TaskName,$TaskPath) $global:bridgeCalls += 'enable' }
function Start-ScheduledTask { param($TaskName,$TaskPath) $global:bridgeCalls += 'start' }
function Stop-ScheduledTask { param($TaskName,$TaskPath) $global:bridgeCalls += 'stop' }
function Unregister-ScheduledTask { param($TaskName,$TaskPath,$Confirm) $global:bridgeCalls += 'remove' }
$global:LASTEXITCODE = 0
& $Bridge
$bridgeExit = $LASTEXITCODE
if ($bridgeExit -ne 0) { exit $bridgeExit }
if (-not $Foreign -and $Operation -ne 'read' -and -not $global:bridgeCalls.Contains($Operation)) {
    throw ('Expected scheduler operation was not called: ' + $Operation)
}
