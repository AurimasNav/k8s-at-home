#Requires -Version 7.4
<#
.SYNOPSIS
    Write a workflow's CI report into the pull request description, replacing its previous one.
.DESCRIPTION
    Each workflow owns one section between "<!-- ci-report:<name>:start/end -->" markers, so a
    re-run (new commit or "Re-run jobs") replaces only its own section and leaves the author's
    text and other workflows' sections alone.

    Sections go after a "<!-- Reviewable:start -->" marker. Renovate ignores everything after
    that marker when it checks whether its PR body changed (renovatebot/renovate,
    lib/modules/platform/pr-body.ts), so it does not rewrite the body and drop the report.

    The report lists each job's result (from NEEDS, the workflow's toJSON(needs)) followed by
    the Markdown each job put in its outputs named report*. GitHub drops a job output that
    contains a secret value, so the fragments must mask secrets (the report scripts do).

    It never fails the job on API errors, because the Renovate auto-merge treats any failed
    check as a blocker; it prints a warning instead. It skips the update when the PR head has
    moved on, so a slow run cannot overwrite the report of a newer commit.

    Needs GH_TOKEN with pull-requests: write. GITHUB_API_URL can point at a test server.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string] $Repository,
    [Parameter(Mandatory)] [int] $PullRequest,
    # Head commit the report describes (github.event.pull_request.head.sha).
    [Parameter(Mandatory)] [string] $HeadSha,
    [Parameter(Mandatory)] [ValidatePattern('^[a-z0-9-]+$')] [string] $Section,
    [Parameter(Mandatory)] [string] $Title,
    # Job id => display name, in table order. Jobs not listed are not shown.
    [Parameter(Mandatory)] [System.Collections.Specialized.OrderedDictionary] $Jobs,
    [string] $NeedsJson = $env:NEEDS,
    [string[]] $Note = @(),
    [string] $RunUrl = "$env:GITHUB_SERVER_URL/$env:GITHUB_REPOSITORY/actions/runs/$env:GITHUB_RUN_ID/attempts/$env:GITHUB_RUN_ATTEMPT",
    [int] $MaxAttempts = 4
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'CiTools.psm1') -Force

$anchor = '<!-- Reviewable:start -->'
$start = "<!-- ci-report:$($Section):start -->"
$end = "<!-- ci-report:$($Section):end -->"
$bodyLimit = 65536

function Get-ResultIcon([string] $Result) {
    switch ($Result) {
        'success' { '✅' }
        'failure' { '❌' }
        'cancelled' { '⏹️' }
        'skipped' { '⏭️' }
        default { '❔' }
    }
}

function Get-Report([System.Collections.Specialized.OrderedDictionary] $JobNames, [string] $NeedsText, [string[]] $Notes) {
    $needs = if ($NeedsText) { $NeedsText | ConvertFrom-Json -AsHashtable } else { @{} }
    $vilnius = [TimeZoneInfo]::FindSystemTimeZoneById('Europe/Vilnius')
    $now = [TimeZoneInfo]::ConvertTime([DateTimeOffset]::UtcNow, $vilnius).ToString('yyyy-MM-dd HH:mm')
    $short = $HeadSha.Substring(0, [Math]::Min(7, $HeadSha.Length))
    $commitUrl = "$env:GITHUB_SERVER_URL/$Repository/commit/$HeadSha"

    $out = [Collections.Generic.List[string]]::new()
    $out.Add("### $Title")
    $out.Add("Commit [``$short``]($commitUrl) · [workflow run]($RunUrl) · updated $now (Vilnius)")
    $out.Add('')
    $out.Add('| Check | Result |')
    $out.Add('| --- | --- |')
    foreach ($id in $JobNames.Keys) {
        $result = if ($needs.ContainsKey($id)) { [string]$needs[$id].result } else { 'not run' }
        $out.Add("| $($JobNames[$id] | ConvertTo-MarkdownCell) | $(Get-ResultIcon $result) $result |")
    }
    foreach ($n in $Notes) { $out.Add(''); $out.Add("> $n") }
    foreach ($id in $JobNames.Keys) {
        if (-not $needs.ContainsKey($id)) { continue }
        $outputs = $needs[$id].outputs
        if ($outputs -isnot [hashtable]) { continue }
        # Every output named report* is a Markdown fragment, shown in name order.
        foreach ($name in ($outputs.Keys | Where-Object { $_ -like 'report*' } | Sort-Object)) {
            $fragment = ([string]$outputs[$name]).Trim()
            if ($fragment) {
                $out.Add('')
                $out.Add($fragment)
            }
        }
    }
    $out -join "`n"
}

function Merge-Section([string] $Body, [string] $Block) {
    $Body = [string]$Body
    $i = $Body.IndexOf($start)
    $j = if ($i -ge 0) { $Body.IndexOf($end, $i) } else { -1 }
    if ($i -ge 0 -and $j -gt $i) {
        return $Body.Substring(0, $i) + $Block + $Body.Substring($j + $end.Length)
    }
    if (-not $Body.Contains($anchor)) {
        $Body = $Body.TrimEnd() + "`n`n$anchor"
    }
    $Body.TrimEnd() + "`n`n$Block`n"
}

function Invoke-GitHub([string] $Method, [string] $Uri, $Body) {
    $params = @{
        Method  = $Method
        Uri     = $Uri
        Headers = @{
            Authorization          = "Bearer $env:GH_TOKEN"
            Accept                 = 'application/vnd.github+json'
            'X-GitHub-Api-Version' = '2022-11-28'
        }
    }
    if ($null -ne $Body) {
        $params.Body = $Body | ConvertTo-Json -Compress
        $params.ContentType = 'application/json; charset=utf-8'
    }
    Invoke-RestMethod @params -MaximumRetryCount 2 -RetryIntervalSec 3
}

try {
    if (-not $env:GH_TOKEN) { throw 'GH_TOKEN is not set' }
    $api = if ($env:GITHUB_API_URL) { $env:GITHUB_API_URL.TrimEnd('/') } else { 'https://api.github.com' }
    $uri = "$api/repos/$Repository/pulls/$PullRequest"

    $content = Get-Report -JobNames $Jobs -NeedsText $NeedsJson -Notes $Note
    $block = "$start`n$content`n$end"

    for ($attempt = 1; $attempt -le $MaxAttempts; $attempt++) {
        $pr = Invoke-GitHub GET $uri $null
        if ($pr.head.sha -ne $HeadSha) {
            Write-Host "PR head is now $($pr.head.sha), not $($HeadSha): leaving the report to the newer run."
            exit 0
        }
        $body = [string]$pr.body
        $new = Merge-Section $body $block
        if ($new.Length -gt $bodyLimit) {
            $short = "$start`n### $Title`nThe report is too long for the PR description; see the [workflow run]($RunUrl).`n$end"
            $new = Merge-Section $body $short
            $block = $short
        }
        if ($new -eq $body) {
            Write-Host "Section '$Section' is already up to date."
            exit 0
        }
        $null = Invoke-GitHub PATCH $uri @{ body = $new }

        # Two workflows can edit the body at the same time; the later write wins. Read back,
        # and write again if our section was lost.
        Start-Sleep -Seconds (2 + (Get-Random -Maximum 4))
        $check = ([string](Invoke-GitHub GET $uri $null).body) -replace "`r`n", "`n"
        if ($check.Contains(($block -replace "`r`n", "`n"))) {
            Write-Host "Updated section '$Section' of PR #$PullRequest ($($content.Length) characters)."
            exit 0
        }
        Write-Host "Section '$Section' was overwritten by a concurrent update (attempt $attempt), retrying."
    }
    Write-Host "::warning::Could not update the PR description after $MaxAttempts attempts."
} catch {
    Write-Host "::warning::PR report not updated: $($_.Exception.Message)"
}
exit 0
