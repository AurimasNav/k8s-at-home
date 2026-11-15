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

# Runs $Action, retrying transient failures (up to $Attempts tries, 5 s apart, then 10 s, ...).
# The PowerShell Gallery install now and then fails with "Object reference not set to an instance
# of an object" inside PSResourceGet; a retry gets past it. $What names the step in errors.
function Invoke-WithRetry([string] $What, [scriptblock] $Action, [int] $Attempts = 3) {
    for ($attempt = 1; ; $attempt++) {
        try {
            return & $Action
        } catch {
            if ($attempt -ge $Attempts) {
                throw "$What failed after $attempt attempts: $($_.Exception.Message)"
            }
            Write-Warning "$What failed (attempt $attempt of $Attempts): $($_.Exception.Message)"
            Start-Sleep -Seconds (5 * $attempt)
        }
    }
}

if (-not (Get-Module -ListAvailable PSScriptAnalyzer | Where-Object { -not $Version -or $_.Version -eq $Version })) {
    if (-not $Version) { throw 'PSSCRIPTANALYZER_VERSION is not set' }
    Invoke-WithRetry "Installing PSScriptAnalyzer $Version" {
        if (Get-Command Install-PSResource -ErrorAction SilentlyContinue) {
            Install-PSResource PSScriptAnalyzer -Version $Version -Scope CurrentUser -TrustRepository -Quiet
        } else {
            Install-Module PSScriptAnalyzer -RequiredVersion $Version -Scope CurrentUser -Force
        }
    }
}
Import-Module PSScriptAnalyzer

$files = @(& git ls-files '*.ps1' '*.psm1' '*.psd1')
# A crash names the file; findings themselves are output, not errors, so they are never retried.
# (A plain variable: PSScriptAnalyzer doesn't see a parameter used only inside a script block.)
$settingsFile = $Settings
$issues = @($files | ForEach-Object {
        $file = $_
        Invoke-WithRetry "PSScriptAnalyzer on $file" { Invoke-ScriptAnalyzer -Path $file -Settings $settingsFile } -Attempts 2
    })
foreach ($i in $issues) {
    $path = [IO.Path]::GetRelativePath((Get-Location).Path, $i.ScriptPath)
    "${path}:$($i.Line): $($i.Severity) $($i.RuleName): $($i.Message)"
}
"PSScriptAnalyzer: $($files.Count) files, $($issues.Count) findings"
if ($issues.Count) { exit 1 }
