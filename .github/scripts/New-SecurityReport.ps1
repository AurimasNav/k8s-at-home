#Requires -Version 7.4
<#
.SYNOPSIS
    Markdown summary of the trivy and gitleaks results for the PR description.
.DESCRIPTION
    Reads the JSON reports written by the security job:
      - trivy-secret.json  trivy fs --scanners secret
      - trivy-config.json  trivy config (after .trivyignore.yaml)
      - gitleaks.json      gitleaks git (commits of the PR, or full history outside PRs)
    Lists every secret finding (rule, file, line; never the matched text), the blocking
    misconfigurations, all misconfigurations in files the PR changes, and repo-wide totals.
    Sets the step output "report".
#>
[CmdletBinding()]
param(
    [string] $TrivySecret = 'trivy-secret.json',
    [string] $TrivyConfig = 'trivy-config.json',
    [string] $Gitleaks = 'gitleaks.json',
    [string] $GitleaksScope = 'repository history',
    [string] $BaseSha = $env:BASE_SHA,
    [string[]] $BlockingSeverity = @('HIGH', 'CRITICAL'),
    [int] $MaxRows = 30
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'CiTools.psm1') -Force

$changed = @(if ($BaseSha) { Get-ChangedFile -BaseSha $BaseSha })
$order = @{ CRITICAL = 0; HIGH = 1; MEDIUM = 2; LOW = 3; UNKNOWN = 4 }

function Read-Json([string] $Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    $raw = Get-Content -Raw -LiteralPath $Path
    if (-not $raw.Trim()) { return $null }
    $raw | ConvertFrom-Json -AsHashtable -Depth 64
}

function Get-TrivyItem($Report, [string] $Kind) {
    if (-not $Report -or -not $Report.ContainsKey('Results') -or -not $Report.Results) { return @() }
    foreach ($r in $Report.Results) {
        if (-not $r.ContainsKey($Kind) -or -not $r[$Kind]) { continue }
        foreach ($f in $r[$Kind]) {
            if ($f.ContainsKey('Status') -and $f.Status -ne 'FAIL') { continue }
            $line = if ($f.ContainsKey('CauseMetadata') -and $f.CauseMetadata -and $f.CauseMetadata.ContainsKey('StartLine')) { $f.CauseMetadata.StartLine } elseif ($f.ContainsKey('StartLine')) { $f.StartLine } else { '' }
            [pscustomobject]@{
                Id       = if ($f.ContainsKey('ID')) { $f.ID } else { $f.RuleID }
                Severity = [string]$f.Severity
                File     = [string]$r.Target
                Line     = $line
                Title    = [string]$f.Title
                Url      = if ($f.ContainsKey('PrimaryURL')) { [string]$f.PrimaryURL } else { '' }
            }
        }
    }
}

function Format-FindingTable([object[]] $Items, [int] $Limit, [switch] $WithSeverity) {
    $rows = [Collections.Generic.List[string]]::new()
    $head = if ($WithSeverity) { '| Severity | ID | File | Title |' } else { '| Rule | File | Title |' }
    $rows.Add($head)
    $rows.Add(($head -replace '[^|]+', ' --- '))
    foreach ($i in ($Items | Select-Object -First $Limit)) {
        $id = if ($i.Url) { "[$($i.Id)]($($i.Url))" } else { "$($i.Id)" }
        $file = "$($i.File)$(if ($i.Line) { ":$($i.Line)" })" | ConvertTo-MarkdownCell -MaxLength 100 -Code
        if ($WithSeverity) {
            $rows.Add("| $($i.Severity) | $id | $file | $($i.Title | ConvertTo-MarkdownCell) |")
        } else {
            $rows.Add("| $id | $file | $($i.Title | ConvertTo-MarkdownCell) |")
        }
    }
    if ($Items.Count -gt $Limit) { $rows.Add(''); $rows.Add("… $($Items.Count - $Limit) more in the workflow run.") }
    $rows -join "`n"
}

$md = [Collections.Generic.List[string]]::new()
$md.Add('#### Security')
$md.Add('')

# Secrets: trivy (working tree) and gitleaks (commits). Never print the matched text.
$secrets = @(Get-TrivyItem (Read-Json $TrivySecret) 'Secrets')
$leaksJson = Read-Json $Gitleaks
$leaks = @(if ($leaksJson) {
        foreach ($l in $leaksJson) {
            [pscustomobject]@{ Id = $l.RuleID; File = $l.File; Line = $l.StartLine; Title = "commit $(([string]$l.Commit).Substring(0, [Math]::Min(7, ([string]$l.Commit).Length)))"; Severity = ''; Url = '' }
        }
    })
$trivySecretState = if (Test-Path -LiteralPath $TrivySecret) { if ($secrets.Count) { "❌ $($secrets.Count) found" } else { '✅ none' } } else { '❔ not run' }
$gitleaksState = if (Test-Path -LiteralPath $Gitleaks) { if ($leaks.Count) { "❌ $($leaks.Count) found" } else { '✅ none' } } else { '❔ not run' }

# Misconfigurations (after .trivyignore.yaml).
$configJson = Read-Json $TrivyConfig
$misconf = @(Get-TrivyItem $configJson 'Misconfigurations' | Sort-Object { $order[$_.Severity] }, File, Line)
$blocking = @($misconf | Where-Object { $BlockingSeverity -contains $_.Severity })
$inChanged = @($misconf | Where-Object { $changed -contains $_.File })
$bySeverity = ($misconf | Group-Object Severity | Sort-Object { $order[$_.Name] } | ForEach-Object { "$($_.Count) $($_.Name.ToLowerInvariant())" }) -join ', '
$configState = if (-not $configJson) { '❔ not run' } elseif ($blocking.Count) { "❌ $($blocking.Count) $($BlockingSeverity -join '/')" } else { "✅ no $($BlockingSeverity -join '/')" }

$md.Add('| Scan | Result | Notes |')
$md.Add('| --- | --- | --- |')
$md.Add("| Secrets in files (trivy) | $trivySecretState | working tree |")
$md.Add("| Secrets in commits (gitleaks) | $gitleaksState | $GitleaksScope |")
$changedNote = if ($BaseSha) { "$($inChanged.Count) in changed files; " } else { '' }
$md.Add("| Misconfigurations (trivy) | $configState | $changedNote$(if ($bySeverity) { "repo total: $bySeverity (baseline, non-blocking)" } else { 'repo total: 0' }) |")

if ($secrets.Count) { $md.Add(''); $md.Add('**Secrets in files (trivy)**'); $md.Add(''); $md.Add((Format-FindingTable $secrets -Limit $MaxRows)) }
if ($leaks.Count) {
    $md.Add(''); $md.Add('**Secrets in commits (gitleaks)**: rotate the secret, then remove it from history or allowlist a false positive in `.gitleaks.toml`.'); $md.Add('')
    $md.Add((Format-FindingTable $leaks -Limit $MaxRows))
}
if ($blocking.Count) { $md.Add(''); $md.Add("**Blocking misconfigurations** (fix, or add to ``.trivyignore.yaml`` with a reason)"); $md.Add(''); $md.Add((Format-FindingTable $blocking -Limit $MaxRows -WithSeverity)) }
$other = @($inChanged | Where-Object { $BlockingSeverity -notcontains $_.Severity })
if ($other.Count) {
    $md.Add('')
    $md.Add("<details><summary>Non-blocking misconfigurations in changed files ($($other.Count))</summary>`n`n$(Format-FindingTable $other -Limit $MaxRows -WithSeverity)`n</details>")
}
Write-StepOutput -Name report -Value ($md -join "`n")
exit 0
