#Requires -Version 7.4
<#
.SYNOPSIS
    Install pinned CI tools into ~/bin, verifying each download against its release checksums.
.DESCRIPTION
    Versions come from environment variables named <TOOL>_VERSION (e.g. TOFU_VERSION), which the
    workflows pin with Renovate comments. Linux x64 only (GitHub-hosted runners). Adds ~/bin to
    GITHUB_PATH when running in Actions.
.EXAMPLE
    .github/scripts/Install-CiTools.ps1 -Tool trivy, gitleaks
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet('actionlint', 'gitleaks', 'helm', 'kubeconform', 'kustomize', 'tflint', 'tofu', 'trivy')]
    [string[]] $Tool
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true
Import-Module (Join-Path $PSScriptRoot 'CiTools.psm1') -Force

# Url: release directory. File/Sums: asset and checksum file. Member: path of the binary in the
# archive. {0} is the version.
$catalog = @{
    actionlint  = @{ Url = 'https://github.com/rhysd/actionlint/releases/download/v{0}'; File = 'actionlint_{0}_linux_amd64.tar.gz'; Sums = 'actionlint_{0}_checksums.txt'; Member = 'actionlint' }
    gitleaks    = @{ Url = 'https://github.com/gitleaks/gitleaks/releases/download/v{0}'; File = 'gitleaks_{0}_linux_x64.tar.gz'; Sums = 'gitleaks_{0}_checksums.txt'; Member = 'gitleaks' }
    helm        = @{ Url = 'https://get.helm.sh'; File = 'helm-v{0}-linux-amd64.tar.gz'; Sums = 'helm-v{0}-linux-amd64.tar.gz.sha256sum'; Member = 'linux-amd64/helm' }
    kubeconform = @{ Url = 'https://github.com/yannh/kubeconform/releases/download/v{0}'; File = 'kubeconform-linux-amd64.tar.gz'; Sums = 'CHECKSUMS'; Member = 'kubeconform' }
    kustomize   = @{ Url = 'https://github.com/kubernetes-sigs/kustomize/releases/download/kustomize%2Fv{0}'; File = 'kustomize_v{0}_linux_amd64.tar.gz'; Sums = 'checksums.txt'; Member = 'kustomize' }
    tflint      = @{ Url = 'https://github.com/terraform-linters/tflint/releases/download/v{0}'; File = 'tflint_linux_amd64.zip'; Sums = 'checksums.txt'; Member = 'tflint' }
    tofu        = @{ Url = 'https://github.com/opentofu/opentofu/releases/download/v{0}'; File = 'tofu_{0}_linux_amd64.tar.gz'; Sums = 'tofu_{0}_SHA256SUMS'; Member = 'tofu' }
    trivy       = @{ Url = 'https://github.com/aquasecurity/trivy/releases/download/v{0}'; File = 'trivy_{0}_Linux-64bit.tar.gz'; Sums = 'trivy_{0}_checksums.txt'; Member = 'trivy' }
}

$bin = Join-Path $HOME 'bin'
$null = New-Item -ItemType Directory -Force -Path $bin
$work = Join-Path ([IO.Path]::GetTempPath()) "ci-tools-$([guid]::NewGuid().ToString('N'))"
$null = New-Item -ItemType Directory -Path $work

try {
    foreach ($name in $Tool) {
        $version = [Environment]::GetEnvironmentVariable("$($name.ToUpperInvariant())_VERSION")
        if (-not $version) { throw "$($name.ToUpperInvariant())_VERSION is not set" }
        $t = $catalog[$name]
        $archive = Get-VerifiedAsset -BaseUrl ($t.Url -f $version) -File ($t.File -f $version) `
            -SumsFile ($t.Sums -f $version) -Directory $work
        $target = Join-Path $bin $name
        if ($archive.EndsWith('.zip')) {
            # .NET zip API: no dependency on the Microsoft.PowerShell.Archive module.
            $zip = [IO.Compression.ZipFile]::OpenRead($archive)
            try {
                [IO.Compression.ZipFileExtensions]::ExtractToFile($zip.GetEntry($t.Member), $target, $true)
            } finally {
                $zip.Dispose()
            }
        } else {
            $strip = ($t.Member -split '/').Count - 1
            & tar -xzf $archive -C $bin "--strip-components=$strip" $t.Member
        }
        & chmod +x $target
        Write-Host "$name $version -> $target"
    }
    if ($env:GITHUB_PATH) { Add-Content -LiteralPath $env:GITHUB_PATH -Value $bin }
} finally {
    Remove-Item -Recurse -Force -LiteralPath $work -ErrorAction SilentlyContinue
}
