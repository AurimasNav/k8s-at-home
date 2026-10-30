#Requires -Version 7.4
<#
.SYNOPSIS
    Generate HCL and import blocks for Cloudflare resources that already exist.
.DESCRIPTION
    Output goes to iac/cloudflare/generated/ (git-ignored) for review; nothing is written to
    state. Move the reviewed blocks into the stack under readable names, then plan: the
    imports are applied by CI like any other change.
    Needs cf-terraforming and tofu on PATH, CLOUDFLARE_API_TOKEN (read is enough) and
    CLOUDFLARE_ACCOUNT_ID.
.EXAMPLE
    iac/cloudflare/scripts/generate-imports.ps1                                   # DNS records
.EXAMPLE
    iac/cloudflare/scripts/generate-imports.ps1 -ResourceType cloudflare_ruleset
.EXAMPLE
    iac/cloudflare/scripts/generate-imports.ps1 -ResourceType cloudflare_zone_setting `
        -ResourceId 'cloudflare_zone_setting=ssl,always_use_https,min_tls_version'
#>
[CmdletBinding()]
param(
    [string] $ResourceType = 'cloudflare_dns_record',
    [string] $ZoneName = 'sync.lt',
    # Needed by types that cannot be listed, e.g. zone settings.
    [string] $ResourceId
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true

foreach ($name in 'CLOUDFLARE_API_TOKEN', 'CLOUDFLARE_ACCOUNT_ID') {
    if ([string]::IsNullOrEmpty([Environment]::GetEnvironmentVariable($name))) { throw "$name is not set" }
}
$tofu = (Get-Command tofu -CommandType Application).Source
$null = Get-Command cf-terraforming -CommandType Application

$stack = Split-Path -Parent $PSScriptRoot
$out = Join-Path $stack 'generated'
$null = New-Item -ItemType Directory -Force -Path $out

$zones = Invoke-RestMethod -Uri 'https://api.cloudflare.com/client/v4/zones' `
    -Headers @{ Authorization = "Bearer $env:CLOUDFLARE_API_TOKEN" } `
    -Body @{ name = $ZoneName; 'account.id' = $env:CLOUDFLARE_ACCOUNT_ID }
if (-not $zones.result) { throw "zone $ZoneName not found in the account" }
$zoneId = $zones.result[0].id

# cf-terraforming reads provider schemas from an initialised working directory.
$work = Join-Path ([IO.Path]::GetTempPath()) "cf-tf-$([guid]::NewGuid().ToString('N'))"
$null = New-Item -ItemType Directory -Path $work
try {
    Copy-Item (Join-Path $stack 'versions.tf'), (Join-Path $stack '.terraform.lock.hcl') $work
    $null = & $tofu "-chdir=$work" init -backend=false -input=false -lockfile=readonly

    $common = @(
        '--zone', $zoneId, '--token', $env:CLOUDFLARE_API_TOKEN, '--resource-type', $ResourceType,
        '--terraform-install-path', $work, '--terraform-binary-path', $tofu
    )
    if ($ResourceId) { $common += '--resource-id', $ResourceId }

    $base = Join-Path $out ($ResourceType -replace ',', '_')
    # cf-terraforming also reads CLOUDFLARE_ACCOUNT_ID and refuses --zone together with an
    # account, so hide it from these calls.
    $accountId = $env:CLOUDFLARE_ACCOUNT_ID
    $env:CLOUDFLARE_ACCOUNT_ID = $null
    try {
        & cf-terraforming generate @common | Set-Content -LiteralPath "$base.tf"
        & cf-terraforming import --modern-import-block @common | Set-Content -LiteralPath "${base}_imports.tf"
    } finally {
        $env:CLOUDFLARE_ACCOUNT_ID = $accountId
    }
    Write-Host "Wrote $base.tf and ${base}_imports.tf for review."
} finally {
    Remove-Item -Recurse -Force -LiteralPath $work -ErrorAction SilentlyContinue
}
