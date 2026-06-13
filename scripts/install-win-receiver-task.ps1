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
$trigger   = New-ScheduledTaskTrigger -AtLogOn
$settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $task -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
Write-Output "task registered: $task"

Start-ScheduledTask -TaskName $task
Start-Sleep -Seconds 3
$c2 = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue
if ($c2) { Write-Output "OK listening on $port pid $($c2.OwningProcess)" } else { Write-Output "FAIL not listening - check log" }
