$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $projectRoot

$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$supervisor = Join-Path $projectRoot 'scripts\console_supervisor.py'

function Test-BackendListening {
    return $null -ne (Get-NetTCPConnection -LocalPort 8876 -State Listen -ErrorAction SilentlyContinue)
}

# 8876 已有后端监听时不重复启动，直接打开控制台
if (-not (Test-BackendListening)) {
    if (-not (Test-Path -LiteralPath $python)) {
        python -m venv .venv
        & $python -m pip install -r requirements.txt
    }

    & $python -m pdd_rpa.cli init

    # 后台直接拉起守护进程（不依赖计划任务，开机不自启）
    Start-Process -FilePath $python -ArgumentList "`"$supervisor`"" -WorkingDirectory $projectRoot -WindowStyle Hidden

    $deadline = (Get-Date).AddSeconds(40)
    while (-not (Test-BackendListening)) {
        if ((Get-Date) -gt $deadline) {
            throw '后端启动超时：8876 端口未就绪，请查看 runtime\backend.log'
        }
        Start-Sleep -Milliseconds 500
    }
}

Start-Process 'http://127.0.0.1:8876/'
Write-Host 'PDD RPA 控制台已就绪：http://127.0.0.1:8876/'
