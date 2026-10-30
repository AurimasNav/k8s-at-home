#Requires -Version 7.4
<#
.SYNOPSIS
    Render every app ArgoCD deploys, with the kustomize flags ArgoCD uses
    (gitops/argocd/patch.configmap.yaml). Output: <OutDir>/<app>.yaml plus argocd-apps.yaml.
.DESCRIPTION
    The root app (argocd-apps/base) is the source of truth for what is deployed: only paths it
    points at are rendered. -Root renders another checkout (the PR base, for the manifest diff).
#>
[CmdletBinding()]
param(
    [string] $Root = '.',
    [string] $OutDir = 'rendered'
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false

$flags = '--enable-helm', '--load-restrictor', 'LoadRestrictionsNone'
$null = New-Item -ItemType Directory -Force -Path $OutDir
$OutDir = (Resolve-Path -LiteralPath $OutDir).Path
$Root = (Resolve-Path -LiteralPath $Root).Path

$rootFile = Join-Path $OutDir 'argocd-apps.yaml'
& kustomize build @flags (Join-Path $Root 'argocd-apps/base') > $rootFile
if ($LASTEXITCODE -ne 0) { throw 'kustomize build argocd-apps/base failed' }

$apps = @(Select-String -LiteralPath $rootFile -Pattern '^\s+path: (gitops/\S+)' |
        ForEach-Object { $_.Matches[0].Groups[1].Value } | Sort-Object -Unique)
Write-Host "Deployed apps: $($apps.Count)"

$failed = [Collections.Generic.List[string]]::new()
foreach ($app in $apps) {
    $name = $app.Substring('gitops/'.Length)
    $dir = Join-Path $Root $app
    if (-not (Test-Path -LiteralPath (Join-Path $dir 'kustomization.yaml'))) {
        Write-Host "::error file=argocd-apps/base::Application points to $app, which has no kustomization.yaml"
        $failed.Add($name)
        continue
    }
    $err = Join-Path $OutDir "$name.err"
    & kustomize build @flags $dir > (Join-Path $OutDir "$name.yaml") 2> $err
    if ($LASTEXITCODE -ne 0) {
        $msg = (Get-Content -Raw -LiteralPath $err) -replace '\s+', ' '
        Write-Host "::error file=$app/kustomization.yaml::kustomize build failed: $($msg.Substring(0, [Math]::Min(500, $msg.Length)))"
        $failed.Add($name)
    }
    Remove-Item -LiteralPath $err -ErrorAction SilentlyContinue
}

if ($failed.Count) {
    Write-Host "Render failed for: $($failed -join ', ')"
    exit 1
}
Write-Host "Rendered $($apps.Count) apps + root app into $OutDir"
