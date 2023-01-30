# script must run from the same working dir where `akeyless_creds.csv` and `akeyless-secret.yaml` is located

function ConvertTo-Base64 {
    [CmdletBinding()]
    [OutputType([string])]
    param(
        [Parameter(Mandatory)]
        $Text
    )
    $encodedBytes = [System.Text.Encoding]::UTF8.GetBytes($Text)
    $encodedText = [System.Convert]::ToBase64String($encodedBytes)
    return $encodedText
}

$AkeylessCreds = Get-Content -Path ./akeyless_creds.csv -Raw -ErrorAction Stop | ConvertFrom-Csv
$AkeylessSecret = Get-Content -Path ./akeyless-secret.yaml -Raw -ErrorAction Stop

$AccessIdEncoded = ConvertTo-Base64 -Text $AkeylessCreds.'Access ID' -ErrorAction Stop
$AccessKeyEncoded = ConvertTo-Base64 -Text $AkeylessCreds.'Access Key' -ErrorAction Stop

$UpdatedSecret = $AkeylessSecret -replace 'external-secrets-id', "$AccessIdEncoded" -replace 'external-secrets-access-key', "$AccessKeyEncoded"
Set-Content -Value $UpdatedSecret -Path ./akeyless-secret.yaml -ErrorAction Stop

Write-Host "Updated 'akeyless-secret.yaml' with values from 'akeyless_creds.csv'."

