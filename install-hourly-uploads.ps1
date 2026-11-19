$ErrorActionPreference = 'Stop'
$projectPath = $PSScriptRoot
$pythonPath = Join-Path $projectPath '.venv\Scripts\python.exe'
$settingsPath = Join-Path $projectPath 'browser-upload.json'
if (!(Test-Path -LiteralPath $pythonPath) -or !(Test-Path -LiteralPath $settingsPath)) { throw 'Install uploader and configure accounts first.' }
$taskName = 'Twitch Clips Hourly Browser Uploads'
$arguments = '-m gta_clips.browser_schedule --settings "' + $settingsPath + '"'
$action = New-ScheduledTaskAction -Execute $pythonPath -Argument $arguments -WorkingDirectory $projectPath
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(5) -RepetitionInterval (New-TimeSpan -Minutes 5)
$principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
$taskSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 8) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $taskSettings -Description 'Check every five minutes; publish at most one clip per hour per enabled tested destination. No AI calls.' -Force | Select-Object TaskName,State
