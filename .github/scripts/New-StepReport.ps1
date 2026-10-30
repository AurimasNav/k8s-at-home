#Requires -Version 7.4
<#
.SYNOPSIS
    Markdown report for a job whose steps tee their output to <LogDir>/<step id>.log.
.DESCRIPTION
    One table row per check with the step outcome (from STEPS, the job's toJSON(steps)). Below it:
    for failed checks, the end of the log; for passed checks, any log lines that mention a file
    changed in the PR (warnings a reviewer should see). Sets the step output "report".
.EXAMPLE
    .github/scripts/New-StepReport.ps1 -Title 'Lint' -Check ([ordered]@{ yamllint = 'yamllint' })
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string] $Title,
    # Step id => display name, in table order.
    [Parameter(Mandatory)] [System.Collections.Specialized.OrderedDictionary] $Check,
    [string] $StepsJson = $env:STEPS,
    [string] $LogDir = 'ci-logs',
    [string] $BaseSha = $env:BASE_SHA,
    [int] $TailLines = 60,
    [int] $MaxChangedFileLines = 40
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'CiTools.psm1') -Force

$steps = if ($StepsJson) { $StepsJson | ConvertFrom-Json -AsHashtable } else { @{} }
$changed = @(if ($BaseSha) { Get-ChangedFile -BaseSha $BaseSha })
$ansi = '\x1b\[[0-9;]*[A-Za-z]'

$rows = [Collections.Generic.List[string]]::new()
$details = [Collections.Generic.List[string]]::new()
foreach ($id in $Check.Keys) {
    $outcome = if ($steps.ContainsKey($id)) { [string]$steps[$id].outcome } else { 'not run' }
    $icon = switch ($outcome) { 'success' { '✅' } 'failure' { '❌' } 'skipped' { '⏭️' } default { '❔' } }
    $log = Join-Path $LogDir "$id.log"
    $lines = @(if (Test-Path -LiteralPath $log) { Get-Content -LiteralPath $log | ForEach-Object { $_ -replace $ansi, '' } })

    $note = ''
    if ($outcome -eq 'failure' -and $lines.Count) {
        $tail = $lines | Select-Object -Last $TailLines
        $details.Add("<details open><summary>$($Check[$id] | ConvertTo-MarkdownCell): output</summary>`n`n$(Format-CodeBlock -Text ($tail -join "`n"))`n</details>")
    } elseif ($changed.Count -and $lines.Count) {
        $hits = @($lines | Where-Object { $l = $_; @($changed | Where-Object { $l.Contains($_) }).Count -gt 0 })
        if ($hits.Count) {
            $note = "$($hits.Count) in changed files"
            $shown = $hits | Select-Object -First $MaxChangedFileLines
            $more = if ($hits.Count -gt $MaxChangedFileLines) { "`n… $($hits.Count - $MaxChangedFileLines) more" } else { '' }
            $details.Add("<details><summary>$($Check[$id] | ConvertTo-MarkdownCell): $note</summary>`n`n$(Format-CodeBlock -Text ($shown -join "`n"))$more`n</details>")
        }
    }
    $rows.Add("| $($Check[$id] | ConvertTo-MarkdownCell) | $icon $outcome | $note |")
}

$md = @("#### $Title", '', '| Check | Result | Notes |', '| --- | --- | --- |') + $rows
if ($details.Count) { $md += ''; $md += $details }
Write-StepOutput -Name report -Value ($md -join "`n")
