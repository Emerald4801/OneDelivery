$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
try {
    $request = [Console]::In.ReadToEnd() | ConvertFrom-Json
    if ([string]$request.guild -notmatch '^\d+$' -or [string]$request.channel -notmatch '^\d+$') { throw 'Invalid scope' }
    $root = [IO.Path]::GetFullPath([string]$request.root)
    $suffix = [string]$request.guild + '-' + [string]$request.channel
    $result = @()
    foreach ($kind in @('worker','window')) {
        $name = if ($kind -eq 'worker') { 'VKDiscordPublisher-' + $suffix } else { 'VKDiscordPublisher-Status-' + $suffix }
        $task = Get-ScheduledTask -TaskName $name -TaskPath '\' -ErrorAction SilentlyContinue
        if (-not $task) { $result += @{kind=$kind; exists=$false}; continue }
        $script = if ($kind -eq 'worker') { 'ongoing.py' } else { 'status_app.py' }
        $argument = '"' + (Join-Path $root $script) + '"'
        if ($kind -eq 'window') { $argument += ' --tray' }
        $actions = @($task.Actions)
        $matches = $actions.Count -eq 1 -and $actions[0].Execute -eq (Join-Path $root '.venv\Scripts\pythonw.exe') -and
            $actions[0].Arguments -eq $argument -and $actions[0].WorkingDirectory -eq $root
        $state = [string]$task.State
        if ($state -notin @('Disabled','Queued','Ready','Running','Unknown')) { $state = 'Unknown' }
        $entry = @{kind=$kind; exists=$true; matches=$matches; enabled=[bool]$task.Settings.Enabled; state=$state}
        $info = Get-ScheduledTaskInfo -TaskName $name -TaskPath '\' -ErrorAction SilentlyContinue
        if ($info) { $entry.last_result = [long]$info.LastTaskResult }
        $result += $entry
    }
    @{ok=$true; tasks=@($result)} | ConvertTo-Json -Depth 5 -Compress
} catch {
    @{ok=$false} | ConvertTo-Json -Compress
    exit 1
}
