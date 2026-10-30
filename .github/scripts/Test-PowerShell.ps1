#Requires -Version 7.4
<#
.SYNOPSIS
    Lint every PowerShell file in the repo with PSScriptAnalyzer (PSScriptAnalyzerSettings.psd1).
.DESCRIPTION
    Installs the pinned module version (PSSCRIPTANALYZER_VERSION) when it is missing. Prints one
    line per finding as "path:line: Severity RuleName: message" and exits 1 when there are any.
#>
[CmdletBinding()]
param(
    [string] $Version = $env:PSSCRIPTANALYZER_VERSION,
    [string] $Settings = 'PSScriptAnalyzerSettings.psd1'
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if (-not (Get-Module -ListAvailable PSScriptAnalyzer | Where-Object { -not $Version -or $_.Version -eq $Version })) {
    if (-not $Version) { throw 'PSSCRIPTANALYZER_VERSION is not set' }
    if (Get-Command Install-PSResource -ErrorAction SilentlyContinue) {
        Install-PSResource PSScriptAnalyzer -Version $Version -Scope CurrentUser -TrustRepository -Quiet
    } else {
        Install-Module PSScriptAnalyzer -RequiredVersion $Version -Scope CurrentUser -Force
    }
}
Import-Module PSScriptAnalyzer

$files = @(& git ls-files '*.ps1' '*.psm1' '*.psd1')
$issues = @($files | ForEach-Object { Invoke-ScriptAnalyzer -Path $_ -Settings $Settings })
foreach ($i in $issues) {
    $path = [IO.Path]::GetRelativePath((Get-Location).Path, $i.ScriptPath)
    "${path}:$($i.Line): $($i.Severity) $($i.RuleName): $($i.Message)"
}
"PSScriptAnalyzer: $($files.Count) files, $($issues.Count) findings"
if ($issues.Count) { exit 1 }
