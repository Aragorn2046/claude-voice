$ErrorActionPreference = "Continue"
$port = 9876
$task = "ShelbyVoiceReceiver"
$script = "C:\Users\Arago\scripts\win-audio-receiver.py"

# Stop any current listener on the port (e.g. the temp-dir instance) to free the bind.
$c = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue
if ($c) {
    try { Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue; Write-Output "stopped old listener pid $($c.OwningProcess)" } catch {}
    Start-Sleep -Seconds 1
}

# Resolve a no-console python (pythonw) if possible.
$exe = "C:\Users\Arago\AppData\Local\Python\bin\pythonw.exe"
if (-not (Test-Path $exe)) { $g = Get-Command pythonw -ErrorAction SilentlyContinue; if ($g) { $exe = $g.Source } }
if (-not (Test-Path $exe)) { $g = Get-Command python -ErrorAction SilentlyContinue; if ($g) { $exe = $g.Source } }
Write-Output "python: $exe"

$action    = New-ScheduledTaskAction -Execute $exe -Argument "$script $port"

# AtLogOn fires once per interactive logon. But the receiver is a long-running
# server: if its process dies/crashes/is-killed mid-session the task returns to
# Ready and -RestartCount (which only covers *failure to start*) does NOT relaunch
# it — that is the "present but not running" gap that silently broke :9876.
# Fix: graft an indefinite 5-min repetition onto the logon trigger so a dead
# receiver is relaunched within 5 minutes. -MultipleInstances IgnoreNew (below)
# makes each repeat a no-op while the server is still alive, so no double-bind.
$trigger   = New-ScheduledTaskTrigger -AtLogOn
$trigger.Repetition = (New-ScheduledTaskTrigger -Once -At (Get-Date).AddSeconds(10) -RepetitionInterval (New-TimeSpan -Minutes 5)).Repetition

$settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $task -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
Write-Output "task registered: $task (AtLogOn + 5min self-heal repetition, IgnoreNew, no exec-time-limit)"

Start-ScheduledTask -TaskName $task
Start-Sleep -Seconds 3
$c2 = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue
if ($c2) { Write-Output "OK listening on $port pid $($c2.OwningProcess)" } else { Write-Output "FAIL not listening - check log" }
