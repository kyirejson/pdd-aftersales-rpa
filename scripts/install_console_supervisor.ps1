$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$supervisor = Join-Path $projectRoot 'scripts\console_supervisor.py'
$taskName = 'PDD RPA Console Supervisor'

if (-not (Test-Path -LiteralPath $python)) {
    throw "未找到虚拟环境 Python：$python"
}

$action = New-ScheduledTaskAction -Execute $python -Argument "`"$supervisor`"" -WorkingDirectory $projectRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -User $env:USERNAME `
    -RunLevel Limited `
    -Force | Out-Null

Start-ScheduledTask -TaskName $taskName
Write-Host "已安装并启动：$taskName"
