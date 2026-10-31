<#
.SYNOPSIS
  Read text from a local image through the Windows built-in OCR engine.

.DESCRIPTION
  The desktop messenger clients used by this project do not consistently
  expose their chat header and message bubbles through UI Automation.  This
  helper uses the locally installed Windows OCR language pack instead of a
  network service.  It is intentionally read-only: it opens an image file and
  prints JSON to stdout.
#>

[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)]
  [ValidateNotNullOrEmpty()]
  [string]$ImagePath,

  [ValidateNotNullOrEmpty()]
  [string]$Language = "zh-Hans-CN"
)

$ErrorActionPreference = "Stop"
# Force UTF-8 because the Python bridge consumes the JSON directly.  Windows
# PowerShell otherwise uses the active console code page and replaces Chinese
# OCR glyphs before they ever reach the comparison step.
$utf8 = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8

Add-Type -AssemblyName System.Runtime.WindowsRuntime

# PowerShell cannot infer the generic WinRT AsTask overload.  Resolve it once
# and invoke it with the concrete result type for each asynchronous operation.
$asTaskGeneric = [System.WindowsRuntimeSystemExtensions].GetMethods() |
  Where-Object {
    $_.Name -eq "AsTask" -and
    $_.GetParameters().Count -eq 1 -and
    $_.ReturnType.Name -eq 'Task`1'
  } |
  Select-Object -First 1

if (-not $asTaskGeneric) {
  throw "Windows Runtime async bridge is unavailable"
}

function Await-WinRt {
  param(
    [Parameter(Mandatory = $true)]$Operation,
    [Parameter(Mandatory = $true)][Type]$ResultType
  )
  $method = $asTaskGeneric.MakeGenericMethod($ResultType)
  $task = $method.Invoke($null, @($Operation))
  $task.Wait()
  return $task.Result
}

$resolvedPath = (Resolve-Path -LiteralPath $ImagePath).Path
$storageFileType = [Windows.Storage.StorageFile,Windows.Storage,ContentType=WindowsRuntime]
$accessModeType = [Windows.Storage.FileAccessMode,Windows.Storage,ContentType=WindowsRuntime]
$randomAccessStreamType = [Windows.Storage.Streams.IRandomAccessStream,Windows.Storage.Streams,ContentType=WindowsRuntime]
$decoderType = [Windows.Graphics.Imaging.BitmapDecoder,Windows.Graphics.Imaging,ContentType=WindowsRuntime]
$softwareBitmapType = [Windows.Graphics.Imaging.SoftwareBitmap,Windows.Graphics.Imaging,ContentType=WindowsRuntime]
$languageType = [Windows.Globalization.Language,Windows.Globalization,ContentType=WindowsRuntime]
$ocrType = [Windows.Media.Ocr.OcrEngine,Windows.Media.Ocr,ContentType=WindowsRuntime]
$ocrResultType = [Windows.Media.Ocr.OcrResult,Windows.Media.Ocr,ContentType=WindowsRuntime]

$file = Await-WinRt ($storageFileType::GetFileFromPathAsync($resolvedPath)) $storageFileType
$stream = Await-WinRt ($file.OpenAsync($accessModeType::Read)) $randomAccessStreamType
$decoder = Await-WinRt ($decoderType::CreateAsync($stream)) $decoderType
$bitmap = Await-WinRt ($decoder.GetSoftwareBitmapAsync()) $softwareBitmapType
$engine = $ocrType::TryCreateFromLanguage($languageType::new($Language))

if ($null -eq $engine) {
  throw "Windows OCR language is unavailable: $Language"
}

$ocr = Await-WinRt ($engine.RecognizeAsync($bitmap)) $ocrResultType
$lines = @(
  $ocr.Lines | ForEach-Object {
    [PSCustomObject]@{
      text = [string]$_.Text
      words = @(
        $_.Words | ForEach-Object {
          [PSCustomObject]@{
            text = [string]$_.Text
            left = [double]$_.BoundingRect.X
            top = [double]$_.BoundingRect.Y
            width = [double]$_.BoundingRect.Width
            height = [double]$_.BoundingRect.Height
          }
        }
      )
    }
  }
)

[PSCustomObject]@{
  language = $Language
  text = [string]$ocr.Text
  lines = $lines
} | ConvertTo-Json -Compress -Depth 8
