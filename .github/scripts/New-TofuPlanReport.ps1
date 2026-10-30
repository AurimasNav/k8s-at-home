#Requires -Version 7.4
<#
.SYNOPSIS
    Markdown report of a saved OpenTofu plan for the PR description.
.DESCRIPTION
    The repo is public, so the report is public. Values tofu marks sensitive are already hidden
    as "(sensitive value)". On top of that the report masks:
      - the values of the environment variables named in -RedactEnv (tokens, account ID, ...),
      - API token IDs (the ID of an API token is also its R2 access key ID).
    Permission group IDs get their name appended (from the permission groups data source), so a
    token change reads "DNS Write" instead of a hex ID.

    Sets the step output "report" (size-capped for the PR description) and writes the whole
    report to the job summary. Never prints the plan to the log.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string] $StackDir,
    # Exit code of "tofu plan -detailed-exitcode": 0 no changes, 2 changes, other = error.
    [Parameter(Mandatory)] [int] $ExitCode,
    [string] $PlanFile = 'tfplan',
    # Combined stdout/stderr of tofu init + plan; used for the error report.
    [string] $PlanLog = 'plan.log',
    [string[]] $RedactEnv = @(),
    [int] $MaxChars = 25000
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
Import-Module (Join-Path $PSScriptRoot 'CiTools.psm1') -Force

$secrets = [Collections.Generic.List[string]]::new()
foreach ($name in $RedactEnv) {
    $v = [Environment]::GetEnvironmentVariable($name)
    if ($v -and $v.Length -ge 6) { $secrets.Add($v) }
}

function Protect-Text([string] $Text) {
    foreach ($s in $secrets) { $Text = $Text.Replace($s, '***') }
    $Text
}

$title = "#### tofu plan: ``$StackDir``"
if ($ExitCode -ne 0 -and $ExitCode -ne 2) {
    $log = if (Test-Path -LiteralPath $PlanLog) { Get-Content -Raw -LiteralPath $PlanLog } else { '' }
    $lines = @($log -split "`r?`n")
    $first = 0
    for ($i = 0; $i -lt $lines.Count; $i++) { if ($lines[$i] -match '^(│ )?Error|^╷') { $first = $i; break } }
    $excerpt = ($lines[$first..($lines.Count - 1)] | Select-Object -First 80) -join "`n"
    $md = @($title, '', "❌ **Plan failed** (exit code $ExitCode).", '', (Format-CodeBlock -Text (Protect-Text $excerpt)))
    if ($env:GITHUB_STEP_SUMMARY) { Add-Content -LiteralPath $env:GITHUB_STEP_SUMMARY -Value ($md -join "`n") }
    Write-StepOutput -Name report -Value ($md -join "`n")
    exit 0
}

$json = (& tofu "-chdir=$StackDir" show -json $PlanFile) -join "`n"
if ($LASTEXITCODE -ne 0) { throw 'tofu show -json failed' }
$plan = $json | ConvertFrom-Json -AsHashtable -Depth 100
$text = (& tofu "-chdir=$StackDir" show -no-color $PlanFile) -join "`n"
if ($LASTEXITCODE -ne 0) { throw 'tofu show failed' }

# Token IDs: mask them like secrets.
$changes = @(if ($plan.ContainsKey('resource_changes') -and $plan.resource_changes) { $plan.resource_changes })
foreach ($rc in $changes) {
    if ($rc.type -notin 'cloudflare_account_token', 'cloudflare_api_token') { continue }
    foreach ($side in 'before', 'after') {
        $v = $rc.change[$side]
        if ($v -is [hashtable] -and $v.ContainsKey('id') -and $v.id) { $secrets.Add([string]$v.id) }
    }
    if ($rc.change.ContainsKey('importing') -and $rc.change.importing -and $rc.change.importing.ContainsKey('id') -and $rc.change.importing.id) {
        $secrets.Add([string]$rc.change.importing.id)
    }
}

# Permission group ID => "Name (scope)" from the data source read during the plan.
$names = @{}
function Find-PermissionGroup($Module) {
    if (-not $Module) { return }
    foreach ($r in @(if ($Module.ContainsKey('resources')) { $Module.resources })) {
        if ($r.mode -eq 'data' -and $r.type -eq 'cloudflare_account_api_token_permission_groups_list' -and $r.values.result) {
            foreach ($g in $r.values.result) {
                $scope = (@($g.scopes) | ForEach-Object { ($_ -split '\.')[-1] }) -join ','
                $names[[string]$g.id] = "$($g.name) ($scope)"
            }
        }
    }
    foreach ($child in @(if ($Module.ContainsKey('child_modules')) { $Module.child_modules })) { Find-PermissionGroup $child }
}
if ($plan.ContainsKey('prior_state') -and $plan.prior_state -and $plan.prior_state.values) {
    Find-PermissionGroup $plan.prior_state.values.root_module
}

# Counts and the list of resources that change.
$icons = @{ create = '➕ create'; update = '🔄 update'; delete = '🗑️ destroy'; replace = '♻️ replace'; import = '📥 import'; read = '📖 read' }
$rows = [Collections.Generic.List[object]]::new()
foreach ($rc in $changes) {
    $actions = @($rc.change.actions)
    $kind = if ($actions -contains 'delete' -and $actions -contains 'create') { 'replace' }
    elseif ($actions -contains 'create') { 'create' }
    elseif ($actions -contains 'delete') { 'delete' }
    elseif ($actions -contains 'update') { 'update' }
    elseif ($actions -contains 'read') { 'read' }
    else { '' }
    $importing = $rc.change.ContainsKey('importing') -and $rc.change.importing
    if (-not $kind -and $importing) { $kind = 'import' }
    if (-not $kind) { continue }
    $label = $icons[$kind]
    if ($importing -and $kind -ne 'import') { $label = "📥 import + $label" }
    $rows.Add([pscustomobject]@{ Address = $rc.address; Kind = $kind; Label = $label; Import = [bool]$importing })
}
$count = { param($k) @($rows | Where-Object Kind -EQ $k).Count }
$summary = "**$(@($rows | Where-Object Import).Count) to import, $(& $count 'create') to add, $(& $count 'update') to change, $(& $count 'replace') to replace, $(& $count 'delete') to destroy.**"
$destructive = @($rows | Where-Object { $_.Kind -in 'delete', 'replace' })

$md = [Collections.Generic.List[string]]::new()
$md.Add($title)
$md.Add('')
if (-not $rows.Count) {
    $md.Add('✅ No changes. Cloudflare matches the code.')
} else {
    $md.Add($summary)
    if ($destructive.Count) { $md.Add(''); $md.Add("> ⚠️ **$($destructive.Count) resource(s) will be destroyed or replaced.** Check them before merging.") }
    $md.Add('')
    $md.Add('| Resource | Action |')
    $md.Add('| --- | --- |')
    foreach ($r in $rows | Sort-Object { $_.Kind -notin 'delete', 'replace' }, Address) {
        $md.Add("| $(Protect-Text $r.Address | ConvertTo-MarkdownCell -MaxLength 150 -Code) | $($r.Label) |")
    }

    # Plan body: from the action list to the "Plan:" line.
    $lines = @($text -split "`r?`n")
    $from = 0
    for ($i = 0; $i -lt $lines.Count; $i++) { if ($lines[$i] -match 'will perform the following actions') { $from = $i + 1; break } }
    $body = foreach ($line in $lines[$from..($lines.Count - 1)]) {
        if ($line -match '^\s*Plan: ') { break }
        $l = [regex]::Replace($line, '"([0-9a-f]{32})"', {
                param($m)
                $id = $m.Groups[1].Value
                if ($names.ContainsKey($id)) { "$($m.Value) # $($names[$id])" } else { $m.Value }
            })
        # Diff-style markers in column 1 so GitHub colours added/removed lines.
        if ($l -match '^(\s+)([-+~])(\s.*)$') {
            $marker = if ($Matches[2] -eq '~') { '!' } else { $Matches[2] }
            $l = "$marker$($Matches[1])$($Matches[3])"
        }
        $l
    }
    $body = ($body -join "`n").Trim("`n")
    # One line per permission group: "{ id = "<id>" }, # Name (scope)" instead of four.
    $body = [regex]::Replace($body, '(?m)^(\S?)(\s*)\{\n\S?\s*id\s*=\s*("[0-9a-f]{32}")( # [^\n]*)?\n\S?\s*\},?', {
            param($m)
            "$($m.Groups[1].Value)$($m.Groups[2].Value){ id = $($m.Groups[3].Value) },$($m.Groups[4].Value)"
        })
    $body = Protect-Text $body
    $md.Add('')
    $head = $md -join "`n"
    $footer = "`n`n<sub>Sensitive values, the account ID and API token IDs are masked; this description is public.</sub>"
    $summaryBlock = Format-CodeBlock -Text $body -Language diff -MaxLines 100000 -MaxChars 900000
    $prBlock = Format-CodeBlock -Text $body -Language diff -MaxLines 2000 -MaxChars $MaxChars
    if ($prBlock -ne $summaryBlock) { $prBlock += ': the whole plan is in the summary of the workflow run.' }
    if ($env:GITHUB_STEP_SUMMARY) {
        Add-Content -LiteralPath $env:GITHUB_STEP_SUMMARY -Value "$head<details><summary>Full plan</summary>`n`n$summaryBlock`n</details>$footer"
    }
    Write-StepOutput -Name report -Value "$head<details><summary>Full plan</summary>`n`n$prBlock`n</details>$footer"
    exit 0
}
$md.Add('')
$md.Add('<sub>Sensitive values, the account ID and API token IDs are masked; this description is public.</sub>')
if ($env:GITHUB_STEP_SUMMARY) { Add-Content -LiteralPath $env:GITHUB_STEP_SUMMARY -Value ($md -join "`n") }
Write-StepOutput -Name report -Value ($md -join "`n")
exit 0
