$ErrorActionPreference = 'SilentlyContinue'
$taskName = 'PDD RPA Console Supervisor'

Write-Host "正在停止计划任务 $taskName..." -ForegroundColor Cyan
Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue

# 结束 8876 端口占用的进程
$connections = Get-NetTCPConnection -LocalPort 8876 -ErrorAction SilentlyContinue
foreach ($conn in $connections) {
    if ($conn.OwningProcess -gt 0) {
        $pidNum = $conn.OwningProcess
        Write-Host "终止占用 8876 端口的进程 (PID: $pidNum)..."
        Stop-Process -Id $pidNum -Force -ErrorAction SilentlyContinue
    }
}

# 结束属于本项目的 python.exe 守护进程
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Get-CimInstance Win32_Process | Where-Object {
    $_.Name -eq 'python.exe' -and ($_.CommandLine -like "*console_supervisor*" -or $_.CommandLine -like "*pdd_rpa.cli serve*")
} | ForEach-Object {
    $procId = $_.ProcessId
    Write-Host "终止相关 Python 进程 (PID: $procId)..."
    Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
}

# 清理锁文件
$lockFile = Join-Path $projectRoot 'runtime\console_supervisor.lock'
if (Test-Path -LiteralPath $lockFile) {
    Remove-Item -LiteralPath $lockFile -Force -ErrorAction SilentlyContinue
}

Write-Host "后端控制台与守护进程已全部停止。" -ForegroundColor Green
