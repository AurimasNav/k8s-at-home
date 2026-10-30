#Requires -Version 7.4
<#
.SYNOPSIS
    Run one CI check, show its output in the log and keep a copy in <LogDir>/<Id>.log.
.DESCRIPTION
    New-StepReport.ps1 reads the copy for the PR report. The step fails (exit code > 0) when
    the last native command in the block exits non-zero or the block throws.
.EXAMPLE
    .github/scripts/Invoke-Check.ps1 yamllint { pipx run yamllint -c .yamllint.yaml . }
#>
[CmdletBinding(PositionalBinding = $false)]
param(
    [Parameter(Mandatory, Position = 0)] [ValidatePattern('^[a-z0-9-]+$')] [string] $Id,
    [Parameter(Mandatory, Position = 1)] [scriptblock] $Check,
    [string] $LogDir = 'ci-logs'
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Continue'
$PSNativeCommandUseErrorActionPreference = $false

$null = New-Item -ItemType Directory -Force -Path $LogDir
# .NET resolves relative paths against the process directory, not the PowerShell location.
$log = Join-Path (Resolve-Path -LiteralPath $LogDir).Path "$Id.log"
$writer = [IO.StreamWriter]::new($log, $false, [Text.UTF8Encoding]::new($false))
$global:LASTEXITCODE = 0
$threw = $false
try {
    try {
        & $Check 2>&1 | ForEach-Object {
            $line = "$_"
            $writer.WriteLine($line)
            Write-Host $line
        }
    } catch {
        $threw = $true
        $line = "$($_.Exception.Message)"
        $writer.WriteLine($line)
        Write-Host "::error::$Id`: $line"
    }
} finally {
    $writer.Dispose()
}
$code = [int]$global:LASTEXITCODE
if ($threw -and $code -eq 0) { $code = 1 }
exit $code
