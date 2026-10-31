param(
    [switch]$SkipTests,
    [switch]$SkipSupervisor,
    [switch]$SkipBundleVerification
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$bundleRoot = Split-Path -Parent $projectRoot
Set-Location -LiteralPath $projectRoot

function Test-Python313([string]$candidate) {
    if (-not (Test-Path -LiteralPath $candidate)) {
        return $false
    }
    try {
        & $candidate -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 13) else 1)"
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}

function Find-Python313 {
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe'),
        (Join-Path $env:ProgramFiles 'Python313\python.exe')
    )
    foreach ($candidate in $candidates) {
        if (Test-Python313 $candidate) {
            return $candidate
        }
    }
    $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($pythonCommand -and (Test-Python313 $pythonCommand.Source)) {
        return $pythonCommand.Source
    }
    return $null
}

Write-Host '[1/8] 校验迁移包完整性...'
if (-not $SkipBundleVerification) {
    $verifier = Join-Path $bundleRoot '00_校验迁移包.ps1'
    if (-not (Test-Path -LiteralPath $verifier)) {
        throw "迁移包缺少完整性校验脚本：$verifier"
    }
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $verifier -BundleRoot $bundleRoot -Quiet
    if ($LASTEXITCODE -ne 0) {
        throw '迁移包完整性校验失败，拒绝继续安装'
    }
} else {
    Write-Warning '已显式跳过迁移包完整性校验。只有刚刚独立完成过 SHA-256 校验时才应使用此选项。'
}

Write-Host '[2/8] 检查 Python 3.13 x64...'
$basePython = Find-Python313
if (-not $basePython) {
    $installer = Join-Path $bundleRoot 'offline\python-3.13.14-amd64.exe'
    if (-not (Test-Path -LiteralPath $installer)) {
        throw "未找到 Python 3.13，离线安装包也不存在：$installer"
    }
    Write-Host '安装迁移包自带的 Python 3.13.14（仅当前用户）...'
    $process = Start-Process -FilePath $installer -Wait -PassThru -ArgumentList @(
        '/quiet',
        'InstallAllUsers=0',
        'PrependPath=1',
        'Include_launcher=1',
        'Include_test=0',
        'Shortcuts=0'
    )
    if ($process.ExitCode -ne 0) {
        throw "Python 离线安装失败，退出码：$($process.ExitCode)"
    }
    $basePython = Find-Python313
    if (-not $basePython) {
        throw 'Python 安装完成但仍未找到 Python 3.13 解释器'
    }
}

Write-Host '[3/8] 创建独立虚拟环境...'
$venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) {
    & $basePython -m venv (Join-Path $projectRoot '.venv')
    if ($LASTEXITCODE -ne 0) {
        throw '创建 .venv 失败'
    }
}

Write-Host '[4/8] 从离线 wheelhouse 安装锁定依赖...'
$wheelhouse = Join-Path $bundleRoot 'offline\wheelhouse'
if (-not (Test-Path -LiteralPath $wheelhouse)) {
    throw "迁移包缺少离线依赖目录：$wheelhouse"
}
& $venvPython -m pip install --no-index --find-links $wheelhouse -r requirements.lock.txt
if ($LASTEXITCODE -ne 0) {
    throw '离线依赖安装失败'
}

Write-Host '[5/8] 重绑新电脑路径并强制安全暂停...'
$manifestPath = Join-Path $bundleRoot 'MIGRATION_MANIFEST.json'
$sourceRoot = ''
if (Test-Path -LiteralPath $manifestPath) {
    $manifest = Get-Content -Raw -Encoding UTF8 -LiteralPath $manifestPath | ConvertFrom-Json
    $sourceRoot = [string]$manifest.source_project_root
}
& $venvPython scripts\prepare_migrated_machine.py --source-root $sourceRoot
if ($LASTEXITCODE -ne 0) {
    throw '迁移路径和数据库安全状态准备失败'
}

Write-Host '[6/8] 初始化并验证代码...'
& $venvPython -m pdd_rpa.cli init
if ($LASTEXITCODE -ne 0) {
    throw '项目初始化失败'
}
if (-not $SkipTests) {
    & $venvPython -m pytest -q
    if ($LASTEXITCODE -ne 0) {
        throw '项目测试未通过，未启动控制台'
    }
}

Write-Host '[7/8] 生成新电脑只读预检报告...'
& $venvPython scripts\migration_preflight.py
$preflightExit = $LASTEXITCODE
if ($preflightExit -eq 1) {
    throw '迁移基础环境检查失败，请查看 runtime\migration_preflight.json'
}
if ($preflightExit -eq 2) {
    Write-Warning '基础环境已就绪；仍需按迁移说明完成账号登录、Profile 映射和消息/WDT 预检。'
}

Write-Host '[8/8] 安装控制台守护进程...'
if (-not $SkipSupervisor) {
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\install_console_supervisor.ps1
    if ($LASTEXITCODE -ne 0) {
        throw '控制台守护进程安装失败'
    }
    $deadline = (Get-Date).AddSeconds(45)
    $ready = $false
    while ((Get-Date) -lt $deadline) {
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8876/' -TimeoutSec 2
            if ($response.StatusCode -eq 200) {
                $ready = $true
                break
            }
        } catch {
            Start-Sleep -Milliseconds 800
        }
    }
    if (-not $ready) {
        throw '守护进程已安装，但 8876 控制台未在 45 秒内就绪'
    }
    Start-Process 'http://127.0.0.1:8876/'
}

Write-Host ''
Write-Host '迁移项目安装完成。当前数据库已全局暂停，正式运行不会自动启动。' -ForegroundColor Green
Write-Host '请继续执行迁移说明中的登录、Profile 映射和真实只读预检。'
