#Requires -Version 7.4
<#
.SYNOPSIS
    Markdown diff of the rendered manifests between the PR base and the PR head.
.DESCRIPTION
    Compares <Base>/<app>.yaml with <Head>/<app>.yaml (output of Render-Apps.ps1): which ArgoCD
    apps the PR changes, adds or removes, and a unified diff per app, size-capped for the PR
    description. Sets the step output "report" and writes the full diff to <DiffFile>.

    The report is public. Before diffing, the values under data/stringData of every Secret and
    every caBundle are replaced by "<masked>": they are either secrets or certificates that Helm
    generates anew on each render (cilium, victoria-metrics), which would otherwise show up as a
    change in every PR. Added or removed keys still show.
#>
[CmdletBinding()]
param(
    [string] $Base = 'rendered-base',
    [string] $Head = 'rendered',
    [string] $DiffFile = 'rendered.diff',
    [int] $MaxCharsPerApp = 6000,
    [int] $MaxChars = 24000
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
Import-Module (Join-Path $PSScriptRoot 'CiTools.psm1') -Force

function Protect-Document([string] $Document) {
    # Secret: mask every value under top-level data/stringData (block scalars included).
    # Any kind: mask caBundle values.
    $isSecret = $Document -match '(?m)^kind: Secret\s*$'
    $out = [Collections.Generic.List[string]]::new()
    $inData = $false
    $skipDeeperThan = -1
    foreach ($line in $Document -split '\n') {
        $indent = $line.Length - $line.TrimStart().Length
        if ($skipDeeperThan -ge 0) {
            if ($line.Trim() -eq '' -or $indent -gt $skipDeeperThan) { continue }
            $skipDeeperThan = -1
        }
        if ($indent -eq 0 -and $line.Trim()) { $inData = $isSecret -and $line -match '^(data|stringData):\s*$' }
        if ($inData -and $indent -eq 2 -and $line -match '^(  [^\s#][^:]*:)(\s*)(.*)$') {
            $out.Add("$($Matches[1]) <masked>")
            if ($Matches[3] -match '^[|>]' -or $Matches[3] -eq '') { $skipDeeperThan = 2 }
            continue
        }
        if ($line -match '^(\s*caBundle:)\s*\S') {
            $out.Add("$($Matches[1]) <masked>")
            continue
        }
        $out.Add($line)
    }
    $out -join "`n"
}

function Copy-Masked([string] $From, [string] $To) {
    $null = New-Item -ItemType Directory -Force -Path $To
    if (-not (Test-Path -LiteralPath $From)) { return }
    foreach ($f in Get-ChildItem -LiteralPath $From -Filter '*.yaml' -File) {
        $docs = (Get-Content -Raw -LiteralPath $f.FullName) -replace "`r`n", "`n" -split '(?m)^---$'
        $masked = $docs | ForEach-Object { Protect-Document $_ }
        Set-Content -LiteralPath (Join-Path $To $f.Name) -Value ($masked -join '---') -NoNewline
    }
}

$work = Join-Path ([IO.Path]::GetTempPath()) "manifest-diff-$([guid]::NewGuid().ToString('N'))"
Copy-Masked $Base (Join-Path $work 'base')
Copy-Masked $Head (Join-Path $work 'head')
$baseNamesSource = $Base
$Base = Join-Path $work 'base'
$Head = Join-Path $work 'head'

function Get-Name([string] $Dir) {
    if (-not (Test-Path -LiteralPath $Dir)) { return @() }
    @(Get-ChildItem -LiteralPath $Dir -Filter '*.yaml' -File | ForEach-Object BaseName)
}

$baseNames = Get-Name $Base
$headNames = Get-Name $Head
$all = @($baseNames + $headNames | Sort-Object -Unique)

$changes = [Collections.Generic.List[object]]::new()
$fullDiff = [Text.StringBuilder]::new()
foreach ($name in $all) {
    $a = Join-Path $Base "$name.yaml"
    $b = Join-Path $Head "$name.yaml"
    if (-not (Test-Path -LiteralPath $a)) { $a = '/dev/null' }
    if (-not (Test-Path -LiteralPath $b)) { $b = '/dev/null' }
    $diff = (& git diff --no-index --no-color --no-ext-diff -U3 "--src-prefix=base/" "--dst-prefix=pr/" -- $a $b) -join "`n"
    if ($LASTEXITCODE -eq 0) { continue }
    if ($LASTEXITCODE -ne 1) { throw "git diff failed for $name" }
    # Drop the git header; keep hunks only.
    $hunks = ($diff -split "`n" | Where-Object { $_ -notmatch '^(diff --git|index |--- |\+\+\+ |new file mode|deleted file mode)' }) -join "`n"
    $added = ([regex]::Matches($hunks, '(?m)^\+')).Count
    $removed = ([regex]::Matches($hunks, '(?m)^-')).Count
    $kind = if ($a -eq '/dev/null') { 'added' } elseif ($b -eq '/dev/null') { 'removed' } else { 'changed' }
    $label = if ($name -eq 'argocd-apps') { 'argocd-apps (root app: Applications)' } else { $name }
    $changes.Add([pscustomobject]@{ Name = $name; Label = $label; Kind = $kind; Added = $added; Removed = $removed; Hunks = $hunks })
    $null = $fullDiff.AppendLine($diff)
}
Set-Content -LiteralPath $DiffFile -Value $fullDiff.ToString()
Remove-Item -Recurse -Force -LiteralPath $work -ErrorAction SilentlyContinue
$baseMissing = -not (Test-Path -LiteralPath $baseNamesSource)

$md = [Collections.Generic.List[string]]::new()
$md.Add('#### Rendered manifests (what ArgoCD will apply)')
$md.Add('')
if ($baseMissing) {
    $md.Add('The PR base could not be rendered, so every app shows as added.')
    $md.Add('')
}
if (-not $changes.Count) {
    $md.Add("No changes in the rendered manifests of the $($headNames.Count) deployed apps.")
} else {
    $summary = foreach ($k in 'changed', 'added', 'removed') {
        $names = @($changes | Where-Object Kind -EQ $k | ForEach-Object { "``$($_.Name)``" })
        if ($names.Count) { "$k`: $($names -join ', ')" }
    }
    $md.Add("$($changes.Count) app(s) $($summary -join '; ').")
    $budget = $MaxChars
    foreach ($c in $changes) {
        $text = $c.Hunks
        $cut = ''
        $limit = [Math]::Min($MaxCharsPerApp, $budget)
        if ($text.Length -gt $limit) {
            $text = $text.Substring(0, [Math]::Max(0, $text.Substring(0, $limit).LastIndexOf("`n")))
            $cut = "`nDiff truncated; the full diff is in the workflow run artifact ``rendered-manifests``."
        }
        $budget -= $text.Length
        $body = if ($text) { "`n`n$(Format-CodeBlock -Text $text -Language diff)$cut" } else { "`n`nNot shown: report size limit reached; see the ``rendered-manifests`` artifact." }
        $md.Add('')
        $md.Add("<details><summary><code>$($c.Label)</code> $($c.Kind) (+$($c.Added) −$($c.Removed))</summary>$body`n</details>")
    }
}
Write-StepOutput -Name report -Value ($md -join "`n")
