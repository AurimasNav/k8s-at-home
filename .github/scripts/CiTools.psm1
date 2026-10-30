#Requires -Version 7.4
# Shared helpers for the CI scripts in .github/scripts. Import with:
#   Import-Module (Join-Path $PSScriptRoot 'CiTools.psm1') -Force
Set-StrictMode -Version Latest

function Get-VerifiedAsset {
    <#
    .SYNOPSIS
        Download a release asset and check it against the release's SHA-256 checksum file.
    .OUTPUTS
        Path of the downloaded file.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] [string] $BaseUrl,
        [Parameter(Mandatory)] [string] $File,
        [Parameter(Mandatory)] [string] $SumsFile,
        [Parameter(Mandatory)] [string] $Directory
    )
    $path = Join-Path $Directory $File
    Invoke-WebRequest -Uri "$BaseUrl/$File" -OutFile $path -MaximumRetryCount 3 -RetryIntervalSec 2
    $sums = (Invoke-WebRequest -Uri "$BaseUrl/$SumsFile" -MaximumRetryCount 3 -RetryIntervalSec 2).Content
    if ($sums -is [byte[]]) { $sums = [Text.Encoding]::UTF8.GetString($sums) }
    $line = $sums -split "`n" |
        Where-Object { $_ -match "^([0-9a-f]{64})\s+\*?$([regex]::Escape($File))\s*$" } |
        Select-Object -First 1
    if (-not $line) { throw "no checksum for $File in $SumsFile" }
    $expected = ($line -split '\s+')[0]
    $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $path).Hash.ToLowerInvariant()
    if ($actual -ne $expected) { throw "checksum mismatch for $File" }
    Write-Host "$File`: checksum OK"
    $path
}

function Write-StepOutput {
    <#
    .SYNOPSIS
        Set a (multi-line) step output; prints the value instead when not in GitHub Actions.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] [string] $Name,
        [Parameter(Mandatory)] [AllowEmptyString()] [string] $Value
    )
    if (-not $env:GITHUB_OUTPUT) {
        Write-Output $Value
        return
    }
    $delimiter = "EOF_$([guid]::NewGuid().ToString('N'))"
    Add-Content -LiteralPath $env:GITHUB_OUTPUT -Value "$Name<<$delimiter`n$Value`n$delimiter"
}

function ConvertTo-MarkdownCell {
    <#
    .SYNOPSIS
        Make text safe for a Markdown table cell: one line, pipes and HTML escaped, length capped.
    #>
    [CmdletBinding()]
    param(
        [Parameter(ValueFromPipeline)] [AllowNull()] [AllowEmptyString()] [string] $Text,
        [int] $MaxLength = 120,
        # Wrap in a code span. Entities are not decoded inside code, so only pipes are escaped.
        [switch] $Code
    )
    process {
        $t = ([string]$Text) -replace '\s+', ' '
        $t = $t.Trim()
        if ($t.Length -gt $MaxLength) { $t = $t.Substring(0, $MaxLength - 1) + '…' }
        if ($Code) {
            $t = $t -replace '\|', '\|'
            # In double-quoted PowerShell strings `` is one literal backtick.
            if ($t.Contains('`')) { return "```` $t ````" }
            return "``$t``"
        }
        [Net.WebUtility]::HtmlEncode($t) -replace '\|', '&#124;'
    }
}

function Format-CodeBlock {
    <#
    .SYNOPSIS
        Wrap text in a fenced code block, keeping at most MaxLines lines and MaxChars characters.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] [AllowEmptyString()] [string] $Text,
        [string] $Language = '',
        [int] $MaxLines = 400,
        [int] $MaxChars = 15000
    )
    $lines = @($Text -split "`r?`n")
    $note = ''
    if ($lines.Count -gt $MaxLines) {
        $note = "… $($lines.Count - $MaxLines) more lines not shown"
        $lines = $lines[0..($MaxLines - 1)]
    }
    $body = $lines -join "`n"
    if ($body.Length -gt $MaxChars) {
        $body = $body.Substring(0, $MaxChars)
        $body = $body.Substring(0, [Math]::Max(0, $body.LastIndexOf("`n")))
        $note = '… truncated'
    }
    # A fence longer than any backtick run inside the text cannot be closed early by it.
    $longest = ([regex]::Matches($body, '`+') | ForEach-Object Length | Measure-Object -Maximum).Maximum
    $fence = '`' * [Math]::Max(3, [int]$longest + 1)
    $block = "$fence$Language`n$body`n$fence"
    if ($note) { $block += "`n$note" }
    $block
}

function Get-ChangedFile {
    <#
    .SYNOPSIS
        Files changed between a base commit and HEAD (paths relative to the repo root).
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)] [string] $BaseSha)
    $files = & git diff --name-only --no-renames $BaseSha HEAD
    if ($LASTEXITCODE -ne 0) { throw "git diff against $BaseSha failed" }
    @($files | Where-Object { $_ })
}

Export-ModuleMember -Function Get-VerifiedAsset, Write-StepOutput, ConvertTo-MarkdownCell, Format-CodeBlock, Get-ChangedFile
