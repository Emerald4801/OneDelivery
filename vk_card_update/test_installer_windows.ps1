param([string]$TestRoot,
      [ValidateSet('Running','Ready')][string]$InitialState = 'Running',
      [bool]$InitiallyEnabled = $true)
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
function Stop-ScheduledTask { param($TaskName,$ErrorAction) $global:cardUpdateTestTask.State='Ready'; $global:cardUpdateTestCalls+='stop' }
function Start-ScheduledTask { param($TaskName,$ErrorAction) $global:cardUpdateTestTask.State='Running'; $global:cardUpdateTestCalls+='start' }
function Start-Process { param($FilePath,$ArgumentList,$WorkingDirectory) $global:cardUpdateTestCalls+='viewer' }
$configBefore = [System.IO.File]::ReadAllBytes((Join-Path $TestRoot 'config.json'))
& (Join-Path $PSScriptRoot 'apply_card_update.ps1') -Target $TestRoot
if ($global:cardUpdateTestTask.Settings.Enabled -ne $InitiallyEnabled -or $global:cardUpdateTestTask.State -ne $InitialState) {
    throw 'The previous worker state was not restored.'
}
$configAfter = [System.IO.File]::ReadAllBytes((Join-Path $TestRoot 'config.json'))
if ([Convert]::ToBase64String($configBefore) -ne [Convert]::ToBase64String($configAfter)) { throw 'Configuration changed.' }
if (-not $global:cardUpdateTestCalls.Contains('viewer')) { throw 'The UI restart was not requested.' }
Write-Output ('Isolated Windows installer test passed: ' + ($global:cardUpdateTestCalls -join ', '))
