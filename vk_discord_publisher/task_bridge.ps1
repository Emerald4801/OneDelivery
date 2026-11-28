$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
try {
    $request = [Console]::In.ReadToEnd() | ConvertFrom-Json
    $name = [string]$request.name
    if ($name -notmatch '^VKDiscordPublisher-(Status-)?\d+-\d+$') { throw 'Invalid task name.' }
    $root = [IO.Path]::GetFullPath([string]$request.root)
    $kind = if ($name -like 'VKDiscordPublisher-Status-*') { 'status_app.py' } else { 'ongoing.py' }
    $arguments = '"' + (Join-Path $root $kind) + '"'
    if ($kind -eq 'status_app.py') { $arguments += ' --tray' }
    $task = Get-ScheduledTask -TaskPath '\' -TaskName $name -ErrorAction SilentlyContinue
    if ($task -and ($task.Actions.Count -ne 1 -or
        $task.Actions[0].Execute -ne (Join-Path $root '.venv\Scripts\pythonw.exe') -or
        $task.Actions[0].Arguments -ne $arguments -or
        $task.Actions[0].WorkingDirectory -ne $root)) {
        throw 'Task belongs to a different installation. No changes made.'
    }
    $result = $null
    switch ([string]$request.operation) {
        'read' {
            if ($task) { $result = @{ xml=(Export-ScheduledTask -TaskName $name -TaskPath '\'); enabled=[bool]$task.Settings.Enabled; running=($task.State -eq 'Running') } }
        }
        'disable' { Disable-ScheduledTask -TaskName $name -TaskPath '\' | Out-Null }
        'stop' {
            if ($kind -ne 'ongoing.py') { throw 'Only the worker can be stopped here.' }
            Stop-ScheduledTask -TaskName $name -TaskPath '\'
        }
        'start' {
            if (-not $task) { throw 'Task does not exist.' }
            Enable-ScheduledTask -TaskName $name -TaskPath '\' | Out-Null
            Start-ScheduledTask -TaskName $name -TaskPath '\'
        }
        'remove' {
            if ($task -and $task.State -eq 'Running' -and $kind -eq 'ongoing.py') { throw 'Worker task is still running.' }
            if ($task) { Unregister-ScheduledTask -TaskName $name -TaskPath '\' -Confirm:$false }
        }
        'restore' {
            [xml]$definition = [string]$request.xml
            $action = $definition.Task.Actions.Exec
            if ($definition.Task.Actions.ChildNodes.Count -ne 1 -or @($action).Count -ne 1 -or $action.Command -ne (Join-Path $root '.venv\Scripts\pythonw.exe') -or
                $action.Arguments -ne $arguments -or $action.WorkingDirectory -ne $root) { throw 'Invalid task definition.' }
            $definition.Task.RegistrationInfo.URI = '\' + $name
            Register-ScheduledTask -TaskName $name -TaskPath '\' -Xml $definition.OuterXml -Force | Out-Null
            if (-not $request.enabled) { Disable-ScheduledTask -TaskName $name -TaskPath '\' | Out-Null }
        }
        default { throw 'Unsupported operation.' }
    }
    @{ok=$true; result=$result} | ConvertTo-Json -Depth 8 -Compress
} catch {
    @{ok=$false; error=$_.Exception.Message} | ConvertTo-Json -Compress
    exit 1
}
