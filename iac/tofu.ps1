#Requires -Version 7.4
<#
.SYNOPSIS
    Run OpenTofu in a stack with the credential names used in Doppler and GitHub.
.DESCRIPTION
    Maps TOFU_R2_ACCESS_KEY_ID / TOFU_R2_SECRET_ACCESS_KEY to the S3 backend credentials,
    TOFU_STATE_PASSPHRASE, CLOUDFLARE_ACCOUNT_ID and HOME_PUBLIC_IPV4 to TF_VAR_*, and picks the Cloudflare
    token: CLOUDFLARE_IAC_READ_TOKEN by default, CLOUDFLARE_API_TOKEN (the "iac" write token)
    with -Write. Applies normally go through CI.
.EXAMPLE
    iac/tofu.ps1 cloudflare init
.EXAMPLE
    doppler run -- pwsh iac/tofu.ps1 cloudflare plan
#>
[CmdletBinding(PositionalBinding = $false)]
param(
    [Parameter(Mandatory, Position = 0)]
    [string] $Stack,

    # Use the write token (CLOUDFLARE_API_TOKEN) instead of the read-only one.
    [switch] $Write,

    [Parameter(Position = 1, ValueFromRemainingArguments)]
    [string[]] $TofuArgs
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false

$dir = Join-Path $PSScriptRoot $Stack
if (-not (Test-Path -LiteralPath $dir -PathType Container)) { throw "no stack at $dir" }

function Get-RequiredEnv([string] $Name) {
    $value = [Environment]::GetEnvironmentVariable($Name)
    if ([string]::IsNullOrEmpty($value)) { throw "$Name is not set" }
    $value
}

$env:AWS_ACCESS_KEY_ID = Get-RequiredEnv 'TOFU_R2_ACCESS_KEY_ID'
$env:AWS_SECRET_ACCESS_KEY = Get-RequiredEnv 'TOFU_R2_SECRET_ACCESS_KEY'
$env:TF_VAR_state_passphrase = Get-RequiredEnv 'TOFU_STATE_PASSPHRASE'
$env:TF_VAR_cloudflare_account_id = Get-RequiredEnv 'CLOUDFLARE_ACCOUNT_ID'
$env:TF_VAR_home_public_ipv4 = Get-RequiredEnv 'HOME_PUBLIC_IPV4'
if ($Write) {
    $null = Get-RequiredEnv 'CLOUDFLARE_API_TOKEN'
} else {
    $env:CLOUDFLARE_API_TOKEN = Get-RequiredEnv 'CLOUDFLARE_IAC_READ_TOKEN'
}

& tofu "-chdir=$dir" @TofuArgs
exit $LASTEXITCODE
