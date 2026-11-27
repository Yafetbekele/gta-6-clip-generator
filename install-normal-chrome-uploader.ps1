param([switch]$Activate, [switch]$Rollback)
$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$settingsPath = Join-Path $projectRoot 'browser-upload.json'
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$pythonwPath = Join-Path $projectRoot '.venv\Scripts\pythonw.exe'
$taskName = 'Twitch Clips Normal Chrome Queue'
if ($Rollback) {
    Push-Location $projectRoot
    try {
        & $pythonPath -c "import json,pathlib; p=pathlib.Path('browser-upload.json'); c=json.loads(p.read_text(encoding='utf-8-sig')); c.pop('youtube_transport',None); p.write_text(json.dumps(c,indent=2)+'\n',encoding='utf-8')"
        if ($LASTEXITCODE -ne 0) { throw 'Could not restore legacy transport.' }
    } finally { Pop-Location }
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host 'Legacy transport restored. Stop the extension before using the legacy uploader.'
    exit
}
if ($Activate) {
    Push-Location $projectRoot
    try {
        & $pythonPath -c "from pathlib import Path; from gta_clips.chrome_bridge import activate; activate(Path('browser-upload.json'))"
        if ($LASTEXITCODE -ne 0) { throw 'Current upload round must finish before activation. Try again afterward.' }
    } finally { Pop-Location }
}
$action = New-ScheduledTaskAction -Execute $pythonwPath -Argument "-m gta_clips.chrome_bridge --settings `"$settingsPath`"" -WorkingDirectory $projectRoot
$trigger = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(1)) -RepetitionInterval (New-TimeSpan -Hours 1)
$logon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$taskSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger @($trigger,$logon) -Settings $taskSettings -Description 'Local queue for the normal Chrome extension. No console window; one upload at a time.' -Force | Out-Null
Start-ScheduledTask -TaskName $taskName
Write-Host 'Local queue started without a console window. Pair the extension before starting posting.'
