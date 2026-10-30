#Requires -Version 7.4
<#
.SYNOPSIS
    Install OpenTofu and tflint into ~/bin, verifying release checksums.
.DESCRIPTION
    Versions come from TOFU_VERSION / TFLINT_VERSION (pinned in .github/workflows/iac.yaml).
    Linux x64 only (GitHub-hosted runners). Adds ~/bin to GITHUB_PATH when running in Actions.
#>
[CmdletBinding()]
param(
    [string] $TofuVersion = $env:TOFU_VERSION,
    [string] $TflintVersion = $env:TFLINT_VERSION
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true
if (-not $TofuVersion -or -not $TflintVersion) { throw 'TOFU_VERSION and TFLINT_VERSION are required' }

$bin = Join-Path $HOME 'bin'
$null = New-Item -ItemType Directory -Force -Path $bin
$work = Join-Path ([IO.Path]::GetTempPath()) "iac-tools-$([guid]::NewGuid().ToString('N'))"
$null = New-Item -ItemType Directory -Path $work

function Get-VerifiedAsset {
    param([string] $BaseUrl, [string] $File, [string] $SumsFile)
    $path = Join-Path $work $File
    Invoke-WebRequest -Uri "$BaseUrl/$File" -OutFile $path -MaximumRetryCount 3 -RetryIntervalSec 2
    $sums = (Invoke-WebRequest -Uri "$BaseUrl/$SumsFile" -MaximumRetryCount 3 -RetryIntervalSec 2).Content
    if ($sums -is [byte[]]) { $sums = [Text.Encoding]::UTF8.GetString($sums) }
    $line = $sums -split "`n" | Where-Object { $_ -match "^([0-9a-f]{64})\s+\*?$([regex]::Escape($File))\s*$" } |
        Select-Object -First 1
    if (-not $line) { throw "no checksum for $File in $SumsFile" }
    $expected = ($line -split '\s+')[0]
    $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $path).Hash.ToLowerInvariant()
    if ($actual -ne $expected) { throw "checksum mismatch for $File" }
    Write-Host "$File`: OK"
    $path
}

try {
    $file = "tofu_${TofuVersion}_linux_amd64.tar.gz"
    $archive = Get-VerifiedAsset "https://github.com/opentofu/opentofu/releases/download/v$TofuVersion" $file "tofu_${TofuVersion}_SHA256SUMS"
    & tar -xzf $archive -C $bin tofu

    $archive = Get-VerifiedAsset "https://github.com/terraform-linters/tflint/releases/download/v$TflintVersion" 'tflint_linux_amd64.zip' 'checksums.txt'
    # .NET zip API: no dependency on the Microsoft.PowerShell.Archive module.
    $zip = [IO.Compression.ZipFile]::OpenRead($archive)
    try {
        [IO.Compression.ZipFileExtensions]::ExtractToFile($zip.GetEntry('tflint'), (Join-Path $bin 'tflint'), $true)
    } finally {
        $zip.Dispose()
    }
    & chmod +x (Join-Path $bin 'tflint') (Join-Path $bin 'tofu')

    if ($env:GITHUB_PATH) { Add-Content -LiteralPath $env:GITHUB_PATH -Value $bin }
    (& (Join-Path $bin 'tofu') version)[0]
    (& (Join-Path $bin 'tflint') --version)[0]
} finally {
    Remove-Item -Recurse -Force -LiteralPath $work -ErrorAction SilentlyContinue
}
