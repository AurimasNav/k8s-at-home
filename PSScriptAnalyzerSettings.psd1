@{
    # Repo-wide PowerShell lint settings (CI: .github/workflows/pr-checks.yaml, job "lint").
    Severity     = @('Error', 'Warning')
    ExcludeRules = @(
        # Scripts are CLIs run in CI; host output is the intended interface.
        'PSAvoidUsingWriteHost'
        # Scripts require PowerShell 7 (#Requires -Version 7.4), which reads BOM-less UTF-8;
        # the BOM only matters for Windows PowerShell 5.1.
        'PSUseBOMForUnicodeEncodedFile'
    )
}
