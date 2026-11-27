param([string]$TestRoot,
      [ValidateSet('Running','Ready')][string]$InitialState = 'Running',
      [bool]$InitiallyEnabled = $true,
      [bool]$ExpectSuccess = $true,
      [string]$StopSignal = '')
$ErrorActionPreference = 'Stop'
$TestRoot = [System.IO.Path]::GetFullPath($TestRoot)
$global:cardUpdateTestTask = [PSCustomObject]@{
    State = $InitialState
    Settings = [PSCustomObject]@{Enabled = $InitiallyEnabled}
    Actions = @([PSCustomObject]@{
        Execute = (Join-Path $TestRoot '.venv\Scripts\pythonw.exe')
        Arguments = ('"' + (Join-Path $TestRoot 'ongoing.py') + '"')
    })
}
$global:cardUpdateTestCalls = @()
function Get-ScheduledTask { param($TaskName,$ErrorAction) return $global:cardUpdateTestTask }
function Disable-ScheduledTask { param($TaskName,$ErrorAction) $global:cardUpdateTestTask.Settings.Enabled=$false; $global:cardUpdateTestCalls+='disable' }
function Enable-ScheduledTask { param($TaskName,$ErrorAction) $global:cardUpdateTestTask.Settings.Enabled=$true; $global:cardUpdateTestCalls+='enable' }
function Stop-ScheduledTask {
    param($TaskName,$ErrorAction)
    $global:cardUpdateTestTask.State='Ready'
    $global:cardUpdateTestCalls+='stop'
    if ($StopSignal) { [System.IO.File]::WriteAllText($StopSignal, 'stop requested') }
}
function Start-ScheduledTask { param($TaskName,$ErrorAction) $global:cardUpdateTestTask.State='Running'; $global:cardUpdateTestCalls+='start' }
function Start-Process { param($FilePath,$ArgumentList,$WorkingDirectory) $global:cardUpdateTestCalls+='viewer' }
$configBefore = [System.IO.File]::ReadAllBytes((Join-Path $TestRoot 'config.json'))
$originals = @{}
foreach ($name in @('publish_album_v3.py','ongoing.py')) {
    $originals[$name] = [System.IO.File]::ReadAllBytes((Join-Path $TestRoot $name))
}
& (Join-Path $PSScriptRoot 'apply_card_update.ps1') -Target $TestRoot
$updateCode = $LASTEXITCODE
if (($ExpectSuccess -and $updateCode -ne 0) -or (-not $ExpectSuccess -and $updateCode -eq 0)) {
    throw ('Unexpected installer exit code: ' + $updateCode)
}
if ($global:cardUpdateTestTask.Settings.Enabled -ne $InitiallyEnabled -or $global:cardUpdateTestTask.State -ne $InitialState) {
    throw 'The previous worker state was not restored.'
}
$configAfter = [System.IO.File]::ReadAllBytes((Join-Path $TestRoot 'config.json'))
if ([Convert]::ToBase64String($configBefore) -ne [Convert]::ToBase64String($configAfter)) { throw 'Configuration changed.' }
if (-not $global:cardUpdateTestCalls.Contains('viewer')) { throw 'The UI restart was not requested.' }
foreach ($name in $originals.Keys) {
    $content = [System.IO.File]::ReadAllBytes((Join-Path $TestRoot $name))
    if ($ExpectSuccess) {
        if (-not [System.Text.Encoding]::UTF8.GetString($content).StartsWith('# VK_CARD_STATUS_UPDATE_1')) { throw ('Update missing: ' + $name) }
    } elseif ([Convert]::ToBase64String($content) -ne [Convert]::ToBase64String($originals[$name])) {
        throw ('Busy installer changed source: ' + $name)
    }
}
Write-Output ('Isolated Windows installer test passed: ' + ($global:cardUpdateTestCalls -join ', '))
