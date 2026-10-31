param(
    [string]$BundleRoot = '',
    [switch]$Quiet
)

$ErrorActionPreference = 'Stop'

if (-not $BundleRoot) {
    $scriptDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
    if (Test-Path -LiteralPath (Join-Path $scriptDirectory 'SHA256SUMS.txt')) {
        $BundleRoot = $scriptDirectory
    } else {
        $BundleRoot = Split-Path -Parent $scriptDirectory
    }
}
$resolvedRoot = (Resolve-Path -LiteralPath $BundleRoot).Path.TrimEnd('\', '/')
$checksumPath = Join-Path $resolvedRoot 'SHA256SUMS.txt'
$detachedPath = Join-Path $resolvedRoot 'SHA256SUMS.sha256'

if (-not (Test-Path -LiteralPath $checksumPath -PathType Leaf)) {
    throw "缺少主校验清单：$checksumPath"
}
if (-not (Test-Path -LiteralPath $detachedPath -PathType Leaf)) {
    throw "缺少主校验清单的独立摘要：$detachedPath"
}

$detachedLine = (Get-Content -LiteralPath $detachedPath -Raw -Encoding UTF8).Trim()
if ($detachedLine -notmatch '^([0-9a-fA-F]{64})  SHA256SUMS\.txt$') {
    throw 'SHA256SUMS.sha256 格式无效'
}
$expectedListHash = $Matches[1].ToLowerInvariant()
$actualListHash = (Get-FileHash -LiteralPath $checksumPath -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actualListHash -ne $expectedListHash) {
    throw 'SHA256SUMS.txt 自身摘要不匹配，迁移包可能不完整或已被修改'
}

$entries = New-Object System.Collections.Generic.List[object]
$lineNumber = 0
foreach ($line in Get-Content -LiteralPath $checksumPath -Encoding UTF8) {
    $lineNumber++
    if (-not $line) {
        continue
    }
    if ($line -notmatch '^([0-9a-fA-F]{64})  (.+)$') {
        throw "SHA256SUMS.txt 第 $lineNumber 行格式无效"
    }
    $relative = $Matches[2].Replace('/', '\')
    if ([IO.Path]::IsPathRooted($relative)) {
        throw "校验清单包含绝对路径：$relative"
    }
    $target = [IO.Path]::GetFullPath((Join-Path $resolvedRoot $relative))
    $rootPrefix = $resolvedRoot + [IO.Path]::DirectorySeparatorChar
    if (-not $target.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "校验清单路径越界：$relative"
    }
    $entries.Add([pscustomobject]@{
        Expected = $Matches[1].ToLowerInvariant()
        Relative = $relative
        Target = $target
    })
}

$failures = New-Object System.Collections.Generic.List[string]
for ($index = 0; $index -lt $entries.Count; $index++) {
    $entry = $entries[$index]
    if (-not $Quiet -and ($index % 100 -eq 0 -or $index + 1 -eq $entries.Count)) {
        $percent = if ($entries.Count) { [math]::Floor(($index + 1) * 100 / $entries.Count) } else { 100 }
        Write-Progress -Activity '校验迁移包完整性' -Status "$($index + 1)/$($entries.Count) $($entry.Relative)" -PercentComplete $percent
    }
    if (-not (Test-Path -LiteralPath $entry.Target -PathType Leaf)) {
        $failures.Add("缺失：$($entry.Relative)")
        continue
    }
    try {
        $actual = (Get-FileHash -LiteralPath $entry.Target -Algorithm SHA256).Hash.ToLowerInvariant()
    } catch {
        $failures.Add("无法读取：$($entry.Relative)；$($_.Exception.Message)")
        continue
    }
    if ($actual -ne $entry.Expected) {
        $failures.Add("摘要不匹配：$($entry.Relative)")
    }
}
if (-not $Quiet) {
    Write-Progress -Activity '校验迁移包完整性' -Completed
}

if ($failures.Count) {
    Write-Host "迁移包校验失败，共 $($failures.Count) 项：" -ForegroundColor Red
    $failures | Select-Object -First 20 | ForEach-Object { Write-Host "- $_" -ForegroundColor Red }
    if ($failures.Count -gt 20) {
        Write-Host "- 其余 $($failures.Count - 20) 项已省略" -ForegroundColor Red
    }
    exit 2
}

Write-Host "迁移包校验通过：$($entries.Count) 个文件，SHA-256 全部匹配。" -ForegroundColor Green
exit 0
